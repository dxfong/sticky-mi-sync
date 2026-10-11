# -*- coding: utf-8 -*-
"""切到 fabric 通道**之前**的安全检查：比对两侧 id 集合。

为什么必须先做这一步
--------------------
`link.graph_id` 存的是**当前通道**（outlook）的便笺 id。
如果 fabric 用的 id 体系不一样，切过去之后：
  · `link` 里 164 条全部"对不上"
  · 引擎会认为**便笺侧全被删了** → 走"删除传播"逻辑
  · 虽然大规模删除安全阀会拦下整轮（阈值 max(5, 映射×20%)），
    但那是"拦下"，不是"正确"—— 而且会一直拦，同步彻底停摆。

所以：**先只读拉一次 fabric 列表，把 id 集合和 link 表对一遍**，
确认一致率再决定要不要真的切。

只用 GET，不写任何东西。
"""
from __future__ import annotations

import json
import sqlite3
import sys

sys.path.insert(0, "/app")

DB = "/app/data/state.db"


def main() -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    link_ids = {str(r["graph_id"]) for r in
                c.execute("SELECT graph_id FROM link WHERE graph_id IS NOT NULL")}
    print("=" * 78)
    print(f"link 表里的便笺 id 数 = {len(link_ids)}")

    # 当前通道（只读探测）
    try:
        from notesync.store import Store
        from notesync.graph import RealGraph
        store = Store("/app/data")
        rg = RealGraph(store)
        st = rg.status()
        print(f"当前通道 = {st.get('channel')!r}  （{st.get('channel_label')}）")
    except Exception as e:
        print("探测当前通道失败：", type(e).__name__, str(e)[:200])
        store = None

    # fabric 通道
    print()
    print("-" * 78)
    print("尝试拉 fabric 列表（只读）")
    print("-" * 78)
    try:
        from notesync.graph import NotesFabricGraph
        if store is None:
            from notesync.store import Store
            store = Store("/app/data")
        fg = NotesFabricGraph(store)
        fs = fg.status()
        print(f"  fabric connected = {fs.get('connected')}")
        print(f"  fabric account   = {fs.get('account')!r}")
        if fs.get("error"):
            print(f"  fabric error     = {fs.get('error')}")
        if not fs.get("connected"):
            print()
            print("  ✖ fabric 还没登录（cred 表里没有 fabric_auth）。")
            print("    → 先在界面「账号与登录」里把 MSAuth1.0 usertoken 粘进来，再跑本脚本。")
            return 2
        # ★ 自带重试：容器 → substrate 这条链路实测抖动剧烈
        #   （IncompleteRead(15878/109000)、TimeoutError 反复出现）。
        #   引擎的 `_pull_list` 有重试，但我这里直接调 provider 就没有 ——
        #   所以本脚本自己重试，否则一次抖动就白跑。
        notes = None
        last = None
        for attempt in range(1, 6):
            try:
                notes = fg.list_notes()
                break
            except Exception as e:
                last = e
                print(f"  第 {attempt} 次失败：{type(e).__name__}: {str(e)[:120]}")
                if attempt < 5:
                    import time as _t
                    _t.sleep(3)
        if notes is None:
            print(f"  ✖ 重试 5 次仍失败：{type(last).__name__}: {str(last)[:200]}")
            return 3
    except Exception as e:
        print(f"  ✖ 拉取失败：{type(e).__name__}: {str(e)[:300]}")
        return 3

    fab_ids = {str(n.get("id")) for n in notes}
    print(f"  fabric 便笺数 = {len(fab_ids)}")

    both = link_ids & fab_ids
    only_link = link_ids - fab_ids
    only_fab = fab_ids - link_ids

    print()
    print("=" * 78)
    print("id 集合比对")
    print("=" * 78)
    print(f"  两边都有（可继续配对）  = {len(both)}")
    print(f"  只在 link（fabric 没有） = {len(only_link)}")
    print(f"  只在 fabric（未配对）    = {len(only_fab)}")

    if link_ids:
        rate = len(both) / len(link_ids) * 100
        print()
        print(f"  一致率 = {rate:.1f}%")
        if rate >= 95:
            print("  ✅ id 体系一致 —— 可以安全切到 fabric")
        elif rate >= 50:
            print("  ⚠ 部分一致 —— 切换前请人工确认，可能造成大量重建")
        else:
            print("  ❌ 基本不一致 —— **不要切**，会引发大规模重建/删除传播")

    if only_link:
        print()
        print("  只在 link 的前 5 条：")
        for i in list(only_link)[:5]:
            print("    -", i[:56])
    if only_fab:
        print()
        print("  只在 fabric 的前 5 条：")
        for i in list(only_fab)[:5]:
            print("    +", i[:56])
    return 0


if __name__ == "__main__":
    sys.exit(main())
