# -*- coding: utf-8 -*-
"""A/B 对照用：同一 URL 用 python 拉 4 次。

对照对象是主机 curl（同一 URL、同一时刻）。已实测：
    curl -4  → 4/4 成功，每次 1806673 字节完整
    本脚本  → 见输出
"""
import time
import urllib.request

U = "https://graph.microsoft.com/v1.0/$metadata"
for i in range(4):
    t0 = time.time()
    got = 0
    try:
        req = urllib.request.Request(U, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            while True:
                b = r.read(65536)
                if not b:
                    break
                got += len(b)
        print("  py #%d  %d 字节  %.1fs  OK" % (i + 1, got, time.time() - t0))
    except Exception as e:
        print("  py #%d  %d 字节  %.1fs  %s: %s"
              % (i + 1, got, time.time() - t0, type(e).__name__, str(e)[:60]))
