# -*- coding: utf-8 -*-
"""测国外大响应的**吞吐曲线**：到底是"被限速"还是"连接被掐"。

为什么必须先分清这两件事：
  · 被限速 → 拉长 timeout / 减小单次响应体积就能治
  · 被掐断 → 只能重试或换链路
现象上两者都表现为"读一半就超时"，但修法完全相反。

实测背景（2026-10-11）：
  主机 curl -4 下载 Cloudflare 2MB：**3/3 成功，1.2s，约 1.7MB/s**
  容器 python urllib 下同一个 URL：**3/3 超时，26s 只拿到约 1MB（≈40KB/s）**
  → 同一 netns、同一 IPv4、同一目标，差别在**客户端**
所以这里要看清 python 侧到底是"匀速慢"还是"卡住"。

逐秒打点，长超时（90s）。只读。
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

TARGETS = [
    ("Cloudflare 2MB",
     "https://speed.cloudflare.com/__down?bytes=2000000"),
    ("微软 Graph $metadata",
     "https://graph.microsoft.com/v1.0/$metadata"),
    ("微软 outlook REST（无凭据，看响应头能否回来）",
     "https://outlook.office.com/api/v2.0/me/notes"),
]
TIMEOUT = 90
UA = {"User-Agent": "Mozilla/5.0"}

for name, url in TARGETS:
    print("=" * 74)
    print(name)
    print("  " + url)
    print("=" * 74)
    t0 = time.time()
    got = 0
    marks: list[tuple[float, int]] = []
    last_mark = 0.0
    err = ""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                el = time.time() - t0
                if el - last_mark >= 1.0:
                    marks.append((el, got))
                    last_mark = el
                if got > 6_000_000:
                    err = "（超过 6MB，主动停）"
                    break
    except urllib.error.HTTPError as e:
        try:
            got = len(e.read())
        except Exception:
            pass
        err = f"HTTP {e.code}（链路是通的）"
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:90]}"

    dt = time.time() - t0
    rate = got / dt if dt else 0
    print(f"  结果: {got} 字节 / {dt:.1f}s  =  {rate/1024:.1f} KB/s   {err}")
    if marks:
        print("  逐秒累计：")
        prev_t, prev_b = 0.0, 0
        for t, b in marks[:14]:
            seg = b - prev_b
            print(f"    {t:5.1f}s  累计 {b:>9}  本段 {seg:>8} 字节 "
                  f"({seg/(t-prev_t)/1024:7.1f} KB/s)")
            prev_t, prev_b = t, b
        if len(marks) > 14:
            print(f"    … 共 {len(marks)} 秒有数据")
    print()
