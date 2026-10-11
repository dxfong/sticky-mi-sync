# -*- coding: utf-8 -*-
"""轮转对照：找出让 Python 的请求也能成功的那个参数。

已确立的事实（完全相同的 URL + 相同的 Bearer 令牌，交替执行）：
    主机 curl  → 3/3 成功，2123 字节，0.65s
    容器 python → 3/3 失败（2 次 30s 超时、1 次 IncompleteRead(168)）
  → 同一台机器、同一 netns、同一 IP、同一请求。**网络没问题，
     是 Python 这一路的 TLS/HTTP 行为不被这条链路接受。**

★ 为什么用"轮转"而不是"一项跑完再跑下一项"：
   这条链路是**分窗口间歇**的（同一参数这轮 3/3、下轮 0/3）。
   逐项跑完会被时间偏置骗到 —— 轮转能让所有候选吃到同样的窗口分布。

每个候选每轮各试 1 次，共 N 轮，最后比成功率。
"""
from __future__ import annotations

import http.client
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")
from notesync.store import Store      # noqa: E402
from notesync.graph import RealGraph  # noqa: E402

store = Store("/app/data")
TOKEN = RealGraph(store).ensure_token()
HOST = "outlook.office.com"
PATH = ("/api/beta/me/notes?$select=Id,Subject,Body,ChangeKey,"
        "LastModifiedDateTime,IsDeleted&$top=2")
FULL = 2123          # curl 拿到的完整长度


def mk_default():
    return ssl.create_default_context()


def mk_alpn11():
    c = ssl.create_default_context()
    c.set_alpn_protocols(["http/1.1"])
    return c


def mk_alpn_h2():
    c = ssl.create_default_context()
    c.set_alpn_protocols(["h2", "http/1.1"])
    return c


def mk_tls12_alpn():
    c = ssl.create_default_context()
    c.maximum_version = ssl.TLSVersion.TLSv1_2
    c.set_alpn_protocols(["http/1.1"])
    return c


def mk_p256_alpn():
    c = ssl.create_default_context()
    try:
        c.set_ecdh_curve("prime256v1")
    except Exception:
        pass
    c.set_alpn_protocols(["http/1.1"])
    return c


def mk_noticket_alpn():
    c = ssl.create_default_context()
    c.options |= getattr(ssl, "OP_NO_TICKET", 0x00004000)
    c.set_alpn_protocols(["http/1.1"])
    return c


CANDIDATES = [
    ("① 默认（无 ALPN）", mk_default),
    ("② ALPN http/1.1", mk_alpn11),
    ("③ ALPN h2,http/1.1", mk_alpn_h2),
    ("④ TLS1.2 + ALPN http/1.1", mk_tls12_alpn),
    ("⑤ 曲线 prime256v1 + ALPN", mk_p256_alpn),
    ("⑥ 关 session ticket + ALPN", mk_noticket_alpn),
]


def attempt(make_ctx) -> int:
    """返回收到的字节数；0 表示失败。"""
    try:
        s = socket.create_connection((HOST, 443), timeout=20)
        s = make_ctx().wrap_socket(s, server_hostname=HOST)
        c = http.client.HTTPConnection(HOST)
        c.sock = s
        c.request("GET", PATH, headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Host": HOST,
        })
        r = c.getresponse()
        n = len(r.read())
        c.close()
        return n
    except Exception:
        return 0


ROUNDS = 4
tally = {name: 0 for name, _ in CANDIDATES}
print("=" * 78)
print(f"轮转 {ROUNDS} 轮，每轮 6 个候选各试 1 次（完整长度 {FULL} 字节）")
print("=" * 78)
for rnd in range(1, ROUNDS + 1):
    line = []
    for name, mk in CANDIDATES:
        n = attempt(mk)
        if n >= FULL:
            tally[name] += 1
        line.append(f"{name[0]}{'✅' if n >= FULL else ('~' if n else '❌')}")
    print(f"  第 {rnd} 轮:  " + "  ".join(line))

print()
print("成功率：")
for name, _ in CANDIDATES:
    ok = tally[name]
    mark = "✅" if ok == ROUNDS else ("⚠ " if ok else "❌")
    print(f"  {mark} {name:<30} {ok}/{ROUNDS}")
