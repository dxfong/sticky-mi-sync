# -*- coding: utf-8 -*-
"""诊断：本来已配对一致的小米笔记，为什么突然变成「未关联」并被跳过。

要回答的问题：
  1. pairing_since 是什么时候？
  2. 哪些小米笔记**不在 link 表**里（= 引擎眼里的"未关联"）？
  3. 这些"未关联"的笔记，在便笺侧有没有**内容完全相同**的对应条目？
     - 有 → 说明它**本来配对过**，是 link 丢了（这才是用户遇到的情况）
     - 没有 → 说明它确实从来没同步过（那就是设计行为，正常）
  4. link 表里有没有"半条"记录（mi_id 有、graph_id 空，或反之）？
  5. 回收站里有没有相关线索？

只读，不改任何东西。
"""
from __future__ import annotations

import datetime
import json
import sqlite3

DB = "/app/data/state.db"
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row


def ts(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return str(v)
    # 毫秒 vs 秒
    if n > 10 ** 11:
        n //= 1000
    return datetime.datetime.fromtimestamp(n).strftime("%m-%d %H:%M:%S")


def meta(k, dft=""):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else dft


print("=" * 78)
print("① 首配对保护")
print("=" * 78)
ps = meta("pairing_since")
print(f"  pairing_since = {ps or '(空)'}  →  {ts(ps) if ps else '-'}")
print(f"  initial_pairing 策略 = {meta('initial_pairing', '(默认 graph_authoritative)')}")

print()
print("=" * 78)
print("② 数据规模")
print("=" * 78)
n_link = c.execute("SELECT count(*) FROM link").fetchone()[0]
n_trash = c.execute("SELECT count(*) FROM trash").fetchone()[0]
caches = {r["side"]: r["n"] for r in c.execute(
    "SELECT side, count(*) n FROM notes_cache GROUP BY side")}
print(f"  link 表  = {n_link}")
print(f"  trash 表 = {n_trash}")
print(f"  notes_cache = {caches}")

print()
print("=" * 78)
print("③ link 表里的「半条」记录（缺一侧 id）")
print("=" * 78)
rows = c.execute(
    "SELECT mi_id, graph_id, tombstone FROM link "
    "WHERE mi_id IS NULL OR graph_id IS NULL "
    "   OR trim(mi_id)='' OR trim(graph_id)=''").fetchall()
if not rows:
    print("  无 —— link 表里每行两侧 id 都齐全 ✅")
for r in rows:
    print(f"  mi={r['mi_id']!r} g={r['graph_id']!r} tomb={r['tombstone']!r}")

print()
print("=" * 78)
print("④ 未关联的小米笔记 —— 便笺侧有没有内容相同的对应条目？")
print("=" * 78)
linked_mi = {str(r["mi_id"]) for r in c.execute("SELECT mi_id FROM link")}
# 小米侧缓存
m_notes = []
for r in c.execute("SELECT payload FROM notes_cache WHERE side='xiaomi'"):
    try:
        m_notes.append(json.loads(r["payload"]))
    except json.JSONDecodeError:
        pass
g_notes = []
for r in c.execute("SELECT payload FROM notes_cache WHERE side='graph'"):
    try:
        g_notes.append(json.loads(r["payload"]))
    except json.JSONDecodeError:
        pass

# 便笺侧按正文建索引（同正文 = 同一条内容的两个副本）
def norm(t):
    return (t or "").replace("\r\n", "\n").replace("\r", "\n").strip()

g_by_text: dict[str, list] = {}
for n in g_notes:
    g_by_text.setdefault(norm(n.get("text")), []).append(n)

orphans = [n for n in m_notes if str(n.get("id")) not in linked_mi]
print(f"  小米缓存 {len(m_notes)} 条，其中**未关联** {len(orphans)} 条")
print()
for n in orphans:
    mid = str(n.get("id"))
    mt = int(n.get("modify_date") or 0)
    dup = g_by_text.get(norm(n.get("text")), [])
    flag = "★ 便笺侧有同内容条目" if dup else "（便笺侧无同内容条目）"
    print(f"  ── mi={mid}")
    print(f"     小米时间   : {ts(mt)}")
    print(f"     是否配对前 : {'是（早于 pairing_since）' if ps and mt < int(ps) else '否'}")
    print(f"     标题       : {(n.get('subject') or n.get('text') or '')[:60]!r}")
    print(f"     便笺侧对应 : {flag}"
          + (f" ×{len(dup)}" if dup else ""))
    for d in dup[:2]:
        ht = str(d.get("modified_iso") or "")
        print(f"        └ g={str(d.get('id'))[:24]}...  time={ht}")
    print()

print("=" * 78)
print("⑤ 未关联的便笺（反向核对）")
print("=" * 78)
linked_g = {str(r["graph_id"]) for r in c.execute(
    "SELECT graph_id FROM link WHERE graph_id IS NOT NULL")}
g_orphans = [n for n in g_notes if str(n.get("id")) not in linked_g]
print(f"  便笺缓存 {len(g_notes)} 条，其中**未关联** {len(g_orphans)} 条")
for n in g_orphans[:10]:
    print(f"    g={str(n.get('id'))[:24]}... time={n.get('modified_iso')} "
          f"{(n.get('text') or '')[:40]!r}")

print()
print("=" * 78)
print("⑥ 回收站里最近 10 条（找「是不是被删过又回来」的痕迹）")
print("=" * 78)
try:
    for r in c.execute(
            "SELECT * FROM trash ORDER BY deleted_at DESC LIMIT 10"):
        d = dict(r)
        print("  " + json.dumps(
            {k: (str(v)[:50] if v is not None else None)
             for k, v in d.items() if k in (
                 "id", "side", "note_id", "deleted_at", "title", "reason")},
            ensure_ascii=False))
except Exception as e:
    print("  读取失败:", e)
