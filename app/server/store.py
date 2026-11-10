# -*- coding: utf-8 -*-
"""SQLite 存储层。

身份约定（很重要）：
  每块硬盘用 `disk_key` 标识，形如 `sn:<序列号>`；只有完全拿不到序列号时才用
  `dv:<设备名>` 兜底。设备名会随换盘位、换主板、加装硬盘而变化，序列号不会。

表设计：
  disks          每块盘（按设备名）的静态信息与最近一次快照，带 disk_key
  samples        采样点（累计读写 + 速率 + 温度 + 健康度 + 已用寿命），按 disk_key 归档
  counter_state  /proc/diskstats 累计值到"跨重启连续计数"的换算状态，按 disk_key 记
  bench          测速结果，带 disk_key / serial
  events         事件流水（告警、变化记录）

分层保留（v4 起）：
  60 秒一条的原始点只在 samples 里留 raw_keep_days 天，之后压缩成小时级和天级聚合，
  这两层永久保留。这样"永久保存历史"和"体积不无限膨胀"可以同时成立：
  保留的是这台盘的历史本身（每天读写多少、温度峰值、寿命消耗），
  丢掉的是 5 年后没人再看的分钟级抖动。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS disks (
    disk_key        TEXT PRIMARY KEY,
    device          TEXT,
    model           TEXT,
    serial          TEXT,
    firmware        TEXT,
    capacity        INTEGER,
    protocol        TEXT,
    is_ssd          INTEGER,
    first_seen      INTEGER,
    last_seen       INTEGER,
    health          INTEGER,
    temp            INTEGER,
    power_on_hours  INTEGER,
    power_cycles    INTEGER,
    unsafe_shutdowns INTEGER,
    media_errors    INTEGER,
    tbw             INTEGER,
    tbr             INTEGER,
    percent_used    INTEGER,
    smart_ok        INTEGER DEFAULT 0,
    smart_error     TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    disk_key    TEXT,
    serial      TEXT,
    device      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    acc_read    INTEGER NOT NULL,
    acc_write   INTEGER NOT NULL,
    read_rate   INTEGER,
    write_rate  INTEGER,
    temp        INTEGER,
    health      INTEGER,
    percent_used INTEGER
);

CREATE TABLE IF NOT EXISTS counter_state (
    disk_key    TEXT PRIMARY KEY,
    device      TEXT,
    last_read   INTEGER,
    last_write  INTEGER,
    acc_read    INTEGER DEFAULT 0,
    acc_write   INTEGER DEFAULT 0,
    last_ts     INTEGER
);

CREATE TABLE IF NOT EXISTS bench (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    device      TEXT NOT NULL,
    disk_key    TEXT,
    serial      TEXT,
    model       TEXT,
    read_mbps   REAL,
    write_mbps  REAL,
    size_mb     INTEGER,
    note        TEXT,
    read_rounds  TEXT,
    write_rounds TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    disk_key    TEXT,
    device      TEXT,
    level       TEXT,
    rule        TEXT,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS rollup_hour (
    disk_key    TEXT NOT NULL,
    hour_ts     INTEGER NOT NULL,
    read_delta  INTEGER,
    write_delta INTEGER,
    read_max    INTEGER,
    read_avg    INTEGER,
    write_max   INTEGER,
    write_avg   INTEGER,
    temp_max    INTEGER,
    temp_min    INTEGER,
    temp_avg    REAL,
    health_min  INTEGER,
    percent_max INTEGER,
    n           INTEGER,
    PRIMARY KEY (disk_key, hour_ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_day (
    disk_key    TEXT NOT NULL,
    day         TEXT NOT NULL,
    read_delta  INTEGER,
    write_delta INTEGER,
    read_max    INTEGER,
    read_avg    INTEGER,
    write_max   INTEGER,
    write_avg   INTEGER,
    temp_max    INTEGER,
    temp_min    INTEGER,
    temp_avg    REAL,
    health_min  INTEGER,
    percent_max INTEGER,
    n           INTEGER,
    PRIMARY KEY (disk_key, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_state (
    name        TEXT PRIMARY KEY,
    last_ts     INTEGER
);
"""

DISK_FIELDS = (
    "device", "model", "serial", "firmware", "capacity", "protocol", "is_ssd",
    "health", "temp", "power_on_hours", "power_cycles", "unsafe_shutdowns",
    "media_errors", "tbw", "tbr", "percent_used", "smart_ok", "smart_error",
)


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """disks 表主键从 device 换成 disk_key。

    设备名（nvme0n1 / sda）在重启后会变，同一个序列号会被记成好几行，
    查询时按 device 一关联就张冠李戴。这里按 disk_key 合并成一行，
    冲突时保留 last_seen 最新的那条（所以 ORDER BY last_seen ASC）。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(disks)")}
    if "disk_key" not in cols:
        return
    pk = [row["name"] for row in conn.execute("PRAGMA table_info(disks)") if row["pk"]]
    if pk and pk[0] == "disk_key":
        return                                    # 已经是新结构

    conn.execute(
        "UPDATE disks SET disk_key = CASE"
        "   WHEN serial IS NOT NULL AND TRIM(serial) NOT IN ('', 'N/A', 'UNKNOWN', '000000000000')"
        "     THEN 'sn:' || TRIM(serial)"
        "   ELSE 'dv:' || device END"
        " WHERE disk_key IS NULL OR disk_key = ''"
    )
    # 在合并 disks 之前，先把各表里还空着的 disk_key 按「当前还在的 device 映射」补上：
    # 合并之后同一块盘只剩一行，旧设备名就查不到了
    for table in ("samples", "bench", "events"):
        conn.execute(
            f"UPDATE {table} SET disk_key = COALESCE("
            f"   (SELECT d.disk_key FROM disks d WHERE d.device = {table}.device),"
            f"   'dv:' || {table}.device)"
            " WHERE disk_key IS NULL OR disk_key = ''"
        )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS disks_v5 (
               disk_key TEXT PRIMARY KEY, device TEXT, model TEXT, serial TEXT, firmware TEXT,
               capacity INTEGER, protocol TEXT, is_ssd INTEGER, first_seen INTEGER, last_seen INTEGER,
               health INTEGER, temp INTEGER, power_on_hours INTEGER, power_cycles INTEGER,
               unsafe_shutdowns INTEGER, media_errors INTEGER, tbw INTEGER, tbr INTEGER,
               percent_used INTEGER, smart_ok INTEGER DEFAULT 0, smart_error TEXT)"""
    )
    conn.execute(
        """INSERT OR REPLACE INTO disks_v5
               (disk_key, device, model, serial, firmware, capacity, protocol, is_ssd,
                first_seen, last_seen, health, temp, power_on_hours, power_cycles,
                unsafe_shutdowns, media_errors, tbw, tbr, percent_used, smart_ok, smart_error)
           SELECT disk_key, device, model, serial, firmware, capacity, protocol, is_ssd,
                  first_seen, last_seen, health, temp, power_on_hours, power_cycles,
                  unsafe_shutdowns, media_errors, tbw, tbr, percent_used, smart_ok, smart_error
           FROM disks ORDER BY last_seen ASC"""
    )
    conn.execute("DROP TABLE disks")
    conn.execute("ALTER TABLE disks_v5 RENAME TO disks")
    conn.commit()


