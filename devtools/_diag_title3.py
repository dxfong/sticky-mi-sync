# -*- coding: utf-8 -*-
"""拆开看这条笔记：标题到底存在哪、有没有参与同步。只读。"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")
from notesync.store import Store                  # noqa: E402
from notesync.xiaomi import RealXiaomi            # noqa: E402
from notesync.textutil import mi_content_to_text  # noqa: E402

NID = "51115357469950656"

st = Store("/app/data")
x = RealXiaomi(st)

links = {str(l["mi_id"]): l for l in st.all_links()}
print("=" * 80)
print("① 这条有没有参与同步")
print("=" * 80)
lk = links.get(NID)
print("  在 link 表里:", "是 → g=" + str(lk.get("graph_id"))[:30] if lk else "**否**")
print("  当前映射总数:", len(links))

print()
print("=" * 80)
print("② 详情接口返回（标题存在哪）")
print("=" * 80)
d = x._get(f"/note/note/{NID}")
data = (d or {}).get("data") or {}
print("  字段:", sorted(data.keys()))
for k in ("subject", "snippet", "content", "extraInfo", "modifyDate", "createDate"):
    v = data.get(k)
    if isinstance(v, str) and len(v) > 300:
        v = v[:300] + " …"
    print(f"  {k:<12} = {v!r}")

print()
print("=" * 80)
print("③ 正文 XML 的开头（标题是不是嵌在 content 里）")
print("=" * 80)
c = data.get("content") or ""
print("  原始 content 前 400 字:")
print("   ", c[:400].replace("\n", "\\n"))
print()
print("  经 mi_content_to_text 转换后前 120 字:")
print("   ", repro := mi_content_to_text(c)[:120])

print()
print("=" * 80)
print("④ list_notes() 对这条给出的 subject / 正文首行")
print("=" * 80)
for n in x.list_notes():
    if str(n.get("id")) == NID:
        print("  subject  =", repr((n.get("subject") or "")[:80]))
        print("  正文首行 =", repr((n.get("text") or "")[:80]))
        break
else:
    print("  list_notes() 里没有这条（可能被文件夹过滤掉了）")
