# -*- coding: utf-8 -*-
"""把 warn/error 日志**归一化成"模式"**，看哪些是反复出现的、能不能规避。

归一化规则：把数字、长 id、十六进制、括号里的细节抹掉 —— 只留"形状"。
这样 200 条只差几个数字的消息会合并成 1 个模式。

每个模式给出：次数、天数、首次/最近、每日峰值、示例原文。
只读。
"""
from __future__ import annotations

import collections
import datetime
import re
import sqlite3

c = sqlite3.connect("/app/data/state.db")
c.row_factory = sqlite3.Row


def f(ts, fmt="%m-%d %H:%M"):
    return datetime.datetime.fromtimestamp(int(ts)).strftime(fmt)


def norm(m: str) -> str:
    s = m
    s = re.sub(r"0x[0-9a-fA-F]+", "<hex>", s)
    s = re.sub(r"\b[0-9a-f]{16,}\b", "<id>", s)
    s = re.sub(r"https?://\S+", "<url>", s)
    s = re.sub(r"[A-Za-z0-9_\-]{24,}", "<tok>", s)
    s = re.sub(r"\d+(\.\d+)?", "<n>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150]


rows = c.execute(
    "SELECT ts, level, msg, n FROM log WHERE level <> 'info' ORDER BY rowid"
).fetchall()

print("=" * 92)
print(f"warn/error 总计 {len(rows)} 行")
if rows:
    print(f"时间跨度 {f(rows[0]['ts'], '%Y-%m-%d %H:%M')} ~ "
          f"{f(rows[-1]['ts'], '%Y-%m-%d %H:%M')}")
print("=" * 92)

grp: dict[str, dict] = {}
for r in rows:
    k = norm(r["msg"])
    g = grp.setdefault(k, {"rows": 0, "times": 0, "days": set(),
                           "first": r["ts"], "last": r["ts"],
                           "sample": r["msg"], "levels": set()})
    g["rows"] += 1
    g["times"] += int(r["n"] or 1)
    g["days"].add(f(r["ts"], "%m-%d"))
    g["first"] = min(g["first"], r["ts"])
    g["last"] = max(g["last"], r["ts"])
    g["levels"].add(r["level"])
    if len(r["msg"]) > len(g["sample"]):
        g["sample"] = r["msg"]

print(f"归一化后 {len(grp)} 个模式，按出现次数排序：\n")
ordered = sorted(grp.items(), key=lambda kv: -kv[1]["times"])
for i, (k, g) in enumerate(ordered, 1):
    days = sorted(g["days"])
    print(f"[{i}] {g['times']} 次 / {g['rows']} 行 / 出现在 {len(days)} 天")
    print(f"    首次 {f(g['first'], '%Y-%m-%d %H:%M')}   最近 {f(g['last'], '%m-%d %H:%M')}")
    print(f"    级别 {','.join(sorted(g['levels']))}")
    print(f"    模式 {k[:130]}")
    print(f"    原文 {g['sample'][:160]}")
    print()

# 按天 × 模式，看是不是集中爆发
print("=" * 92)
print("按天分布（只列前 6 个模式）")
print("=" * 92)
for k, g in ordered[:6]:
    per = collections.Counter()
    for r in rows:
        if norm(r["msg"]) == k:
            per[f(r["ts"], "%m-%d")] += int(r["n"] or 1)
    top = ", ".join(f"{d}:{v}" for d, v in sorted(per.items()))
    print(f"  {g['times']:>4} 次  {k[:56]}")
    print(f"        {top[:300]}")
