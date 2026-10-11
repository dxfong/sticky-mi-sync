# -*- coding: utf-8 -*-
"""对照实验：同一时刻、同一容器里，outlook 通道 vs fabric 通道各拉 3 次。

目的：判断 fabric 拉不通到底是
  · 这条链路对**所有**大响应都不行（那 outlook 也该失败）
  · 还是**只有 substrate.office.com 这个域名**不行

只读。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/app")
from notesync.store import Store  # noqa: E402

store = Store("/app/data")


def trial(label, fn, n=3):
    print(f"--- {label} ---")
    okn = 0
    for i in range(1, n + 1):
        t0 = time.time()
        try:
            r = fn()
            dt = time.time() - t0
            print(f"  {i}) ✅ {len(r)} 条  {dt:.1f}s")
            okn += 1
        except Exception as e:
            dt = time.time() - t0
            print(f"  {i}) ❌ {type(e).__name__}: {str(e)[:90]}  {dt:.1f}s")
        time.sleep(1)
    print(f"  → {okn}/{n} 成功")
    print()
    return okn


from notesync.graph import RealGraph, NotesFabricGraph  # noqa: E402

rg = RealGraph(store)
try:
    print("RealGraph 通道 =", rg.status().get("channel"))
except Exception as e:
    print("RealGraph status 失败:", str(e)[:120])
print()

a = trial("outlook 通道（RealGraph）", lambda: rg.list_notes())

fg = NotesFabricGraph(store)
b = trial("fabric 通道（NotesFabric）", lambda: fg.list_notes())

print("=" * 60)
print(f"outlook {a}/3   fabric {b}/3")
if a > b:
    print("→ 结论：outlook 可用，fabric 当前拉不通")
elif a == b == 0:
    print("→ 结论：两条通道现在都拉不通（链路整体抖动）")
else:
    print("→ 结论：两条通道都能用")
