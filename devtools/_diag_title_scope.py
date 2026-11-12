# -*- coding: utf-8 -*-
"""统计：有多少条有自定义标题（extraInfo.title），便笺侧 Subject 是否全空。

这决定"标题纳入同步"的迁移代价：如果只有少数几条，首轮可以按小米为准写一次；
如果是大面积，就必须只建基线、不写。
只读。
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")
from notesync.store import Store                            # noqa: E402
from notesync.xiaomi import RealXiaomi                      # noqa: E402
from notesync.graph import RealGraph, OUTLOOK, _request     # noqa: E402

st = Store("/app/data")
x, g = RealXiaomi(st), RealGraph(st)

print("=" * 80)
print("① 小米侧：extraInfo 里带 title 的条数（全量 vs 同步范围）")
print("=" * 80)
raw = x._get("/note/full/page", {"limit": 300, "ts": 0, "syncData": "true"})
entries = ((raw or {}).get("data") or {}).get("entries") or []
folder_id = str(st.cfg["xiaomi"].get("folder_id") or "")

def title_of(e):
    ei = e.get("extraInfo") or ""
    try:
        d = json.loads(ei) if ei else {}
    except Exception:
        return ""
    return (d.get("title") or "").strip()

tot = sum(1 for e in entries if title_of(e))
inscope = [e for e in entries if str(e.get("folderId") or "") == folder_id]
inscope_titled = [e for e in inscope if title_of(e)]
print(f"  全量 {len(entries)} 条，其中有自定义标题的 {tot} 条")
print(f"  同步范围 {len(inscope)} 条，其中有自定义标题的 {len(inscope_titled)} 条")
for e in inscope_titled[:10]:
    print(f"    mi={e.get('id')}  title={title_of(e)!r}  "
          f"subject={(e.get('subject') or '')[:20]!r}  "
          f"contentLen≈{len(e.get('content') or '')}")

print()
print("=" * 80)
print("② 便笺侧：Subject 是不是全空")
print("=" * 80)
tok = g.ensure_token()
nonempty = 0
seen = 0
url = (f"{OUTLOOK}/me/notes?$select=Id,Subject,Body,ChangeKey,LastModifiedDateTime"
       f"&$top=100")
while url and seen < 300:
    code, data = _request("GET", url, tok)
    if code != 200:
        print("  HTTP", code)
        break
    for it in data.get("value") or []:
        seen += 1
        if (it.get("Subject") or "").strip():
            nonempty += 1
            if nonempty <= 5:
                print(f"    非空 Subject: {(it.get('Subject') or '')[:60]!r}")
    url = data.get("@odata.nextLink", "")
print(f"  便笺共扫 {seen} 条，Subject 非空 {nonempty} 条")

print()
print("=" * 80)
print("③ 结论")
print("=" * 80)
if len(inscope_titled) == 0:
    print("  范围内没有任何自定义标题 → 只需支持后续同步")
elif len(inscope_titled) <= 5:
    print(f"  只有 {len(inscope_titled)} 条有自定义标题 → 首轮按小米为准写一次代价很小")
else:
    print(f"  有 {len(inscope_titled)} 条 → 不宜首轮批量写，必须先建基线")
