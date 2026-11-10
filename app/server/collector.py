# -*- coding: utf-8 -*-
"""磁盘信息采集：sysfs + smartctl。

数据分两层：
  * 基础信息（型号/序列号/容量/协议）来自 /sys/block，无需特权
  * 健康信息（健康度/温度/通电时长/写入量）来自 smartctl，需要读取 /dev/*（加入 disk 用户组）
smartctl 不可用或执行失败时降级为 None，前端显示“—”。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

DISK_PREFIX = ("sd", "nvme", "vd", "hd")
IGNORED = ("loop", "ram", "dm-", "md", "sr", "zram")

SECTOR = 512
NVME_DATA_UNIT = 512000  # NVMe 的 data units 以 512000 字节为 1 单位

# ATA 属性 ID。SATA 盘没有 NVMe 那种语义明确的健康日志，只能靠这几项拼，
# 而它们在不同主控上的含义并不统一，所以取用顺序和折算方式都要保守。
ATA_LIFE_IDS = (231, 233)  # SSD Life Left / Media Wearout Indicator：直接是剩余百分比
ATA_WEAR_ID = 177          # Wear Leveling Count：厂商私有的归一化值，只作最后兜底
ATA_LBA_WRITE = 241        # Total LBAs Written
ATA_LBA_READ = 242         # Total LBAs Read

# "854 GB" / "1.2 TB" 这种带单位的人读字符串
_HUMAN_SIZE = re.compile(r"([\d.]+)\s*(TB|GB|MB|KB)\b", re.I)
_UNIT_BYTES = {"KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}

# 虚拟磁盘的型号/厂商特征（小写匹配）
VIRTUAL_HINTS = (
    "qemu", "vbox", "virtualbox", "vmware", "virtual", "virtio", "msft",
    "xen", "bochs", "parallels", "innotek", "amazon elastic", "google persistentdisk",
    "oracle", "openstack", "cloud disk", "hyper-v", "kvm",
)

_smart_cache: dict[str, tuple[float, dict | None, str]] = {}
CACHE_TTL = 45          # 默认值；采样线程会按采集间隔调整，见 set_cache_ttl


def set_cache_ttl(seconds: float) -> None:
    """让 smartctl 缓存时长跟着采集间隔走。

    固定 45 秒在 10 秒采集时会让连续几条采样共用同一份 SMART 数据，
    温度/健康度看着"不动"；而在 600 秒采集时又毫无必要地反复调用。
    """
    global CACHE_TTL
    CACHE_TTL = max(5.0, min(45.0, float(seconds)))


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return fp.read().strip()
    except OSError:
        return default


def _read_int(path: str, default: int | None = None) -> int | None:
    raw = _read(path)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def disk_key(info: dict) -> str:
    """硬盘的稳定身份：优先序列号，没有序列号才退回设备名。

    设备名（sdc / nvme0n1）会因为换盘位、换主板、加装硬盘而变化，
    序列号才是这块盘本身的标识，历史数据一律按它归档。
    """
    serial = str(info.get("serial") or "").strip()
    if serial and serial.upper() not in ("", "N/A", "UNKNOWN", "000000000000"):
        return "sn:" + serial
    device = str(info.get("device") or "").strip()
    return "dv:" + device


def find_smartctl(app_dir: str) -> str | None:
    machine = platform.machine()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    candidates = [
        os.path.join(app_dir, "server", "bin", f"smartctl-{arch}"),
        os.path.join(app_dir, "server", "bin", "smartctl"),
        "/usr/sbin/smartctl",
        "/usr/bin/smartctl",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("smartctl")


_NVME_CTRL_RE = re.compile(r"/(nvme\d+)(?:/|$)")


def nvme_controller(device: str) -> str:
    """返回该磁盘背后的 NVMe 控制器名（nvme0…），不是 NVMe 时返回空串。

    铁威马 TOS 7 把 NVMe 命名空间重命名成 sdza / sdzb …（不是常见的 nvme0n1），
    但 sysfs 里它仍然挂在 NVMe 控制器下面：

        /sys/block/sdza -> …/nvme/nvme0/sdza

    所以不能再靠设备名判断协议，要顺真实路径把控制器找回来：
    SMART 只能对 /dev/nvme0 读，对 /dev/sdza 读会直接 INQUIRY failed。
    """
    if device.startswith("nvme"):
        # nvme0n1 -> nvme0（不能用 split("n")，那样会切出空串）
        match = re.match(r"(nvme\d+)", device)
        return match.group(1) if match else ""
    real = os.path.realpath(f"/sys/block/{device}")
    match = _NVME_CTRL_RE.search(real)
    return match.group(1) if match else ""


def _protocol(device: str) -> str:
    if device.startswith("nvme") or nvme_controller(device):
        return "NVMe"
    real = os.path.realpath(f"/sys/block/{device}")
    if "usb" in real:
        return "USB"
    return "SATA"


def _is_disk(name: str) -> bool:
    if name.startswith("(") or any(name.startswith(p) for p in IGNORED):
        return False
    if name.startswith("nvme"):
        return bool(re.fullmatch(r"nvme\d+n\d+", name))
    return bool(re.fullmatch(r"(sd|vd|hd)[a-z]+", name)) and not re.search(r"\d$", name)


def list_devices() -> list[str]:
    try:
        names = sorted(os.listdir("/sys/block"))
    except OSError:
        return []
    return [n for n in names if _is_disk(n)]


def base_info(device: str) -> dict:
    sysdir = f"/sys/block/{device}"
    size_sectors = _read_int(f"{sysdir}/size", 0) or 0
    rotational = _read_int(f"{sysdir}/queue/rotational", 1)
    vendor = _read(f"{sysdir}/device/vendor")

    ctrl = nvme_controller(device)
    if ctrl:
        # NVMe 的型号/序列号/固件只在控制器节点上，命名空间节点下取不到
        model = _read(f"/sys/class/nvme/{ctrl}/model")
        serial = _read(f"/sys/class/nvme/{ctrl}/serial")
        firmware = _read(f"/sys/class/nvme/{ctrl}/firmware_rev")
    else:
        model = _read(f"{sysdir}/device/model") or _read(f"{sysdir}/device/name")
        serial = _read(f"{sysdir}/device/serial")
        firmware = _read(f"{sysdir}/device/rev") or _read(f"{sysdir}/device/firmware_rev")

    return {
        "device": device,
        "path": f"/dev/{device}",
        # 读 SMART 用的节点：NVMe 一律走控制器，其余就是设备本身
        "smart_path": f"/dev/{ctrl}" if ctrl else f"/dev/{device}",
        "model": model.strip(),
        "vendor": vendor.strip(),
        "serial": serial.strip(),
        "firmware": firmware.strip(),
        "capacity": size_sectors * SECTOR,
        "protocol": _protocol(device),
        "rotational": rotational,
        "is_ssd": rotational == 0,
    }


# ------------------------------------------------------------------ SMART

def _ata_attr(table: list[dict], attr_id: int) -> dict | None:
    for row in table:
        if row.get("id") == attr_id:
            return row
    return None


def _ata_raw(attr: dict | None) -> int | None:
    if not attr:
        return None
    raw = attr.get("raw") or {}
    value = raw.get("value")
    return value if isinstance(value, int) else None


def _ata_percent(attr: dict | None) -> float | None:
    """ATA 归一化值 → 剩余寿命百分比。

    value 和 thresh 都是 1-100 的归一化值，厂商的约定是降到 thresh 就该换了。
    所以直接把 value 当剩余百分比会系统性高估：
    value=75、thresh=50 时真实剩余是 50%，不是 75%。
    """
    if not attr:
        return None
    value = attr.get("value")
    if not isinstance(value, int) or value < 0:
        return None
    thresh = attr.get("thresh")
    if isinstance(thresh, int) and 0 < thresh < 100:
        pct = (value - thresh) / (100 - thresh) * 100
    else:
        pct = float(value)
    return max(0.0, min(100.0, pct))


def _ata_bytes(attr: dict | None, block_size: int = SECTOR) -> int | None:
    """ID 241 / 242 的原始值 → 字节。

    smartctl 对部分型号会给出带单位的字符串（"854 GB"），优先用它；
    否则按 LBA × 逻辑扇区大小算。厂商把 241 记成 GB、32MB 单位的型号确实存在，
    这种只能靠调用方做合理性校验（_ata_volume_sane）。
    """
    if not attr:
        return None
    raw = attr.get("raw") or {}
    text = str(raw.get("string") or "")
    match = _HUMAN_SIZE.search(text)
    if match:
        return int(float(match.group(1)) * _UNIT_BYTES[match.group(2).upper()])
    value = raw.get("value")
    if not isinstance(value, int):
        return None
    return value * max(512, int(block_size or SECTOR))


def _ata_volume_sane(value: int | None, capacity: int | None, hours: int | None) -> bool:
    """判断换算出来的写入/读取量是否可信。

    单位猜错时算出来的数会离谱到没法用（差几个数量级），
    这时宁可不显示，也不要给用户一个错误的「累计写入」。
    """
    if not value or value <= 0:
        return False
    if not capacity:
        return True
    if value > capacity * 100000:            # 超过容量的 10 万倍，不可能
        return False
    if hours and hours > 168 and value < capacity * 0.05:
        return False                          # 用了一周以上，写入还不到容量的 5%
    return True


# 241 / 242 的候选单位。绝大多数盘按逻辑扇区记（512B，4Kn 盘是 4096B），
# 但也有主控按 MB / 32MB / GB 记（FORESEE 这块就是按 MB）。
# 顺序从单元到多元，取"最保守且说得通"的那个。
_ATA_UNIT_CANDIDATES = (1024 ** 2, 32 * 1024 ** 2, 1024 ** 3)


def _ata_unit_guess(raw: int, capacity: int | None, hours: int | None,
                    block_size: int) -> int | None:
    """识别 241/242 的单位，返回每单位字节数；判断不了返回 None。

    只用物理约束当尺子，**不碰厂商私有属性**——177 之类的 raw 语义因主控而异，
    拿它当基准只会推出"64GB 盘写了 1.6PB"这种荒唐结论。

    两条线：
      上限 = 容量 × 3000（NAND 擦写寿命决定写入不可能更多，否则早报废了）；
      下限 = 长期通电的盘总归写过一点（低于此说明单位被高估）。
    落在两条线之间的候选里取最小的单位——单位差通常是整数量级，小的更接近真相。
    """
    base = max(512, int(block_size or SECTOR))
    if not capacity or raw <= 0:
        return None
    ceiling = capacity * 3000
    floor = capacity * (0.02 if (hours or 0) > 2000 else 0.001)
    for mult in (base, *_ATA_UNIT_CANDIDATES):
        value = raw * mult
        if value > ceiling:
            continue
        if value >= floor:
            return mult
    return None


def _ata_volumes(attrs: list[dict], capacity: int | None, hours: int | None,
                 block_size: int) -> tuple[int | None, int | None, bool]:
    """算这块 SATA 盘的累计写入/读取字节数。

    返回 (写入, 读取, 是否靠推断得出)。识别不出单位时返回 (None, None, False)，
    由界面显示「—」——宁可缺数据，也不给一个错的数。

    241 和 242 是同一块盘上的同一套计数，单位必然一致：
    先用 241 定单位，242 沿用同一个，避免两个数字一个 PB 一个 MB。
    """
    write_attr = _ata_attr(attrs, ATA_LBA_WRITE)
    read_attr = _ata_attr(attrs, ATA_LBA_READ)
    if not write_attr and not read_attr:
        return None, None, False

    # 1) smartctl 给了带单位的人读字符串，最可靠
    written = _ata_bytes(write_attr, block_size)
    read = _ata_bytes(read_attr, block_size)
    if _ata_volume_sane(written, capacity, hours):
        return written, (read if _ata_volume_sane(read, capacity, hours) else None), False

    # 2) 按扇区算不合理，按物理约束重新选单位
    raw = ((write_attr or {}).get("raw") or {}).get("value")
    if isinstance(raw, int) and raw > 0:
        unit = _ata_unit_guess(raw, capacity, hours, block_size)
        if unit and unit != block_size:
            written = raw * unit
            if _ata_volume_sane(written, capacity, hours):
                raw_read = ((read_attr or {}).get("raw") or {}).get("value")
                read = raw_read * unit if isinstance(raw_read, int) else None
                return written, read, True

    # 3) 实在判断不了就留空
    return None, None, False


def _block_name(path: str) -> str | None:
    """把 /dev/xxx 解析成 sysfs 里的块设备名（dm-0 / md0 / sda1）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISBLK(st.st_mode):
        return None
    major, minor = os.major(st.st_rdev), os.minor(st.st_rdev)
    target = f"{major}:{minor}"
    base = "/sys/class/block"
    try:
        for name in os.listdir(base):
            if _read(os.path.join(base, name, "dev")) == target:
                return name
    except OSError:
        return None
    return None


