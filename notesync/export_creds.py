#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机已登录的凭据导出成一段可粘贴的文本。

为什么需要这个工具
------------------
微软便笺的 NotesFabric 通道用的是 `MSAuth1.0 usertoken` —— 这个令牌由**便笺网页版
在页面里生成**，OAuth / 设备码都换不出来（设备码换到的是 Graph 令牌，那是另一条
通道，读不到 2026 年的便笺）。

所以在容器 / 服务器这种没有浏览器 profile 的新环境里，首次登录**只有一条路**：
在已经登录好的机器上把凭据导出来，粘过去。

用法
----
    python -m notesync.export_creds

它会打印两段文本：
  ① 微软便笺凭据（JSON）   → 粘到页面「导入并验证」
  ② 小米笔记 Cookie       → 粘到小米卡片的 Cookie 输入框

⚠ 输出里是**真实凭据**（能读你全部便笺）。只贴到自己的容器里，别外传、别贴群里。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .store import Store

BAR = "═" * 62


def main() -> int:
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
    if not data_dir.exists():
        print(f"找不到数据目录：{data_dir}")
        print("在项目根目录执行，或传一个目录：python -m notesync.export_creds /path/to/data")
        return 1

    store = Store(data_dir)

    fab = store.get_cred("fabric_auth", {}) or {}
    cookie = store.get_cred("xiaomi_cookie", {}) or {}
    pass_tok = store.get_cred("xiaomi_pass_token", {}) or {}

    print(BAR)
    print("sticky-mi-sync 凭据导出")
    print(BAR)
    print("⚠  以下内容含真实凭据（可读取你的全部便笺）。")
    print("   只粘到你自己的容器 / 服务器页面里，不要外传。")
    print()

    # ---------------- 微软
    print("【1/2】微软便笺 —— 粘到页面「导入并验证」框")
    print("-" * 62)
    if fab.get("authorization"):
        payload = {
            "authorization": fab["authorization"],
            "anchormailbox": fab.get("anchormailbox", ""),
            "sdkversion": fab.get("sdkversion", "StickyNotes-Web/11.5.10"),
        }
        print(json.dumps(payload, ensure_ascii=False))
        acct = str(fab.get("anchormailbox", "")).replace("MSA:", "")
        print(f"\n（账号：{acct}）")
    else:
        print("✗ 本机没有微软便笺凭据（fabric_auth 为空）。")
        print("  先在本机把便笺登录跑通，再导出。")
    print()

    # ---------------- 小米
    print("【2/2】小米笔记 —— 粘到小米卡片的 Cookie 输入框")
    print("-" * 62)
    if cookie:
        # 页面的 Cookie 输入框接受 `k=v; k=v` 形式
        keys = ("serviceToken", "passToken", "userId", "deviceId",
                "cUserId", "pass_ua", "uLocale")
        pairs = [f"{k}={cookie[k]}" for k in keys if k in cookie]
        print("; ".join(pairs))
        print()
        print(f"（小米账号 userId：{cookie.get('userId', '(未知)')}）")
        if pass_tok.get("passToken"):
            print("（已包含 passToken —— 装了 migate 的话能自动续期，不用重复扫码）")
    else:
        print("✗ 本机没有小米 Cookie。")
    print()

    # ---------------- 其他需要手动设置的
    print("【附】目标文件夹")
    print("-" * 62)
    cfg_path = data_dir / "config.json"
    fid = ""
    if cfg_path.exists():
        try:
            fid = (json.loads(cfg_path.read_text(encoding="utf-8"))
                   .get("xiaomi", {}).get("folder_id", ""))
        except Exception:
            pass
    print(f"文件夹 ID：{fid or '(未设置)'}")
    print("（新环境里在小米卡片的文件夹下拉里选一次即可，不必手动填）")
    print()
    print(BAR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
