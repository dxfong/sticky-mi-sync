# -*- coding: utf-8 -*-
"""深挖：为什么 last_ok_at 停在 10-13，以及那串 InterfaceError 是什么。

要看清三件事：
  ① 现在的实时日志（info）—— 服务到底还在不在干活
  ② 所有 InterfaceError / 致命 的**原文与时间**
  ③ 各天的"成功拉取"痕迹 —— 但 info 只留 500 条（约 24 分钟），
     所以更靠得住的判据是 `last_ok_at` 与 `link.last_synced_at`
只读。
"""
from __future__ import annotations

import datetime
import sqlite3

c = sqlite3.connect("/app/data/state.db")
c.row_factory = sqlite3.Row


def f(ts, fmt="%m-%d %H:%M:%S"):
    return datetime.datetime.fromtimestamp(int(ts)).strftime(fmt)


print("=" * 78)
print("① 最近 18 条日志（实时在干什么）")
print("=" * 78)
for r in c.execute("SELECT ts, level, msg FROM log ORDER BY rowid DESC LIMIT 18"):
    print(f"  {f(r['ts'])} [{r['level']:<5}] {r['msg'][:110]}")

print()
print("=" * 78)
print("② InterfaceError / 致命 / 同步循环异常 —— 完整原文")
print("=" * 78)
n = 0
for r in c.execute(
        "SELECT ts, level, msg FROM log WHERE level IN ('warn','error') "
        "AND (msg LIKE '%InterfaceError%' OR msg LIKE '%致命%' "
        "     OR msg LIKE '%同步循环异常%' OR msg LIKE '%同步已中止%') "
        "ORDER BY rowid"):
    n += 1
    print(f"  {f(r['ts'])} [{r['level']}] {r['msg']}")
print(f"  —— 共 {n} 条")

print()
print("=" * 78)
print("③ 其余告警（按时间，10-13 之后的全部）")
print("=" * 78)
since = int(datetime.datetime(2026, 10, 13, 0, 0).timestamp())
n = 0
for r in c.execute(
        "SELECT ts, level, msg FROM log WHERE level IN ('warn','error') "
        "AND ts >= ? ORDER BY rowid", (since,)):
    n += 1
    print(f"  {f(r['ts'])} [{r['level']}] {r['msg'][:150]}")
print(f"  —— 10-13 起共 {n} 条")