def _base_disks(name: str, depth: int = 0) -> set[str]:
    """递归展开 dm/md 的 slaves，返回底层物理磁盘名（sda / nvme0n1）。"""
    if depth > 5:
        return set()
    slaves_dir = f"/sys/class/block/{name}/slaves"
    try:
        slaves = os.listdir(slaves_dir)
    except OSError:
        slaves = []
    if not slaves:
        # 分区则取父设备
        if _read(f"/sys/class/block/{name}/partition"):
            parent = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{name}")))
            return _base_disks(parent, depth + 1) if parent else {name}
        return {name}
    result: set[str] = set()
    for slave in slaves:
        result |= _base_disks(slave, depth + 1)
    return result


def mount_points() -> dict[str, list[str]]:
    """返回 物理磁盘 -> 它承载的挂载点列表。"""
    mapping: dict[str, list[str]] = {}
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fp:
            lines = fp.readlines()
    except OSError:
        return mapping
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/"):
            continue
        mp = parts[1].replace("\\040", " ")
        if mp.startswith("/sys") or mp.startswith("/proc"):
            continue
        name = _block_name(parts[0])
        if not name:
            continue
        for disk in _base_disks(name):
            mapping.setdefault(disk, []).append(mp)
    return mapping


def storage_role(device: str) -> str:
    """这块盘现在被谁占用：plain / raid / lvm。

    盘被组进 md 或 LVM 之后，即使上面有文件系统也未必挂载；
    这时测不了写入，得把原因说清楚，别只甩一句"未测"让用户猜。
    """
    parts = [device]
    try:
        parts += [p for p in os.listdir(f"/sys/class/block/{device}") if p.startswith(device)]
    except OSError:
        pass
    holders: list[str] = []
    for part in parts:
        try:
            holders.extend(os.listdir(f"/sys/class/block/{part}/holders"))
        except OSError:
            continue
    if any(h.startswith("md") for h in holders):
        return "raid"
    if any(h.startswith("dm-") for h in holders):
        return "lvm"
    return "plain"


