# -*- coding: utf-8 -*-
"""缩小到「请求头」：为什么 urllib 挂、裸 socket 通。

已确立（同一时间窗、同一 URL、同一令牌）：
    裸 socket + http.client，`$top=100`（390865 字节） → 5/5 ✅
    urllib.request.urlopen（应用真实用的那个）        → 0/5 ❌
    `$top=2` + ALPN h2                                → 0/5 ❌（阳性对照）

裸 socket 与我发的头：Authorization / Accept / User-Agent / Host
urllib 会**额外**加两个（http.client 的实现细节）：
    · `Accept-Encoding: identity`
    · `Connection: close`
这就是唯一剩下的差别。

只读。
"""
from __future__ import annotations

import http.client
import socket
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, "/app")
from notesync.store import Store      # noqa: E402
from notesync.graph import RealGraph  # noqa: E402

store = Store("/app/data")
TOKEN = RealGraph(store).ensure_token()
HOST = "outlook.office.com"
SEL = "Id,Subject,Body,ChangeKey,LastModifiedDateTime,IsDeleted"
PATH = f"/api/beta/me/notes?$select={SEL}&$top=100"
FULL_MIN = 380000        # 这一页约 390KB


def raw(extra: dict | None = None) -> int:
    try:
        s = socket.create_connection((HOST, 443), timeout=25)
        s = ssl.create_default_context().wrap_socket(s, server_hostname=HOST)
        c = http.client.HTTPConnection(HOST)
        c.sock = s
        h = {"Authorization": "Bearer " + TOKEN,
             "Accept": "application/json",
             "User-Agent": "Mozilla/5.0",
             "Host": HOST}
        h.update(extra or {})
        c.request("GET", PATH, headers=h)
        r = c.getresponse()
        n = len(r.read())
        c.close()
        return n
    except Exception:
        return 0


def via_urllib() -> int:
    """应用真实路径。"""
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


PROBES = [
    ("裸 socket（基准）", lambda: raw()),
    ("+ Accept-Encoding: identity", lambda: raw({"Accept-Encoding": "identity"})),
    ("+ Connection: close", lambda: raw({"Connection": "close"})),
    ("+ 两个都加", lambda: raw({"Accept-Encoding": "identity",
                                "Connection": "close"})),
    ("urllib（应用路径）", via_urllib),
]

ROUNDS = 4
tally = [0] * len(PROBES)
print("=" * 78)
print(f"轮转 {ROUNDS} 轮，每轮 {len(PROBES)} 个探针各 1 次（这一页约 390KB）")
print("=" * 78)
for r in range(1, ROUNDS + 1):
    parts = []
    for i, (name, fn) in enumerate(PROBES):
        n = fn()
        if n >= FULL_MIN:
            tally[i] += 1
        parts.append(f"{n//1024}K")
    print(f"  轮 {r}:  " + "  ".join(f"{p:>6}" for p in parts))

print()
for i, (name, _) in enumerate(PROBES):
    ok = tally[i]
    mark = "✅" if ok == ROUNDS else ("⚠ " if ok else "❌")
    print(f"  {mark} {name:<32} {ok}/{ROUNDS}")
