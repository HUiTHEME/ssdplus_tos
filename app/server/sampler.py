# -*- coding: utf-8 -*-
"""后台采样线程：定期扫描磁盘、写库，并维护内存中的最新快照。"""

from __future__ import annotations

import threading
import time
import traceback

import collector
import store


class Sampler(threading.Thread):
    def __init__(self, app_dir: str, db_path: str, interval: int = 60, retention_days: int = 0,
                 include_virtual: bool = False, only_ssd: bool = True, on_tick=None,
                 raw_keep_days: int = 90, hour_keep_days: int = 730):
        super().__init__(daemon=True, name="ssdplus-sampler")
        self.app_dir = app_dir
        self.db_path = db_path
        MIN_INTERVAL = 60
        self.interval = max(MIN_INTERVAL, int(interval))
        # smartctl 缓存不要长过采集间隔，否则连续几条采样是同一份数据
        collector.set_cache_ttl(self.interval / 2)
        # 原始层保留天数：老配置 retention_days > 0 时优先尊重用户旧设置
        self.raw_keep_days = int(retention_days) if int(retention_days or 0) > 0 else int(raw_keep_days or 0)
        self.hour_keep_days = int(hour_keep_days or 0)
        self.include_virtual = include_virtual
        self.only_ssd = only_ssd
        self.on_tick = on_tick          # 每轮采样完成后的回调（告警、备份等）
        self.latest: dict[str, dict] = {}
        self.last_run: int | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()   # 保证同一时刻只有一轮采集在跑
        self._stop = threading.Event()
        self._rollup_at = 0.0
        self._pruned_at = 0.0

    def snapshot(self) -> tuple[dict[str, dict], int | None, str | None]:
        with self._lock:
            return dict(self.latest), self.last_run, self.last_error

    def refresh_now(self, timeout: float = 25.0) -> bool:
        """立刻扫一遍磁盘，只更新内存快照，**不写库**。

        界面上的"刷新"用它：拔掉硬盘后能马上从列表里消失。
        刻意不落库——否则每点一次就多一条采样，采样间隔和速率计算都被打乱。
        返回 False 表示等了 timeout 还没等到（采样线程正在跑）。
        """
        if not self._tick_lock.acquire(timeout=timeout):
            return False
        try:
            self.tick(persist=False)
            return True
        finally:
            self._tick_lock.release()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._tick_lock:
                    self.tick()
            except Exception:
                self.last_error = traceback.format_exc(limit=2)
            self._stop.wait(self.interval)

    def tick(self, persist: bool = True) -> None:
        """采一轮。

        persist=False 时只刷新内存快照（供界面手动刷新），不写库、不触发
        告警和备份——刷新不该在历史里留下采样点。
        """
        started = int(time.time())
        try:
            disks = collector.snapshot(self.app_dir, self.include_virtual, self.only_ssd)
        except Exception:
            self.last_error = traceback.format_exc(limit=2)
            return

        if not persist:
            # 累计读写沿用上一轮的值：这次没采样，界面上的数字不该跳
            with self._lock:
                prev = self.latest
            for device, info in disks.items():
                old = prev.get(device) or {}
                info["acc_read"] = old.get("acc_read", 0)
                info["acc_write"] = old.get("acc_write", 0)
                info["read_rate"] = old.get("read_rate")
                info["write_rate"] = old.get("write_rate")
            with self._lock:
                self.latest = disks
                self.last_error = None
            return

        conn = store.connect(self.db_path)
        try:
            for device, info in disks.items():
                key = info.get("key") or ("dv:" + device)
                # 逐条不提交，整轮落一次盘：7 块盘原本每轮 21 次 fsync，
                # 对装在 U 盘/系统盘上的 NAS 是没必要的写入磨损
                store.upsert_disk(conn, info, started, commit=False)
                acc_read, acc_write, read_rate, write_rate = store.update_counter(
                    conn, key, device,
                    info.get("session_read", 0), info.get("session_write", 0), started,
                    commit=False,
                )
                info["acc_read"] = acc_read
                info["acc_write"] = acc_write
                info["read_rate"] = read_rate
                info["write_rate"] = write_rate
                store.add_sample(conn, info, started, acc_read, acc_write,
                                 read_rate, write_rate, commit=False)
            conn.commit()

            now = time.time()
            # 分层聚合：先聚合、后清理，顺序不能反。回填大库时每批只跑十几秒，
            # 免得长时间占着采样线程。
            if now - self._rollup_at > 300:
                self._rollup_at = now
                try:
                    store.rollup_run(conn, budget_seconds=15.0)
                    if now - self._pruned_at > 3600:
                        store.prune_raw(conn, self.raw_keep_days)
                        store.prune_hours(conn, self.hour_keep_days)
                        self._pruned_at = now
                except Exception:
                    # 聚合失败不能影响采样写入，下一轮再试
                    pass
        finally:
            conn.close()

        with self._lock:
            self.latest = disks
            self.last_run = started
            self.last_error = None

        if self.on_tick:
            try:
                self.on_tick(disks, started)
            except Exception:
                # 告警/备份失败不能影响采样
                pass
