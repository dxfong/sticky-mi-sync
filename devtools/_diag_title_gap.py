# -*- coding: utf-8 -*-
"""只读统计：用"空白归一化"的判据，看两侧标题真正不一致的有几条。

判据（与引擎一致）：
  便笺显式标题 = Subject，除非
     · 空 / == 正文首行 / 正文（空白归一化后）以它开头 / 长度>120
  小米显式标题 = extraInfo.title
"""
from __future__ import annotations

import re
import sys

sys.path.insert(0, "/app")
from notesync.store import Store        # noqa: E402
from notesync.graph import RealGraph    # noqa: E402
from notesync.xiaomi import RealXiaomi  # noqa: E402
from notesync.textutil import first_line  # noqa: E402


def nw(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


st = Store("/app/data")
g, x = RealGraph(st), RealXiaomi(st)
gn = {n["id"]: n for n in g.list_notes()}
mn = {n["id"]: n for n in x.list_notes()}
links = st.all_links()

differ = []
for lk in links:
    mi, gid = str(lk.get("mi_id") or ""), str(lk.get("graph_id") or "")
    m, gg = mn.get(mi), gn.get(gid)
    if not m or not gg:
        continue
    sub = (gg.get("title") or "").strip()
    body = nw(gg.get("text") or "")
    fl = nw(first_line(gg.get("text") or ""))
    if (not sub) or sub == fl or body.startswith(nw(sub)) or len(sub) > 120:
        g_x = ""
    else:
        g_x = sub
    m_x = (m.get("title") or "").strip()
    if g_x != m_x:
        differ.append((mi, g_x, m_x, len(body)))

print(f"映射 {len(links)} 条，两侧标题**不一致**的 {len(differ)} 条：")
for mi, gx, mx, bl in differ:
    print(f"  mi={mi}  便笺={gx[:40]!r}  小米={mx[:40]!r}  正文{bl}字")
if not differ:
    print("  （没有）")
