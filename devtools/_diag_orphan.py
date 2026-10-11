# -*- coding: utf-8 -*-
"""找出"小米多出来的那一条"到底是什么。

为什么不能读 `notes_cache` 表：那张表**只在 `fetch_only` 里整表覆盖**，
`run_once` 走的是 `last_notes`（内存）+ 逐条 `cache_upsert`。
被跳过的条目永远不会 upsert 进去 —— 所以缓存里看起来"全都配对好了"，
实测就踩了这个坑（缓存 164 / 远端 165）。

所以这里**直接调 provider 的 list_notes()** 拿实时列表。
只读（GET），不写任何东西。
"""
from __future__ import annotations

import datetime
import json
import sys

sys.path.insert(0, "/app")
from notesync.store import Store           # noqa: E402
from notesync.xiaomi import RealXiaomi     # noqa: E402


def ts(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return str(v)
    if n > 10 ** 11:
        n //= 1000
    return datetime.datetime.fromtimestamp(n).strftime("%m-%d %H:%M:%S")


store = Store("/app/data")
x = RealXiaomi(store)

print("=" * 78)
print("实时拉小米列表（GET，只读）")
print("=" * 78)
notes = x.list_notes()
print(f"  远端小米笔记 = {len(notes)} 条")

links = {str(l["mi_id"]): l for l in store.all_links()}
print(f"  link 表      = {len(links)} 条")
print()

orphans = [n for n in notes if str(n.get("id")) not in links]
print(f"  **未关联** {len(orphans)} 条")
print()
for n in orphans:
    mid = str(n.get("id"))
    mt = int(n.get("modify_date") or 0)
    print("  ─────────────────────────────────────────────")
    print(f"  mi_id       = {mid}")
    print(f"  修改时间    = {ts(mt)}")
    print(f"  subject     = {n.get('subject')!r}")
    print(f"  folder_id   = {n.get('folder_id')!r}")
    print(f"  正文字数    = {len(n.get('text') or '')}")
    print(f"  正文前 80   = {(n.get('text') or '')[:80]!r}")
    print()

# ---- 反向：link 指向的 mi_id 有没有已经不在远端的 ----
remote_ids = {str(n.get("id")) for n in notes}
missing = [mid for mid in links if mid not in remote_ids]
print(f"  link 指向但远端已没有的 mi_id = {len(missing)} 条")
for mid in missing[:10]:
    l = links[mid]
    print(f"    mi={mid}  g={str(l.get('graph_id'))[:28]}...  "
          f"last_synced={ts(l.get('last_synced_at'))}")

# ---- 内容重复检查：orphan 的正文在已配对的笔记里有没有一样的 ----
print()
print("=" * 78)
print("内容重复检查（orphan 的正文 vs 已配对的小米笔记）")
print("=" * 78)


def norm(t):
    return (t or "").replace("\r\n", "\n").replace("\r", "\n").strip()


for n in orphans:
    txt = norm(n.get("text"))
    same = [m for m in notes
            if str(m.get("id")) != str(n.get("id")) and norm(m.get("text")) == txt]
    print(f"  orphan mi={str(n.get('id'))[:12]} → 正文相同的小米笔记 {len(same)} 条")
    for m in same:
        mid = str(m.get("id"))
        print(f"    · mi={mid[:12]} time={ts(m.get('modify_date'))} "
              f"已配对={'是' if mid in links else '否'}")
