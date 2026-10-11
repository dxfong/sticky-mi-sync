# -*- coding: utf-8 -*-
"""诊断：容器下载**大响应**为什么总被掐断。

现象（2026-10-11）：向 substrate.office.com 拉 fabric 便笺列表，
5 次全部失败，且都在 ~100KB 之前断：
    IncompleteRead(3542 bytes read, 105615 more expected)
    IncompleteRead(1372 bytes read, 107785 more expected)
    TimeoutError / SSL: UNEXPECTED_EOF_WHILE_READING

而同一台机器上的 **Windows** 浏览器访问同一个域名是好的。
→ 怀疑是容器/主机的网络路径对**大响应**有问题（MTU/MSS、中间设备、
  代理对大响应的处理等），而不是代码问题。

本脚本对**多个主机**各下 1MB，看是不是 substrate 专有：
  · 若所有主机都断在大致相同的字节数 → 通用问题（MTU / MSS clamp 嫌疑）
  · 若只有 substrate 断 → 该域名专有（路由 / 中间设备针对性处理）

只读，不写任何东西。
"""
from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request

TARGETS = [
    ("Cloudflare 1MB", "https://speed.cloudflare.com/__down?bytes=1048576"),
    ("Cloudflare 100KB", "https://speed.cloudflare.com/__down?bytes=102400"),
    ("微软 Graph（大响应）", "https://graph.microsoft.com/v1.0/$metadata"),
    ("substrate NotesFabric", "https://substrate.office.com/NotesFabric/api/v2.0/me/notes"),
]

print("=" * 74)
print("MTU 探测")
print("=" * 74)
# 用 UDP + 不碎片（DF）探测本机可用 MTU。需要一个不回包的地址，
# 所以直接看常见的 1500 / 1492 / 1400 里哪个能过（用 TCP MSS 推算更稳，
# 但容器里没有 ping/ip，只能靠下载表现间接推断）。
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("1.1.1.1", 443))
    print("  本地出口地址:", s.getsockname())
    s.close()
except Exception as e:
    print("  TCP 连接测试失败:", e)

print()
print("=" * 74)
print("大响应下载测试（每个 10 秒超时）")
print("=" * 74)
for name, url in TARGETS:
    t0 = time.time()
    got = 0
    note = ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                if got > 3_000_000:
                    note = "（超过 3MB，主动停）"
                    break
        ok = "✅ 完整"
    except urllib.error.HTTPError as e:
        # 401/404 也算"链路通了"，把 body 读完看字节数
        try:
            got = len(e.read())
        except Exception:
            got = 0
        ok = f"HTTP {e.code}（链路通，读回 {got} 字节）"
        note = ""
    except Exception as e:
        ok = f"❌ {type(e).__name__}: {str(e)[:70]}"
    dt = time.time() - t0
    print(f"  {name:<22} {got:>9} 字节  {dt:5.1f}s  {ok} {note}")
