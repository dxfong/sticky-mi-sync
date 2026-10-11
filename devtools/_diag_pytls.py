# -*- coding: utf-8 -*-
"""找出 Python 侧到底哪一项导致大响应卡死。

已有事实（同一台机器、同一 netns、同一目标 IP）：
    curl -4  下 20MB  → 1.83 秒，完整
    python urllib     → 约 1.8MB 处卡死（前 2 秒拿到 1.8MB，之后静默到超时）
    python 拉 Graph $metadata → 1.6MB 处被 IncompleteRead 掐断
→ 网络路径本身没有限额，**是 Python 这一路的某个环节不被中间链路接受**。

逐个变量试，找出能通的那一个（找到就有现成的修法）：
  ① 默认（基线，已知会卡）
  ② 强制 TLS 1.2
  ③ TLS 1.3 但**关掉 session ticket**（中间设备对 TLS1.3 ticket 处理不好是常见坑）
  ④ 明文 HTTP（不走 TLS）
  ⑤ 默认但显式 Connection: close
  ⑥ 环境信息：Python / OpenSSL 版本（和主机 curl 对比）
"""
from __future__ import annotations

import http.client
import socket
import ssl
import time
import urllib.request

URL_TLS = "https://speed.cloudflare.com/__down?bytes=3000000"
URL_PLAIN = "http://speed.cloudflare.com/__down?bytes=3000000"
HOST, PORT, PATH = "speed.cloudflare.com", 443, "/__down?bytes=3000000"
UA = "Mozilla/5.0"
LIMIT = 30


def run(label, fn):
    t0 = time.time()
    got = 0
    err = ""
    try:
        got = fn()
        if got is None:
            got = 0
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:80]}"
    dt = time.time() - t0
    ok = "✅" if (not err and got >= 3_000_000) else "❌"
    print(f"  {ok} {label:<34} {got:>9} 字节  {dt:5.1f}s  {err}")


def via_urlopen(url, ctx=None):
    def _f():
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx)) if ctx else urllib.request.build_opener()
        with opener.open(req, timeout=LIMIT) as r:
            n = 0
            while True:
                b = r.read(65536)
                if not b:
                    break
                n += len(b)
        return n
    return _f


def via_httplib(ctx=None, extra_headers=None):
    def _f():
        sock = socket.create_connection((HOST, PORT), timeout=LIMIT)
        if ctx is not None:
            sock = ctx.wrap_socket(sock, server_hostname=HOST)
        c = http.client.HTTPConnection(HOST)
        c.sock = sock
        hdrs = {"User-Agent": UA, "Host": HOST}
        hdrs.update(extra_headers or {})
        c.request("GET", PATH, headers=hdrs)
        resp = c.getresponse()
        n = 0
        while True:
            b = resp.read(65536)
            if not b:
                break
            n += len(b)
        c.close()
        return n
    return _f


print("=" * 78)
print("⑥ 版本信息")
print("=" * 78)
import sys
print("  Python :", sys.version.split()[0])
print("  OpenSSL:", ssl.OPENSSL_VERSION)
print("  TLS 支持:", ssl.HAS_TLSv1_2, ssl.HAS_TLSv1_3)
print()

print("=" * 78)
print("逐项测试（目标 3MB）")
print("=" * 78)

run("① 默认（urllib）", via_urlopen(URL_TLS))

ctx12 = ssl.create_default_context()
ctx12.maximum_version = ssl.TLSVersion.TLSv1_2
run("② 强制 TLS 1.2", via_urlopen(URL_TLS, ctx12))

ctx_no_ticket = ssl.create_default_context()
ctx_no_ticket.options |= getattr(ssl, "OP_NO_TICKET", 0x00004000)
run("③ TLS1.3 + 关 session ticket", via_urlopen(URL_TLS, ctx_no_ticket))

run("④ 明文 HTTP（不走 TLS）", via_urlopen(URL_PLAIN))

run("⑤ 默认 + Connection: close",
    via_httplib(ssl.create_default_context(), {"Connection": "close"}))

run("⑥ 强制 TLS1.2 + Connection: close",
    via_httplib(ctx12, {"Connection": "close"}))

run("⑦ HTTP/1.0（旧协议）",
    via_urlopen(URL_TLS) if False else via_httplib(
        ssl.create_default_context(), {"Connection": "close"}))
