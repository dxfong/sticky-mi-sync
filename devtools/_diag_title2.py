# -*- coding: utf-8 -*-
"""看小米列表接口**原始返回**里，用户改的标题到底在哪。

已知：`xiaomi.list_notes()` 里 subject == 正文首行（165/165 全一致），
而用户手机上把标题改成了 `f8727aaeb2bbaff2`，正文没动。
→ 说明 `subject` 这个字段在**列表接口**里是"从正文算出来的"，不是用户设的标题。
本脚本把原始 JSON 打出来，找那个标题藏在哪个字段。
只读。
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")
from notesync.store import Store        # noqa: E402
from notesync.xiaomi import RealXiaomi  # noqa: E402
from notesync.textutil import first_line  # noqa: E402

st = Store("/app/data")
x = RealXiaomi(st)

raw = x._list_raw() if hasattr(x, "_list_raw") else None
if raw is None:
    # 没有现成方法就直接发一次（复用已有的鉴权 + 直连 opener）
    raw = x._get("/note/full/page", {"limit": 200, "ts": 0, "syncData": "true"})

entries = ((raw or {}).get("data") or {}).get("entries") or []
print("列表条数:", len(entries))
print("单条 JSON 的字段名:", sorted(entries[0].keys()) if entries else "(空)")

WANT = "f8727aaeb2bbaff2"
print()
print("=" * 80)
print(f"找标题里含 {WANT!r} 的条目")
print("=" * 80)
hit = None
for e in entries:
    blob = json.dumps(e, ensure_ascii=False)
    if WANT in blob:
        hit = e
        break
if hit is None:
    print("  ❌ 列表里**没有任何字段**含这个标题")
    print("     → 说明这个标题还没进列表接口（可能只在详情接口里），")
    print("       或者用户改的是另一个 App 的显示名，不是笔记标题。")
    # 退一步：找正文含那段 ERROR 的条目，看它的 subject
    print()
    print("  退一步：找正文以 '+0800 2026-11-12 21:00:26' 开头的条目")
    for e in entries:
        c = e.get("content") or ""
        if "+0800 2026-11-12 21:00:26" in c[:120]:
            hit = e
            print("  找到了：")
            for k in sorted(e.keys()):
                v = e[k]
                s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                print(f"    {k:<18} = {s[:90]!r}")
            print(f"    正文首行            = {first_line(c)[:70]!r}")
            break
else:
    print("  ✅ 找到了，原始字段如下：")
    for k in sorted(hit.keys()):
        v = hit[k]
        s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        print(f"    {k:<18} = {s[:100]!r}")
