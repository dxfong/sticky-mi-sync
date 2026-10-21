# -*- coding: utf-8 -*-
"""健康回顾：从 10-12 修复上线到现在（10-21），服务到底出过错没有。

用现在**保留 90 天**的问题日志来回答，而不是凭印象。

看五件事：
  ① 容器/服务的运行连续性（有没有重启、停过多久）
  ② 问题日志按天分布 + 按内容归类（哪类错误出现过几次）
  ③ 最后一次成功 / 最后一次失败的时间
  ④ 映射表 last_synced_at 的最大值 —— 说明"最近一次真正同步动作"是什么时候
  ⑤ 数据规模（便笺 / 小米 / 映射 / 回收站）

只读。
"""
from __future__ import annotations

import collections
import datetime
import sqlite3
import sys

sys.path.insert(0, "/app")
from notesync.store import Store, LOG_KEEP_INFO, LOG_KEEP_PROBLEM_DAYS  # noqa: E402

c = sqlite3.connect("/app/data/state.db")
c.row_factory = sqlite3.Row


def f(ts, fmt="%m-%d %H:%M"):
    return datetime.datetime.fromtimestamp(int(ts)).strftime(fmt)


st = Store("/app/data")

print("=" * 78)
print("① 运行连续性")
print("=" * 78)
startup = [(r["ts"], r["msg"]) for r in c.execute(
    "SELECT ts, msg FROM log WHERE msg LIKE '%服务启动%' ORDER BY ts")]
print(f"  日志里可见的启动次数：{len(startup)}")
for ts, m in startup[-6:]:
    print(f"    {f(ts, '%m-%d %H:%M:%S')}  {m[:60]}")
stops = [(r["ts"], r["msg"]) for r in c.execute(
    "SELECT ts, msg FROM log WHERE msg LIKE '%收到信号%' ORDER BY ts")]
print(f"  停止信号次数：{len(stops)}")
for ts, m in stops[-4:]:
    print(f"    {f(ts, '%m-%d %H:%M:%S')}  {m[:60]}")

print()
print("=" * 78)
print("② 问题日志（保留策略：info %d 条 / warn+error %d 天）" % (
    LOG_KEEP_INFO, LOG_KEEP_PROBLEM_DAYS))
print("=" * 78)
print("  各级别条数：", st.log_counts())

probs = st.recent_logs(5000, levels=["warn", "error"])
if not probs:
    print("  ✅ **一条问题日志都没有**")
else:
    print(f"  共 {len(probs)} 条，跨度 "
          f"{f(probs[-1]['ts'], '%m-%d %H:%M')} ~ {f(probs[0]['ts'], '%m-%d %H:%M')}")
    print()
    # 按天
    per_day = collections.Counter(f(p["ts"], "%m-%d") for p in probs)
    print("  按天分布：")
    for d in sorted(per_day):
        print(f"    {d}  {per_day[d]:>3} 条")
    print()
    # 按内容归类（取前 70 字做归并，保留数字差异会拆开，所以再按特征词归一次）
    def bucket(msg: str) -> str:
        for key in ("IncompleteRead", "timed out", "UNEXPECTED_EOF",
                    "ConnectionReset", "RemoteDisconnected", "配额",
                    "凭据", "未登录", "重复副本", "配对开始前", "同步已中止",
                    "网络抖动", "致命"):
            if key in msg:
                return key
        return msg[:48]
    kinds = collections.Counter(bucket(p["msg"]) for p in probs)
    print("  按类型归类：")
    for k, v in kinds.most_common(12):
        print(f"    {v:>3} 次  {k}")

print()
print("=" * 78)
print("③ 成功 / 失败时间戳")
print("=" * 78)
for k in ("last_ok_at", "last_fail_at", "fail_streak", "paused_at"):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    v = (r["v"] if r else "") or "(空)"
    if k in ("last_ok_at", "last_fail_at", "paused_at") and v not in ("", "(空)"):
        v = f(int(v), "%m-%d %H:%M:%S")
    print(f"  {k:<13} {v}")

print()
print("=" * 78)
print("④ 映射表：最近一次真正写入/同步")
print("=" * 78)
n = c.execute("SELECT count(*) FROM link").fetchone()[0]
mx = c.execute("SELECT max(last_synced_at) FROM link").fetchone()[0]
mn = c.execute("SELECT min(last_synced_at) FROM link").fetchone()[0]
print(f"  映射条数：{n}")
print(f"  最早 last_synced_at：{f(mn, '%Y-%m-%d %H:%M') if mn else '-'}")
print(f"  最近 last_synced_at：{f(mx, '%Y-%m-%d %H:%M') if mx else '-'}")

print()
print("=" * 78)
print("⑤ 数据规模")
print("=" * 78)
for side, tbl in (("便笺", "graph"), ("小米", "xiaomi")):
    try:
        k = c.execute("SELECT count(*) FROM notes_cache WHERE side=?", (tbl,)).fetchone()[0]
        print(f"  {side}缓存：{k} 条")
    except Exception as e:
        print(f"  {side}缓存：读取失败 {e}")
print("  回收站：", c.execute("SELECT count(*) FROM trash").fetchone()[0], "条")
print("  配置：", (lambda d: f"mode={d['graph']['mode']} auto_sync={d['auto_sync']} interval={d['sync_interval_sync'] if False else d['sync_interval_sec']}s"
                  )(st.cfg))
