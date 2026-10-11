# -*- coding: utf-8 -*-
"""确认修法：把 `Connection: close` 换成 `keep-alive`。

上一步结论（同一时间窗，同一 URL，`$top=100` 约 390KB）：
    裸 socket（不发 Connection）           4/4 ✅
    + Accept-Encoding: identity          4/4 ✅
    + Connection: close                  0/4 ❌  ← 唯一触发点
    urllib 默认（http.client 会补 close）  0/4 ❌

→ `Connection: close` 让服务端在响应体发完后立刻关连接，
  而中间链路（透明代理）在数据完整送达前就把 TCP 拆了 → 响应尾部丢失。

修法：发 `Connection: keep-alive`（我们自己读完就关，不需要服务端先关）。
本脚本验证这一行改动是否真的有效。
"""
from __future__ import annotations

import http.client
import socket
import ssl
import sys
import urllib.request

sys.path.insert(0, "/app")
from notesync.store import Store      # noqa: E402
from notesync.graph import RealGraph  # noqa: E402

store = Store("/app/data")
TOKEN = RealGraph(store).ensure_token()
HOST = "outlook.office.com"
SEL = "Id,Subject,Body,ChangeKey,LastModifiedDateTime,IsDeleted"
PATH = f"/api/beta/me/notes?$select={SEL}&$top=100"
FULL_MIN = 380000


def raw(extra: dict | None = None) -> int:
    try:
        s = socket.create_connection((HOST, 443), timeout=25)
        s = ssl.create_default_context().wrap_socket(s, server_hostname=HOST)
        c = http.client.HTTPConnection(HOST)
        c.sock = s
        h = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json",
             "User-Agent": "Mozilla/5.0", "Host": HOST}
        h.update(extra or {})
        c.request("GET", PATH, headers=h)
        r = c.getresponse()
        n = len(r.read())
        c.close()
        return n
    except Exception:
        return 0


def via_urllib(extra: dict | None = None) -> int:
    """应用真实路径（urllib）。extra 用来注入 Connection 覆盖默认值。"""
    try:
        req = urllib.request.Request("https://" + HOST + PATH, headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            **(extra or {}),
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            return len(r.read())
    except Exception:
        return 0


PROBES = [
    ("裸 socket 基准（不发）", lambda: raw()),
    ("裸 socket + keep-alive", lambda: raw({"Connection": "keep-alive"})),
    ("urllib 默认（= close）", lambda: via_urllib()),
    ("★ urllib + keep-alive", lambda: via_urllib({"Connection": "keep-alive"})),
]

ROUNDS = 4
tally = [0] * len(PROBES)
print("=" * 78)
print(f"轮转 {ROUNDS} 轮（这一页约 390KB）")
print("=" * 78)
for r in range(1, ROUNDS + 1):
    parts = []
    for i, (name, fn) in enumerate(PROBES):
        n = fn()
        if n >= FULL_MIN:
            tally[i] += 1
        parts.append(f"{n//1024}K")
    print(f"  轮 {r}:  " + "  ".join(f"{p:>7}" for p in parts))

print()
for i, (name, _) in enumerate(PROBES):
    ok = tally[i]
    mark = "✅" if ok == ROUNDS else ("⚠ " if ok else "❌")
    print(f"  {mark} {name:<34} {ok}/{ROUNDS}")