def best_write_dir(device: str, mount_map: dict[str, list[str]] | None = None) -> str | None:
    """挑一个该磁盘承载的、可写的挂载点，用于写入测速。"""
    mapping = mount_map if mount_map is not None else mount_points()
    candidates = mapping.get(device) or []
    best, best_free = None, -1
    for mp in candidates:
        try:
            st = os.statvfs(mp)
        except OSError:
            continue
        if not os.access(mp, os.W_OK):
            continue
        free = st.f_bavail * st.f_frsize
        # 系统盘尽量排在后面
        score = free if mp not in ("/", "/boot", "/boot/efi") else free // 8
        if score > best_free:
            best, best_free = mp, score
    return best


def is_virtual(device: str, info: dict) -> bool:
    """判断是否为虚拟机/云主机上的虚拟磁盘。"""
    if device.startswith(("vd", "xvd", "zd")):
        return True
    text = " ".join(str(info.get(key, "")) for key in ("model", "vendor", "serial")).lower()
    if any(hint in text for hint in VIRTUAL_HINTS):
        return True
    real = os.path.realpath(f"/sys/block/{device}")
    return "virtio" in real or "/virtual/" in real


# SATA 固态常见的 SMART 属性：寿命余量 / 磨损指标 / 磨损均衡
SSD_ATTR_IDS = (231, 233, 177)


