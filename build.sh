#!/bin/bash
# ============================================================
# SSD助手 —— 铁威马 TOS 7 安装包（.deb）构建脚本
#
#   ./build.sh                构建当前机器架构对应的包
#   ./build.sh x86_64         构建 x86_64（amd64）
#   ./build.sh aarch64        构建 aarch64（arm64）
#   ./build.sh all            两个架构各出一个包
#
# 产物：dist/ssdplus_<platform>.deb
#
# 后端与前端源码在 app/ 下；本目录只额外放了 TOS 专属的打包元数据
# 和一层很薄的前端适配（overrides/）。
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$HERE/packaging"
OVR="$HERE/overrides"
DIST="$HERE/dist"
STAGE="${TMPDIR:-/tmp}/ssdplus-tos-build"

# 源码目录：仓库里就是 app/；本地开发时也可能还挂在上一级的 ssdplus/app
SRC="$HERE/app"
if [ ! -f "$SRC/server/main.py" ] && [ -f "$HERE/../ssdplus/app/server/main.py" ]; then
  SRC="$HERE/../ssdplus/app"
fi

APPID=$(python3 -c "import json;print(json.load(open('$PKG/config.ini'))['id'])")
VERSION=$(python3 -c "import json;print(json.load(open('$PKG/config.ini'))['version'])")
SYSTEM_ID=$(python3 -c "import json;print(json.load(open('$PKG/config.ini'))['system_id'])")

clean_dir() { python3 -c "import shutil,sys;shutil.rmtree(sys.argv[1],ignore_errors=True)" "$1"; }

[ -f "$SRC/server/main.py" ] || { echo "错误：找不到后端源码 $SRC/server/main.py" >&2; exit 1; }

mkdir -p "$DIST"

build_one() {
  local platform="$1" dpkgarch="$2"
  local out="$DIST/${APPID}_${platform}.deb"

  echo "==> 构建 ${APPID} ${VERSION} (${platform})"
  clean_dir "$STAGE"
  local app="$STAGE/usr/local/$APPID"
  mkdir -p "$app/bin" "$app/server" "$app/images/icons" "$app/init.d" "$STAGE/DEBIAN"

  # --- 后端（Python，标准库，无第三方依赖）---
  cp "$SRC/server/"*.py "$app/server/"
  find "$app/server" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
  find "$app/server" -name "*.pyc" -delete 2>/dev/null || true
  # 不打包自带的 smartctl 二进制：TOS 系统自带 smartmontools，
  # 由 Depends 保证存在，跟着发行版升级比捆一个固定版本更靠谱。
  rm -rf "$app/server/bin"
  chmod 644 "$app/server/"*.py

  # --- 启动器 ---
  cp "$PKG/bin/ssdplus" "$app/bin/ssdplus"
  chmod 755 "$app/bin/ssdplus"

  # --- 平台元数据 ---
  cp "$PKG/config.ini"              "$app/config.ini"
  cp "$PKG/$APPID.lang"             "$app/$APPID.lang"
  cp "$PKG/$APPID.env"              "$app/$APPID.env"
  cp "$PKG/images/icons/$APPID.svg" "$app/images/icons/$APPID.svg"
  cp "$PKG/init.d/$SYSTEM_ID.service" "$app/init.d/$SYSTEM_ID.service"
  chmod 644 "$app/config.ini" "$app/$APPID.lang" "$app/$APPID.env" \
            "$app/images/icons/$APPID.svg" "$app/init.d/$SYSTEM_ID.service"

  # --- 许可与隐私（平台审核要求包内自带，且能直接打开）---
  # LICENSE / PRIVACY.md / THIRD-PARTY-NOTICES.md 同时放两处：
  #   · /usr/local/<appid>/     应用目录，用户和审核人员能直接看
  #   · /usr/share/doc/<appid>/ Debian 约定的文档位置（copyright 按规范归档）
  cp "$HERE/LICENSE"                  "$app/LICENSE"
  cp "$HERE/PRIVACY.md"               "$app/PRIVACY.md"
  cp "$HERE/THIRD-PARTY-NOTICES.md"   "$app/THIRD-PARTY-NOTICES.md"
  chmod 644 "$app/LICENSE" "$app/PRIVACY.md" "$app/THIRD-PARTY-NOTICES.md"

  local doc="$STAGE/usr/share/doc/$APPID"
  mkdir -p "$doc"
  # TOS 的 dpkg 配了 path-exclude=/usr/share/doc/*，只放行 copyright，
  # 所以这里只放 copyright；可读的许可与隐私原文放在 /usr/local/ssdplus/ 下。
  { echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"; \
    echo; echo "Upstream-Name: ssdplus"; \
    echo "Source: https://github.com/HUiTHEME/ssdplus_tos"; \
    echo; echo "Files: *"; \
    echo "Copyright: 2026 疯狂的大叔 (HUiTHEME)"; \
    echo "License: MIT"; \
    echo; sed -e 's/^/ /' -e 's/^ \.$/ ./' "$HERE/LICENSE"; \
    echo; echo "Files: debian/*"; \
    echo "Copyright: 2026 疯狂的大叔 (HUiTHEME)"; \
    echo "License: MIT"; \
  } > "$doc/copyright"
  chmod 644 "$doc/copyright"

  # config.ini 的 platform 必须与这个包的实际架构一致：平台提交时会拿它和
  # 开发者平台上登记的架构比对，不匹配直接驳回，且不接受 all / 多值。
  python3 - "$app/config.ini" "$platform" "$VERSION" <<'PYV'
import json, sys
path, plat, ver = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path, encoding='utf-8'))
d['platform'] = plat
d['version'] = ver
with open(path, 'w', encoding='utf-8') as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
    f.write('\n')
