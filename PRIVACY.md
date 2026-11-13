# SSD助手 隐私政策

| 项目 | 内容 |
|---|---|
| 适用应用 | SSD助手（应用 ID：`ssdplus`），铁威马 TOS 7 的 deb 应用 |
| 适用版本 | 1.0.2 及以后版本 |
| 生效日期 | 2026-11-14 |
| 政策版本 | 1.0.2 |
| 开发者 | 疯狂的大叔（HUiTHEME） |
| 公开地址 | https://github.com/HUiTHEME/ssdplus_tos/blob/main/PRIVACY.md |
| 联系方式 | https://github.com/HUiTHEME/ssdplus_tos/issues |

## 一句话说明

**SSD助手只在本机读取硬盘的 SMART 数据并存放在本机，不联网、不上传、不采集任何
个人身份信息，也没有账号体系、统计埋点和行为追踪。**

---

## 1. 我们处理哪些数据

本应用处理的数据全部来自**本机硬件**和**你在界面上的操作**，共四类：

| 类别 | 具体内容 | 来源 |
|---|---|---|
| 硬盘身份 | 设备名（如 `nvme0n1`）、型号、序列号、固件版本、容量、接口协议 | 系统 `/sys` 与 `smartctl` |
| 硬盘状态 | 温度、健康度、通电时间、累计读写量、异常断电次数、介质错误计数、SMART 属性表 | `smartctl` |
| 时间序列 | 按采样间隔记录的各盘读写增量、温度与状态快照；测速结果；告警事件 | 应用自身采样 |
| 应用设置 | 采样间隔、保留天数、告警阈值、备份份数等 | 你在设置页填写 |

**明确不处理**：姓名、手机号、邮箱、账号密码、IP 地址、MAC 地址、地理位置、
浏览记录、剪贴板内容、硬盘里的文件内容或文件名、其他应用的进程与数据。

序列号仅用于在同一台设备上区分不同的硬盘（判断"是不是同一块盘"），不会离开本机。
如你不希望记录序列号，可在卸载前清空历史数据，或在应用设置中清空历史。

## 2. 数据存放在哪里

所有数据都写在本机应用目录下，不经过任何服务器：

| 路径 | 内容 | 创建者 |
|---|---|---|
| `/usr/local/ssdplus/data/ssdplus.db`（含 `-wal` / `-shm`） | SQLite 数据库：硬盘身份、采样明细、小时/天聚合、测速、事件 | 应用 |
| `/usr/local/ssdplus/data/etc/` | 应用设置（JSON） | 应用 |
| `/usr/local/ssdplus/data/backup/` | 数据库快照（可在设置里改到你选定的目录） | 应用 |
| `/usr/local/ssdplus/logs/` | 运行日志 | 应用 |
| `/usr/local/ssdplus/webui/` | 从包内 `webui.bz2` 解压出的页面文件 | 安装脚本 |

数据库权限为 `750`，属主为专用系统用户 `ssdplus`，其他普通用户无法读取。
应用**不监听 TCP 端口**，只监听 `/var/api/ssdplus.sock`，由 TOS 网关转发并做登录鉴权，
因此只有已登录这台 NAS 的用户才能看到这些数据；管理员本来就能看到全部硬盘信息。

## 3. 保留期限

| 数据 | 保留期限 | 是否可调 |
|---|---|---|
| 60 秒级明细采样 | 90 天（超期自动删除） | 可调，7–365 天，设置页「分钟级明细保留」 |
| 小时级聚合 | 730 天 | 固定 |
| 天级聚合（每天读写量、温度峰值、寿命消耗） | 长期保留 | 固定 |
| 测速结果 | 每个卷保留最近若干次 | 可在测速页手动清除 |
| 告警事件 | 保留在数据库中，可在概况页清除 | 可手动清除 |
| 数据库快照备份 | 按「份数」与「占用上限」双上限轮转，总是保留最新一份 | 可调，设置页「备份与恢复」 |
| 运行日志 | 由 systemd journal 管理，随系统轮转 | 由 TOS 统一管理 |

数据库体积不会无限增长：明细层滚动删除，聚合层按天存储，备份按上限轮转。

## 4. 你的权利