def looks_like_ssd(info: dict) -> bool:
    """判断是否为固态硬盘；判不准时倾向于保留（宁可多显示，也别漏盘）。

    以前写的是 rotation_rate == 1，问题是不少 SATA 固态（老主控、桥接芯片、
    部分固件）会把这项报成 0，于是被当成机械盘、整个从列表里消失。
    按 ATA 规范：1 表示固态，>1 才是真实转速，0 表示固件没报。
    """
    if info.get("protocol") == "NVMe":
        return True
    rate = info.get("rotation_rate")
    if isinstance(rate, int):
        return rate <= 1
    if info.get("rotational", 0) == 0:
        return True
    # 转速信息缺失，但 SMART 里带着固态专属属性，同样算固态
    for attr in (info.get("attributes") or []):
        if attr.get("id") in SSD_ATTR_IDS:
            return True
    return False


def smart_read(dev_path: str, smartctl: str | None) -> tuple[dict | None, str]:
    """返回 (sm 数据, 错误信息)。结果带缓存，避免频繁调用。"""
    now = time.time()
    cached = _smart_cache.get(dev_path)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1], cached[2]

    if not smartctl:
        result = (None, "未找到 smartctl")
        _smart_cache[dev_path] = (now, *result)
        return result

    cmd = [smartctl, "-a", "-j", "--nocheck=standby", dev_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=25)
        out = proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        result = (None, "smartctl 执行超时")
        _smart_cache[dev_path] = (now, *result)
        return result
    except OSError as exc:
        result = (None, f"smartctl 无法执行: {exc}")
        _smart_cache[dev_path] = (now, *result)
        return result

    if not out.strip():
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        result = (None, err[-1] if err else "无输出")
        _smart_cache[dev_path] = (now, *result)
        return result

    try:
        data = json.loads(out)
    except ValueError:
        result = (None, "smartctl 输出解析失败")
        _smart_cache[dev_path] = (now, *result)
        return result

    messages = data.get("smartctl", {}).get("messages") or []
    error = ""
    if messages:
        error = "; ".join(str(m.get("string", "")) for m in messages[:2])

    parsed = parse_smart(data)
    _smart_cache[dev_path] = (now, parsed, error)
    return parsed, error


