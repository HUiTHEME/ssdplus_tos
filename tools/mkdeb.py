#!/usr/bin/env python3
"""mkdeb.py —— 在 macOS / Linux 上直接产出标准 .deb（不依赖 dpkg-deb）。

.deb 就是 ar 归档，里面固定三个成员：

    debian-binary      内容 "2.0\\n"
    control.tar.gz     元数据与安装脚本（./control、./postinst ...）
    data.tar.gz        实际文件（./usr/local/<appid>/...）

ar 的每个成员头 60 字节，内容按偶数长度对齐（奇数补一个 \\n）。
macOS 自带的 ar 在写长文件名和头字段时和 Debian 的约定不一致，
所以这里直接用 Python 拼字节，保证和 dpkg-deb 的输出同构。

用法：
    python3 mkdeb.py <包根目录> <输出.deb>
包根目录下需要有 DEBIAN/，其余内容按安装路径摆放（usr/local/xxx/...）。
"""
import gzip
import io
import os
import sys
import tarfile
import time

AR_MAGIC = b"!<arch>\n"


def _tar_bytes(root: str, entries: list, mtime: int) -> bytes:
    """把给定的相对路径列表打成 tar.gz，路径统一加 ./ 前缀（dpkg 的习惯）。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for rel in entries:
            src = os.path.join(root, rel)
            st = os.lstat(src)
            info = tarfile.TarInfo("./" + rel)
            info.mtime = mtime
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mode = st.st_mode & 0o7777
            if os.path.isdir(src):
                info.type = tarfile.DIRTYPE
                info.size = 0
                tar.addfile(info)
            else:
                with open(src, "rb") as fp:
                    data = fp.read()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def _walk_files(root: str, sub: str) -> list:
    """递归收集 sub 下的所有文件/目录（相对路径，目录在前）。"""
    result = []
    base = os.path.join(root, sub)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            result.append(rel_dir)
        for name in sorted(filenames):
            result.append(os.path.relpath(os.path.join(dirpath, name), root))
    return result


def _ar_member(name: str, data: bytes) -> bytes:
    header = (
        name.encode().ljust(16)
        + str(int(time.time())).encode().ljust(12)
        + b"0".ljust(6)          # uid
        + b"0".ljust(6)          # gid
        + b"100644".ljust(8)     # mode
        + str(len(data)).encode().ljust(10)
        + b"`\n"
    )
    pad = b"\n" if len(data) % 2 else b""
    return header + data + pad


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: mkdeb.py <pkg-root> <output.deb>", file=sys.stderr)
        return 2
    root, out_path = sys.argv[1], sys.argv[2]
    debian = os.path.join(root, "DEBIAN")
    if not os.path.isdir(debian):
        print(f"error: {debian} not found", file=sys.stderr)
        return 1

    mtime = int(os.environ.get("SOURCE_DATE_EPOCH") or time.time())

    # control.tar.gz：DEBIAN 里的全部内容，路径是 ./control、./postinst ...
    ctrl_entries = sorted(
        os.path.relpath(os.path.join(debian, n), debian) for n in os.listdir(debian)
    )
    control_tar = _tar_bytes(debian, ctrl_entries, mtime)

    # data.tar.gz：除 DEBIAN 以外的所有内容
    data_entries = _walk_files(root, "usr") if os.path.isdir(os.path.join(root, "usr")) else []
    if not data_entries:
        print("error: no usr/ payload", file=sys.stderr)
        return 1
    data_tar = _tar_bytes(root, data_entries, mtime)

    blob = AR_MAGIC
    blob += _ar_member("debian-binary", b"2.0\n")
    blob += _ar_member("control.tar.gz", control_tar)
    blob += _ar_member("data.tar.gz", data_tar)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fp:
        fp.write(blob)
    print(f"built {out_path} ({len(blob)/1024:.1f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
