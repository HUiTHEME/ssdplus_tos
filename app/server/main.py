#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD助手 —— 后端服务

标准库 HTTP 服务 + 静态托管 + JSON API。
数据来源：sysfs / /proc/diskstats（基础与实时）+ smartctl（健康信息）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socketserver
import sys
import threading
import time
import traceback
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import alerts
import backup
import bench
import collector
import sampler as sampler_mod
import priv
import store

APP_NAME = "ssdplus"


def read_version() -> str:
    """版本号优先取平台注入的 TRIM_APPVER，其次读 config.ini / manifest，避免写死。

    飞牛用 manifest（key = value），铁威马 TOS 用 config.ini（JSON），
    两个都读一下，同一份源码在哪边都能显示正确版本。
    """
    env = os.environ.get("TRIM_APPVER")
    if env:
        return env.strip()
    base = os.path.dirname(os.path.abspath(__file__))
    for rel in (os.path.join("..", "..", "config.ini"), os.path.join("..", "config.ini")):
        try:
            with open(os.path.join(base, rel), "r", encoding="utf-8") as fp:
                version = str(json.load(fp).get("version") or "").strip()
            if version:
                return version
        except (OSError, ValueError, AttributeError):
            continue
    for rel in (os.path.join("..", "..", "manifest"), os.path.join("..", "manifest")):
        try:
            with open(os.path.join(base, rel), "r", encoding="utf-8") as fp:
                for line in fp:
                    key, _, value = line.partition("=")
                    if key.strip() == "version":
                        return value.strip()
        except OSError:
            continue
    return "1.0.0"


VERSION = read_version()

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def log(message: str, exc: bool = False) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"{stamp} [{APP_NAME}] {message}\n")
    if exc:
        traceback.print_exc(file=sys.stdout)
    sys.stdout.flush()


# ------------------------------------------------------------------ 配置

class Config:
    def __init__(self, port: int, www_dir: str, data_dir: str, etc_dir: str,
                 socket_path: str = "", prefix: str = "") -> None:
        self.port = port
        # 网关口（Unix Socket）模式：飞牛统一网关把 /app/{appname}/... 转发进来，不占用任何端口
        self.socket_path = socket_path
        self.prefix = (prefix or "").rstrip("/")
        self.www_dir = os.path.abspath(www_dir)
        self.data_dir = os.path.abspath(data_dir)
        self.etc_dir = os.path.abspath(etc_dir)
        self.app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.db_path = os.path.join(self.data_dir, "ssdplus.db")
        self.file = os.path.join(self.etc_dir, "config.json")
        # 先把默认值放进 self.raw，再读取文件合并；否则首次运行写配置时会拿到未初始化的 raw
        self.raw = self._defaults()
        self._load()

    def _defaults(self) -> dict:
        return {
            "sample_interval": 600,    # 采样间隔（秒）
            "retention_days": 0,       # 旧键：>0 时作为原始层保留天数（兼容老配置）
            # 分层保留：原始 60 秒点留 raw_keep_days 天，之后只留小时级和天级聚合（永久）
            "raw_keep_days": 90,
            "hour_keep_days": 730,
            "bench_size_mb": 256,      # 测速文件大小
            "show_virtual_disks": False,  # 是否显示虚拟机/云主机虚拟磁盘
            "only_ssd": True,             # 只显示固态硬盘
            # 告警（只写站内事件，暂不做 Webhook 外发）
            "alert_enabled": True,
            "notify_health_below": 90,
            "alert_temp_high": 60,
            "alert_interval_seconds": 600,   # 同一问题的通知频率（秒）
            # 备份
            "backup_enabled": True,
            "backup_dir": "",
            "backup_interval_days": 7,
            "backup_keep": 3,
            "backup_keep_mb": 500,
            "backup_last_at": 0,
            "backup_last_path": "",
        }

    MIGRATION_KEY = "keep_all_history"

    ALERT_MIGRATION_KEY = "alert_v2"

    BACKUP_MIGRATION_KEY = "backup_v2"

    DEFAULTS_V3_KEY = "defaults_v3"

    def _load(self) -> dict:
        self._load_file()
        # 早期版本默认 365 天自动清理，与"永久保留"的定位冲突，这里一次性改成 0
        if not self.raw.get(self.MIGRATION_KEY):
            self.raw["retention_days"] = 0
            self.raw[self.MIGRATION_KEY] = True
            self.save()
        # 告警设置换代：冷却小时 → 通知频率（秒）；阈值默认值上调；去掉重复/废弃键
        if not self.raw.get(self.ALERT_MIGRATION_KEY):
            old_hours = self.raw.pop("alert_cooldown_hours", None)
            try:
                old_hours = int(old_hours) if old_hours is not None else None
            except (TypeError, ValueError):
                old_hours = None
            if "alert_interval_seconds" not in self.raw:
                # 旧值就是默认 6 小时的话，直接落到新默认 10 分钟
                self.raw["alert_interval_seconds"] = 600 if old_hours in (None, 6) else old_hours * 3600
            self.raw.pop("alert_webhook", None)
            self.raw.pop("alert_health_below", None)
            # 只有还停留在旧默认值时才跟着改，用户自己调过的不动
            if int(self.raw.get("notify_health_below") or 0) == 60:
                self.raw["notify_health_below"] = 90
            if int(self.raw.get("alert_temp_high") or 0) == 70:
                self.raw["alert_temp_high"] = 60
            self.raw[self.ALERT_MIGRATION_KEY] = True
            self.save()
        # 备份默认值下调（份数 14→5、上限 2048→1024 MB）；
        # 只有还停在旧默认值时才跟着改，用户自己调过的不动
        if not self.raw.get(self.BACKUP_MIGRATION_KEY):
            if int(self.raw.get("backup_keep") or 0) == 14:
                self.raw["backup_keep"] = 5
            if int(self.raw.get("backup_keep_mb") or 0) == 2048:
                self.raw["backup_keep_mb"] = 1024
            self.raw.pop("backup_keep_mb_old", None)
            self.raw[self.BACKUP_MIGRATION_KEY] = True
            self.save()
        # 默认值再调一档：采样 60→600 秒、备份间隔 1→7 天、份数 5→3、上限 1024→500 MB。
        # 同样只改还停在上一版默认值的项。
        if not self.raw.get(self.DEFAULTS_V3_KEY):
            if int(self.raw.get("sample_interval") or 0) == 60:
                self.raw["sample_interval"] = 600
            if int(self.raw.get("backup_interval_days") or 0) == 1:
                self.raw["backup_interval_days"] = 7
            if int(self.raw.get("backup_keep") or 0) == 5:
                self.raw["backup_keep"] = 3
            if int(self.raw.get("backup_keep_mb") or 0) == 1024:
                self.raw["backup_keep_mb"] = 500
            self.raw[self.DEFAULTS_V3_KEY] = True
            self.save()
        return self.raw

    def _load_file(self) -> None:
        try:
            with open(self.file, "r", encoding="utf-8") as fp:
                stored = json.load(fp)
            if isinstance(stored, dict):
                self.raw.update(stored)
        except FileNotFoundError:
            # 首次运行：把默认配置落盘，方便用户直接编辑
            self.save()
        except Exception:
            log("读取配置失败，使用默认配置", exc=True)

    def reload(self) -> None:
        """重新读一遍配置文件。

        设置页改完采样间隔后要让运行中的采样线程立刻生效，
        而采样线程在特权进程里，只能由它自己重读。
        """
        self._load_file()

    def get(self, key: str):
        return self.raw.get(key)

    def set(self, key: str, value) -> None:
        self.raw[key] = value

    def save(self, data: dict | None = None) -> None:
        if isinstance(data, dict):
            self.raw.update(data)
        # 原子写：先写临时文件再 rename。直接 open("w") 是"先截断再写"，
        # 进程在写一半时被杀（升级、断电）会留下半截 JSON，下次启动就退回默认配置，
        # 用户改过的设置全丢。
        tmp = self.file + ".tmp"
        try:
            os.makedirs(self.etc_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self.raw, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self.file)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            # 配置目录不可写时不影响服务启动，本次运行使用内存中的配置
            log(f"配置写入失败（{self.file}），本次运行使用内存配置", exc=True)

    # 应用设置页上能改的那些数值项，点「恢复默认」时回到这里的值。
    # 刻意不含 backup_dir（用户可能指定了自己的目录）和 backup_last_*（运行状态）。
    RESETTABLE = (
        "sample_interval", "raw_keep_days", "bench_size_mb",
        "notify_health_below", "alert_temp_high", "alert_interval_seconds",
        "backup_enabled", "backup_interval_days", "backup_keep", "backup_keep_mb",
    )

    def reset_defaults(self) -> dict:
        """把可编辑的数值项恢复成设计默认值，返回实际发生变化的项。"""
        defaults = self._defaults()
        changed: dict = {}
        for key in self.RESETTABLE:
            if key in defaults and self.raw.get(key) != defaults[key]:
                self.raw[key] = defaults[key]
                changed[key] = defaults[key]
        if changed:
            self.save()
        return changed


