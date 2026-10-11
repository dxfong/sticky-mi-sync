# -*- coding: utf-8 -*-
"""量化"拉取不到"到底多严重，并试一个可能有效的修法。

背景（同一 netns / 同一 IP / 同一目标）：
    curl -4    20MB  → 1.83s 完整，多次稳定成功
    python     3MB   → 多数卡在 1~2MB；同样的代码重跑一次又成功（间歇性）
→ 不是确定性故障，是链路质量变差 + Python 这一路对它更敏感。

所以这里做两件事：
  ① 用**应用真实的那条路径**（RealGraph.list_notes）连试 6 次，给出成功率
  ② 试把 socket 接收缓冲调大（丢包 + 小缓冲会导致吞吐崩塌）
     —— 若明显有效，就是可以在应用里落地的修法
只读。
"""
from __future__ import annotations

import socket
import sys
import time

sys.path.insert(0, "/app")

print("=" * 74)
print("① 应用真实路径：RealGraph.list_notes() 连试 6 次")
print("=" * 74)
from notesync.store import Store      # noqa: E402
from notesync.graph import RealGraph  # noqa: E402

store = Store("/app/data")
g = RealGraph(store)
okn = 0
for i in range(1, 7):
    t0 = time.time()
    try:
        ns = g.list_notes()
        print(f"  {i}) ✅ {len(ns)} 条  {time.time()-t0:.1f}s")
        okn += 1
    except Exception as e:
        print(f"  {i}) ❌ {type(e).__name__}: {str(e)[:80]}  {time.time()-t0:.1f}s")
    time.sleep(1)
print(f"  → 成功 {okn}/6")

print()
print("=" * 74)
print("② 试加大 socket 接收缓冲（4MB）后同样的下载")
print("=" * 74)

HOST, PORT, PATH = "speed.cloudflare.com", 443, "/__down?bytes=3000000"


def fetch(rcvbuf=None, tls=True):
    import http.client
    import ssl
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.settimeout(30)
    s.connect((HOST, PORT))
    eff = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    if tls:
        ctx = ssl.create_default_context()
        s = ctx.wrap_socket(s, server_hostname=HOST)
    c = http.client.HTTPConnection(HOST)
    c.sock = s
    c.request("GET", PATH, headers={"User-Agent": "Mozilla/5.0", "Host": HOST})
    r = c.getresponse()
    n = 0
    while True:
        b = r.read(65536)
        if not b:
            break
        n += len(b)
    c.close()
    return n, eff


for label, rb in (("默认缓冲", None), ("SO_RCVBUF=4MB", 4 * 1024 * 1024),
                  ("SO_RCVBUF=8MB", 8 * 1024 * 1024)):
    okn = 0
    sizes = []
    for i in range(4):
        t0 = time.time()
        try:
            n, eff = fetch(rb)
            sizes.append(n)
            if n >= 3_000_000:
                okn += 1
        except Exception as e:
            sizes.append(0)
        time.sleep(0.5)
    eff_txt = f"（内核实际 {eff/1024/1024:.1f}MB）" if rb else ""
    print(f"  {label:<16} 成功 {okn}/4  收到 {sizes}{eff_txt}")