def init_db(db_path: str) -> None:
    """建库建表 + 增量迁移。只加列/补数据，绝不 DROP 用户的历史。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(SCHEMA)
        if version < 3:
            _migrate_to_v3(conn)
        if version < 5:
            _migrate_to_v5(conn)
        added = _sync_columns(conn)
        if added:
            print(f"数据库结构补齐：新增 {added} 个字段", flush=True)
        _ensure_indexes(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """索引在迁移之后建立，老库补完列才不会报 no such column。"""
    # device 与 disk_key 一一对应，两个索引内容重复：只留 disk_key 的那个，
    # 省下约 20% 的写入体积（一条样本的索引开销从两份降成一份）。
    try:
        conn.execute("DROP INDEX IF EXISTS idx_samples_device_ts")
    except sqlite3.OperationalError:
        pass
    # disk_key 是 disks 的主键，SQLite 已经自带唯一索引，再建一个完全重复：
    # 只会让每次 upsert_disk 多维护一份索引。老库里可能残留，顺手删掉。
    try:
        conn.execute("DROP INDEX IF EXISTS idx_disks_key")
    except sqlite3.OperationalError:
        pass
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_samples_key_ts ON samples (disk_key, ts)",
        "CREATE INDEX IF NOT EXISTS idx_bench_key_ts ON bench (disk_key, ts)",
        "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)",
        "CREATE INDEX IF NOT EXISTS idx_events_key_rule ON events (disk_key, rule, ts)",
    ):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass


_TABLE_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*(?:WITHOUT\s+ROWID\s*)?;", re.S
)


def _sync_columns(conn: sqlite3.Connection) -> int:
    """按 SCHEMA 把老库里缺失的列补齐（只加列，不删不改数据）。

    老版本升级上来的库，CREATE TABLE IF NOT EXISTS 不会补列，缺列会让写入直接报错
    （例如实测老库的 bench 表没有 note / read_rounds，测速结果就永远存不进去）。
    """
    added = 0
    for table, body in _TABLE_RE.findall(SCHEMA):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        # 先把表级约束整段去掉，否则 "PRIMARY KEY (a, b)" 会被逗号切成两半，
        # 后半截 "b)" 会被当成列名拼进 ALTER TABLE（复合主键表就是这么炸的）
        body = re.sub(
            r"(?i)\b(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)\s*\([^)]*\)"
            r"(\s+REFERENCES\s+\w+\s*\([^)]*\))?", "", body)
        for raw in body.split(","):
            parts = raw.strip().split()
            if not parts:
                continue
            name = parts[0]
            if name.upper() in ("PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"):
                continue
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                continue
            if name in existing:
                continue
            decl = ""
            if len(parts) > 1 and parts[1].upper() not in (
                "PRIMARY", "NOT", "UNIQUE", "DEFAULT", "CHECK", "REFERENCES",
            ):
                decl = parts[1]
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}".strip())
            existing.add(name)
            added += 1
    return added


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3：引入 disk_key（序列号身份），补齐列并回填历史数据。"""
    _ensure_columns(conn, "disks", {"disk_key": "TEXT"})
    _ensure_columns(conn, "samples", {"disk_key": "TEXT", "serial": "TEXT", "percent_used": "INTEGER"})
    _ensure_columns(conn, "bench", {"disk_key": "TEXT", "serial": "TEXT"})
    _ensure_columns(conn, "events", {"disk_key": "TEXT", "rule": "TEXT"})

    # 先把 disks 的 disk_key 按序列号补上
    conn.execute(
        "UPDATE disks SET disk_key = CASE"
        "   WHEN serial IS NOT NULL AND TRIM(serial) NOT IN ('', 'N/A', 'UNKNOWN', '000000000000')"
        "     THEN 'sn:' || TRIM(serial)"
        "   ELSE 'dv:' || device END"
        " WHERE disk_key IS NULL OR disk_key = ''"
    )
    # counter_state 原来以设备名为主键，改成 disk_key
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(counter_state)")}
    if "disk_key" not in cols:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS counter_state_v3 ("
            " disk_key TEXT PRIMARY KEY, device TEXT, last_read INTEGER, last_write INTEGER,"
            " acc_read INTEGER DEFAULT 0, acc_write INTEGER DEFAULT 0, last_ts INTEGER)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO counter_state_v3 (disk_key, device, last_read, last_write,"
            " acc_read, acc_write, last_ts)"
            " SELECT COALESCE((SELECT d.disk_key FROM disks d WHERE d.device = c.device),"
            "                 'dv:' || c.device),"
            "        c.device, c.last_read, c.last_write, c.acc_read, c.acc_write, c.last_ts"
            " FROM counter_state c"
        )
        conn.execute("DROP TABLE counter_state")
        conn.execute("ALTER TABLE counter_state_v3 RENAME TO counter_state")

    # 再回填 samples / bench
    conn.execute(
        "UPDATE samples SET disk_key = COALESCE("
        "   (SELECT d.disk_key FROM disks d WHERE d.device = samples.device),"
        "   'dv:' || samples.device)"
        " WHERE disk_key IS NULL OR disk_key = ''"
    )
    conn.execute(
        "UPDATE samples SET serial = COALESCE("
        "   (SELECT d.serial FROM disks d WHERE d.device = samples.device), '')"
        " WHERE serial IS NULL"
    )
    conn.execute(
        "UPDATE bench SET serial = COALESCE("
        "   (SELECT d.serial FROM disks d WHERE d.device = bench.device), '')"
        " WHERE serial IS NULL"
    )
    conn.execute(
        "UPDATE bench SET disk_key = COALESCE("
        "   (SELECT d.disk_key FROM disks d WHERE d.device = bench.device),"
        "   'dv:' || bench.device)"
        " WHERE disk_key IS NULL OR disk_key = ''"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_key_ts ON samples (disk_key, ts)")
    conn.commit()


# ------------------------------------------------------------------ 磁盘信息

def upsert_disk(conn: sqlite3.Connection, info: dict, ts: int, commit: bool = True) -> None:
    row = {k: info.get(k) for k in DISK_FIELDS}
    row["disk_key"] = info.get("key")
    row["is_ssd"] = 1 if info.get("is_ssd") else 0
    row["smart_ok"] = 1 if info.get("smart_ok") else 0
    # 主键是 disk_key：设备名重启后会变，同一块盘不能因为换了 sda/nvme0n1 就另起一行
    existing = conn.execute(
        "SELECT first_seen FROM disks WHERE disk_key = ?", (row["disk_key"],)).fetchone()
    first_seen = existing["first_seen"] if existing else ts
    conn.execute(
        """INSERT INTO disks (disk_key, device, model, serial, firmware, capacity, protocol, is_ssd,
                              first_seen, last_seen, health, temp, power_on_hours, power_cycles,
                              unsafe_shutdowns, media_errors, tbw, tbr, percent_used,
                              smart_ok, smart_error)
           VALUES (:disk_key, :device, :model, :serial, :firmware, :capacity, :protocol, :is_ssd,
                   :first_seen, :last_seen, :health, :temp, :power_on_hours, :power_cycles,
                   :unsafe_shutdowns, :media_errors, :tbw, :tbr, :percent_used,
                   :smart_ok, :smart_error)
           ON CONFLICT(disk_key) DO UPDATE SET
               device=excluded.device, model=excluded.model, serial=excluded.serial,
               firmware=excluded.firmware, capacity=excluded.capacity, protocol=excluded.protocol,
               is_ssd=excluded.is_ssd, last_seen=excluded.last_seen, health=excluded.health,
               temp=excluded.temp, power_on_hours=excluded.power_on_hours,
               power_cycles=excluded.power_cycles, unsafe_shutdowns=excluded.unsafe_shutdowns,
               media_errors=excluded.media_errors, tbw=excluded.tbw, tbr=excluded.tbr,
               percent_used=excluded.percent_used, smart_ok=excluded.smart_ok,
               smart_error=excluded.smart_error""",
        {**row, "first_seen": first_seen, "last_seen": ts},
    )
    if commit:
        conn.commit()


def disk_meta(conn: sqlite3.Connection, disk_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM disks WHERE disk_key = ? ORDER BY last_seen DESC LIMIT 1", (disk_key,)
    ).fetchone()
    return dict(row) if row else None


def all_disk_meta(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM disks ORDER BY last_seen DESC").fetchall()
    return [dict(row) for row in rows]


# ------------------------------------------------------------------ 采样

def update_counter(conn: sqlite3.Connection, disk_key: str, device: str,
                   cur_read: int, cur_write: int, ts: int,
                   commit: bool = True) -> tuple[int, int, int | None, int | None]:
    """把 /proc/diskstats 的"自开机累计"换算成跨重启、跨设备名单调递增的累计值。"""
    row = conn.execute(
        "SELECT last_read, last_write, acc_read, acc_write, last_ts FROM counter_state WHERE disk_key = ?",
        (disk_key,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO counter_state (disk_key, device, last_read, last_write, acc_read, acc_write, last_ts)"
            " VALUES (?, ?, ?, ?, 0, 0, ?)",
            (disk_key, device, cur_read, cur_write, ts),
        )
        if commit:
            conn.commit()
        return 0, 0, None, None

    last_read, last_write = row["last_read"] or 0, row["last_write"] or 0
    acc_read, acc_write = row["acc_read"] or 0, row["acc_write"] or 0
    last_ts = row["last_ts"] or ts

    rebooted = cur_read < last_read or cur_write < last_write
    d_read = cur_read if rebooted else max(0, cur_read - last_read)
    d_write = cur_write if rebooted else max(0, cur_write - last_write)
    acc_read += d_read
    acc_write += d_write

    dt = ts - last_ts
    read_rate = int(d_read / dt) if dt > 0 and not rebooted else None
    write_rate = int(d_write / dt) if dt > 0 and not rebooted else None

    conn.execute(
        "UPDATE counter_state SET device=?, last_read=?, last_write=?, acc_read=?, acc_write=?, last_ts=?"
        " WHERE disk_key=?",
        (device, cur_read, cur_write, acc_read, acc_write, ts, disk_key),
    )
    if commit:
        conn.commit()
    return acc_read, acc_write, read_rate, write_rate


def add_sample(conn: sqlite3.Connection, info: dict, ts: int, acc_read: int, acc_write: int,
               read_rate: int | None, write_rate: int | None, commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO samples (disk_key, serial, device, ts, acc_read, acc_write,"
        " read_rate, write_rate, temp, health, percent_used)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (info.get("key"), info.get("serial") or "", info.get("device") or "", ts,
         acc_read, acc_write, read_rate, write_rate,
         info.get("temp"), info.get("health"), info.get("percent_used")),
    )
    if commit:
        conn.commit()


def prune(conn: sqlite3.Connection, retention_days: int) -> None:
    edge = int(time.time()) - max(1, retention_days) * 86400
    conn.execute("DELETE FROM samples WHERE ts < ?", (edge,))
    conn.commit()


# ------------------------------------------------------------------ 分层聚合
#
# 设计要点（改之前先读一遍）：
#   1. 聚合边界永远落在本地日边界上（只聚合到"今天 00:00"为止），
#      所以"天层覆盖到昨天 + 今天由原始层实时算"不会有半天的缺口或重复。
#   2. 先聚合、后清理。原始层的删除边界绝不超过已聚合边界，
#      否则那段时间就真的没了。
#   3. 首次升级要回填历史，回填是分批推进的，靠 rollup_state 断点续传；
#      回填未完成前所有查询仍走原始层，不会出现"历史页突然空了"。
#   4. 聚合行存的是增量（read_delta/write_delta）而不是累计值，
#      跨日边界不需要再去猜基线。

DAY = 86400
ROLLUP_BATCH_DAYS = 30
ROLLUP_BATCH_SECONDS = 120.0


def _today_start(ts: int | None = None) -> int:
    """本地时区今天 00:00 的时间戳。"""
    ts = int(time.time()) if ts is None else int(ts)
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def _hour_floor(ts: int) -> int:
    return int(ts) // 3600 * 3600


def _day_start_ts(day: str) -> int:
    """'YYYY-MM-DD' → 本地 00:00 的时间戳。"""
    try:
        return int(time.mktime(time.strptime(day, "%Y-%m-%d")))
    except (ValueError, TypeError):
        return 0


def _state_get(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT last_ts FROM rollup_state WHERE name = ?", (name,)).fetchone()
    return int(row["last_ts"]) if row and row["last_ts"] is not None else None


def _state_set(conn: sqlite3.Connection, name: str, value: int) -> None:
    conn.execute(
        "INSERT INTO rollup_state (name, last_ts) VALUES (?, ?)"
        " ON CONFLICT(name) DO UPDATE SET last_ts = excluded.last_ts",
        (name, int(value)),
    )


def rollup_backfilled(conn: sqlite3.Connection) -> bool:
    """历史是否已全部聚合进天层。未完成前查询走原始层。"""
    return _state_get(conn, "day") is not None and bool(_state_get(conn, "backfill_done"))


def rollup_progress(conn: sqlite3.Connection) -> dict:
    """给设置页看的进度：已聚合到哪天、还剩多少。"""
    edge_day = _state_get(conn, "day")
    row = conn.execute("SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM samples").fetchone()
    total = int(row["n"] or 0) if row else 0
    done = rollup_backfilled(conn)
    if edge_day is None or not row or row["lo"] is None:
        return {"done": done, "edge": edge_day, "pending": total, "total": total}
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM samples WHERE ts >= ?", (edge_day,)
    ).fetchone()
    return {"done": done, "edge": edge_day,
            "pending": int(pending["n"] or 0), "total": total}


def _aggregate_range(conn: sqlite3.Connection, lo: int, hi: int) -> None:
    """把 [lo, hi) 的原始样本压成小时行，再把小时行压成天行。幂等。

    天层必须从 lo 所在那天的 00:00 重新算起：批次边界常落在一天中间，
    若只聚合本批那半天再覆盖整天，前面半天会被抹掉（实测每日增量少 10~40%）。
    小时层是按小时粒度 UPSERT 的，不存在这种半桶问题。
    """
    if hi <= lo:
        return

    conn.execute(
        """INSERT INTO rollup_hour (disk_key, hour_ts, read_delta, write_delta,
                                    read_max, read_avg, write_max, write_avg,
                                    temp_max, temp_min, temp_avg, health_min, percent_max, n)
           SELECT disk_key,
                  (ts / 3600) * 3600 AS h,
                  MAX(acc_read) - MIN(acc_read),
                  MAX(acc_write) - MIN(acc_write),
                  MAX(read_rate), CAST(AVG(read_rate) AS INTEGER),
                  MAX(write_rate), CAST(AVG(write_rate) AS INTEGER),
                  MAX(temp), MIN(temp), AVG(temp),
                  MIN(health), MAX(percent_used),
                  COUNT(*)
           FROM samples
           WHERE ts >= ? AND ts < ? AND disk_key IS NOT NULL
           GROUP BY disk_key, h
           ON CONFLICT(disk_key, hour_ts) DO UPDATE SET
               read_delta=excluded.read_delta, write_delta=excluded.write_delta,
               read_max=excluded.read_max, read_avg=excluded.read_avg,
               write_max=excluded.write_max, write_avg=excluded.write_avg,
               temp_max=excluded.temp_max, temp_min=excluded.temp_min,
               temp_avg=excluded.temp_avg, health_min=excluded.health_min,
               percent_max=excluded.percent_max, n=excluded.n""",
        (lo, hi),
    )

    day_lo = _day_start_ts(time.strftime("%Y-%m-%d", time.localtime(lo)))
    # 天层直接从原始样本算，不经过小时层：小时桶按 MAX-MIN 求增量时，
    # 每个桶会漏掉"桶尾到下一桶首"的那一个采样间隔，24 个桶叠起来能差 8%。
    # 直接对整天取 MAX-MIN 与查询口径完全一致，误差只剩跨批次边界那一个间隔。
    conn.execute(
        """INSERT INTO rollup_day (disk_key, day, read_delta, write_delta,
                                   read_max, read_avg, write_max, write_avg,
                                   temp_max, temp_min, temp_avg, health_min, percent_max, n)
           SELECT disk_key,
                  strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d,
                  MAX(acc_read) - MIN(acc_read),
                  MAX(acc_write) - MIN(acc_write),
                  MAX(read_rate), CAST(AVG(read_rate) AS INTEGER),
                  MAX(write_rate), CAST(AVG(write_rate) AS INTEGER),
                  MAX(temp), MIN(temp), AVG(temp),
                  MIN(health), MAX(percent_used),
                  COUNT(*)
           FROM samples
           WHERE ts >= ? AND ts < ? AND disk_key IS NOT NULL
           GROUP BY disk_key, d
           ON CONFLICT(disk_key, day) DO UPDATE SET
               read_delta=excluded.read_delta, write_delta=excluded.write_delta,
               read_max=excluded.read_max, read_avg=excluded.read_avg,
               write_max=excluded.write_max, write_avg=excluded.write_avg,
               temp_max=excluded.temp_max, temp_min=excluded.temp_min,
               temp_avg=excluded.temp_avg, health_min=excluded.health_min,
               percent_max=excluded.percent_max, n=excluded.n""",
        (day_lo, hi),
    )
    conn.commit()


def rollup_step(conn: sqlite3.Connection, batch_days: int = ROLLUP_BATCH_DAYS,
                now: int | None = None) -> dict:
    """推进一次聚合。返回 {advanced, lo, hi, done}。

    从最早样本往后推，断点存在 rollup_state.day。整档推进到"今天 00:00"为止，
    今天的样本永远留在原始层，由查询时实时算。
    """
    now = int(time.time()) if now is None else int(now)
    edge = _today_start(now)          # 聚合上界：今天 00:00（不含）
    last = _state_get(conn, "day")

    if last is None:
        row = conn.execute("SELECT MIN(ts) AS lo FROM samples").fetchone()
        lo = int(row["lo"]) if row and row["lo"] is not None else edge
        lo = _hour_floor(lo)
    else:
        lo = last

    if lo >= edge:
        _state_set(conn, "day", lo)
        _state_set(conn, "backfill_done", 1)
        conn.commit()
        return {"advanced": False, "lo": lo, "hi": lo, "done": True}

    hi = min(lo + batch_days * DAY, edge)
    _aggregate_range(conn, lo, hi)
    _state_set(conn, "day", hi)
    done = hi >= edge
    if done:
        _state_set(conn, "backfill_done", 1)
    conn.commit()
    return {"advanced": True, "lo": lo, "hi": hi, "done": done}


def rollup_run(conn: sqlite3.Connection, now: int | None = None,
               budget_seconds: float = ROLLUP_BATCH_SECONDS) -> dict:
    """连续推进直到追平或超时。开机后立刻调一次即可。"""
    started = time.time()
    steps = 0
    result = {"advanced": False, "lo": 0, "hi": 0, "done": True}
    while True:
        result = rollup_step(conn, now=now)
        if result["advanced"]:
            steps += 1
        if result["done"] or not result["advanced"]:
            break
        if time.time() - started > budget_seconds:
            break
    result["steps"] = steps
    return result


def prune_raw(conn: sqlite3.Connection, raw_keep_days: int, now: int | None = None) -> int:
    """删掉超出原始层保留期的样点。只在回填完成后动手，且绝不超过已聚合边界。"""
    if raw_keep_days <= 0 or not rollup_backfilled(conn):
        return 0
    now = int(time.time()) if now is None else int(now)
    edge = _today_start(now) - (max(1, raw_keep_days) - 1) * DAY
    done = _state_get(conn, "day") or 0
    edge = min(edge, done)
    if edge <= 0:
        return 0
    cur = conn.execute("DELETE FROM samples WHERE ts < ?", (edge,))
    conn.commit()
    return cur.rowcount or 0


def prune_hours(conn: sqlite3.Connection, hour_keep_days: int, now: int | None = None) -> int:
    """小时层保留期到了就删（天层已经永久留住了日级数据）。0 表示永久。"""
    if hour_keep_days <= 0:
        return 0
    now = int(time.time()) if now is None else int(now)
    edge = _today_start(now) - max(1, hour_keep_days) * DAY
    cur = conn.execute("DELETE FROM rollup_hour WHERE hour_ts < ?", (edge,))
    conn.commit()
    return cur.rowcount or 0



def samples_for(conn: sqlite3.Connection, disk_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, acc_read, acc_write, read_rate, write_rate, temp, health, percent_used"
        " FROM samples WHERE disk_key = ? ORDER BY ts ASC",
        (disk_key,),
    ).fetchall()
    return [dict(row) for row in rows]


def daily_history(conn: sqlite3.Connection, disk_keys: list[str], days: int | None = None) -> list[dict]:
    """按天聚合读写增量与平均温度（多盘时求和/平均）。

    走天层：回填完成后只读 rollup_day（几千行，O(天数)），今天这一天还没进天层，
    由原始层实时补上——所以历史页永远看到"到此刻为止"的完整曲线。
    """
    if not disk_keys:
        return []

    if not rollup_backfilled(conn):
        # 老库刚升级、回填还没跑完：继续用原始层，不能出现历史空白
        since = int(time.time()) - days * 86400 if days else None
        return _raw_daily(conn, disk_keys, since)

    today_ts = _today_start()
    today = time.strftime("%Y-%m-%d", time.localtime(today_ts))
    marks = ",".join("?" * len(disk_keys))
    where = f"disk_key IN ({marks}) AND day < ?"
    params: list = [*disk_keys, today]
    if days:
        floor = time.strftime("%Y-%m-%d", time.localtime(int(time.time()) - days * 86400))
        where += " AND day >= ?"
        params.append(floor)

    out = []
    # 多块盘要合并成一行（读/写求和、温度取平均），与原始层聚合口径一致
    for row in conn.execute(
        f"SELECT day, SUM(read_delta) AS r, SUM(write_delta) AS w, AVG(temp_avg) AS t"
        f" FROM rollup_day WHERE {where} GROUP BY day ORDER BY day ASC", params
    ):
        temp = row["t"]
        out.append({
            "date": row["day"],
            "read": max(0, int(row["r"] or 0)),
            "write": max(0, int(row["w"] or 0)),
            "temp": round(temp, 1) if isinstance(temp, (int, float)) else None,
        })
    out.extend(_raw_daily(conn, disk_keys, today_ts))
    return out


def _raw_daily(conn: sqlite3.Connection, disk_keys: list[str], since: int | None) -> list[dict]:
    """从原始样本现算每日增量（回填期兜底，以及天层里还没有的"今天"）。

    必须先在 (disk_key, 天) 里各自取 MIN/MAX，再跨盘求和：
    三块盘的累计计数器起点不同，混在一起取 MIN/MAX 得到的差值没有意义。
    """
    if not disk_keys:
        return []
    marks = ",".join("?" * len(disk_keys))
    where = f"disk_key IN ({marks})"
    params: list = [*disk_keys]
    if since:
        where += " AND ts >= ?"
        params.append(int(since))
    rows = conn.execute(
        f"""SELECT d, SUM(r1 - r0) AS r, SUM(w1 - w0) AS w, AVG(temp) AS temp FROM (
                SELECT disk_key,
                       strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d,
                       MIN(acc_read) AS r0, MAX(acc_read) AS r1,
                       MIN(acc_write) AS w0, MAX(acc_write) AS w1,
                       AVG(temp) AS temp
                FROM samples
                WHERE {where}
                GROUP BY disk_key, d
            )
            GROUP BY d ORDER BY d ASC""",
        params,
    ).fetchall()
    out = []
    for row in rows:
        out.append({
            "date": row["d"],
            "read": max(0, int(row["r"] or 0)),
            "write": max(0, int(row["w"] or 0)),
            "temp": round(row["temp"], 1) if isinstance(row["temp"], (int, float)) else None,
        })
    return out


def day_series(conn: sqlite3.Connection, disk_key: str, date: str,
               limit: int = 288) -> dict:
    """某一天（本地时区）的采样点，用于热力图悬停时看日内曲线。

    返回 {"total": 原始条数, "points": [...]}。点数超过 limit 时按时间分桶，
    每桶取读写速率与温度的最大值 —— 抽样不能把峰值抹掉，那正是用户想看的。
    """
    try:
        start = int(time.mktime(time.strptime(date, "%Y-%m-%d")))
    except (ValueError, TypeError):
        return {"total": 0, "points": []}
    rows = conn.execute(
        """SELECT ts, read_rate, write_rate, temp
           FROM samples
           WHERE disk_key = ? AND ts >= ? AND ts < ?
           ORDER BY ts ASC""",
        (disk_key, start, start + 86400),
    ).fetchall()
    if not rows:
        # 原始层已经清掉这几天（超过保留期），降到小时层仍能看日内走势
        return _hour_series(conn, disk_key, start)
    total = len(rows)
    if not total:
        return {"total": 0, "points": []}

    def pack(ts, read_v, write_v, temp_v):
        return {"ts": int(ts), "read": int(read_v or 0),
                "write": int(write_v or 0), "temp": temp_v}

    if total <= limit:
        points = [pack(r["ts"], r["read_rate"], r["write_rate"], r["temp"]) for r in rows]
    else:
        step = (total + limit - 1) // limit
        points = []
        for i in range(0, total, step):
            chunk = rows[i:i + step]
            # 时间取桶内写入最高的那条，峰值时刻才对得上（而不是桶的起点）
            busiest = max(chunk, key=lambda r: (r["write_rate"] or 0))
            points.append(pack(
                busiest["ts"],
                max((r["read_rate"] or 0) for r in chunk),
                max((r["write_rate"] or 0) for r in chunk),
                max((r["temp"] or 0) for r in chunk) or None,
            ))
    return {"total": total, "points": points}


def _hour_series(conn: sqlite3.Connection, disk_key: str, start: int) -> dict:
    """小时层的日内明细（24 个点）。total 仍然是原始采样条数，前端文案不用改。"""
    rows = conn.execute(
        "SELECT hour_ts, read_max, write_max, temp_max, n FROM rollup_hour"
        " WHERE disk_key = ? AND hour_ts >= ? AND hour_ts < ? ORDER BY hour_ts ASC",
        (disk_key, start, start + 86400),
    ).fetchall()
    if not rows:
        return {"total": 0, "points": []}
    points = []
    total = 0
    for row in rows:
        total += int(row["n"] or 0)
        points.append({
            "ts": int(row["hour_ts"]),
            "read": int(row["read_max"] or 0),
            "write": int(row["write_max"] or 0),
            "temp": row["temp_max"],
        })
    return {"total": total, "points": points}


def count_samples(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()
    return int(row["n"] or 0) if row else 0


# ------------------------------------------------------------------ 天层读写（导出/导入用）

_DAY_COLS = ("day, read_delta, write_delta, read_max, read_avg, write_max, write_avg,"
             " temp_max, temp_min, temp_avg, health_min, percent_max, n")


def daily_rows(conn: sqlite3.Connection, disk_key: str) -> list[dict]:
    """某块盘的天层全量。导出时带上它，才不会只导出最近 90 天。"""
    rows = conn.execute(
        f"SELECT {_DAY_COLS} FROM rollup_day WHERE disk_key = ? ORDER BY day ASC", (disk_key,)
    ).fetchall()
    return [dict(row) for row in rows]


def merge_daily(conn: sqlite3.Connection, disk_key: str, rows: list[dict]) -> int:
    """把导出文件里的天层合并回来（同一天以文件为准）。"""
    added = 0
    for row in rows:
        day = str(row.get("day") or "").strip()
        if len(day) != 10:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO rollup_day (disk_key, " + _DAY_COLS + ")"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (disk_key, day,
             row.get("read_delta"), row.get("write_delta"),
             row.get("read_max"), row.get("read_avg"),
             row.get("write_max"), row.get("write_avg"),
             row.get("temp_max"), row.get("temp_min"), row.get("temp_avg"),
             row.get("health_min"), row.get("percent_max"), row.get("n")),
        )
        added += 1
    conn.commit()
    return added


def daily_writes(conn: sqlite3.Connection, disk_key: str, days: int = 30) -> list[dict]:
    """近 N 天每天写入字节数，用于概况页迷你趋势。"""
    return daily_history(conn, [disk_key], days)


def daily_writes_map(conn: sqlite3.Connection, disk_keys: list[str],
                     days: int = 30) -> dict[str, list[int]]:
    """一次查出多块盘近 N 天的每日写入量，返回 key -> [bytes...]，按天升序。"""
    if not disk_keys:
        return {}
    out: dict[str, list[int]] = {key: [] for key in disk_keys}

    if rollup_backfilled(conn):
        today_ts = _today_start()
        today = time.strftime("%Y-%m-%d", time.localtime(today_ts))
        floor = time.strftime("%Y-%m-%d", time.localtime(int(time.time()) - days * 86400))
        marks = ",".join("?" * len(disk_keys))
        for row in conn.execute(
            f"SELECT disk_key, write_delta FROM rollup_day"
            f" WHERE disk_key IN ({marks}) AND day >= ? AND day < ? ORDER BY day ASC",
            [*disk_keys, floor, today],
        ):
            out.setdefault(row["disk_key"], []).append(max(0, int(row["write_delta"] or 0)))
        # 今天的写入量还在原始层
        for row in conn.execute(
            f"SELECT disk_key, MAX(acc_write) - MIN(acc_write) AS w FROM samples"
            f" WHERE disk_key IN ({marks}) AND ts >= ? GROUP BY disk_key",
            [*disk_keys, today_ts],
        ):
            out.setdefault(row["disk_key"], []).append(max(0, int(row["w"] or 0)))
        return out

    marks = ",".join("?" * len(disk_keys))
    since = int(time.time()) - days * 86400
    rows = conn.execute(
        f"""SELECT disk_key,
                   strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d,
                   MIN(acc_write) AS w0, MAX(acc_write) AS w1
            FROM samples
            WHERE disk_key IN ({marks}) AND ts >= ?
            GROUP BY disk_key, d ORDER BY d ASC""",
        [*disk_keys, since],
    ).fetchall()
    for row in rows:
        out.setdefault(row["disk_key"], []).append(max(0, (row["w1"] or 0) - (row["w0"] or 0)))
    return out


def life_inputs(conn: sqlite3.Connection, disk_key: str) -> dict:
    """寿命预估需要的原始数据：已用寿命百分比的首末样本、写入速率。

    优先用天层（体积恒定），天层还没有今天，最后一点用原始层补上；
    回填未完成时整段回退到原始层。
    """
    out: dict = {"first": None, "last": None, "write_per_day": None, "span_days": 0}

    rows = conn.execute(
        "SELECT day, percent_max FROM rollup_day"
        " WHERE disk_key = ? AND percent_max IS NOT NULL ORDER BY day ASC",
        (disk_key,),
    ).fetchall()
    if len(rows) >= 2:
        out["first"] = {"ts": _day_start_ts(rows[0]["day"]), "pct": rows[0]["percent_max"]}
        out["last"] = {"ts": _day_start_ts(rows[-1]["day"]), "pct": rows[-1]["percent_max"]}

    # 健康度回退（SATA 盘常没有寿命百分比，用 100-health 折算）
    if out["first"] is None:
        rows = conn.execute(
            "SELECT day, health_min FROM rollup_day"
            " WHERE disk_key = ? AND health_min IS NOT NULL ORDER BY day ASC",
            (disk_key,),
        ).fetchall()
        if len(rows) >= 2:
            out["first"] = {"ts": _day_start_ts(rows[0]["day"]), "pct": max(0, 100 - rows[0]["health_min"])}
            out["last"] = {"ts": _day_start_ts(rows[-1]["day"]), "pct": max(0, 100 - rows[-1]["health_min"])}

    if out["first"] is None:
        # 天层还没攒够（新装或回填中），走原始层
        _life_from_samples(conn, disk_key, out)

    # 天层只到昨天，今天的最新值从原始层取，否则"已用寿命"会滞后一天
    if out["last"] is not None:
        row = conn.execute(
            "SELECT ts, percent_used, health FROM samples"
            " WHERE disk_key = ? AND ts > ? AND (percent_used IS NOT NULL OR health IS NOT NULL)"
            " ORDER BY ts DESC LIMIT 1",
            (disk_key, out["last"]["ts"]),
        ).fetchone()
        if row:
            pct = row["percent_used"] if isinstance(row["percent_used"], int) else None
            if pct is None and isinstance(row["health"], int):
                pct = max(0, 100 - row["health"])
            if pct is not None and pct >= out["last"]["pct"]:
                out["last"] = {"ts": int(row["ts"]), "pct": pct}

    if out["first"] and out["last"] and out["last"]["ts"] > out["first"]["ts"]:
        out["span_days"] = round((out["last"]["ts"] - out["first"]["ts"]) / 86400, 1)

    if out["write_per_day"] is None:
        floor = time.strftime("%Y-%m-%d", time.localtime(int(time.time()) - 30 * 86400))
        row = conn.execute(
            "SELECT SUM(write_delta) AS s, COUNT(*) AS c FROM rollup_day"
            " WHERE disk_key = ? AND day >= ? AND write_delta IS NOT NULL",
            (disk_key, floor),
        ).fetchone()
        if row and row["c"] and int(row["c"]) >= 2 and row["s"] is not None:
            out["write_per_day"] = max(0, int(row["s"])) / int(row["c"])
        else:
            edge = int(time.time()) - 30 * 86400
            row = conn.execute(
                "SELECT MIN(acc_write) AS w0, MAX(acc_write) AS w1 FROM samples"
                " WHERE disk_key = ? AND ts >= ?",
                (disk_key, edge),
            ).fetchone()
            if row and row["w0"] is not None and row["w1"] is not None:
                out["write_per_day"] = max(0, (row["w1"] or 0) - (row["w0"] or 0)) / 30
    return out


def _life_from_samples(conn: sqlite3.Connection, disk_key: str, out: dict) -> None:
    """原始层的寿命数据（天层未就绪时的兜底）。"""
    rows = conn.execute(
        "SELECT ts, percent_used, health FROM samples"
        " WHERE disk_key = ? AND percent_used IS NOT NULL ORDER BY ts ASC",
        (disk_key,),
    ).fetchall()
    used = [r for r in rows if isinstance(r["percent_used"], int)]
    if len(used) >= 2:
        out["first"] = {"ts": used[0]["ts"], "pct": used[0]["percent_used"]}
        out["last"] = {"ts": used[-1]["ts"], "pct": used[-1]["percent_used"]}

    if out["first"] is None:
        rows = conn.execute(
            "SELECT ts, health FROM samples WHERE disk_key = ? AND health IS NOT NULL ORDER BY ts ASC",
            (disk_key,),
        ).fetchall()
        if len(rows) >= 2:
            out["first"] = {"ts": rows[0]["ts"], "pct": max(0, 100 - (rows[0]["health"] or 0))}
            out["last"] = {"ts": rows[-1]["ts"], "pct": max(0, 100 - (rows[-1]["health"] or 0))}


# ------------------------------------------------------------------ 测速

def add_bench(conn: sqlite3.Connection, device: str, model: str, read_mbps: float,
              write_mbps: float | None, size_mb: int, note: str = "",
              read_rounds: list | None = None, write_rounds: list | None = None,
              disk_key: str | None = None, serial: str | None = None) -> None:
    conn.execute(
        "INSERT INTO bench (ts, device, disk_key, serial, model, read_mbps, write_mbps, size_mb,"
        " note, read_rounds, write_rounds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), device, disk_key, serial, model, read_mbps, write_mbps, size_mb,
         note, json.dumps(read_rounds or []), json.dumps(write_rounds or [])),
    )
    conn.commit()


def list_bench(conn: sqlite3.Connection, limit: int = 30,
               disk_key: str | None = None) -> list[dict]:
    """测速记录。按 disk_key 取某块盘的（序列号才是身份，设备名重启后会变）。"""
    if disk_key:
        rows = conn.execute(
            "SELECT * FROM bench WHERE disk_key = ? ORDER BY ts DESC LIMIT ?",
            (disk_key, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM bench ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for key in ("read_rounds", "write_rounds"):
            raw = item.get(key)
            try:
                item[key] = json.loads(raw) if raw else []
            except (TypeError, ValueError):
                item[key] = []
        out.append(item)
    return out


def clear_all_history(conn: sqlite3.Connection) -> dict:
    """清空全部历史记录，从零开始。

    包含明细、天/时聚合、测速记录、事件告警和累计计数器基线。
    **disks 表保留**：那是硬盘的身份与 SMART 快照，不是历史数据；
    counter_state 必须一起清，否则下一次采样会把出厂累计值当成这期间的增量。
    """
    counts = {}
    # rollup_state 也要一起清：它是"聚合推进到哪天"的断点，留着旧断点的话，
    # 清空后新样本虽然照常入库，但天层进度条会显示"已聚合到很久以前"，
    # 与空库自相矛盾；清掉后系统会从第一条新样本重新回填，真正从零开始。
    for table in ("samples", "rollup_hour", "rollup_day", "bench", "events",
                  "counter_state", "rollup_state"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            counts[table] = 0
    conn.commit()
    # 空间不会自动还给磁盘，顺手收一次（失败不影响结果）
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError:
        pass
    return counts


def clear_bench(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM bench")
    conn.commit()


def clear_events(conn: sqlite3.Connection) -> int:
    """清空事件流水（用户手动清理）。返回删掉的条数。"""
    cur = conn.execute("DELETE FROM events")
    conn.commit()
    return cur.rowcount or 0


# ------------------------------------------------------------------ 事件

def add_event(conn: sqlite3.Connection, disk_key: str | None, device: str | None,
              level: str, rule: str, message: str) -> None:
    conn.execute(
        "INSERT INTO events (ts, disk_key, device, level, rule, message) VALUES (?, ?, ?, ?, ?, ?)",
        (int(time.time()), disk_key, device, level, rule, message),
    )
    conn.commit()


def list_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def last_event_ts(conn: sqlite3.Connection, disk_key: str, rule: str) -> int | None:
    row = conn.execute(
        "SELECT ts FROM events WHERE disk_key = ? AND rule = ? ORDER BY ts DESC LIMIT 1",
        (disk_key, rule),
    ).fetchone()
    return int(row["ts"]) if row else None


# ------------------------------------------------------------------ 导入

def merge_samples(conn: sqlite3.Connection, disk_key: str, serial: str, device: str,
                  rows: list[dict]) -> tuple[int, int]:
    """按 (disk_key, ts) 去重合并。返回 (新增, 跳过)。"""
    existing = {
        r["ts"] for r in conn.execute(
            "SELECT ts FROM samples WHERE disk_key = ?", (disk_key,)
        ).fetchall()
    }
    added = skipped = 0
    for row in rows:
        ts = row.get("ts")
        if not isinstance(ts, int):
            continue
        if ts in existing:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO samples (disk_key, serial, device, ts, acc_read, acc_write,"
            " read_rate, write_rate, temp, health, percent_used)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (disk_key, serial, device, ts,
             int(row.get("read_bytes_total") or 0), int(row.get("write_bytes_total") or 0),
             row.get("read_rate_bps"), row.get("write_rate_bps"),
             row.get("temp_c"), row.get("health_pct"), row.get("percent_used")),
        )
        existing.add(ts)
        added += 1
    conn.commit()
    return added, skipped
