# -*- coding: utf-8 -*-
"""确认 urllib 是把 `Connection: close` 写死的，并找出可用的替代传输。

已知：
    裸 socket + http.client（不发 Connection） → 4/4 ✅
    urllib（默认会补 Connection: close）        → 0/4 ❌
    urllib + 手动指定 keep-alive               → 0/4 ❌（覆盖不掉）

本脚本：
  ① 打印 CPython `urllib.request.AbstractHTTPHandler.do_open` 里与 Connection 有关的源码行
  ② 试 `http.client.HTTPSConnection` 直接发（不经过 urllib）是否可行
     —— 如果可行，就用它当替代传输，改动最小
"""
from __future__ import annotations

import http.client
import inspect
import sys
import time
import urllib.request

sys.path.insert(0, "/app")
from notesync.store import Store      # noqa: E402
from notesync.graph import RealGraph  # noqa: E402

print("=" * 78)
print("① urllib 源码里与 Connection 有关的行")
print("=" * 78)
src = inspect.getsource(urllib.request.AbstractHTTPHandler.do_open)
for i, line in enumerate(src.splitlines(), 1):
    if "onnection" in line:
        print(f"  {i:>3}| {line.strip()}")
print()

store = Store("/app/data")
TOKEN = RealGraph(store).ensure_token()
HOST = "outlook.office.com"
SEL = "Id,Subject,Body,ChangeKey,LastModifiedDateTime,IsDeleted"
PATH = f"/api/beta/me/notes?$select={SEL}&$top=100"
FULL_MIN = 380000


def via_httpsconnection() -> int:
    """直接 http.client.HTTPSConnection（不经 urllib）。"""
    try:
        c = http.client.HTTPSConnection(HOST, 443, timeout=25)
        c.request("GET", PATH, headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        })
        r = c.getresponse()
        n = len(r.read())
        c.close()
        return n
    except Exception:
        return 0


def via_urllib() -> int:
    try:
        req = urllib.request.Request("https://" + HOST + PATH, headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            return len(r.read())
    except Exception:
        return 0


PROBES = [("http.client.HTTPSConnection（替代方案）", via_httpsconnection),
          ("urllib（现状）", via_urllib)]
ROUNDS = 4
tally = [0] * len(PROBES)
print("=" * 78)
print(f"② 轮转 {ROUNDS} 轮（这一页约 390KB）")
print("=" * 78)
for r in range(1, ROUNDS + 1):
    parts = []
    for i, (_, fn) in enumerate(PROBES):
        n = fn()
        if n >= FULL_MIN:
            tally[i] += 1
        parts.append(f"{n//1024}K")
    print(f"  轮 {r}:  " + "  ".join(f"{p:>9}" for p in parts))
print()
for i, (name, _) in enumerate(PROBES):
    mark = "✅" if tally[i] == ROUNDS else ("⚠ " if tally[i] else "❌")
    print(f"  {mark} {name:<40} {tally[i]}/{ROUNDS}")