class App:
    """运行期状态容器。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sampler: sampler_mod.Sampler | None = None
        self.bench_state: dict = {"running": False, "started_at": None, "result": None, "error": None}
        self.bench_lock = threading.Lock()
        self.alerts = alerts.AlertEngine()
        # True = 特权进程内（有设备权限）；False = 降权后的 Web 进程，靠 socket 请求
        self.privileged = False


# ------------------------------------------------------------------ 工具函数

def human_status(disk: dict, threshold: int) -> str:
    if not disk.get("smart_ok"):
        return "unknown"
    if disk.get("smart_passed") is False:
        return "bad"
    if disk.get("health") is None and disk.get("smart_passed") is None:
        # 拿不到健康度（如虚拟磁盘），不要误判为正常
        return "unknown"
    health = disk.get("health")
    if isinstance(health, int):
        if health < 40:
            return "bad"
        if health < threshold:
            return "warn"
    if (disk.get("media_errors") or 0) > 0:
        return "warn"
    return "ok"


def public_disk(info: dict, threshold: int) -> dict:
    return {
        "device": info.get("device"),
        "key": info.get("key") or ("dv:" + str(info.get("device") or "")),
        "path": info.get("path"),
        "model": info.get("model") or info.get("device"),
        "serial": info.get("serial") or "",
        "firmware": info.get("firmware") or "",
        "capacity": info.get("capacity") or 0,
        "protocol": info.get("protocol") or "",
        "bus": info.get("bus") or "",
        "is_ssd": bool(info.get("is_ssd")),
        "temp": info.get("temp"),
        "health": info.get("health"),
        "status": human_status(info, threshold),
        "smart_ok": bool(info.get("smart_ok")),
        "smart_error": info.get("smart_error") or "",
        "tbw": info.get("tbw"),
        "tbr": info.get("tbr"),
        "volume_estimated": bool(info.get("volume_estimated")),
        "power_on_hours": info.get("power_on_hours"),
        "power_cycles": info.get("power_cycles"),
        "unsafe_shutdowns": info.get("unsafe_shutdowns"),
        "media_errors": info.get("media_errors"),
        "percent_used": info.get("percent_used"),
        "available_spare": info.get("available_spare"),
        "available_spare_threshold": info.get("available_spare_threshold"),
        "critical_warning": info.get("critical_warning"),
        "controller_busy_time": info.get("controller_busy_time"),
        "num_err_log_entries": info.get("num_err_log_entries"),
        "host_reads": info.get("host_reads"),
        "host_writes": info.get("host_writes"),
        "rotation_rate": info.get("rotation_rate"),
        "reallocated_sectors": info.get("reallocated_sectors"),
        "pending_sectors": info.get("pending_sectors"),
        "uncorrectable_sectors": info.get("uncorrectable_sectors"),
        "crc_errors": info.get("crc_errors"),
        "read_rate": info.get("read_rate"),
        "write_rate": info.get("write_rate"),
        "acc_read": info.get("acc_read") or 0,
        "acc_write": info.get("acc_write") or 0,
    }


def resolve_disk(app: App, device: str) -> tuple[str, dict] | tuple[None, None]:
    """设备名 → (disk_key, 实时信息)。历史数据一律按 disk_key 走。"""
    disks, _, _ = app.sampler.snapshot()
    info = disks.get(device)
    if not info:
        return None, None
    return info.get("key") or ("dv:" + device), info


def find_disk(app: App, key: str = "", device: str = "") -> tuple[str, str, dict]:
    """按 disk_key（优先）或设备名定位一块盘，返回 (key, 当前设备名, 实时信息)。

    设备名重启后会在 nvme0n1 / sda 之间乱跳，所以磁盘的身份一律认 disk_key；
    只有真要读写 /dev/xxx 或挂载点时，才用这里返回的"当前"设备名。
    """
    disks, _, _ = app.sampler.snapshot()
    key = str(key or "").strip()
    device = str(device or "").strip()
    if key:
        for dev, info in disks.items():
            if (info.get("key") or ("dv:" + dev)) == key:
                return key, dev, info
        return key, "", {}      # 盘还在库里，但这次没插（例如已被拔掉）
    info = disks.get(device)
    if not info:
        return "", device, {}
    return info.get("key") or ("dv:" + device), device, info


def life_estimate(conn, disk_key: str) -> dict:
    """按已用寿命（NVMe Percentage Used，回退到健康度）的消耗速度估算剩余可用时间。"""
    data = store.life_inputs(conn, disk_key)
    out = {
        "status": "insufficient",
        "note": "需要累积更多采样数据",
        "used_pct": None,
        "per_year_pct": None,
        "remaining_days": None,
        "write_per_day": data.get("write_per_day"),
        "span_days": data.get("span_days") or 0,
    }
    first, last = data.get("first"), data.get("last")
    if not first or not last:
        return out

    out["used_pct"] = last["pct"]
    span = out["span_days"]
    if span < 1:
        out["note"] = "采样跨度不足 1 天"
        return out

    delta = last["pct"] - first["pct"]
    if delta <= 0:
        out.update(status="flat", note="按当前强度，暂未观测到寿命消耗")
        return out

    per_day = delta / span
    out["per_year_pct"] = round(per_day * 365, 2)
    out["remaining_days"] = int(max(0.0, 100 - last["pct"]) / per_day)
    out["status"] = "ok"
    out["note"] = ""
    return out


def fmt_days(days: int | None) -> str:
    if days is None:
        return "—"
    if days >= 365 * 30:
        return "30 年以上"
    if days >= 365:
        return f"{days // 365} 年 {int(days % 365 / 30)} 个月"
    if days >= 30:
        return f"{days // 30} 个月"
    return f"{days} 天"


def build_history(app: App, query: dict) -> dict:
    """按硬盘返回全部历史（按天聚合），供热力图与年份切换使用。

    优先认 `keys`（disk_key），老链接里的 `devices`（设备名）仍然兼容——
    设备名重启后会变，只能作为兜底。
    """
    disks, _, _ = app.sampler.snapshot()
    all_devices = sorted(disks.keys())
    key_of = {d: (disks[d].get("key") or ("dv:" + d)) for d in all_devices}

    raw_keys = (query.get("keys") or [""])[0]
    raw_devices = (query.get("devices") or [""])[0]
    if raw_keys:
        keys = [k for k in raw_keys.split(",") if k]
        selected = [d for d in all_devices if key_of[d] in keys]
    else:
        selected = [d for d in raw_devices.split(",") if d in all_devices]
        keys = [key_of[d] for d in selected]

    conn = store.connect(app.cfg.db_path)
    try:
        daily = store.daily_history(conn, keys, days=None) if keys else []
    finally:
        conn.close()

    years = sorted({int(row["date"][:4]) for row in daily if row.get("date")}, reverse=True)
    device = selected[0] if selected else ""
    info = disks.get(device, {})

    return {
        "key": keys[0] if len(keys) == 1 else "",
        "keys": keys,
        "device": device,
        "model": info.get("model") or device,
        "serial": info.get("serial") or "",
        "years": years,
        "daily": daily,
        "has_data": bool(daily),
    }


def build_overview(app: App) -> dict:
    threshold = int(app.cfg.get("notify_health_below") or 60)
    disks, last_run, error = app.sampler.snapshot()
    items = [public_disk(info, threshold) for info in disks.values()]
    items.sort(key=lambda d: d["device"])

    conn = store.connect(app.cfg.db_path)
    try:
        keys = [d["key"] for d in items]
        sparks = store.daily_writes_map(conn, keys, days=30) if keys else {}
        for item in items:
            item["spark"] = sparks.get(item["key"]) or []
            item["life"] = life_estimate(conn, item["key"])
    finally:
        conn.close()

    abnormal = [d for d in items if d["status"] in ("warn", "bad")]
    total_capacity = sum(d["capacity"] or 0 for d in items)
    written = sum(d["tbw"] or 0 for d in items)
    read = sum(d["tbr"] or 0 for d in items)

    if not items:
        state, title, desc = "empty", "尚未识别到磁盘", "请确认设备已连接，或稍后刷新。"
    elif abnormal:
        state = "warn" if all(d["status"] == "warn" for d in abnormal) else "bad"
        title = f"发现 {len(abnormal)} 块硬盘异常"
        desc = "建议尽快备份重要数据，并检查硬盘健康状态。"
    else:
        state, title, desc = "ok", "运行顺利", "硬盘状态良好，未发现异常。"

    return {
        "generated_at": int(time.time()),
        "sample_interval": int(app.cfg.get("sample_interval") or 60),
        "last_sample_at": last_run,
        "sampler_error": error,
        "summary": {
            "state": state,
            "title": title,
            "desc": desc,
            "disk_count": len(items),
            "total_capacity": total_capacity,
            "total_written": written,
            "total_read": read,
            "abnormal": len(abnormal),
            "smart_supported": any(d["smart_ok"] for d in items),
        },
        "disks": items,
    }


# ------------------------------------------------------------------ 路由

def api_health(app: App, query: dict) -> dict:
    return {"status": "ok", "app": APP_NAME, "version": VERSION, "server_time": int(time.time())}


def api_overview(app: App, query: dict) -> dict:
    return build_overview(app)


def api_disks(app: App, query: dict) -> dict:
    data = build_overview(app)
    return {"items": data["disks"], "total": len(data["disks"])}


def api_history(app: App, query: dict) -> dict:
    return build_history(app, query)


def api_day(app: App, query: dict) -> dict:
    """某块盘某一天的采样明细，供热力图悬停展示日内曲线。"""
    key_in = str((query.get("key") or [""])[0])
    device_in = str((query.get("device") or [""])[0])
    date = str((query.get("date") or [""])[0])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return {"error": "bad date", "total": 0, "points": []}
    key, device, info = find_disk(app, key_in, device_in)
    if not key:
        return {"error": "no disk", "date": date, "total": 0, "points": []}
    conn = store.connect(app.cfg.db_path)
    try:
        data = store.day_series(conn, key, date)
    finally:
        conn.close()
    data["date"] = date
    data["key"] = key
    data["device"] = device
    data["model"] = info.get("model") or device
    return data


def api_refresh(app: App, body: dict, query: dict) -> dict:
    """立刻重新扫描磁盘。

    采样间隔是 10 分钟，拔掉硬盘后界面最多要等这么久才会更新，
    这个接口让「刷新」按钮和刚打开页面时能马上拿到真实设备列表。
    """
    ok = app.sampler.refresh_now()
    disks, last, error = app.sampler.snapshot()
    if not ok:
        return {"ok": False, "error": "正在采集，请稍后再试",
                "count": len(disks), "last_sample_at": last}
    log(f"手动刷新磁盘列表：{len(disks)} 块")
    return {"ok": True, "count": len(disks), "last_sample_at": last,
            "sampler_error": error}


def _sync_sampler(app: App) -> None:
    """把配置改动推给真正在跑采样线程的那个进程。

    降权后 Web 进程手里只有一个只读快照代理，改它的 interval 没有任何效果，
    必须让特权进程重读配置——否则设置页改的间隔要等重启才生效。
    """
    if app.sampler is None:
        return
    if not getattr(app, "privileged", False):
        res = priv.call(app.cfg.data_dir, {"cmd": "apply_settings"}, 5.0)
        if not res.get("ok"):
            log(f"同步采样设置失败：{res.get('error')}")
        return
    app.sampler.interval = max(MIN_INTERVAL, int(app.cfg.get("sample_interval") or 600))
    app.sampler.raw_keep_days = int(app.cfg.get("raw_keep_days") or 90)
    collector.set_cache_ttl(app.sampler.interval / 2)


def apply_settings_here(app: App) -> dict:
    """特权侧：重读配置文件并更新采样线程。"""
    app.cfg.reload()
    app.sampler.interval = max(MIN_INTERVAL, int(app.cfg.get("sample_interval") or 600))
    app.sampler.raw_keep_days = int(app.cfg.get("raw_keep_days") or 90)
    collector.set_cache_ttl(app.sampler.interval / 2)
    log(f"采样间隔已更新为 {app.sampler.interval}s")
    return {"ok": True, "interval": app.sampler.interval}


def api_settings(app: App, query: dict) -> dict:
    cfg = app.cfg
    return {
        "sample_interval": cfg.get("sample_interval"),
        "raw_keep_days": int(cfg.get("raw_keep_days") or 90),
        "bench_size_mb": cfg.get("bench_size_mb"),
        "notify_health_below": cfg.get("notify_health_below"),
        "show_virtual_disks": bool(cfg.get("show_virtual_disks")),
        "only_ssd": bool(cfg.get("only_ssd")),
        "alert_enabled": bool(cfg.get("alert_enabled")),
        "alert_temp_high": cfg.get("alert_temp_high"),
        "alert_interval_seconds": cfg.get("alert_interval_seconds"),
        "backup_enabled": bool(cfg.get("backup_enabled")),
        "backup_dir": cfg.get("backup_dir") or os.path.join(cfg.data_dir, "backup"),
        "backup_interval_days": cfg.get("backup_interval_days"),
        "backup_keep": cfg.get("backup_keep"),
        "backup_keep_mb": int(cfg.get("backup_keep_mb") or 500),
        "backup_last_at": cfg.get("backup_last_at") or 0,
        "backup_last_path": cfg.get("backup_last_path") or "",
        "data_dir": cfg.data_dir,
        "etc_dir": cfg.etc_dir,
        "smartctl": collector.find_smartctl(cfg.app_dir) or "",
    }


def api_about(app: App, query: dict) -> dict:
    data = {
        "app": APP_NAME,
        "name": "SSD助手",
        "version": VERSION,
        "python": sys.version.split()[0],
        "data_dir": app.cfg.data_dir,
        "etc_dir": app.cfg.etc_dir,
        "db_path": app.cfg.db_path,
        "smartctl": collector.find_smartctl(app.cfg.app_dir) or "未找到",
        "disks": len(app.sampler.snapshot()[0]),
        "db_size": _db_size(app.cfg.db_path),
        "sample_count": _sample_count(app.cfg.db_path),
        "skipped": _skipped_disks(app),
        "raw_keep_days": int(app.cfg.get("raw_keep_days") or 90),
    }
    try:
        conn = store.connect(app.cfg.db_path)
        try:
            data["rollup"] = store.rollup_progress(conn)
        finally:
            conn.close()
    except Exception:
        data["rollup"] = {"done": True, "edge": None, "pending": 0, "total": 0}
    return data


def _skipped_disks(app: App) -> list[dict]:
    """列出被"只显示固态"筛掉的盘。

    用户反馈过「系统里能看到、应用里没有」，靠这份清单能直接看出
    是判定条件把它当机械盘了，还是根本不在这台机器的 /sys/block 里。
    """
    out: list[dict] = []
    try:
        smartctl = collector.find_smartctl(app.cfg.app_dir)
        for name in collector.list_devices():
            info = collector.base_info(name)
            if info.get("protocol") == "NVMe" or info.get("is_ssd"):
                continue
            sm, _err = collector.smart_read(
                info.get("smart_path") or info["path"], smartctl)
            if sm:
                for key in ("model", "rotation_rate", "serial"):
                    if sm.get(key) is not None:
                        info[key] = sm[key]
                if sm.get("attributes"):
                    info["attributes"] = sm["attributes"]
            if collector.looks_like_ssd(info):
                continue
            out.append({
                "device": name,
                "model": info.get("model") or "",
                "rotation_rate": info.get("rotation_rate"),
                "rotational": info.get("rotational"),
            })
    except Exception:
        return []
    return out


def _db_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _sample_count(path: str) -> int:
    try:
        conn = store.connect(path)
    except Exception:
        return 0
    try:
        return store.count_samples(conn)
    except Exception:
        return 0
    finally:
        conn.close()


def api_bench_list(app: App, query: dict) -> dict:
    """测速页数据。

    注意：`items` 是**所有硬盘**的测速记录，和上面选中的盘无关。
    历史测速是独立版块，选盘只影响仪表盘显示哪块盘的成绩；
    前端会从这份全量列表里按 disk_key 挑出"该盘最近一次"喂给仪表盘。
    """
    key_in = str((query.get("key") or [""])[0])
    device_in = str((query.get("device") or [""])[0])
    key, device, _ = find_disk(app, key_in, device_in)
    conn = store.connect(app.cfg.db_path)
    try:
        rows = store.list_bench(conn, 30)     # 全量，不按选中盘过滤
    finally:
        conn.close()
    plan = None
    if device and re.fullmatch(r"[a-zA-Z0-9]{1,16}", device):
        # 可写判断要看真实权限：Web 进程降权后对 /vol2 这类 700 目录没权限，
        # 自己算会把"能测"误判成"不能测"，所以交给特权进程回答
        if getattr(app, "privileged", False):
            plan = bench_targets(device)
        else:
            res = priv.call(app.cfg.data_dir, {"cmd": "bench_plan", "device": device}, 3.0)
            plan = res.get("plan") if res.get("ok") else None
    # 测速跑在特权进程里，进度状态得向它要
    state = app.bench_state
    if not getattr(app, "privileged", False):
        res = priv.call(app.cfg.data_dir, {"cmd": "bench_state"}, 3.0)
        if res.get("ok") and isinstance(res.get("state"), dict):
            state = res["state"]
    return {"items": rows, "state": state, "plan": plan,
            "key": key, "device": device}


def bench_targets(device: str) -> dict:
    """确定这块盘的测速目标：原始设备用于读，挂载卷用于写。

    写入必须在文件系统里做（写裸设备会毁数据），所以找不到可写挂载点时
    只能测读——把原因带回去，界面才能告诉用户是"没挂载"还是"进了 RAID 池"。
    """
    dev_node = f"/dev/{device}"
    read_device = dev_node if os.path.exists(dev_node) and os.access(dev_node, os.R_OK) else None
    write_dir = collector.best_write_dir(device)

    reason = ""
    if not write_dir:
        role = collector.storage_role(device)
        if role == "raid":
            reason = "未挂载（RAID 成员）"
        elif role == "lvm":
            reason = "未挂载（LVM 存储池）"
        else:
            reason = "未挂载到存储空间"
    return {"read_device": read_device, "write_dir": write_dir, "write_reason": reason}


def _run_bench(app: App, device: str, size_mb: int, targets: dict) -> None:
    rounds: dict[str, list] = {"read": [], "write": []}

    def progress(stage: str, percent: int, extra: dict | None = None) -> None:
        app.bench_state.update(stage=stage, percent=percent)
        if not extra:
            return
        app.bench_state["live"] = extra
        value = extra.get("mbps")
        phase = extra.get("phase")
        if phase in rounds and isinstance(value, (int, float)):
            rounds[phase].append(value)
            # 前端据此按顺序播放指针，避免轮次太快被轮询漏掉
            app.bench_state["rounds"] = {k: list(v) for k, v in rounds.items()}

    try:
        result = bench.run(
            write_dir=targets.get("write_dir"),
            size_mb=size_mb,
            read_device=targets.get("read_device"),
            progress=progress,
        )
        if result.get("cancelled"):
            # 用户中途停止：本次不入库，前端会把表盘还原成上一条历史成绩
            app.bench_state.update(running=False, result=None, error=None,
                                   stage=None, percent=0, cancelled=True)
            log(f"测速已停止 {device}（已跑 {len(result.get('read_rounds') or [])} 轮读、"
                f"{len(result.get('write_rounds') or [])} 轮写，未计入历史）")
            return
        disks, _, _ = app.sampler.snapshot()
        model = (disks.get(device) or {}).get("model") or device
        note = "读取:%s · 写入:%s" % (
            "设备直读" if result["read_source"] == "raw" else "测试文件",
            targets.get("write_dir") or targets.get("write_reason") or "未测",
        )
        info = disks.get(device) or {}
        disk_key = info.get("key") or ("dv:" + device)

        # 入库单独兜底：写库失败也不能把已经测出来的数字丢掉，同时要把原因告诉前端
        save_error = ""
        try:
            conn = store.connect(app.cfg.db_path)
            try:
                store.add_bench(conn, device, model, result["read_mbps"],
                                result["write_mbps"], size_mb, note,
                                result.get("read_rounds"), result.get("write_rounds"),
                                disk_key=disk_key, serial=info.get("serial") or "")
            finally:
                conn.close()
        except Exception as exc:
            save_error = "测速结果写入数据库失败：" + str(exc)
            log(save_error, exc=True)

        app.bench_state.update(running=False, result=result, error=save_error or None,
                               stage=None, percent=100)
        log(f"测速完成 {device}: 读 {result['read_mbps']} MB/s, "
            f"写 {result['write_mbps']} MB/s（{note}）" + ("，但未存入历史" if save_error else ""))
    except Exception as exc:
        app.bench_state.update(running=False, result=None, error=str(exc), stage=None)
        log(f"测速失败: {exc}", exc=True)


def bench_start_local(app: App, payload: dict) -> dict:
    """真正启动测速，只能在有设备权限的特权进程里跑。"""
    if app.bench_state.get("running"):
        return {"ok": False, "error": "已有测速任务在执行"}
    payload = payload or {}
    key_in = str(payload.get("key") or "")
    device_in = str(payload.get("device") or "")
    # 前端传 key（序列号）；老请求只带 device 时也认，但设备名重启后会变
    key, device, _ = find_disk(app, key_in, device_in)
    if not key:
        return {"ok": False, "error": "未找到该磁盘"}
    if not device:
        return {"ok": False, "error": "该磁盘当前未接入，无法测速"}
    # 设备名来自请求，先做白名单校验（这个进程是 root，不能容忍拼接）
    if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", device):
        return {"ok": False, "error": "设备名非法"}
    try:
        size_mb = int(payload.get("size_mb") or app.cfg.get("bench_size_mb") or 256)
    except (TypeError, ValueError):
        size_mb = int(app.cfg.get("bench_size_mb") or 256)
    disks, _, _ = app.sampler.snapshot()
    if device not in disks:
        return {"ok": False, "error": f"未找到磁盘 {device}"}

    targets = bench_targets(device)
    if not targets["read_device"] and not targets["write_dir"]:
        return {"ok": False, "error": "该磁盘既无法直接读取，也没有可写的挂载卷，无法测速"}

    app.bench_state = {
        "running": True, "started_at": int(time.time()), "result": None,
        "error": None, "stage": "准备中", "percent": 0,
        "targets": targets, "device": device, "key": key,
    }
    threading.Thread(target=_run_bench, args=(app, device, size_mb, targets), daemon=True).start()
    return {"ok": True, "targets": targets}


def bench_stop_local(app: App) -> dict:
    if not app.bench_state.get("running"):
        return {"ok": False, "error": "当前没有正在进行的测速"}
    if not bench.request_stop():
        return {"ok": False, "error": "测速任务已经结束"}
    app.bench_state["stage"] = "正在停止…"
    log("用户请求停止测速")
    return {"ok": True}


def api_bench_start(app: App, body: dict, query: dict) -> dict:
    payload = {
        "key": (body or {}).get("key"),
        "device": (body or {}).get("device"),
        "size_mb": (body or {}).get("size_mb"),
    }
    if getattr(app, "privileged", False):
        return bench_start_local(app, payload)
    # 降权后的 Web 进程没有设备权限，交给特权进程执行
    return priv.call(app.cfg.data_dir, {"cmd": "bench_start", **payload}, 10.0)


def api_bench_stop(app: App, body: dict, query: dict) -> dict:
    """测到一半不想测了：立刻中断，未完成的成绩不入库。"""
    if getattr(app, "privileged", False):
        return bench_stop_local(app)
    return priv.call(app.cfg.data_dir, {"cmd": "bench_stop"}, 10.0)


def api_bench_clear(app: App, body: dict, query: dict) -> dict:
    conn = store.connect(app.cfg.db_path)
    try:
        store.clear_bench(conn)
    finally:
        conn.close()
    return {"ok": True}


def api_events_clear(app: App, body: dict, query: dict) -> dict:
    conn = store.connect(app.cfg.db_path)
    try:
        removed = store.clear_events(conn)
    finally:
        conn.close()
    log(f"已清空 {removed} 条事件记录")
    return {"ok": True, "removed": removed}


# 采集间隔下限：再密就是给数据库灌水，10 秒一天也有 8640 条/盘
MIN_INTERVAL = 60

INT_KEYS = {
    "sample_interval": (60, 3600), "bench_size_mb": (64, 4096),
    "raw_keep_days": (7, 3650),
    "notify_health_below": (1, 100), "alert_temp_high": (30, 100),
    "alert_interval_seconds": (60, 604800), "backup_interval_days": (1, 30),
    "backup_keep": (1, 365), "backup_keep_mb": (64, 102400),
}
BOOL_KEYS = ("alert_enabled", "backup_enabled", "show_virtual_disks", "only_ssd")
STR_KEYS = ("backup_dir",)


def api_settings_save(app: App, body: dict, query: dict) -> dict:
    body = body or {}
    patch: dict = {}
    for key, (low, high) in INT_KEYS.items():
        if key in body:
            try:
                patch[key] = max(low, min(high, int(body[key])))
            except (TypeError, ValueError):
                continue
    for key in BOOL_KEYS:
        if key in body:
            patch[key] = bool(body[key])
    for key in STR_KEYS:
        if key in body:
            patch[key] = str(body[key] or "").strip()[:500]
    if not patch:
        return {"ok": False, "error": "没有可更新的配置项"}
    app.cfg.save(patch)
    _sync_sampler(app)
    if "backup_keep" in patch or "backup_keep_mb" in patch:
        # 份数/上限一改就顺手清一次，否则旧备份要等到下一次备份才被轮转
        try:
            backup.trim(app)
        except Exception:
            log("清理旧备份失败", exc=True)
    if "alert_enabled" in patch:
        app.alerts.prev.clear()      # 重新建立基线，避免开关一开就刷旧告警
    return {"ok": True, "settings": api_settings(app, {})}


def api_settings_reset(app: App, body: dict, query: dict) -> dict:
    """把设置页上的数值项一键恢复成设计默认值。备份目录、备份历史不动。"""
    changed = app.cfg.reset_defaults()
    _sync_sampler(app)
    if changed:
        try:
            backup.trim(app)
        except Exception:
            log("清理旧备份失败", exc=True)
        log("设置已恢复默认：" + "、".join(sorted(changed)))
    return {"ok": True, "changed": len(changed), "keys": sorted(changed),
            "settings": api_settings(app, {})}


def api_export_json(app: App, query: dict) -> tuple[str, dict] | None:
    """导出单块硬盘的采样记录（按序列号归档，可直接回灌）。"""
    key, device, info = find_disk(app,
                                  str((query.get("key") or [""])[0]),
                                  str((query.get("device") or [""])[0]))
    if not key:
        return None
    if device and not re.fullmatch(r"[a-zA-Z0-9]{1,16}", device):
        return None

    payload = backup.build_payload(app, key, device, info.get("model") or device, info)
    serial = payload["disk"].get("serial") or ""
    safe_serial = backup.safe_name(serial) or device or "disk"
    stamp = time.strftime("%Y%m%d-%H%M")
    filename = f"ssdplus_{safe_serial}_{stamp}.json" if not device \
        else f"ssdplus_{safe_serial}_{device}_{stamp}.json"
    return filename, payload


def api_smart(app: App, query: dict) -> dict:
    """展开硬盘卡片时才拉取的原始 SMART 明细。"""
    key, device, info = find_disk(app,
                                  str((query.get("key") or [""])[0]),
                                  str((query.get("device") or [""])[0]))
    if not key or not info:
        return {"ok": False, "error": "未找到该磁盘"}
    return {
        "ok": True,
        "device": device,
        "key": key,
        "protocol": info.get("protocol") or "",
        "attributes": info.get("attributes") or [],
        "nvme_log": info.get("nvme_log") or {},
    }


def api_events(app: App, query: dict) -> dict:
    try:
        limit = min(200, max(1, int((query.get("limit") or ["50"])[0])))
    except ValueError:
        limit = 50
    conn = store.connect(app.cfg.db_path)
    try:
        rows = store.list_events(conn, limit)
        # 事件里只有设备名，补上型号，前端才能显示"硬盘名称"而不是 sda
        for row in rows:
            key = row.get("disk_key")
            if key and not row.get("model"):
                disk = conn.execute(
                    "SELECT model FROM disks WHERE disk_key = ? AND model IS NOT NULL"
                    " ORDER BY last_seen DESC LIMIT 1", (key,)).fetchone()
                row["model"] = (disk["model"] if disk else "") or ""
    finally:
        conn.close()
    return {"items": rows}


def api_import(app: App, body: dict, query: dict) -> dict:
    try:
        result = backup.import_payload(app, body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    log(f"导入完成：新增 {result['added']} 条采样，涉及 {len(result['disks'])} 块盘")
    return {"ok": True, **result}


def api_clear_data(app: App, body: dict, query: dict) -> dict:
    """清空所有历史记录，重新开始记录。

    只删历史（采样、聚合、测速、事件），硬盘本身的身份和 SMART 快照保留。
    """
    if app.bench_state.get("running"):
        return {"ok": False, "error": "正在测速，请先停止再清空"}
    conn = store.connect(app.cfg.db_path)
    try:
        counts = store.clear_all_history(conn)
    finally:
        conn.close()
    # 内存里的告警基线也清掉，否则清空后第一条样本会被拿去和旧值比
    try:
        app.alerts.prev.clear()
    except Exception:
        pass
    total = counts.get("samples", 0)
    log("已清空历史数据：采样 %d 条、测速 %d 条、事件 %d 条"
        % (counts.get("samples", 0), counts.get("bench", 0), counts.get("events", 0)))
    return {"ok": True, "cleared": counts, "samples": total}


def api_backup_now(app: App, body: dict, query: dict) -> dict:
    try:
        info = backup.write_backup(app)
    except OSError as exc:
        return {"ok": False, "error": f"写入失败：{exc}"}
    log(f"手动备份：{info['path']}")
    return {"ok": True, **info}


def api_backup_list(app: App, query: dict) -> dict:
    items = backup.list_snapshots(app)
    return {"items": items, "keep_mb": int(app.cfg.get("backup_keep_mb") or 500)}


def api_backup_delete(app: App, body: dict, query: dict) -> dict:
    body = body or {}
    path = str(body.get("path") or "")
    if not path:
        return {"ok": False, "error": "缺少快照路径"}
    try:
        result = backup.delete_snapshot(app, path)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    log(f"删除备份快照：{os.path.basename(result['path'])}")
    return {"ok": True, **result}


def api_backup_restore(app: App, body: dict, query: dict) -> dict:
    body = body or {}
    path = str(body.get("path") or "")
    if not path:
        return {"ok": False, "error": "缺少快照路径"}
    try:
        result = backup.restore_snapshot(app, path)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"恢复失败：{exc}"}
    log(f"从快照恢复：{result['path']}（合并 {result['added']} 条采样）")
    return {"ok": True, **result}


def build_report_html(app: App, device: str) -> str | None:
    """单块硬盘的诊断报告（HTML，可直接另存或打印成 PDF）。"""
    key, info = resolve_disk(app, device)
    if not key:
        return None
    conn = store.connect(app.cfg.db_path)
    try:
        daily = store.daily_history(conn, [key], days=365)
        life = life_estimate(conn, key)
        benches = [row for row in store.list_bench(conn, 200) if row.get("disk_key") == key][:10]
    finally:
        conn.close()

    def size(value) -> str:
        if not value:
            return "—"
        units = ("B", "KB", "MB", "GB", "TB", "PB")
        idx = 0
        val = float(value)
        while val >= 1024 and idx < len(units) - 1:
            val /= 1024
            idx += 1
        return f"{val:.1f} {units[idx]}"

    rows = [
        ("设备名", device), ("型号", info.get("model") or "—"),
        ("序列号", info.get("serial") or "—"), ("固件", info.get("firmware") or "—"),
        ("接口", info.get("protocol") or "—"), ("容量", size(info.get("capacity"))),
        ("健康度", f"{info.get('health')}%" if info.get("health") is not None else "—"),
        ("当前温度", f"{info.get('temp')}℃" if info.get("temp") is not None else "—"),
        ("已用寿命", f"{info.get('percent_used')}%" if info.get("percent_used") is not None else "—"),
        ("累计写入", size(info.get("tbw"))), ("累计读取", size(info.get("tbr"))),
        ("通电时长", f"{info.get('power_on_hours')} 小时" if info.get("power_on_hours") else "—"),
        ("通电次数", info.get("power_cycles")), ("异常断电", info.get("unsafe_shutdowns")),
        ("介质错误", info.get("media_errors")),
    ]
    kv = "".join(f"<tr><th>{esc_html(k)}</th><td>{esc_html(str(v if v is not None else '—'))}</td></tr>"
                 for k, v in rows)

    total_write = sum(d["write"] for d in daily)
    total_read = sum(d["read"] for d in daily)
    peak = max([d["write"] for d in daily] + [1])
    bars = "".join(
        f'<div class="bar" title="{d["date"]} {size(d["write"])}">'
        f'<i style="height:{max(2, int(d["write"] / peak * 70))}px"></i></div>'
        for d in daily[-120:]
    ) or '<p class="muted">暂无采样数据</p>'

    bench_rows = "".join(
        f"<tr><td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(b['ts']))}</td>"
        f"<td>{b.get('read_mbps') or '—'} MB/s</td><td>{b.get('write_mbps') or '—'} MB/s</td></tr>"
        for b in benches
    ) or '<tr><td colspan="3" class="muted">暂无测速记录</td></tr>'

    life_line = (
        f"按当前写入强度预计还可使用 <b>{esc_html(fmt_days(life['remaining_days']))}</b>"
        f"（每年消耗约 {life['per_year_pct']}% 寿命）"
        if life.get("status") == "ok"
        else esc_html(life.get("note") or "数据不足，暂无法预估")
    )

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>SSD助手 诊断报告 - {esc_html(device)}</title>
<style>
body{{margin:0;padding:40px;background:#f3f4f6;color:#1f2329;
 font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;font-size:13px}}
.wrap{{max-width:900px;margin:0 auto;background:#fff;border:1px solid #e6e9ee;border-radius:12px;padding:28px 32px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8a9099;font-size:12px;margin-bottom:20px}}
h2{{font-size:13px;margin:26px 0 10px;color:#4b5563}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #eef1f5;vertical-align:top}}
th{{width:110px;color:#8a9099;font-weight:400}}
.note{{background:#f7f9fc;border-radius:8px;padding:12px 14px;color:#3d4652;margin-top:6px}}
.bars{{display:flex;align-items:flex-end;gap:2px;height:76px;margin-top:6px}}
.bar{{flex:1;display:flex;align-items:flex-end}}
.bar i{{display:block;width:100%;background:#7fd8a8;border-radius:2px 2px 0 0}}
.muted{{color:#a5adb8}}
</style></head><body><div class="wrap">
<h1>SSD助手 · 硬盘诊断报告</h1>
<div class="sub">生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')} · 应用版本 {VERSION}</div>
<h2>基本信息</h2><table>{kv}</table>
<h2>寿命预估</h2><div class="note">{life_line}</div>
<h2>近一年写入量</h2><div class="bars">{bars}</div>
<p class="muted">累计写入 {esc_html(size(total_write))} · 累计读取 {esc_html(size(total_read))} · 共 {len(daily)} 天有记录</p>
<h2>最近测速</h2><table><tr><th style="width:150px">时间</th><th>读取</th><th>写入</th></tr>{bench_rows}</table>
<p class="muted" style="margin-top:24px">由 SSD助手 自动生成，数据来源为该盘 SMART 与本地采样记录。</p>
</div></body></html>"""


