# 第三方组件与许可声明 / Third-Party Notices

本文件随安装包分发（`/usr/local/ssdplus/THIRD-PARTY-NOTICES.md`，另见
`/usr/share/doc/ssdplus/copyright`），用于说明 SSD助手 自身的许可、用到的第三方组件
及其许可，以及为何本包不再分发这些组件。

This file ships with the package (`/usr/local/ssdplus/THIRD-PARTY-NOTICES.md`, see also
`/usr/share/doc/ssdplus/copyright`). It states the licence of SSD Assistant itself, the
third-party components it uses, and why none of them are redistributed inside this package.

## 1. 本应用 / This application

| 项目 | 内容 |
|---|---|
| 名称 | SSD助手（应用 ID `ssdplus`） |
| 著作权人 | 疯狂的大叔（HUiTHEME） |
| 许可 | MIT，全文见包内 `LICENSE` 与仓库根目录 `LICENSE` |
| 源码 | https://github.com/HUiTHEME/ssdplus_tos |

本应用的全部后端代码（Python）、前端页面与脚本（HTML/CSS/JavaScript）、应用图标与
界面图形均为本项目原创，未包含任何从其他项目复制或改写的源代码、样式表、脚本库、
字体或图标素材。前端不使用任何第三方 JS 框架或 CDN 资源（无 jQuery / Vue / React /
Chart.js / 外部字体），图形绘制为自写的 SVG 生成代码。

All backend (Python), front-end (HTML/CSS/JavaScript), icons and graphics are original
work of this project. No third-party source code, stylesheet, script library, font or icon
asset is copied into or derived into this package. The front end loads no external JS
framework and no CDN resource; all charts are drawn by the project's own SVG code.

### 商标 / Trademarks

- 「TerraMaster」「TOS」「铁威马」是其权利人的商标，本项目仅在描述运行平台时使用，
  与权利人无隶属或背书关系。
- 「NVMe」是 NVM Express, Inc. 的商标；「SATA」是 SATA-IO 的商标。
- 应用名称「SSD助手」、图标与界面为本项目原创，未使用上述权利人的商标素材。

"TerraMaster", "TOS" and the TerraMaster logo are trademarks of their respective owners.
They are used here only to describe the platform this application runs on. This project is
not affiliated with or endorsed by TerraMaster. "NVMe" is a trademark of NVM Express, Inc.
The application name, icon and UI are original artwork of this project.

## 2. 运行时依赖（不随本包分发）/ Runtime dependencies (not redistributed)

本包**不包含**下列组件的任何二进制、源码或数据文件，仅在运行时以独立进程方式调用
系统已安装的版本；它们由 TOS 系统或 `Depends` 声明的软件包提供，随发行版各自升级。

This package contains **no** binary, source or data file of the components below. They are
invoked as separate processes at run time and are provided by TOS itself or by the packages
declared in `Depends`, each upgraded by the distribution independently.

| 组件 | 许可 | 用途 | 分发方式 |
|---|---|---|---|
| smartmontools（`smartctl`） | GPL-2.0-or-later | 读取硬盘 SMART 属性 | 系统包，`Depends: smartmontools`；仅通过 `subprocess` 调用命令行，不链接其代码 |
| Python 3 及标准库 | PSF License 2.0 | 后端运行时 | 系统包，`Depends: python3`；不随包分发解释器 |
| systemd / glibc | LGPL-2.1-or-later | 服务管理与 C 运行库 | TOS 系统组件 |

关于 GPL：`smartctl` 以独立进程、通过命令行参数调用，双方只交换标准输出文本，
不构成链接或衍生作品，因此本应用整体按 MIT 发布，也没有义务随包分发 GPL 源码。
若你需要 `smartctl` 的源码，可从 https://www.smartmontools.org/ 获取，或在本机执行
`apt-get source smartmontools`。

`smartctl` is invoked as a standalone process over its command line; only stdout text is
exchanged. This is not linking and does not create a derivative work, so this application is
distributed under MIT and carries no obligation to ship GPL sources. smartmontools sources
are available from https://www.smartmontools.org/ or via `apt-get source smartmontools`.

## 3. 平台规范 / Platform specification

应用的目录结构、`config.ini` 字段、systemd 单元、生命周期脚本按铁威马公开发布的
《TOS 7 Application Development Guide》编写。该文档为平台方发布的技术规范，本项目
仅遵循其接口约定，未复制其文档内容到本包中。

The package layout, `config.ini` fields, systemd unit and maintainer scripts follow the
publicly available "TOS 7 Application Development Guide". Only the interface contract is
implemented; no documentation text is copied into this package.

## 4. 反馈 / Contact

许可或署名方面的问题，请通过以下方式联系：

- Issues: https://github.com/HUiTHEME/ssdplus_tos/issues
- 邮箱: HUiTHEME@users.noreply.github.com
