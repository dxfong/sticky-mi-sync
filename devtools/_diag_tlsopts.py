# -*- coding: utf-8 -*-
"""找出「让 Python 也能下完」的 TLS 参数。

对照事实（同一 URL `graph.microsoft.com/v1.0/$metadata`，交替测试）：
    curl -4   8/8 成功，每次 1806673 字节完整
    python    8/8 失败，在 1.2~1.6MB 处被 IncompleteRead / 超时
→ 网络路径没问题，是 **Python 这一路的 TLS 行为**不被中间链路接受。

嫌疑：容器里是 OpenSSL **3.5.7**（很新），默认会带上
      混合后量子密钥交换（X25519MLKEM768），ClientHello 明显更大。
      中间设备（DPI/透明代理）对它的处理可能就是把连接掐掉。

逐个试参数，找到能通的那个 —— 找到就能直接在应用里修。
每项跑 3 次（单次不可信，链路是间歇性的）。
"""
from __future__ import annotations

import http.client
import socket
import ssl
import time

HOST = "graph.microsoft.com"
PATH = "/v1.0/$metadata"
FULL = 1806673          # curl 每次拿到的完整长度
N = 3


def fetch(make_ctx, label, hdrs=None, tls=True, port=None):
    ok = 0
    sizes = []
    for _ in range(N):
        got = 0
        try:
            p = port or (443 if tls else 80)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(25)
            s.connect((HOST, p))
            if tls:
                s = make_ctx().wrap_socket(s, server_hostname=HOST)
            c = http.client.HTTPConnection(HOST)
            c.sock = s
            h = {"User-Agent": "Mozilla/5.0", "Host": HOST, "Accept": "*/*"}
            h.update(hdrs or {})
            c.request("GET", PATH, headers=h)
            r = c.getresponse()
            while True:
                b = r.read(65536)
                if not b:
                    break
                got += len(b)
            c.close()
        except Exception:
            pass
        sizes.append(got)
        if got >= FULL:
            ok += 1
    mark = "✅" if ok == N else ("⚠ " if ok else "❌")
    print(f"  {mark} {label:<38} {ok}/{N}   {sizes}")
    return ok


def c_default():
    return ssl.create_default_context()


def c_tls12():
    c = ssl.create_default_context()
    c.maximum_version = ssl.TLSVersion.TLSv1_2
    return c


def c_x25519():
    c = ssl.create_default_context()
    try:
        c.set_ecdh_curve("X25519")
    except Exception:
        pass
    return c


def c_p256():
    c = ssl.create_default_context()
    try:
        c.set_ecdh_curve("prime256v1")
    except Exception:
        pass
    return c


def c_notls13():
    c = ssl.create_default_context()
    c.minimum_version = ssl.TLSVersion.TLSv1_2
    c.maximum_version = ssl.TLSVersion.TLSv1_2
    return c


print("=" * 78)
print(f"目标 {FULL} 字节（curl 的完整长度），每项 {N} 次")
print("=" * 78)
print("ssl:", ssl.OPENSSL_VERSION)
print()

fetch(c_default, "① 默认")
fetch(c_tls12, "② 只允许 TLS 1.2")
fetch(c_x25519, "③ 指定曲线 X25519")
fetch(c_p256, "④ 指定曲线 prime256v1")
fetch(lambda: ssl.create_default_context(), "⑤ 显式 Connection: close",
      {"Connection": "close"})
fetch(lambda: None, "⑥ 明文 HTTP（80 端口）", tls=False, port=80)
