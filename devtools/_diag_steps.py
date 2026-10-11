# -*- coding: utf-8 -*-
"""定位到具体是哪一步：应用 `list_notes()` 全挂，但单页请求全通。

交错对照结果（同一时间窗）：
    单页 $top=2     5/5 ✅
    单页 $top=100   5/5 ✅（390865 字节，大响应没问题）
    $top=2 + ALPN h2  0/5 ❌（阳性对照，h2 确实被链路打断）
    应用 list_notes() 0/5 ❌  ← 只有它挂

→ 差别只剩：应用要**翻页**（164 条 / 每页 100 条 = 2 页），
  而且走的是 `_paged()` → `_request()`。
本脚本把这条路径逐步拆开，看第几步断。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/app")
from notesync.store import Store                                    # noqa: E402
from notesync.graph import RealGraph, _request, OUTLOOK, _err_text  # noqa: E402

store = Store("/app/data")
g = RealGraph(store)
TOKEN = g.ensure_token()
SEL = "Id,Subject,Body,ChangeKey,LastModifiedDateTime,IsDeleted"
URL1 = f"{OUTLOOK}/me/notes?$select={SEL}&$top=100"

print("=" * 78)
print("逐步拆解（每步独立计时、打印状态码与字节数）")
print("=" * 78)
for rnd in (1, 2, 3):
    print(f"\n--- 第 {rnd} 轮 ---")
    # 步骤 1：第一页
    t0 = time.time()
    try:
        code, data = _request("GET", URL1, TOKEN)
        n = len(data.get("value") or [])
        nxt = data.get("@odata.nextLink", "")
        print(f"  ① 第一页        HTTP {code}  {n} 条  nextLink={'有' if nxt else '无'}"
              f"  {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"  ① 第一页        失败 {type(e).__name__}: {str(e)[:90]}"
              f"  {time.time()-t0:.1f}s")
        continue

    if not nxt:
        print("  ② 没有 nextLink，结束")
        continue
    # 步骤 2：第二页（nextLink）
    t0 = time.time()
    try:
        code2, data2 = _request("GET", nxt, TOKEN)
        n2 = len(data2.get("value") or [])
        print(f"  ② 第二页        HTTP {code2}  {n2} 条  {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"  ② 第二页        失败 {type(e).__name__}: {str(e)[:90]}"
              f"  {time.time()-t0:.1f}s")

    # 步骤 3：完整走应用自己的 _paged
    t0 = time.time()
    try:
        got = g._list_via_outlook(TOKEN)
        print(f"  ③ _list_via_outlook  共 {len(got)} 条  {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"  ③ _list_via_outlook  失败 {type(e).__name__}: {str(e)[:110]}"
              f"  {time.time()-t0:.1f}s")