def esc_html(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


GET_ROUTES = {
    "/api/health": api_health,
    "/api/overview": api_overview,
    "/api/disks": api_disks,
    "/api/history": api_history,
    "/api/settings": api_settings,
    "/api/about": api_about,
    "/api/bench": api_bench_list,
    "/api/smart": api_smart,
    "/api/events": api_events,
    "/api/day": api_day,
    "/api/backup/list": api_backup_list,
}

POST_ROUTES = {
    "/api/bench/start": api_bench_start,
    "/api/bench/stop": api_bench_stop,
    "/api/bench/clear": api_bench_clear,
    "/api/events/clear": api_events_clear,
    "/api/settings": api_settings_save,
    "/api/settings/reset": api_settings_reset,
    "/api/import": api_import,
    "/api/clear-data": api_clear_data,
    "/api/refresh": api_refresh,
    "/api/backup/now": api_backup_now,
    "/api/backup/restore": api_backup_restore,
    "/api/backup/delete": api_backup_delete,
}


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"
    app: App

    def address_string(self):
        # Unix Socket 模式下 client_address 为空字符串，父类会索引越界
        try:
            return self.client_address[0]
        except (TypeError, IndexError):
            return "unix"

    def log_message(self, fmt, *args):  # noqa: A003
        log("%s - %s" % (self.address_string(), fmt % args))

    def _gateway_path(self, parsed):
        """剥掉网关前缀，返回 (路径, 需要重定向到的地址)。"""
        prefix = self.app.cfg.prefix
        path = parsed.path
        # 铁威马 TOS 的网关正常会把 /v2/proxy/<appid> 剥掉再转发；万一某个固件
        # 整段透传进来，这里先自己剥一次，两种形态都能对上路由表。
        for probe in ("/v2/proxy/" + APP_NAME, "/proxy/" + APP_NAME):
            if path == probe or path.startswith(probe + "/"):
                path = path[len(probe):] or "/"
                break
        if not prefix:
            return path, None
        if path == prefix:
            target = prefix + "/"
            if parsed.query:
                target += "?" + parsed.query
            return path, target
        if path.startswith(prefix + "/"):
            return path[len(prefix):] or "/", None
        return path, None

    def _base_href(self) -> str:
        prefix = self.app.cfg.prefix
        return (prefix + "/") if prefix else "/"

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str) -> None:
        # 两边都 realpath：既挡住 ../ 穿越，也不被 www 目录里的软链接带出去
        root = os.path.realpath(self.app.cfg.www_dir)
        target = os.path.realpath(os.path.join(root, path.lstrip("/")))
        if target != root and not target.startswith(root + os.sep):
            self._json({"error": "forbidden"}, 403)
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            # 浏览器/WebView 里 JSON 常常直接显示成空白，这里改成可读的 HTML
            self._html_error(target, path)
            return

        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fp:
            body = fp.read()
        if ext in (".html", ".htm"):
            body = self._inject_base(body)
        self.send_response(200)
        self.send_header("Content-Type", MIME_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _inject_base(self, body: bytes) -> bytes:
        """给 HTML 注入 <base>，前端全部用相对地址，直连与网关两种模式都能跑。"""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        if "<base " in text.lower():
            return body
        tag = '<base href="%s">' % self._base_href()
        m = re.search(r"<head[^>]*>", text, re.IGNORECASE)
        if m:
            text = text[:m.end()] + tag + text[m.end():]
        else:
            text = tag + text
        return text.encode("utf-8")

    def _html_error(self, target: str, path: str) -> None:
        # 注意：不能对整段 HTML 用 .format()——CSS 里的花括号会被当成占位符，
        # 一访问不存在的页面就 KeyError。另外 path 来自 URL，必须转义后输出。
        def esc(value) -> str:
            return html_escape(str(value))
        body = (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>SSD助手</title><style>"
            "body{margin:0;padding:48px;font-family:-apple-system,'PingFang SC',sans-serif;"
            "background:#f3f4f6;color:#1f2329}"
            "h1{font-size:16px;margin:0 0 8px}p{color:#6b7684;font-size:13px;line-height:1.8;margin:4px 0}"
            "code{background:#fff;border:1px solid #e5e8ec;border-radius:4px;padding:1px 6px;font-size:12px}"
            "</style></head><body>"
            "<h1>界面文件缺失</h1>"
            "<p>请求的页面不存在：<code>" + esc(path) + "</code></p>"
            "<p>找不到文件：<code>" + esc(target) + "</code></p>"
            "<p>请卸载后重新安装应用；如果仍然出现，请查看日志 "
            "<code>" + esc(self.app.cfg.data_dir + "/info.log") + "</code>。</p>"
            "</body></html>"
        )
        data = body.encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _download_json(self, filename: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download_html(self, filename: str, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path, redirect = self._gateway_path(parsed)
        if redirect:
            self.send_response(301)
            self.send_header("Location", redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/export":
            try:
                result = api_export_json(self.app, query)
            except Exception:
                log("导出失败", exc=True)
                self._json({"error": "export failed"}, 500)
                return
            if not result:
                self._json({"error": "未找到该磁盘的采样数据"}, 404)
                return
            self._download_json(*result)
            return

        if path == "/api/report":
            device = str((query.get("device") or [""])[0])
            if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", device):
                self._json({"error": "设备名非法"}, 400)
                return
            html = build_report_html(self.app, device)
            if not html:
                self._json({"error": "未找到该磁盘"}, 404)
                return
            self._download_html(
                f"ssdplus-report-{device}-{time.strftime('%Y%m%d-%H%M')}.html", html)
            return

        handler = GET_ROUTES.get(path)
        if handler:
            try:
                self._json(handler(self.app, query))
            except Exception:
                log(f"接口异常: {path}", exc=True)
                self._json({"error": "internal error"}, 500)
            return
        if path.startswith("/api/"):
            self._json({"error": "not found", "path": path}, 404)
            return
        self._file("/index.html" if path == "/" else path)

    def do_POST(self):  # noqa: N802
        # 跨站请求伪造防护：浏览器的跨域 POST 带不了自定义头（预检在我们这里不过），
        # 而 /api/clear-data、/api/backup/restore 都是不可逆操作，被别家网页顺手打一下就麻烦
        if self.headers.get("X-Requested-With") != "SSDPlus":
            self._json({"error": "缺少请求标识"}, 403)
            return
        parsed = urlparse(self.path)
        path, redirect = self._gateway_path(parsed)
        if redirect:
            self._json({"error": "use trailing slash", "path": redirect}, 404)
            return
        handler = POST_ROUTES.get(path)
        if not handler:
            self._json({"error": "not found", "path": path}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024 * 1024:
                self._json({"ok": False, "error": "文件过大（上限 64MB）"}, 413)
                return
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            body = {}
        try:
            self._json(handler(self.app, body, parse_qs(parsed.query)))
        except Exception:
            log(f"接口异常: {path}", exc=True)
            self._json({"error": "internal error"}, 500)


# ------------------------------------------------------------------ 启动

def _env_port(default: int = 8901) -> int:
    """未声明 service_port 时飞牛会注入空字符串，这里兜底。"""
    raw = (os.environ.get("TRIM_SERVICE_PORT") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SSD助手 后端服务")
    base = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument("--port", type=int, default=_env_port())
    parser.add_argument("--socket", default=os.environ.get("SSDPLUS_SOCKET", ""),
                        help="Unix Socket 路径；设置后走飞牛网关模式，不再监听 TCP 端口")
    parser.add_argument("--prefix", default=os.environ.get("SSDPLUS_PREFIX", ""),
                        help="网关前缀，例如 /app/ssdplus")
    parser.add_argument("--www", default=os.path.join(base, "..", "www"))
    parser.add_argument("--data", default=os.path.join(base, "..", "..", "var"))
    parser.add_argument("--etc", default=os.path.join(base, "..", "..", "etc"))
    return parser.parse_args()


def _app_identity() -> tuple[int, int]:
    """config/privilege 里声明的应用用户（默认与 appname 同名）。"""
    name = os.environ.get("TRIM_USERNAME") or "ssdplus"
    try:
        import pwd
        entry = pwd.getpwnam(name)
    except (KeyError, ImportError):
        return -1, -1
    return entry.pw_uid, entry.pw_gid


def _hand_over_ownership(cfg: Config, uid: int, gid: int) -> None:
    """把数据/配置目录交给应用用户：Web 进程降权后还要读写它们。"""
    for root_dir in (cfg.data_dir, cfg.etc_dir):
        for base, dirs, files in os.walk(root_dir):
            for path in [base] + [os.path.join(base, n) for n in dirs + files]:
                try:
                    os.chown(path, uid, gid)
                except OSError:
                    pass


def _fix_db_permissions(cfg: Config, gid: int) -> None:
    """让 SQLite 的 -wal/-shm 对两个进程都可读写。

    root 采样线程和降权后的 Web 进程都写同一个库，但 WAL 模式下 SQLite 会把
    -wal/-shm 建成「主库的权限 + 创建者属主」。若由 root 先创建，就是 root:root
    且只对属主可写，Web 进程（应用用户）再打开就会报「unable to open database file」。

    修法：主库改 0664（SQLite 会照着它建 -wal/-shm），数据目录加 setgid，
    让 root 新建的文件继承应用组——两边都进这个组就能互写。
    """
    try:
        os.chmod(cfg.db_path, 0o664)
        for suffix in ("-wal", "-shm"):
            try:
                os.chmod(cfg.db_path + suffix, 0o664)
            except OSError:
                pass
    except OSError:
        pass
    try:
        # setgid(2) + 组可读写(770)：root 新建的 -wal/-shm 落到应用组名下
        os.chmod(cfg.data_dir, 0o2770)
    except OSError:
        log("设置数据目录 setgid 失败", exc=True)


def _wait_for_priv(cfg: Config, timeout: float = 25.0) -> bool:
    """等特权进程把 socket 建起来，Web 一启动就能用。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(priv.socket_path(cfg.data_dir)):
            if priv.call(cfg.data_dir, {"cmd": "ping"}, 2.0).get("ok"):
                return True
        time.sleep(0.2)
    return False


def _harden_code_dir(cfg: Config) -> None:
    """程序目录对应用用户收紧为只读。

    如果 Web 进程能改自己的代码，攻破它的人就能改 main.py，
    下次启动时这段代码以 root 跑起来——降权等于白做。
    """
    root = os.path.abspath(cfg.app_dir)
    for base, _dirs, files in os.walk(root):
        try:
            os.chmod(base, 0o755)
        except OSError:
            continue
        for name in files:
            path = os.path.join(base, name)
            mode = 0o755 if name.endswith((".py", ".sh", ".bin")) else 0o644
            try:
                os.chmod(path, mode)
            except OSError:
                pass


def run_privileged(cfg: Config, uid: int, gid: int) -> None:
    """特权进程（root）：采样、SMART、测速都在这里做。

    它不对网络暴露任何东西，只监听本地 socket 接受几个固定动作，
    Web 进程即便被攻破也拿不到任意命令执行的能力。
    """
    app = App(cfg)
    app.privileged = True

    def on_tick(disks: dict, ts: int) -> None:
        priv.write_snapshot(cfg.data_dir, disks, ts, None)
        try:
            app.alerts.evaluate(app, disks, ts, logger=log)
        except Exception:
            log("告警检查失败", exc=True)
        try:
            done = backup.maybe_backup(app)
            if done:
                log(f"自动备份完成：{done['path']}（{done['disk_count']} 块盘，"
                    f"{done['bytes'] // 1024} KB，清理旧备份 {done['removed']} 份）")
        except Exception:
            log("自动备份失败", exc=True)

    sampler = sampler_mod.Sampler(
        cfg.app_dir, cfg.db_path,
        interval=int(cfg.get("sample_interval") or 60),
        retention_days=int(cfg.get("retention_days") or 0),
        raw_keep_days=int(cfg.get("raw_keep_days") or 90),
        hour_keep_days=int(cfg.get("hour_keep_days") or 730),
        include_virtual=bool(cfg.get("show_virtual_disks")),
        only_ssd=bool(cfg.get("only_ssd")),
        on_tick=on_tick,
    )
    app.sampler = sampler
    sampler.start()
    log(f"采样线程已启动，间隔 {sampler.interval}s")

    def do_refresh(_req: dict) -> dict:
        ok = app.sampler.refresh_now()
        disks, last, err = app.sampler.snapshot()
        if ok:
            priv.write_snapshot(cfg.data_dir, disks, last, err)
        return {"ok": ok, "count": len(disks), "last_sample_at": last}

    server = priv.PrivServer(cfg.data_dir, {
        "ping": lambda req: {"ok": True},
        "refresh": do_refresh,
        "bench_state": lambda req: {"ok": True, "state": app.bench_state},
        "bench_plan": lambda req: {"ok": True, "plan": bench_targets(str(req.get("device") or ""))},
        "apply_settings": lambda req: apply_settings_here(app),
        "bench_start": lambda req: bench_start_local(app, req),
        "bench_stop": lambda req: bench_stop_local(app),
    }, gid=gid, allow_uid=uid)
    server.start()
    log("特权进程就绪（采样 / SMART / 测速）")
    # 父进程（Web）一退出就跟着结束：降权后的 Web 没权限杀这个 root 进程，
    # 只能靠它自己发现"亲爹没了"
    while True:
        if os.getppid() == 1:
            log("父进程已退出，特权进程一并结束")
            break
        time.sleep(5)


def _libc():
    """拿 libc 句柄；拿不到返回 None（非 Linux 环境直接跳过加固）。"""
    try:
        import ctypes
        try:
            return ctypes.CDLL("libc.so.6", use_errno=True)
        except OSError:
            return ctypes.CDLL(None)      # 退回到当前进程的主程序句柄
    except Exception:
        return None


def _drop_bounding_caps() -> None:
    """清空能力边界（CapBnd）。必须在 setuid 之前调用——需要 CAP_SETPCAP。

    setuid 后 CapEff 已经是 0，但能力边界还是满的：exec 一个带 file capability
    的程序仍可能把能力拿回来。清空边界就彻底断了这条路。
    """
    libc = _libc()
    if libc is None:
        return
    try:
        PR_CAPBSET_DROP = 24
        for cap in range(0, 64):
            libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
    except Exception:
        log("清空能力边界失败", exc=True)


def _set_no_new_privs() -> None:
    """禁止再通过 exec setuid 程序拿到特权（su / mount / passwd 等）。

    系统里有若干 setuid-root 程序，降权后的 Web 进程本是普通用户，
    但 exec 它们时内核仍会按 setuid 位提权。no_new_privs 关掉这个行为。
    """
    libc = _libc()
    if libc is None:
        return
    try:
        PR_SET_NO_NEW_PRIVS = 38
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            log("设置 no_new_privs 返回非 0", exc=True)
    except Exception:
        log("设置 no_new_privs 失败", exc=True)


def run_split(cfg: Config, uid: int, gid: int) -> int:
    """root 启动时拆进程：特权活留给后台，Web 自己降权到应用用户。"""
    _harden_code_dir(cfg)
    _hand_over_ownership(cfg, uid, gid)
    _fix_db_permissions(cfg, gid)
    # 切成应用主组，但保留 config/privilege 声明的附加组（disk）：
    # 少了 disk 组，Web 侧连 /dev/sdX 是否可读都判断不出来
    groups = [gid]
    try:
        import grp
        for name in ("disk",):
            try:
                groups.append(grp.getgrnam(name).gr_gid)
            except KeyError:
                pass
    except ImportError:
        pass
    os.setgroups(groups)
    os.setgid(gid)
    os.umask(0o002)

    # 监听器必须在降权前建好：socket 落在程序目录，降权后那个目录不可写
    server, addr = _make_http_server(cfg)

    pid = os.fork()
    if pid == 0:
        try:
            server.server_close()       # 子进程不对外服务，把监听 fd 还掉
        except OSError:
            pass
        os.setsid()                     # 独立进程组，停止时按组发信号
        try:
            run_privileged(cfg, uid, gid)
        except Exception:
            log("特权进程异常退出", exc=True)
        finally:
            os._exit(0)

    _drop_bounding_caps()               # 能力边界清空，必须在 setuid 之前
    os.setuid(uid)                      # 从这里开始，Web 进程没有任何 root 能力
    _set_no_new_privs()                 # 连 setuid 程序也提不动权
    os.environ["SSDPLUS_WEB"] = "1"
    # 探测放到降权之后：特权 socket 只接受应用用户，root 自己反而是被拒的
    ok = _wait_for_priv(cfg)
    log(f"Web 进程已降权到 uid={uid}（特权采集留在后台 root 进程）")
    if not ok:
        log("警告：等待特权进程超时，SMART 与测速可能不可用")
    return run_web(cfg, privileged=False, server=server, addr=addr)


def _make_http_server(cfg: Config):
    """创建并绑定 HTTP 服务，返回 (server, 地址描述)。

    必须在降权之前调用：Unix socket 落在程序目录里，而降权后那个目录不可写，
    子进程自己去 bind 会直接 PermissionError。
    """
    if cfg.socket_path:
        # 飞牛统一网关模式：只监听 Unix Socket，不占任何 TCP 端口
        os.makedirs(os.path.dirname(cfg.socket_path) or ".", exist_ok=True)
        try:
            os.unlink(cfg.socket_path)
        except FileNotFoundError:
            pass

        class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        server = _UnixServer(cfg.socket_path, Handler)
        try:
            # 默认放开到 666：飞牛网关（nginx worker，身份 www-data）要连这个 socket，
            # 而应用降权成 ssdplus，默认的 0770 会把网关挡在外面，用户点开就是 502。
            # 铁威马 TOS 的网关（TOSDaemon）以 root 连，规范也要求 0660，由 env 覆盖。
            mode = os.environ.get("SSDPLUS_SOCK_MODE") or "0666"
            try:
                os.chmod(cfg.socket_path, int(mode, 8))
            except ValueError:
                os.chmod(cfg.socket_path, 0o666)
        except OSError:
            log("设置 socket 权限失败", exc=True)
        return server, f"unix:{cfg.socket_path}  prefix={cfg.prefix or '-'}"

    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), Handler)
    server.daemon_threads = True
    return server, f"http://0.0.0.0:{cfg.port}"


def run_web(cfg: Config, privileged: bool, server=None, addr: str = "") -> int:
    app = App(cfg)
    app.privileged = privileged

    if privileged:
        # 单进程模式：非 root 启动（本地调试）或找不到应用用户时退化到这里
        def on_tick(disks: dict, ts: int) -> None:
            try:
                app.alerts.evaluate(app, disks, ts, logger=log)
            except Exception:
                log("告警检查失败", exc=True)
            try:
                backup.maybe_backup(app)
            except Exception:
                log("自动备份失败", exc=True)

        sampler = sampler_mod.Sampler(
            cfg.app_dir, cfg.db_path,
            interval=int(cfg.get("sample_interval") or 60),
            retention_days=int(cfg.get("retention_days") or 0),
            raw_keep_days=int(cfg.get("raw_keep_days") or 90),
            hour_keep_days=int(cfg.get("hour_keep_days") or 730),
            include_virtual=bool(cfg.get("show_virtual_disks")),
            only_ssd=bool(cfg.get("only_ssd")),
            on_tick=on_tick,
        )
        app.sampler = sampler
        sampler.start()
        log(f"采样线程已启动，间隔 {sampler.interval}s")
    else:
        # 只读视图：磁盘信息来自特权进程写下的快照
        app.sampler = priv.SnapshotProxy(cfg.data_dir)

    try:
        dropped = backup.trim(app)
        if dropped:
            log(f"按保留策略清理旧备份 {dropped} 份")
    except Exception:
        log("清理旧备份失败", exc=True)

    index_file = os.path.join(cfg.www_dir, "index.html")
    if not os.path.isfile(index_file):
        log(f"警告：前端文件缺失 {index_file}")
        if os.environ.get("TRIM_TEMP_LOGFILE"):
            try:
                with open(os.environ["TRIM_TEMP_LOGFILE"], "a", encoding="utf-8") as fp:
                    fp.write(f"前端文件缺失：{index_file}\n请重新安装应用。\n")
            except OSError:
                pass

    Handler.app = app
    socket_path = cfg.socket_path if cfg.socket_path else ""
    if server is None:
        server, addr = _make_http_server(cfg)

    def shutdown(*_args):
        log("收到退出信号，正在停止")
        # 信号处理里抛异常会打断 serve_forever 的清理流程，这里一律兜住
        try:
            if app.sampler is not None:
                app.sampler.stop()
        except Exception:
            log("停止采样失败", exc=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"服务已启动: {addr}  www={cfg.www_dir}  data={cfg.data_dir}")
    try:
        server.serve_forever()
    finally:
        try:
            if app.sampler is not None:
                app.sampler.stop()
        except Exception:
            log("停止采样失败", exc=True)
        server.server_close()
        if socket_path:
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        log("服务已退出")
    return 0


def main() -> int:
    args = parse_args()
    cfg = Config(args.port, args.www, args.data, args.etc, args.socket, args.prefix)
    for d in (cfg.data_dir, cfg.etc_dir):
        os.makedirs(d, exist_ok=True)
    store.init_db(cfg.db_path)

    # 旧配置里可能存着 10 秒这种过密的值，统一收到下限，避免界面显示与实际不符
    try:
        if int(cfg.get("sample_interval") or 0) < MIN_INTERVAL:
            cfg.save({"sample_interval": MIN_INTERVAL})
            log(f"采集间隔过小，已调整为 {MIN_INTERVAL}s")
    except (TypeError, ValueError):
        pass

    # 以 root 启动就拆进程：特权操作留后台，对外服务的进程必须降权，
    # 否则应用中心会以「使用 root 权限」为由拒绝上架
    if os.geteuid() == 0 and not os.environ.get("SSDPLUS_WEB"):
        uid, gid = _app_identity()
        if uid > 0 and gid > 0:
            return run_split(cfg, uid, gid)
        log("未找到应用用户，保持单进程 root 运行（仅限调试）")

    return run_web(cfg, privileged=True)


if __name__ == "__main__":
    sys.exit(main())
