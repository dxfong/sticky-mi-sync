# -*- coding: utf-8 -*-
"""查清这次的重复（mi=52027832951120288「车牌 317 kumari petorl pump」）。

要回答：
  ① 重复的那条是什么时候产生的（modify_date）
  ② 与它同内容的另一条（已配对）是什么时候的
  ③ 便笺侧对应那条的时间
  ④ 对照历史备份（10-09 / 今日 16:06）里这条**在不在** —— 定位产生时间窗
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

DUP = "52027832951120288"


def tsms(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return str(v)
    if n > 10 ** 11:
        n //= 1000
    return datetime.datetime.fromtimestamp(n).strftime("%m-%d %H:%M:%S")


def norm(t):
    return (t or "").replace("\r\n", "\n").replace("\r", "\n").strip()


store = Store("/app/data")
x = RealXiaomi(store)
notes = x.list_notes()
links = {str(l["mi_id"]): l for l in store.all_links()}

print("=" * 78)
print("① 小米侧同内容的所有条目")
print("=" * 78)
tgt = None
for n in notes:
    if str(n.get("id")) == DUP:
        tgt = n
if tgt is None:
    print("  远端已没有", DUP)
    sys.exit(0)
txt = norm(tgt.get("text"))
print(f"  目标 mi={DUP}  time={tsms(tgt.get('modify_date'))}  "
      f"subject={tgt.get('subject')!r}")
print()
same = [n for n in notes if norm(n.get("text")) == txt]
print(f"  正文完全相同的小米笔记共 {len(same)} 条：")
for n in sorted(same, key=lambda z: int(z.get("modify_date") or 0)):
    mid = str(n.get("id"))
    lk = links.get(mid)
    print(f"    mi={mid}  time={tsms(n.get('modify_date'))}  "
          f"{'已配对 → g=' + str(lk.get('graph_id'))[:30] + '…' if lk else '**未配对**'}")

print()
print("=" * 78)
print("② 便笺侧同内容条目")
print("=" * 78)
try:
    from notesync.graph import RealGraph
    g = RealGraph(store)
    gn = [n for n in g.list_notes() if not n.get("deleted")]
    gsame = [n for n in gn if norm(n.get("text")) == txt]
    print(f"  便笺共 {len(gn)} 条，其中正文相同 {len(gsame)} 条：")
    for n in gsame:
        print(f"    g={str(n.get('id'))[:34]}…  modified={n.get('modified_iso')}")
except Exception as e:
    print("  拉便笺失败:", type(e).__name__, str(e)[:120])

print()
print("=" * 78)
print("③ 链接表里与这些 id 相关的记录")
print("=" * 78)
for n in same:
    mid = str(n.get("id"))
    lk = links.get(mid)
    if lk:
        print(f"  mi={mid}")
        for k in ("graph_id", "mi_modifydate", "last_synced_at",
                  "base_hash", "graph_time_synthetic", "tombstone"):
            v = lk.get(k)
            if k in ("mi_modifydate", "last_synced_at") and v:
                v = f"{v} ({tsms(v)})" if k == "last_synced_at" else tsms(v)
            print(f"     {k:<22} {v}")

print()
print("=" * 78)
print("④ 历史备份里有没有这个 id")
print("=" * 78)
for path in ("/tmp/old0909.db", "/tmp/old1606.db"):
    try:
        o = sqlite3.connect(path)
        o.row_factory = sqlite3.Row
        hit = o.execute("SELECT count(*) FROM notes_cache WHERE side='xiaomi' "
                        "AND payload LIKE ?", ("%" + DUP + "%",)).fetchone()[0]
        n_x = o.execute("SELECT count(*) FROM notes_cache WHERE side='xiaomi'"
                        ).fetchone()[0]
        print(f"  {path}: 小米缓存 {n_x} 条，含目标 id 的行数 = {hit}")
        o.close()
    except Exception as e:
        print(f"  {path}: 打不开（{type(e).__name__}: {str(e)[:60]}）")