def parse_smart(data: dict) -> dict:
    """把 smartctl JSON 归一化成统一字段。"""
    info: dict = {}

    model = data.get("model_name") or data.get("model_family") or ""
    if model:
        info["model"] = str(model).strip()
    if data.get("serial_number"):
        info["serial"] = str(data["serial_number"]).strip()
    if data.get("firmware_version"):
        info["firmware"] = str(data["firmware_version"]).strip()

    cap = (data.get("user_capacity") or {}).get("bytes")
    if isinstance(cap, int) and cap > 0:
        info["capacity"] = cap

    temp = data.get("temperature") or {}
    if isinstance(temp.get("current"), int):
        info["temp"] = temp["current"]

    rate = data.get("rotation_rate")
    if isinstance(rate, int):
        info["rotation_rate"] = rate  # 1 表示固态，其他为转速

    passed = (data.get("smart_status") or {}).get("passed")
    info["smart_passed"] = passed if isinstance(passed, bool) else None

    hours = (data.get("power_on_time") or {}).get("hours")
    if isinstance(hours, int):
        info["power_on_hours"] = hours

    nvme = data.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        info["protocol"] = "NVMe"
        info["nvme_log"] = {k: v for k, v in nvme.items() if not isinstance(v, (dict, list))}
        used = nvme.get("percentage_used")
        if isinstance(used, int):
            info["percent_used"] = used
            info["health"] = max(0, min(100, 100 - used))
        if isinstance(nvme.get("temperature"), int):
            info["temp"] = nvme["temperature"]
        if isinstance(nvme.get("power_on_hours"), int):
            info["power_on_hours"] = nvme["power_on_hours"]
        for key in ("power_cycles", "unsafe_shutdowns", "media_errors",
                    "available_spare", "available_spare_threshold", "critical_warning",
                    "controller_busy_time", "num_err_log_entries", "host_reads", "host_writes"):
            if isinstance(nvme.get(key), int):
                info[key] = nvme[key]
        if isinstance(nvme.get("data_units_written"), int):
            info["tbw"] = nvme["data_units_written"] * NVME_DATA_UNIT
        if isinstance(nvme.get("data_units_read"), int):
            info["tbr"] = nvme["data_units_read"] * NVME_DATA_UNIT
    else:
        attrs = ((data.get("ata_smart_attributes") or {}).get("table")) or []
        if attrs:
            info["protocol"] = "SATA"
            info["attributes"] = [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "value": a.get("value"),
                    "worst": a.get("worst"),
                    "thresh": a.get("thresh"),
                    "raw": (a.get("raw") or {}).get("value"),
                    "raw_string": (a.get("raw") or {}).get("string"),
                }
                for a in attrs
                if isinstance(a, dict)
            ]

            # SATA 盘没有百分比形式的「已用寿命」，只能从属性表反推。
            # 231 / 233 本身就是剩余寿命百分比，最可信；177 是厂商私有的
            # 归一化值（有的盘根本不是磨损含义），只在带有效阈值时兜底。
            life_pct, life_rel = None, None
            for attr_id in ATA_LIFE_IDS:
                pct = _ata_percent(_ata_attr(attrs, attr_id))
                if pct is not None:
                    life_pct, life_rel = pct, attr_id
                    break
            if life_pct is None:
                wear = _ata_attr(attrs, ATA_WEAR_ID)
                if wear and isinstance(wear.get("thresh"), int) and wear["thresh"] > 0:
                    life_pct = _ata_percent(wear)
                    life_rel = ATA_WEAR_ID
            if life_pct is not None:
                info["health"] = int(round(life_pct))
                info["percent_used"] = int(round(100 - life_pct))
                info["life_attr"] = life_rel      # 记下依据哪个属性算的，方便排查

            hours = _ata_raw(_ata_attr(attrs, 9))
            if isinstance(hours, int):
                info["power_on_hours"] = hours
            cycles = _ata_raw(_ata_attr(attrs, 12))
            if isinstance(cycles, int):
                info["power_cycles"] = cycles
            shutdowns = _ata_raw(_ata_attr(attrs, 192))   # Power-Off Retract Count
            if isinstance(shutdowns, int):
                info["unsafe_shutdowns"] = shutdowns
            temp = _ata_raw(_ata_attr(attrs, 194))
            if isinstance(temp, int) and "temp" not in info:
                info["temp"] = temp & 0xFF

            # 写入/读取量的单位因主控而异：多数按 LBA，也有按 MB / GB / 32MB 记的。
            # _ata_volumes 会先按逻辑扇区算，算出来不合常理时用擦除次数反推单位。
            block = data.get("logical_block_size") or data.get("sector_size") or SECTOR
            tbw, tbr, estimated = _ata_volumes(
                attrs, info.get("capacity"), info.get("power_on_hours"), block)
            if tbw is not None:
                info["tbw"] = tbw
            if tbr is not None:
                info["tbr"] = tbr
            if estimated:
                info["volume_estimated"] = True

            spare = _ata_raw(_ata_attr(attrs, 232))       # Available Reserved Space
            if isinstance(spare, int):
                info["available_spare"] = spare

            for attr_id, key in ((5, "reallocated_sectors"), (197, "pending_sectors"),
                                 (198, "uncorrectable_sectors"), (199, "crc_errors")):
                value = _ata_raw(_ata_attr(attrs, attr_id))
                if isinstance(value, int):
                    info[key] = value

    if info.get("smart_passed") is False:
        info["health"] = 0

    # 部分虚拟磁盘会返回 0℃，没有参考价值
    if info.get("temp") == 0:
        info.pop("temp", None)

    return info


