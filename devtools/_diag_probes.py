# -*- coding: utf-8 -*-
"""最终交错对照：把"应用真实路径"和"可疑触发点"放进同一个时间窗比。

背景：链路是**分窗口间歇**的 —— 同一参数可能这轮全过、下轮全挂，
所以逐项跑完的结论都不可信。必须轮转。

四个探针：
  A. `RealGraph.list_notes()`        —— 应用真实路径（多页、$top=100、带正文）
  B. 直接 urllib 单页 `$top=2`        —— 最小请求
  C. 直接 urllib 单页 `$top=100`      —— 大请求（约 270KB）
  D. 单页 `$top=2` 但 ALPN 宣告 h2    —— 已验证 h2 会挂，作为阳性对照

每轮各打一次，共 5 轮。
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
g = RealGraph(store)
TOKEN = g.ensure_token()
HOST = "outlook.office.com"
SEL = "Id,Subject,Body,ChangeKey,LastModifiedDateTime,IsDeleted"


def page(top: int, alpn_h2: bool = False) -> int:
    """返回响应字节数；0 = 失败。"""
    path = f"/api/beta/me/notes?$select={SEL}&$top={top}"
    try:
        s = socket.create_connection((HOST, 443), timeout=20)
        if alpn_h2:
            ctx = ssl.create_default_context()
            ctx.set_alpn_protocols(["h2", "http/1.1"])
            s = ctx.wrap_socket(s, server_hostname=HOST)
        else:
            # 与 urllib 默认一致：不宣告 ALPN
            s = ssl.create_default_context().wrap_socket(s, server_hostname=HOST)
        c = http.client.HTTPConnection(HOST)
        c.sock = s
        c.request("GET", path, headers={
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


def app_path() -> str:
    try:
        ns = g.list_notes()
        return f"{len(ns)}条"
    except Exception as e:
        return type(e).__name__[:12]


ROUNDS = 5
score = {"A": 0, "B": 0, "C": 0, "D": 0}
print("=" * 78)
print("交错对照（A=应用真实路径  B=单页$top=2  C=单页$top=100  D=$top=2+h2）")
print("=" * 78)
for r in range(1, ROUNDS + 1):
    a = app_path()
    b = page(2)
    c = page(100)
    d = page(2, alpn_h2=True)
    if a not in ("TimeoutErr", "IncompleteRe", "URLError", "RemoteDisc", "FabricError"):
        score["A"] += 1
    if b:
        score["B"] += 1
    if c:
        score["C"] += 1
    if d:
        score["D"] += 1
    print(f"  轮 {r}:  A={a:<8} B={b:>7}字节  C={c:>7}字节  D={d:>7}字节")
    time.sleep(0.5)

print()
print(f"  A 应用真实路径      {score['A']}/{ROUNDS}")
print(f"  B 单页 $top=2       {score['B']}/{ROUNDS}")
print(f"  C 单页 $top=100     {score['C']}/{ROUNDS}")
print(f"  D $top=2 + ALPN h2  {score['D']}/{ROUNDS}  （阳性对照，预期全挂）")
