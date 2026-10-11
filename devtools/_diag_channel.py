# -*- coding: utf-8 -*-
"""让应用自己报告当前用的是哪条微软通道（只读探测）。

RealGraph 会在 `status()` 里跑一次 `_detect_channel`（GET，只读，30 秒节流），
返回 channel 取值之一：
    outlook  → Outlook REST（全部便笺 · 含 2026 · 可读写）
    notes    → Graph 官方便笺（ShortNotes）
    mail     → Graph 邮箱便笺（Mail.Read，只读，缺 2026）
"""
import collections
import sys

sys.path.insert(0, "/app")
from notesync.store import Store      # noqa: E402
from notesync.graph import RealGraph  # noqa: E402

store = Store("/app/data")
g = RealGraph(store)
st = g.status()
for k in ("channel", "channel_label", "account", "connected",
          "token_kind", "error"):
    print(f"  {k:<14} = {st.get(k)!r}")

print()
print("--- 实际拉一次列表（只读）---")
try:
    ns = g.list_notes()
    print("  列表条数 =", len(ns))
    ts = sorted((n.get("modified_iso") or "") for n in ns)
    print("  最早 =", ts[0] if ts else "-")
    print("  最新 =", ts[-1] if ts else "-")
    y = collections.Counter((n.get("modified_iso") or "")[:4] for n in ns)
    print("  按年 =", dict(sorted(y.items())))
except Exception as e:
    print("  失败:", type(e).__name__, str(e)[:400])