PYV
  chmod 644 "$app/config.ini"

  # --- 前端：webui.bz2（内部是 webui/ 目录）---
  local web="$STAGE/webui-src/webui"
  mkdir -p "$web/assets"
  cp -R "$SRC/www/." "$web/"
  cp "$OVR/www/assets/tos.js" "$web/assets/tos.js"
  cp "$OVR/www/assets/tos.css" "$web/assets/tos.css"
  # macOS 会在目录里留 ._xxx（AppleDouble）和 .DS_Store，
  # 打进包里既没用又会被平台当成页面文件。
  python3 -c "import os,sys; [os.remove(os.path.join(d,f)) for d,_,fs in os.walk(sys.argv[1]) for f in fs if f.startswith('._') or f in ('.DS_Store','Thumbs.db')]" "$web" 2>/dev/null || true

  # 插入 TOS 专属的样式补丁（.app 的 40px 顶部留白）和平台适配脚本，
  # 再给静态资源带上**内容哈希**的版本号。
  #
  # 这里不能用应用版本号：平台 nginx 对 .js/.css 给了 7 天强缓存，而应用版本
  # 要长期停在 1.0.0，用版本号当查询串的话 URL 一直不变，浏览器永远拿旧文件
  # —— 改了前端却看不到效果，就是这么来的。用文件内容哈希，只要文件变了 URL
  # 就变，缓存必然失效。
  python3 - "$web/index.html" <<'PY'
import hashlib, os, re, sys

path = sys.argv[1]
base = os.path.dirname(path)
html = open(path, encoding='utf-8').read()

# 样式补丁放在 app.css 之后，覆盖同名规则时天然优先
if 'tos.css' not in html:
    html = re.sub(r'(<link[^>]*href="assets/app\.css[^"]*"[^>]*>)',
                  r'\1\n  <link rel="stylesheet" href="assets/tos.css" />', html, count=1)

# 适配脚本必须在 app.js 之前加载（它要包 fetch）
if 'tos.js' not in html:
    html = html.replace('<script src="assets/app.js"></script>',
                        '<script src="assets/tos.js"></script>\n  <script src="assets/app.js"></script>')

def digest(rel):
    try:
        with open(os.path.join(base, rel), 'rb') as fp:
            return hashlib.sha1(fp.read()).hexdigest()[:8]
    except OSError:
        return '0'

pattern = r'((?:href|src)=")(assets/[^"?]+\.(?:css|js))(\?v=[^"]*)?(")'
html = re.sub(pattern, lambda m: m.group(1) + m.group(2) + '?v=' + digest(m.group(2)) + m.group(4), html)
open(path, 'w', encoding='utf-8').write(html)
print('  静态资源版本号：' + ', '.join(
    sorted({m.group(2) + '?v=' + digest(m.group(2)) for m in re.finditer(pattern, html)})))
PY
  grep -q 'assets/tos.js' "$web/index.html" || { echo "错误：index.html 未插入 tos.js" >&2; exit 1; }
  grep -q 'assets/tos.css' "$web/index.html" || { echo "错误：index.html 未插入 tos.css" >&2; exit 1; }
  ( cd "$STAGE/webui-src" && tar -cjf "$app/webui.bz2" webui )
  chmod 644 "$app/webui.bz2"
  clean_dir "$STAGE/webui-src"

  # --- DEBIAN 元数据 ---
  sed -e "s/^Package:.*/Package: ${APPID}/" \
      -e "s/^Version:.*/Version: ${VERSION}/" \
      -e "s/^Architecture:.*/Architecture: ${dpkgarch}/" \
      "$PKG/DEBIAN/control" > "$STAGE/DEBIAN/control"
  for f in postinst prerm postrm; do
    cp "$PKG/DEBIAN/$f" "$STAGE/DEBIAN/$f"
    chmod 755 "$STAGE/DEBIAN/$f"
  done

  # 整个打包树再扫一遍 macOS 垃圾文件：AppleDouble（._xxx）会被平台当成真实文件
  # 装进应用目录，审核时算"多余文件"，也难看。
  find "$STAGE" \( -name "._*" -o -name ".DS_Store" -o -name "Thumbs.db" \) -delete 2>/dev/null || true
  find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$STAGE" -name "*.pyc" -delete 2>/dev/null || true

  # 行尾统一 LF：脚本/配置里混进 CRLF 会让 TOS 报 bad interpreter
  python3 - "$STAGE" <<'PY'
import os, sys
root = sys.argv[1]
for dirpath, _, names in os.walk(root):
    for n in names:
        if n.endswith(('.sh', '.ini', '.lang', '.service', '.env', '.js', '.css', '.html')) or n in ('control', 'postinst', 'prerm', 'postrm'):
            p = os.path.join(dirpath, n)
            b = open(p, 'rb').read()
            if b'\r\n' in b:
                open(p, 'wb').write(b.replace(b'\r\n', b'\n'))
PY

  python3 "$HERE/tools/mkdeb.py" "$STAGE" "$out"
  clean_dir "$STAGE"
  ( cd "$DIST" && if command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "$(basename "$out")" > "$(basename "$out").sha256";
    else sha256sum "$(basename "$out")" > "$(basename "$out").sha256"; fi )
  ls -lh "$out"
}

case "${1:-auto}" in
  x86_64)  build_one x86_64 amd64 ;;
  aarch64) build_one aarch64 arm64 ;;
  all)     build_one x86_64 amd64; build_one aarch64 arm64 ;;
  auto)
    case "$(uname -m)" in
      arm64|aarch64) build_one aarch64 arm64 ;;
      *)             build_one x86_64 amd64 ;;
    esac ;;
  *) echo "用法: $0 [x86_64|aarch64|all]" >&2; exit 1 ;;
esac

echo "==> 完成，产物在 $DIST"