# ------------------------------------------------------------------ 实时计数

def diskstats() -> dict[str, tuple[int, int]]:
    """从 /proc/diskstats 读取累计读写字节数（自本次开机）。"""
    result: dict[str, tuple[int, int]] = {}
    try:
        with open("/proc/diskstats", "r", encoding="utf-8") as fp:
            for line in fp:
                parts = line.split()
                if len(parts) < 10:
                    continue
                name = parts[2]
                if not _is_disk(name):
                    continue
                sectors_read = int(parts[5])
                sectors_written = int(parts[9])
                result[name] = (sectors_read * SECTOR, sectors_written * SECTOR)
    except OSError:
        pass
    return result


def process_io() -> dict[str, int]:
    """整机 IO（参考用）。"""
    try:
        with open("/proc/diskstats", "r", encoding="utf-8") as fp:
            lines = fp.readlines()
    except OSError:
        return {}
    total_read = total_write = 0
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        if parts[2].startswith(("loop", "ram", "dm-", "md")):
            continue
        total_read += int(parts[5]) * SECTOR
        total_write += int(parts[9]) * SECTOR
    return {"read": total_read, "write": total_write}


def _safe_smart(dev_path: str, smartctl: str | None) -> tuple[dict | None, str]:
    """线程池里跑 smartctl 的兜底：任何异常都退化成"这一块读不到"，不拖垮整轮。"""
    try:
        return smart_read(dev_path, smartctl)
    except Exception as exc:          # noqa: BLE001 - 采集失败只影响这一块盘
        return None, f"SMART 读取异常: {exc}"


