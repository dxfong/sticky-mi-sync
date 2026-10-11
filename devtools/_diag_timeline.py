# -*- coding: utf-8 -*-
"""时间线：今天这条链路是什么时候开始抖的？之前真的稳定吗？

回答用户的问题「之前是好的，为什么突然网络不行」——
不看感觉，看日志里的成功/失败分布。
只读。
"""
from __future__ import annotations

import collections
import datetime
import sqlite3

c = sqlite3.connect("/app/data/state.db")


def hm(t):
    return datetime.datetime.fromtimestamp(int(t)).strftime("%m-%d %H:%M")


rows = c.execute("SELECT ts, level, msg FROM log ORDER BY ts").fetchall()
ok = collections.Counter()
bad = collections.Counter()
errs: list[tuple[int, str]] = []
for t, lv, m in rows:
    k = hm(t)
    # 成功：任何一条"…拉取列表…N 条"
    if "拉取列表" in m and "条" in m:
        ok[k] += 1
    if lv in ("warn", "error") and ("失败" in m or "抖动" in m):
        bad[k] += 1
        errs.append((int(t), m))

print("日志表覆盖区间：", hm(rows[0][0]) if rows else "-", "~",
      hm(rows[-1][0]) if rows else "-", f" 共 {len(rows)} 条")
print()
print("按分钟：OK = 成功拉取次数，FAIL = 失败次数")
print(f"  {'时间':<12}{'OK':>4}{'FAIL':>6}")
for k in sorted(set(ok) | set(bad)):
    flag = "  ⚠ 有失败" if bad.get(k) else ""
    print(f"  {k:<12}{ok.get(k, 0):>4}{bad.get(k, 0):>6}{flag}")

print()
if errs:
    print("第一次失败 :", datetime.datetime.fromtimestamp(errs[0][0]).strftime("%m-%d %H:%M:%S"))
    print("最后一次失败:", datetime.datetime.fromtimestamp(errs[-1][0]).strftime("%m-%d %H:%M:%S"))
    print("失败总次数 :", len(errs))
    print()
    print("失败类型分布：")
    kinds = collections.Counter()
    for _, m in errs:
        for k in ("IncompleteRead", "timed out", "UNEXPECTED_EOF",
                  "RemoteDisconnected", "ConnectionReset"):
            if k in m:
                kinds[k] += 1
                break
        else:
            kinds["其他"] += 1
    for k, v in kinds.most_common():
        print(f"  {k:<22}{v}")

print()
for key, label in (("last_ok_at", "last_ok_at"), ("last_fail_at", "last_fail_at")):
    r = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    if r and r[0]:
        print(f"{label:<14}", datetime.datetime.fromtimestamp(int(r[0])).strftime("%m-%d %H:%M:%S"))
    else:
        print(f"{label:<14} (空)")
