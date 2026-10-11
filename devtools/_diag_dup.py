# -*- coding: utf-8 -*-
"""查清 mi_id=52024579892462016 这条（用户确认的异常记录）。

要回答：
  1. 它的**精确**时间字段（原始毫秒 + ISO），别被截断误导
  2. 同内容的那条（已配对）是谁、时间是多少
  3. **它有没有曾经在 link 表里** —— 对照 10-09 的备份 state.db（决定性证据）
  4. 便笺侧有没有对应内容
  5. 远端计数 165 vs link 164，到底差在哪

只读。
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys

sys.path.insert(0, "/app")
from notesync.store import Store           # noqa: E402
from notesync.xiaomi import RealXiaomi     # noqa: E402

TARGET = "52024579892462016"


def iso_ms(ms):
    try:
        n = int(ms) // 1000
        return datetime.datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ms)


def norm(t):
    return (t or "").replace("\r\n", "\n").replace("\r", "\n").strip()


store = Store("/app/data")
x = RealXiaomi(store)

print("=" * 78)
print("① 目标记录（用户指认）")
print("=" * 78)
notes = x.list_notes()
by_id = {str(n.get("id")): n for n in notes}
tgt = by_id.get(TARGET)
if not tgt:
    print("  **远端已不存在这条**")
else:
    print(f"  mi_id        = {TARGET}")
    print(f"  modify_date  = {tgt.get('modify_date')}  → {iso_ms(tgt.get('modify_date'))}（本地时区）")
    print(f"  subject      = {tgt.get('subject')!r}")
    print(f"  folder_id    = {tgt.get('folder_id')!r}")
    print(f"  正文字数     = {len(tgt.get('text') or '')}")

links = {str(l["mi_id"]): l for l in store.all_links()}
print(f"  在 link 表里？ {'是' if TARGET in links else '**否**'}")

print()
print("=" * 78)
print("② 同内容的那条（已配对的）")
print("=" * 78)
if tgt:
    txt = norm(tgt.get("text"))
    for m in notes:
        mid = str(m.get("id"))
        if mid != TARGET and norm(m.get("text")) == txt:
            lk = links.get(mid)
            print(f"  mi_id        = {mid}")
            print(f"  modify_date  = {m.get('modify_date')}  → {iso_ms(m.get('modify_date'))}")
            print(f"  subject      = {m.get('subject')!r}")
            print(f"  folder_id    = {m.get('folder_id')!r}")
            print(f"  在 link 表里？ {'是' if lk else '否'}")
            if lk:
                print(f"    graph_id      = {lk.get('graph_id')}")
                print(f"    last_synced_at= {iso_ms(int(lk.get('last_synced_at') or 0) * 1000)}")
                print(f"    base_hash     = {lk.get('base_hash')}")
            print()

print("=" * 78)
print("③ ★ 决定性证据：对照 10-09 的备份 state.db")
print("=" * 78)
try:
    old = sqlite3.connect("/tmp/old_state.db")
    old.row_factory = sqlite3.Row
    orow = old.execute("SELECT * FROM link WHERE mi_id=?", (TARGET,)).fetchone()
    if orow:
        print("  **备份里这条是配对的** —— 说明 link 是后来丢的！")
        print("  " + json.dumps(dict(orow), ensure_ascii=False, default=str)[:400])
    else:
        print("  备份里也没有这条 —— 说明它在 10-09 之前就没配对过")
    n_old = old.execute("SELECT count(*) FROM link").fetchone()[0]
    print(f"  备份 link 总数 = {n_old}")
    # 备份里 link 的 mi_id 里有没有"看起来像"这条的
    cand = old.execute(
        "SELECT mi_id, graph_id FROM link WHERE mi_id LIKE ?",
        ("5202457989%",)).fetchall()
    if cand:
        print(f"  mi_id 以 5202457989 开头的 link 记录: {len(cand)}")
        for r in cand:
            print("    " + str(dict(r)))
    else:
        print("  没有 mi_id 前缀 5202457989 的记录")
    old.close()
except Exception as e:
    print(f"  读取备份失败：{type(e).__name__}: {e}")

print()
print("=" * 78)
print("④ 便笺侧有没有同内容")
print("=" * 78)
try:
    from notesync.graph import NotesFabricGraph
    g = NotesFabricGraph(store)
    gn = [n for n in g.list_notes() if not n.get("deleted")]
    print(f"  远端便笺 = {len(gn)} 条")
    if tgt:
        txt = norm(tgt.get("text"))
        hit = [n for n in gn if norm(n.get("text")) == txt]
        print(f"  正文与目标相同的便笺 = {len(hit)} 条")
        for n in hit:
            print(f"    g={n.get('id')}  modified={n.get('modified_iso')}")
except Exception as e:
    print(f"  拉便笺失败：{type(e).__name__}: {e}")

print()
print("=" * 78)
print("⑤ 计数对账")
print("=" * 78)
print(f"  远端小米   = {len(notes)}")
print(f"  link 表    = {len(links)}")
orph = [n for n in notes if str(n.get("id")) not in links]
print(f"  未关联小米 = {len(orph)}  → " +
      ", ".join(str(n.get("id")) for n in orph))
remote_ids = {str(n.get("id")) for n in notes}
miss = [m for m in links if m not in remote_ids]
print(f"  link 指向但远端没有的小米 id = {len(miss)}")
