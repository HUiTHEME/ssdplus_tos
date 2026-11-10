#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tos_preview.py —— 在本机模拟铁威马 TOS 的前端运行环境。

用来在没有 TOS 图形界面的情况下核对页面：平台会把页面挂在 /<appid>/、
把接口挂在 /v2/proxy/<appid>/，这里把这两条路径在本机复现出来：

    http://127.0.0.1:<port>/ssdplus/        静态页面（取 webui/ 目录）
    http://127.0.0.1:<port>/v2/proxy/ssdplus/api/...   转发给后端

后端可以是本地起的进程，也可以是一条 SSH 隧道：

    ssh -N -L 19001:/var/api/ssdplus.sock zooo@<nas>
    python3 tools/tos_preview.py --backend 127.0.0.1:19001

和真实平台一致的地方：
  * 页面地址带 /ssdplus/ 前缀，接口必须走 /v2/proxy/ssdplus/
  * 缺 X-Csrf-Token 头的请求一律回 {"is_login":false,...}，
    和 TOS 网关的表现一样（规范 8.10）
"""
from __future__ import annotations

import argparse
import glob
import http.client
import os
import posixpath
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APPID = "ssdplus"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

LOGIN_MISSING = b'{"is_login":false,"code":false,"msg":"please login","data":null}'


class Handler(BaseHTTPRequestHandler):
    root = ""
    backend_host = "127.0.0.1"
    backend_port = 19001
    last_csrf = ""

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- 静态
    def _serve_static(self, rel: str) -> None:
        rel = posixpath.normpath(rel).lstrip("/")
        if rel == "_frame.html":
            # 带平台标题栏的外壳页面：不放在页面目录里，免得被打进包里
            target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_frame.html")
            if not os.path.isfile(target):
                self.send_error(404, "缺少 tools/_frame.html")
                return
            body = open(target, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "X-Csrf-Token=preview-token; Path=/")
            self.end_headers()
            self.wfile.write(body)
            return
        target = os.path.realpath(os.path.join(self.root, rel))
        if not target.startswith(os.path.realpath(self.root)):
            self.send_error(403)
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            self.send_error(404, "not found: %s" % rel)
            return
        body = open(target, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(target)[1].lower(),
                                                  "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # 平台会把这两个 cookie 发给浏览器；这里给一个假的，方便验证请求头
        if target.endswith("index.html"):
            self.send_header("Set-Cookie", "X-Csrf-Token=preview-token; Path=/")
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- 代理
    def _proxy(self, path: str, body: bytes | None) -> None:
        csrf = self.headers.get("X-Csrf-Token") or ""
        Handler.last_csrf = csrf
        if not csrf:
            # 与真实网关一致：没有 X-Csrf-Token 就是未登录
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(LOGIN_MISSING)))
            self.end_headers()
            self.wfile.write(LOGIN_MISSING)
            return

        conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=60)
        headers = {"X-Csrf-Token": csrf}
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]
        if self.headers.get("X-Requested-With"):
            headers["X-Requested-With"] = self.headers["X-Requested-With"]
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type") or "application/json")
            if resp.getheader("Content-Disposition"):
                self.send_header("Content-Disposition", resp.getheader("Content-Disposition"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError as exc:
            msg = ('{"error":"backend unreachable: %s"}' % exc).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        finally:
            conn.close()

    def _handle(self, body: bytes | None = None) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        prefix = "/v2/proxy/%s" % APPID
        if path.startswith(prefix):
            rest = path[len(prefix):] or "/"
            if parsed.query:
                rest += "?" + parsed.query
            self._proxy(rest, body)
            return
        app_root = "/%s/" % APPID
        if path == "/%s" % APPID:
            self.send_response(301)
            self.send_header("Location", app_root)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith(app_root):
            self._serve_static(path[len(app_root):] or "index.html")
            return
        self.send_error(404)

    def do_GET(self):  # noqa: N802
        self._handle()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self._handle(self.rfile.read(length) if length else b"")

    def do_DELETE(self):  # noqa: N802
        self._handle()


def _extract_webui_from_deb(dist_dir: str) -> str | None:
    """从 dist/ 里最新的 deb 解出 webui/，省得手工解 webui.bz2。"""
    import io
    import tarfile
    import tempfile

    debs = sorted(glob.glob(os.path.join(dist_dir, "*.deb")), key=os.path.getmtime)
    if not debs:
        return None
    blob = open(debs[-1], "rb").read()
    if blob[:8] != b"!<arch>\n":
        return None
    off, data_tar = 8, None
    while off < len(blob):
        header = blob[off:off + 60]
        name = header[:16].decode().strip()
        size = int(header[48:58].decode().strip())
        if name == "data.tar.gz":
            data_tar = blob[off + 60:off + 60 + size]
            break
        off += 60 + size + (size % 2)
    if not data_tar:
        return None

    out = tempfile.mkdtemp(prefix="ssdplus-preview-")
    with tarfile.open(fileobj=io.BytesIO(data_tar), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith("/webui.bz2")), None)
        if not member:
            return None
        packed = tar.extractfile(member).read()
    src = os.path.join(out, "webui.bz2")
    open(src, "wb").write(packed)
    with tarfile.open(src, mode="r:bz2") as tar:
        tar.extractall(out)
    web = os.path.join(out, "webui")
    return web if os.path.isfile(os.path.join(web, "index.html")) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="TOS 前端预览")
    here = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument("--root", default=os.path.join(here, "..", "preview", "webui"))
    parser.add_argument("--port", type=int, default=19002)
    parser.add_argument("--backend", default="127.0.0.1:19001")
    args = parser.parse_args()

    host, _, port = args.backend.rpartition(":")
    Handler.root = os.path.abspath(args.root)
    Handler.backend_host = host or "127.0.0.1"
    Handler.backend_port = int(port or 19001)

    if not os.path.isfile(os.path.join(Handler.root, "index.html")):
        auto = _extract_webui_from_deb(os.path.join(here, "..", "dist"))
        if auto:
            Handler.root = auto
            print("页面目录为空，已从 dist/*.deb 自动解出：%s" % auto)
        else:
            print("找不到页面目录：%s（先运行 build.sh，或用 --root 指定）" % Handler.root,
                  file=sys.stderr)
            return 1

    print("预览地址: http://127.0.0.1:%d/%s/" % (args.port, APPID))
    print("带平台标题栏: http://127.0.0.1:%d/%s/_frame.html" % (args.port, APPID))
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
