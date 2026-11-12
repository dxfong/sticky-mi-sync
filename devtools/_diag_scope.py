# -*- coding: utf-8 -*-
"""确认"同步范围"：用户改标题的那条到底在哪个文件夹、为什么不在同步里。只读。"""
from __future__ import annotations

import collections
import json
import sys

sys.path.insert(0, "/app")
from notesync.store import Store        # noqa: E402
from notesync.xiaomi import RealXiaomi  # noqa: E402

NID = "51115357469950656"

st = Store("/app/data")
x = RealXiaomi(st)

cfg = st.cfg["xiaomi"]
print("=" * 78)
print("① 同步配置的文件夹")
print("=" * 78)
print("  folder_id   =", repr(cfg.get("folder_id")))
print("  folder_name =", repr(cfg.get("folder_name")))

print()
print("=" * 78)
print("② 原始列表里各文件夹的条数")
print("=" * 78)
raw = x._get("/note/full/page", {"limit": 300, "ts": 0, "syncData": "true"})
entries = ((raw or {}).get("data") or {}).get("entries") or []
cnt = collections.Counter((e.get("folderId") or "0") for e in entries)
for fid, c in cnt.most_common():
    print(f"  folderId={fid:<22} {c} 条")

print()
print("=" * 78)
print("③ 那条笔记（用户改过标题的）")
print("=" * 78)
for e in entries:
    if str(e.get("id")) == NID:
        print("  folderId =", repr(e.get("folderId")))
        print("  subject  =", repr(e.get("subject")))
        print("  snippet  =", repr((e.get("snippet") or "")[:70]))
        print("  在同步文件夹里吗 →",
              "是" if str(e.get("folderId")) == str(cfg.get("folder_id")) else "**否**")
        break

print()
print("=" * 78)
print("④ 同步实际覆盖的条数 vs 全量")
print("=" * 78)
print("  list_notes()（只含同步文件夹）:", len(x.list_notes()))
print("  原始列表全量              :", len(entries))
print()
print("  明细接口的 payload 位置：")
d = x._get(f"/note/note/{NID}")
print("   顶层键:", list((d or {}).keys()))
ent = (d or {}).get("data", {})
if isinstance(ent, dict) and "entry" in ent:
    ent = ent["entry"]
print("   条目键:", sorted(ent.keys())[:12] if isinstance(ent, dict) else type(ent))
if isinstance(ent, dict):
    print("   subject =", repr(ent.get("subject")))
    print("   snippet =", repr((ent.get("snippet") or "")[:60]))
    print("   content 前 160 字:")
    print("     ", (ent.get("content") or "")[:160].replace("\n", "\\n"))
