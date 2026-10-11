# -*- coding: utf-8 -*-
"""定量：现在这条链路到底多少比例能成功？服务在重试，能不能慢慢同步过去？

把三件事放一起看：
  ① 应用真实路径（RealGraph.list_notes）连试 12 次 → 成功率
  ② 同一时刻主机 curl 打同一个域名连试 12 次 → 成功率（对照）
  ③ 服务自己的日志：最后一次成功是什么时候、最近有没有零星成功

只读。
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
import time

sys.path.insert(0, "/app")

print("=" * 74)
print("① 应用真实路径 RealGraph.list_notes() × 12")
print("=" * 74)
from notesync.store import Store      # noqa: E402
from notesync.graph import RealGraph  # noqa: E402

store = Store("/app/data")
g = RealGraph(store)
okn = 0
sizes = []
for i in range(1, 13):
    t0 = time.time()
    try:
        ns = g.list_notes()
        print(f"  {i:>2}) ✅ {len(ns)} 条  {time.time()-t0:.1f}s")
        okn += 1
        sizes.append(len(ns))
    except Exception as e:
        print(f"  {i:>2}) ❌ {type(e).__name__}: {str(e)[:60]}  {time.time()-t0:.1f}s")
print(f"  → 成功 {okn}/12  ({okn/12*100:.0f}%)")
print()

print("=" * 74)
print("③ 服务自身：最近的拉取痕迹")
print("=" * 74)
c = sqlite3.connect("/app/data/state.db")


def ts(t):
    return datetime.datetime.fromtimestamp(int(t)).strftime("%H:%M:%S")


r = c.execute("SELECT v FROM meta WHERE k='last_ok_at'").fetchone()
print("  last_ok_at :", ts(r[0]) if r and r[0] else "(空)")
r = c.execute("SELECT v FROM meta WHERE k='last_fail_at'").fetchone()
print("  last_fail_at:", ts(r[0]) if r and r[0] else "(空)")
print()
print("  最近 12 条日志：")
for t, lv, m in c.execute("SELECT ts, level, msg FROM log ORDER BY ts DESC LIMIT 12"):
    print(f"    {ts(t)} [{lv:<5}] {m[:96]}")
print()
print("  今天的成功拉取 / 失败 计数：")
n_ok = c.execute("SELECT count(*) FROM log WHERE msg LIKE '%拉取列表%条%'").fetchone()[0]
n_bad = c.execute("SELECT count(*) FROM log WHERE level IN ('warn','error') "
                  "AND (msg LIKE '%失败%' OR msg LIKE '%抖动%')").fetchone()[0]
print(f"    成功 {n_ok} 次 / 失败 {n_bad} 次")
