# -*- coding: utf-8 -*-
"""把本地脚本送进容器并在容器里执行。

用法（Windows 路径，避免 Git Bash 的 MSYS 路径转换）：
    python devtools/_push_run.py <本地文件> <容器内路径> [--run] [传入参数...]

Git Bash 的坑：以 `/` 开头的参数会被 MSYS 转成 Windows 路径
（实测 `/tmp/x.py` → `C:/Users/.../Temp/x.py`，于是 docker cp 报
"Could not find the file C:/Users/..."）。所以：
  · 远端路径写成**容器内绝对路径**，并且调用时设 MSYS_NO_PATHCONV=1
  · 或者干脆把远端路径写死在这个脚本的常量里，不经过命令行
"""
from __future__ import annotations

import base64
import pathlib
import sys

sys.path.insert(0, r"D:/Workbuddy/2026-09-05-02-43-12")
import ssh_run  # noqa: E402

CONTAINER = "sticky-mi-sync"


def push(local: pathlib.Path, remote: str) -> None:
    b64 = base64.b64encode(local.read_bytes()).decode()
    cmd = (
        "echo '" + b64 + "' | base64 -d > /root/_push.py && "
        f"docker cp /root/_push.py {CONTAINER}:{remote} && "
        "rm -f /root/_push.py && echo PUSHED"
    )
    rc, out = ssh_run.run(cmd)
    print(out.strip())
    if rc != 0:
        sys.exit(rc)


def run_in_container(remote: str, args: str = "") -> int:
    rc, out = ssh_run.run(
        f"docker exec {CONTAINER} python {remote} {args}")
    return rc


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    local = pathlib.Path(a[0])
    remote = a[1] if len(a) > 1 else "/tmp/" + local.name
    push(local, remote)
    if "--run" in a:
        rest = " ".join(x for x in a[2:] if x != "--run")
        sys.exit(run_in_container(remote, rest))