| 权利 | 在应用里怎么做 |
|---|---|
| 知情 | 本政策 + 设置页「运行信息」可看到数据库大小、明细条数、数据目录 |
| 查看 | 全部数据在「硬盘概况」「历史数据」「硬盘测速」页面展示 |
| 导出 / 可携带 | 「历史数据」页右上角「导出」按钮，导出该盘的完整记录为 JSON；也可在设置页导出数据库快照 |
| 更正 | 硬盘信息直接来自硬件，应用不做修改；设置项可随时改回默认值 |
| 删除 | 设置页「清空历史数据」清空明细、聚合、测速与事件（保留硬盘身份与当前 SMART 快照）；或卸载应用，见第 7 节 |
| 拒绝 | 不安装、或安装后卸载即可；应用没有需要"同意"才能关闭的数据通道 |

应用不提供也不收集账号，因此不存在"注销账号"这一动作；删除数据即可完成清除。

## 5. 安全措施

- **不以 root 运行**：以专用系统用户 `ssdplus`（`useradd -r`，nologin）运行，
  systemd 只授予读盘必需的三项能力 `CAP_DAC_READ_SEARCH` / `CAP_SYS_ADMIN` /
  `CAP_SYS_RAWIO`，并用 `CapabilityBoundingSet` 限定，配合 `NoNewPrivileges=true`、
  `ProtectSystem=strict`、`ProtectKernelModules=true` 等沙箱选项。
- **无出站网络**：代码中没有任何 HTTP 客户端、上报、更新检查或遥测；运行时不需要
  也无法访问互联网（仅在本机执行 `smartctl` 子进程）。
- **最小暴露面**：不监听 TCP 端口，只有本地 Unix socket；页面与接口都经 TOS 网关的
  登录鉴权与会话令牌（Cookie + `X-Csrf-Token`）。
- **文件权限**：数据目录 `750`，属主为应用用户；socket 为 `0660`，仅网关可连。
- **不引入第三方运行库**：后端只用 Python 标准库，前端无第三方框架，减少供应链风险。

## 6. 第三方共享

**不共享。** 本应用不与任何第三方共享、出售、转让上述数据，也没有广告、统计或
SDK 类组件向外发送数据。

应用与外部程序的唯一交互是调用系统自带的 `smartmontools`（`smartctl`）读取 SMART：
`smartctl` 以**本地子进程**方式运行，只把文本结果输出给本应用，不联网、不上传。
详见 `THIRD-PARTY-NOTICES.md`。

若你自行把「备份目录」指向共享文件夹（如 SMB 共享），备份文件会被该共享的访问者
看到——这是你主动选择的位置，请按需设置共享权限。

## 7. 删除渠道

任选其一，都可彻底删除数据：

1. **应用内清空**：应用设置 → 「清空历史数据」，立即删除明细、聚合、测速与事件。
2. **卸载应用**：在应用中心卸载，或执行 `sudo dpkg --purge ssdplus`。
   `postrm` 会删除数据目录、设置、备份、日志、页面解压目录、systemd 单元、
   `/usr/www/ssdplus` 软链与 `/var/api/ssdplus.sock`，卸载后不留配置文件或用户数据。
3. **手工删除**：`sudo rm -rf /usr/local/ssdplus/data /usr/local/ssdplus/logs`。
4. 若曾把「备份目录」改到共享文件夹，请自行删除该目录下的 `*.db` 快照。

卸载会一并删除历史数据，且不可恢复。需要留存历史请**先导出**：「历史数据 → 导出」，
或设置页「立即备份」后把快照文件复制出去。

## 8. 未成年人

本应用是本地硬件监控工具，不面向未成年人，也不收集任何可直接识别个人身份的信息。

## 9. 政策变更

政策随应用版本更新，最新版本始终在本仓库的 `PRIVACY.md` 与包内
`/usr/local/ssdplus/PRIVACY.md`。若变更涉及数据范围或保留期限，会在 Release 说明中
单独列出。继续使用新版本即表示接受更新后的政策。

## 10. 联系方式

- Issue：https://github.com/HUiTHEME/ssdplus_tos/issues
- 邮箱：HUiTHEME@users.noreply.github.com

---

# SSD Assistant Privacy Policy (English)

