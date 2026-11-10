# -*- coding: utf-8 -*-
"""历史数据的导出、自动备份与导入。

两种备份形态，用途不同：
  · 自动备份 = 库快照（VACUUM INTO 出来的 .db 文件）
    体积等于数据库本身、内存零压力、几秒完成。以前是每天把全部样点 dump 成
    JSON，5 年数据时要吃掉几个 GB 内存、写出 3.5 GB 文件 ×14 份，NAS 直接被 OOM。
  · 手动导出 = JSON（format_version 2），用于单块盘迁移、给别人看。

JSON 导出格式：
    {
      "format": "ssdplus.history",
      "format_version": 2,
      "exported_at": "2026-11-04T17:00:00+0800",
      "disk": {"disk_key","serial","device","model","firmware","protocol","capacity_bytes"},
      "sample_interval_seconds": 60,
      "sample_count": 1234,
      "samples": [{"ts","time","read_bytes_total","write_bytes_total",
                   "read_rate_bps","write_rate_bps","temp_c","health_pct","percent_used"}],
      "daily_count": 365,
      "daily": [{"day","read_delta","write_delta","read_max","...","n"}]
    }

整机备份把多个这样的 payload 放进 "disks" 数组，导入时两种都能吃。
"samples" 只有最近 raw_keep_days 天（分钟级），"daily" 是天层全量；
老版本导出的文件没有 "daily"，导入时会忽略这一段。
"""

from __future__ import annotations

import os
import re
import sqlite3
import time

import store

FORMAT = "ssdplus.history"
FORMAT_VERSION = 2
BACKUP_FORMAT = "ssdplus.backup"
BACKUP_PREFIX = "ssdplus-backup-"
SNAPSHOT_SUFFIX = ".db"


def _sample_rows(conn, disk_key: str) -> list[dict]:
    rows = store.samples_for(conn, disk_key)
    out = []
    for row in rows:
        out.append({
            "ts": row["ts"],
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"])),
            "read_bytes_total": row["acc_read"],
            "write_bytes_total": row["acc_write"],
            "read_rate_bps": row["read_rate"],
            "write_rate_bps": row["write_rate"],
            "temp_c": row["temp"],
            "health_pct": row["health"],
            "percent_used": row["percent_used"],
        })
    return out


def build_payload(app, disk_key: str, device: str, model: str, info: dict | None = None) -> dict:
    conn = store.connect(app.cfg.db_path)
    try:
        samples = _sample_rows(conn, disk_key)
        daily = store.daily_rows(conn, disk_key)
        meta = store.disk_meta(conn, disk_key) or {}
    finally:
        conn.close()

    info = info or {}
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "disk": {
            "disk_key": disk_key,
            "serial": (info.get("serial") or meta.get("serial") or "").strip(),
            "device": device,
            "model": info.get("model") or meta.get("model") or device,
            "firmware": info.get("firmware") or meta.get("firmware") or "",
            "protocol": info.get("protocol") or meta.get("protocol") or "",
            "capacity_bytes": info.get("capacity") or meta.get("capacity") or 0,
        },
        "sample_interval_seconds": int(app.cfg.get("sample_interval") or 60),
        "sample_count": len(samples),
        "samples": samples,
        # 天层全量：samples 只有最近 raw_keep_days 天，长期历史在这一段里
        "daily_count": len(daily),
        "daily": daily,
    }


def build_all(app) -> dict:
    """把当前所有硬盘（含已离线的）都导出成一份备份结构。"""
    disks, _, _ = app.sampler.snapshot()
    conn = store.connect(app.cfg.db_path)
    try:
        known = {row["disk_key"]: row for row in store.all_disk_meta(conn)}
    finally:
        conn.close()

    payloads = []
    seen = set()
    for device, info in disks.items():
        key = info.get("key") or ("dv:" + device)
        payloads.append(build_payload(app, key, device, info.get("model") or device, info))
        seen.add(key)
    for key, meta in known.items():
        if key in seen:
            continue
        payloads.append(build_payload(app, key, meta.get("device") or "", meta.get("model") or ""))

    return {
        "format": BACKUP_FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "disk_count": len(payloads),
        "disks": payloads,
    }


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", text) or "disk"


