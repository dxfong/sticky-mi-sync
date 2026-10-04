#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米账号登录（一次性交互，之后自动续期）。

用法
----
    python -m notesync.mi_login            # 交互登录（推荐，第一次用）
    python -m notesync.mi_login --check    # 只检查环境与凭据状态，不登录

它做三件事：
  1. 用 migate 走小米官方登录（浏览器 / 终端账号密码 / 扫码，三选一）
  2. 拿到 i.mi.com 服务所需的 cookie（serviceToken 等）
  3. 把 **cookie 和 passToken 都写进我们的状态库**

为什么要把 passToken 也存下来
------------------------------
`serviceToken` 是会过期的短效令牌，而 `passToken` 是长效的。
把 passToken 存进我们自己的库之后，`serviceToken` 一旦过期，
后端可以**直接用 passToken 换新的，不需要再输密码、不需要再开浏览器**。
这就是"一次登录、长期无人值守"的实现方式。

Docker 里的用法
---------------
容器里没有浏览器，所以：
    docker exec -it <容器> python -m notesync.mi_login
选 2（终端）输账号密码，或选 3（扫码）用小米手机扫。
凭据写进 /app/data/state.db，所以**只要 data 目录挂了卷，重建容器也不用重新登录**。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notesync.store import Store  # noqa: E402


def check(store: Store) -> int:
    print("=" * 68)
    print("环境与凭据检查")
    print("=" * 68)

    try:
        import migate  # noqa: F401
        print("  migate                : 已安装（自动登录可用）")
    except ImportError:
        print("  migate                : **未安装**")
        print("     装法：pip install migate")
        print("     没装也能用 —— 退回到手工粘贴 Cookie（页面上那个输入框）")

    cookie = store.get_cred("xiaomi_cookie", {}) or {}
    print(f"  serviceToken          : {'有' if cookie.get('serviceToken') else '无'}")
    print(f"  userId                : {cookie.get('userId') or '无'}")
    pt = store.get_cred("xiaomi_pass_token")
    print(f"  passToken（用于续期） : {'有' if pt else '无'}")
    if not pt:
        print("     -> 没有 passToken，serviceToken 过期后需要重新登录一次")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="小米账号登录（一次性）")
    ap.add_argument("--check", action="store_true", help="只检查状态，不登录")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--verify", action="store_true",
                    help="登录后立刻用新凭据打一次列表接口验证")
    args = ap.parse_args()

    store = Store(Path(args.data_dir))
    if args.check:
        return check(store)

    try:
        import migate
    except ImportError:
        print("[x] 没装 migate。先执行：pip install migate", file=sys.stderr)
        print("    （或者直接用网页上的「验证并保存」手工粘贴 Cookie）", file=sys.stderr)
        return 2

    print("=" * 68)
    print("小米账号登录")
    print("=" * 68)
    print("接下来 migate 会让你选登录方式：")
    print("  1 - 浏览器（本机有浏览器时最方便）")
    print("  2 - 终端（直接输账号密码，Docker 里选这个）")
    print("  3 - 扫码（用小米手机扫，不用输密码）")
    print()

    # sid 必须是 i.mi.com —— 它决定拿到的 serviceToken 能访问笔记接口
    params = {"sid": "i.mi.com"}
    pass_token = migate.get_passtoken(params)        # 交互在这里发生
    if not pass_token:
        print("\n[x] 登录失败（没有拿到 passToken）", file=sys.stderr)
        return 3

    svc = None
    try:
        from . import mi_auth
        svc = mi_auth.acquire_service(pass_token, "i.mi.com")
    except Exception as e:
        print(f"\n[!] 换服务凭据失败：{e}", file=sys.stderr)
        print("    登录态已保存到 migate 会话文件，可以再跑一次本命令重试。", file=sys.stderr)
    if not svc:
        return 4
    cookies = (svc or {}).get("cookies") or {}
    if not cookies.get("serviceToken") or not cookies.get("userId"):
        print(f"\n[x] 登录返回的 cookie 不完整：{json.dumps(cookies, ensure_ascii=False)[:300]}",
              file=sys.stderr)
        return 4

    # serviceToken 之外，把 cUserId / slh / ph / deviceId 也一并存下来 ——
    # 有些接口会校验这些辅助字段，缺了会 401。
    merged = dict(cookies)
    for k in ("cUserId", "slh", "ph", "deviceId", "uLocale"):
        if k in pass_token and k not in merged:
            merged[k] = pass_token[k]

    store.set_cred("xiaomi_cookie", merged)
    store.set_cred("xiaomi_pass_token", pass_token)   # 续期用
    store.log(f"小米账号已登录（userId={merged.get('userId')}）")

    print()
    print("=" * 68)
    print("登录成功")
    print("=" * 68)
    print(f"  userId      : {merged.get('userId')}")
    print(f"  已写入      : {Path(args.data_dir) / 'state.db'}")
    print("  serviceToken: 短期有效，过期后后端会自动用 passToken 换新的")
    print("  passToken   : 已保存 —— 只要它不过期就不需要你再登一次")

    if args.verify:
        from notesync.xiaomi import RealXiaomi
        x = RealXiaomi(store)
        try:
            r = x.verify_cookie(merged)
            print(f"  验证        : 通过（{r}）")
        except Exception as e:
            print(f"  验证        : 失败 —— {e}", file=sys.stderr)
            return 5
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