Applies to SSD Assistant (`ssdplus`), a deb application for TerraMaster TOS 7, version
1.0.2 and later. Effective date: 2026-11-14. Public URL:
https://github.com/HUiTHEME/ssdplus_tos/blob/main/PRIVACY.md

**In one sentence: SSD Assistant reads SMART data from the local disks and stores it on the
same device. It has no network access, uploads nothing, collects no personal data, and has
no account system, analytics or tracking.**

### 1. Data processed

Only data originating from the local hardware and from the user's own actions:

- **Disk identity**: device name, model, serial number, firmware, capacity, protocol.
- **Disk status**: temperature, health percentage, power-on hours, total bytes read/written,
  unexpected power-loss count, media errors, raw SMART attributes.
- **Time series**: sampled read/write deltas, temperature and status snapshots, benchmark
  results, alert events.
- **Application settings**: sampling interval, retention days, alert thresholds, backup count.

**Not processed**: name, phone, e-mail, credentials, IP or MAC address, location, browsing
history, clipboard, file contents or file names, data of other applications. Serial numbers
are used only locally to tell disks apart.

### 2. Storage location

Everything is stored under `/usr/local/ssdplus/` on the local device: `data/ssdplus.db`
(database), `data/etc/` (settings), `data/backup/` (snapshots, directory is user-selectable),
`data/../logs/` (logs), `webui/` (unpacked pages). The data directory is mode `750` and owned
by the dedicated system user `ssdplus`. The service listens on a Unix socket only
(`/var/api/ssdplus.sock`), behind the TOS gateway and its login/CSRF checks; it opens no TCP
port.

### 3. Retention period

- 60-second raw samples: 90 days by default (configurable 7–365), then deleted automatically.
- Hourly aggregates: 730 days.
- Daily aggregates: retained long-term.
- Benchmark results and alert events: kept in the database until cleared by the user.
- Database snapshots: rotated by count and total size cap; the newest is always kept.
- Logs: rotated by the system journal.

### 4. User rights

Access (all views), portability (JSON export from the History page, plus database snapshots),
rectification (settings can be reset to defaults), erasure (Settings → "Clear history data",
or uninstall), and refusal (do not install / uninstall). There is no account to delete.

### 5. Security measures

Runs as a non-root system user with only `CAP_DAC_READ_SEARCH`, `CAP_SYS_ADMIN` and
`CAP_SYS_RAWIO`, bounded by `CapabilityBoundingSet`, with `NoNewPrivileges=true`,
`ProtectSystem=strict`, `ProtectKernelModules=true`. No outbound network code paths exist
(the only external program is the local `smartctl` subprocess). No TCP listener; the socket
is `0660` and reachable only by the gateway. Data files are `750`, owned by the app user.
No third-party runtime libraries are used (Python standard library only; no JS framework).

### 6. Third-party sharing

**None.** No data is shared, sold or transferred to any third party. There are no ads,
analytics or SDKs. The only external program is the system-provided `smartmontools`
(`smartctl`), executed as a local subprocess; it does not access the network. See
`THIRD-PARTY-NOTICES.md`.

If the user points the backup directory at a shared folder (e.g. SMB), the snapshots become
visible to whoever can access that share — that is a location the user chose deliberately.

### 7. Deletion channel

1. In-app: Settings → "Clear history data".
2. Uninstall: App Center, or `sudo dpkg --purge ssdplus`. The `postrm` script removes the data
   directory, settings, backups, logs, unpacked pages, the systemd unit, the
   `/usr/www/ssdplus` symlink and `/var/api/ssdplus.sock`; no configuration or user data is
   left behind.
3. Manual: `sudo rm -rf /usr/local/ssdplus/data /usr/local/ssdplus/logs`.
4. If a custom backup directory was configured, delete the `*.db` snapshots there manually.

Uninstalling deletes the history permanently. Export or back up first if the history is
needed.

### 8. Children

This is a local hardware monitoring tool; it is not directed at children and collects no
personally identifiable information.

### 9. Changes

This policy is versioned with the application; the current text is always in the repository
`PRIVACY.md` and inside the package at `/usr/local/ssdplus/PRIVACY.md`. Changes affecting the
scope of data or retention are called out in the release notes.

### 10. Contact

- Issues: https://github.com/HUiTHEME/ssdplus_tos/issues
- E-mail: HUiTHEME@users.noreply.github.com
