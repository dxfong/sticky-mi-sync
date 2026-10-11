# -*- coding: utf-8 -*-
"""关键实验：把单页响应变小，能不能躲过"读到一半被掐"。

背景（2026-10-11 实测）：
  `RealGraph._list_via_outlook` 请求的是
      /me/notes?$select=Id,Subject,Body,...&$top=100
  也就是**一页 100 条、且带正文** → 单页响应约 270KB。
  而失败信息反复出现同一个断点数：
      IncompleteRead(267031 bytes read)   ← 出现 3 次，一模一样
      IncompleteRead(151064 / 229483 / 113804 bytes read)

  ★ 同一个断点重复出现 → 不是随机丢包，而是**响应一旦超过某个体量就被掐**。

所以：把 `$top` 调小 → 单页响应变小 → 可能就通了。
本脚本对若干个 `$top` 各试 3 次，量成功率和响应字节数。只读。

用应用自己的 `ensure_token()` 取令牌（过期会自动走 OAuth 刷新，纯 HTTP 不需要浏览器）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")
from notesync.store import Store      # noqa: E402
from notesync.graph import RealGraph  # noqa: E402

OUTLOOK = "https://outlook.office.com/api/beta"
SEL = "Id,Subject,Body,ChangeKey,LastModifiedDateTime,IsDeleted"

store = Store("/app/data")
g = RealGraph(store)
try:
    token = g.ensure_token()
    print("令牌获取:", "OK" if token else "失败")
except Exception as e:
    print("取令牌失败:", type(e).__name__, str(e)[:150])
    sys.exit(1)

N = 3
print()
print("=" * 76)
print(f"{'$top':>6}  {'成功':>5}   {'各次收到的字节数':<34}  耗时")
print("=" * 76)
for top in (100, 50, 25, 10, 5, 2):
    url = f"{OUTLOOK}/me/notes?$select={SEL}&$top={top}"
    ok = 0
    sizes = []
    dt_all = 0.0
    for _ in range(N):
        t0 = time.time()
        got = 0
        err = ""
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            got = len(raw)
            json.loads(raw)          # 确认是完整 JSON
            ok += 1
        except Exception as e:
            err = type(e).__name__
            try:
                got = len(raw) if "raw" in dir() else got
            except Exception:
                pass
        dt_all += time.time() - t0
        sizes.append(f"{got}{'/'+err if err else ''}")
    print(f"{top:>6}  {ok:>2}/{N}   {' '.join(sizes):<34}  {dt_all/N:.1f}s")

print()
print("说明：字节数后面跟的错误名表示那次失败的类型；")
print("      IncompleteRead = 服务端声明了长度但连接被中途掐断。")
