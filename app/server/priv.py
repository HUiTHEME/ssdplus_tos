# -*- coding: utf-8 -*-
"""特权操作通道。

读 SMART（尤其是 USB 桥接的 NVMe，要 CAP_SYS_RAWIO）和测速直读设备都需要 root，
但对外提供 Web 服务的进程必须是普通用户，否则应用中心不给上架。

所以启动时把进程拆成两个：
  * 特权进程（root）：跑采样线程、执行 smartctl 与测速，把磁盘快照写进
    snapshot.json，并监听 priv.sock 响应「立即采集」「开始/停止测速」；
  * Web 进程（降权到应用用户）：只读快照文件和数据库，需要特权操作时走 socket。

Web 侧不直接持有设备权限，也就拿不到「以 root 身份执行任意命令」的能力，
即使 Web 层出现漏洞，能做的也仅限于这几个固定的动作。
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import traceback

SNAPSHOT_NAME = "snapshot.json"
SOCKET_NAME = "priv.sock"


def _peer_uid(conn: socket.socket) -> int:
    """取对端进程的 uid（SO_PEERCRED），失败返回 -1。"""
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]
    except (OSError, struct.error):
        return -1


def snapshot_path(data_dir: str) -> str:
    return os.path.join(data_dir, SNAPSHOT_NAME)


def socket_path(data_dir: str) -> str:
    return os.path.join(data_dir, SOCKET_NAME)


def write_snapshot(data_dir: str, disks: dict, last_run: int | None, error: str | None) -> None:
    """特权进程把最新快照落盘，Web 进程读文件即可，不用每次走 socket。"""
    tmp = snapshot_path(data_dir) + ".tmp"
    payload = {"ts": int(time.time()), "last_run": last_run,
               "error": error, "disks": disks}
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        os.replace(tmp, snapshot_path(data_dir))     # 原子替换，读端不会读到半个文件
    except (OSError, TypeError, ValueError):
        # 快照写失败不能影响采样本身，Web 那边最多看到旧数据
        pass


class SnapshotProxy:
    """Web 进程里顶替 Sampler 的只读视图，接口与 Sampler 保持一致。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._mtime = -1.0
        self._disks: dict = {}
        self._last: int | None = None
        self._error: str | None = None

    def snapshot(self) -> tuple[dict, int | None, str | None]:
        try:
            stamp = os.stat(snapshot_path(self.data_dir)).st_mtime
            if stamp != self._mtime:
                with open(snapshot_path(self.data_dir), encoding="utf-8") as fp:
                    data = json.load(fp)
                self._mtime = stamp
                self._disks = data.get("disks") or {}
                self._last = data.get("last_run")
                self._error = data.get("error")
        except (OSError, ValueError):
            # 文件还没生成（应用刚启动）或损坏，返回上一次的结果
            pass
        return dict(self._disks), self._last, self._error

    def refresh_now(self, timeout: float = 25.0) -> bool:
        res = call(self.data_dir, {"cmd": "refresh"}, timeout)
        return bool(res.get("ok"))

    def stop(self) -> None:
        """空实现：Web 进程这边没有采样线程可停。

        Sampler 有 stop()，退出流程会统一调 app.sampler.stop()；
        代理对象缺这个方法会在收到 SIGTERM 时抛 AttributeError，
        导致 serve_forever 的 finally 里 socket 清理被跳过。
        """
        return None


def call(data_dir: str, payload: dict, timeout: float = 30.0) -> dict:
    """向特权进程发一条请求，返回它的应答。"""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path(data_dir))
        sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 8 * 1024 * 1024:      # 防止对端异常时无限增长
                break
        return json.loads(buf.decode("utf-8") or "{}")
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"特权进程无响应：{exc}"}
    finally:
        try:
            sock.close()
        except OSError:
            pass


class PrivServer(threading.Thread):
    """特权侧（root）的请求处理器。handlers 由调用方注入，这里只管通讯。"""

    def __init__(self, data_dir: str, handlers: dict, gid: int = -1,
                 allow_uid: int | None = None):
        super().__init__(daemon=True, name="priv-server")
        self.path = socket_path(data_dir)
        self.handlers = handlers
        self.gid = gid
        self.allow_uid = allow_uid      # 只接受这个 uid 的连接（应用用户）
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def run(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(self.path)
            # 只让应用用户能连：Web 进程降权后仍要访问，其他人一律不行
            os.chmod(self.path, 0o660)
            if self.gid >= 0:
                try:
                    os.chown(self.path, 0, self.gid)
                except OSError:
                    pass
            srv.listen(16)
            srv.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        finally:
            try:
                srv.close()
            except OSError:
                pass
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(120.0)
            # 只认应用用户：socket 的组权限是 AppUsers，将来若有别的应用也进这个组，
            # 光靠文件权限挡不住，这里直接按凭据拒绝
            if self.allow_uid is not None and _peer_uid(conn) != self.allow_uid:
                conn.sendall(b'{"ok": false, "error": "forbidden"}\n')
                return
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 1024 * 1024:
                    return
            try:
                req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            except ValueError:
                conn.sendall(b'{"ok": false, "error": "bad json"}\n')
                return
            handler = self.handlers.get(str(req.get("cmd") or ""))
            if not handler:
                reply = {"ok": False, "error": "unknown cmd"}
            else:
                try:
                    reply = handler(req)
                except Exception as exc:           # noqa: BLE001 - 单条请求失败不能拖垮守护
                    reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    traceback.print_exc()
            conn.sendall((json.dumps(reply, ensure_ascii=False, default=str) + "\n")
                         .encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