def snapshot(app_dir: str, include_virtual: bool = False, only_ssd: bool = True) -> dict[str, dict]:
    """扫描磁盘，返回 device -> 信息字典。

    默认过滤掉虚拟磁盘，并且只保留固态硬盘。
    smartctl 每块盘要 1-2 秒，盘多时串行扫完要十几秒，所以并行跑。
    """
    smartctl = find_smartctl(app_dir)
    stats = diskstats()

    bases: dict[str, dict] = {}
    for name in list_devices():
        info = base_info(name)
        info["virtual"] = is_virtual(name, info)
        if info["virtual"] and not include_virtual:
            continue
        bases[name] = info

    names = list(bases)
    smarts: dict[str, tuple[dict | None, str]] = {}
    if names:
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
            results = pool.map(
                lambda n: _safe_smart(bases[n].get("smart_path") or bases[n]["path"], smartctl), names)
            for name, res in zip(names, results):
                smarts[name] = res

    disks: dict[str, dict] = {}
    for name in names:
        info = bases[name]
        sm, err = smarts.get(name, (None, "未扫描"))
        if sm:
            for key in ("model", "serial", "firmware", "capacity"):
                if sm.get(key):
                    info[key] = sm[key]
            for key in ("attributes", "nvme_log"):
                if key in sm:
                    info[key] = sm[key]
            for key in ("health", "temp", "power_on_hours", "power_cycles", "unsafe_shutdowns",
                        "media_errors", "tbw", "tbr", "percent_used", "smart_passed",
                        "available_spare", "available_spare_threshold", "critical_warning",
                        "controller_busy_time", "num_err_log_entries", "host_reads",
                        "host_writes", "rotation_rate",
                        "reallocated_sectors", "pending_sectors", "uncorrectable_sectors", "crc_errors"):
                if key in sm:
                    info[key] = sm[key]
            if sm.get("protocol") and info["protocol"] == "USB":
                info["bus"] = "USB"
        else:
            info["smart_error"] = err
        # 只有真的拿到健康/温度/写入量等字段才算 SMART 可用
        has_smart = bool(sm) and any(
            sm.get(key) is not None
            for key in ("health", "smart_passed", "tbw", "tbr", "power_on_hours", "temp")
        )
        info["smart_ok"] = has_smart
        if not has_smart and not info.get("smart_error"):
            info["smart_error"] = "该磁盘未提供 SMART 健康信息"
        info["ssd"] = looks_like_ssd(info)
        if only_ssd and not info["ssd"]:
            continue
        live = stats.get(name)
        info["session_read"], info["session_write"] = live if live else (0, 0)
        info["device"] = name
        info["key"] = disk_key(info)
        disks[name] = info
    return disks