def write_backup(app) -> dict:
    """把整库做一份一致快照到备份目录（VACUUM INTO，不占内存）。"""
    cfg = app.cfg
    folder = str(cfg.get("backup_dir") or os.path.join(cfg.data_dir, "backup"))
    os.makedirs(folder, exist_ok=True)

    name = BACKUP_PREFIX + time.strftime("%Y%m%d-%H%M%S") + SNAPSHOT_SUFFIX
    path = os.path.join(folder, name)
    # 同一秒内连点两次"立即备份"会撞名，加序号避免互相覆盖
    if os.path.exists(path):
        for i in range(1, 100):
            candidate = os.path.join(folder, f"{BACKUP_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}-{i}{SNAPSHOT_SUFFIX}")
            if not os.path.exists(candidate):
                path = candidate
                break
    tmp = path + ".part"
    try:
        if os.path.exists(tmp):
            os.unlink(tmp)
    except OSError:
        pass

    conn = store.connect(cfg.db_path)
    try:
        disk_count = conn.execute("SELECT COUNT(*) AS n FROM disks").fetchone()["n"] or 0
        # VACUUM INTO 必须在源连接上执行，产出的是独立、完整、一致的一份库
        conn.execute("VACUUM INTO ?", (tmp,))
    finally:
        conn.close()
    os.replace(tmp, path)

    removed = _rotate(folder, int(cfg.get("backup_keep") or 3),
                      int(cfg.get("backup_keep_mb") or 500))
    cfg.set("backup_last_at", int(time.time()))
    cfg.set("backup_last_path", path)
    cfg.save()
    return {"path": path, "disk_count": int(disk_count),
            "bytes": os.path.getsize(path), "removed": removed}


def list_snapshots(app) -> list[dict]:
    """备份目录里可用的库快照，新→旧。"""
    folder = str(app.cfg.get("backup_dir") or os.path.join(app.cfg.data_dir, "backup"))
    out: list[dict] = []
    try:
        names = [f for f in os.listdir(folder)
                 if f.startswith(BACKUP_PREFIX) and f.endswith(SNAPSHOT_SUFFIX)]
    except OSError:
        return out
    for name in sorted(names, reverse=True):
        path = os.path.join(folder, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        out.append({"name": name, "path": path, "bytes": size,
                    "time": int(os.path.getmtime(path))})
    return out


def _tables(conn: sqlite3.Connection, schema: str) -> set[str]:
    return {r[0] for r in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")}


def _resolve_snapshot(app, path: str) -> str:
    """把用户给的文件名/路径收敛成备份目录里的绝对路径，别的地方一律拒绝。"""
    folder = os.path.abspath(str(app.cfg.get("backup_dir") or os.path.join(app.cfg.data_dir, "backup")))
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("快照文件不存在")
    target = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(folder, raw))
    if os.path.dirname(target) != folder or not target.endswith(SNAPSHOT_SUFFIX):
        raise ValueError("不是有效的备份快照")
    if not os.path.isfile(target):
        raise ValueError("快照文件不存在")
    return target


def delete_snapshot(app, path: str) -> dict:
    """删掉一份库快照。删的就是"上次备份"时，顺手把记录清掉，避免界面指向不存在的文件。"""
    target = _resolve_snapshot(app, path)
    name = os.path.basename(target)
    os.unlink(target)
    cfg = app.cfg
    if os.path.basename(str(cfg.get("backup_last_path") or "")) == name:
        cfg.set("backup_last_path", "")
        cfg.save()
    return {"path": target, "name": name}


def restore_snapshot(app, path: str) -> dict:
    """把一份库快照合并回当前库（按 disk_key + ts 去重，不覆盖现场数据）。

    合并而不是整个替换文件：服务不用停，现在的数据也不会被旧快照盖掉。
    """
    target = _resolve_snapshot(app, path)

    conn = store.connect(app.cfg.db_path)
    try:
        conn.execute("ATTACH DATABASE ? AS snap", (target,))
        have = _tables(conn, "snap")
        added = 0

        if "samples" in have:
            cur = conn.execute(
                """INSERT INTO samples (disk_key, serial, device, ts, acc_read, acc_write,
                                        read_rate, write_rate, temp, health, percent_used)
                   SELECT s.disk_key, s.serial, s.device, s.ts, s.acc_read, s.acc_write,
                          s.read_rate, s.write_rate, s.temp, s.health, s.percent_used
                   FROM snap.samples s
                   WHERE NOT EXISTS (
                       SELECT 1 FROM samples t WHERE t.disk_key = s.disk_key AND t.ts = s.ts)"""
            )
            added = cur.rowcount or 0

        for table, cols in (
            ("rollup_hour", "disk_key, hour_ts, read_delta, write_delta, read_max, read_avg,"
                            " write_max, write_avg, temp_max, temp_min, temp_avg, health_min,"
                            " percent_max, n"),
            ("rollup_day", "disk_key, day, read_delta, write_delta, read_max, read_avg,"
                           " write_max, write_avg, temp_max, temp_min, temp_avg, health_min,"
                           " percent_max, n"),
        ):
            if table in have:
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM snap.{table}"
                )

        if "bench" in have:
            conn.execute(
                """INSERT INTO bench (ts, device, disk_key, serial, model, read_mbps, write_mbps,
                                      size_mb, note, read_rounds, write_rounds)
                   SELECT ts, device, disk_key, serial, model, read_mbps, write_mbps,
                          size_mb, note, read_rounds, write_rounds
                   FROM snap.bench b
                   WHERE NOT EXISTS (SELECT 1 FROM bench t WHERE t.ts = b.ts AND t.device = b.device)"""
            )

        if "events" in have:
            conn.execute(
                """INSERT INTO events (ts, disk_key, device, level, rule, message)
                   SELECT ts, disk_key, device, level, rule, message FROM snap.events e
                   WHERE NOT EXISTS (
                       SELECT 1 FROM events t WHERE t.ts = e.ts AND COALESCE(t.rule,'') = COALESCE(e.rule,'')
                           AND COALESCE(t.message,'') = COALESCE(e.message,''))"""
            )

        conn.commit()
        conn.execute("DETACH DATABASE snap")
    finally:
        conn.close()
    return {"path": target, "added": int(added)}


def _rotate(folder: str, keep: int, keep_mb: int) -> int:
    """按"份数 + 总体积"双重上限轮转，永远保留最新一份。"""
    keep = max(1, keep)
    keep_bytes = max(64, keep_mb) * 1024 * 1024
    try:
        files = sorted(
            (f for f in os.listdir(folder)
             if f.startswith(BACKUP_PREFIX) and f.endswith((SNAPSHOT_SUFFIX, ".json"))),
            reverse=True,
        )
    except OSError:
        return 0

    removed = 0
    kept = 0
    total = 0
    for name in files:
        path = os.path.join(folder, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        over_count = kept >= keep
        over_size = kept >= 1 and total + size > keep_bytes
        if over_count or over_size:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
            continue
        kept += 1
        total += size
    return removed


def trim(app) -> int:
    """按当前的份数/体积上限清理一次。改完设置就能立刻看到效果，不必等下次备份。"""
    folder = str(app.cfg.get("backup_dir") or os.path.join(app.cfg.data_dir, "backup"))
    return _rotate(folder, int(app.cfg.get("backup_keep") or 3),
                   int(app.cfg.get("backup_keep_mb") or 500))


def maybe_backup(app) -> dict | None:
    """按配置的间隔决定是否该备份；每天最多检查一次。"""
    cfg = app.cfg
    if not bool(cfg.get("backup_enabled")):
        return None
    interval_days = max(1, int(cfg.get("backup_interval_days") or 7))
    last = int(cfg.get("backup_last_at") or 0)
    if time.time() - last < interval_days * 86400:
        return None
    return write_backup(app)


def import_payload(app, payload: dict) -> dict:
    """导入导出的 JSON；只按序列号匹配，按 (disk_key, ts) 去重。

    支持两种输入：单个 disk payload，或整机备份（含 disks 数组）。
    """
    if not isinstance(payload, dict):
        raise ValueError("文件内容不是 JSON 对象")

    blocks: list[dict] = []
    if isinstance(payload.get("disks"), list):
        blocks = [b for b in payload["disks"] if isinstance(b, dict)]
    elif payload.get("format") == FORMAT or "samples" in payload or "daily" in payload:
        blocks = [payload]
    if not blocks:
        raise ValueError("未找到可导入的数据（需要 ssdplus 导出的 JSON）")

    conn = store.connect(app.cfg.db_path)
    results = []
    try:
        for block in blocks:
            disk = block.get("disk") or {}
            serial = str(disk.get("serial") or "").strip()
            key = str(disk.get("disk_key") or "").strip()
            if not key:
                key = ("sn:" + serial) if serial else ("dv:" + str(disk.get("device") or "unknown"))
            samples = block.get("samples") or []
            if not isinstance(samples, list):
                samples = []
            device = str(disk.get("device") or "")
            added, skipped = store.merge_samples(conn, key, serial, device, samples)
            daily = block.get("daily")
            days = store.merge_daily(conn, key, daily) if isinstance(daily, list) else 0
            results.append({
                "disk_key": key, "serial": serial,
                "model": disk.get("model") or "", "device": device,
                "added": added, "skipped": skipped, "days": days,
            })
    finally:
        conn.close()

    total_added = sum(item["added"] for item in results)
    return {"disks": results, "added": total_added}
