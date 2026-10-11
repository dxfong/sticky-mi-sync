# -*- coding: utf-8 -*-
"""打印 outlook 请求的真实 HTTP 状态码与错误体。

上一步（`_diag_pagesize.py`）所有 `$top` 都是 HTTPError 且字节数为 0，
但没打印状态码 —— 401 / 403 / 429 / 503 的修法完全不同，必须看清。
只读。
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

OUTLOOK = "https://outlook.office.com/api/v2.0"
SEL = "Id,Subject,Body,ChangeKey,LastModifiedDateTime,IsDeleted"

store = Store("/app/data")
g = RealGraph(store)

print("=== 库里的 graph_token ===")
tok = store.get_cred("graph_token", {}) or {}
print("  keys        :", list(tok.keys()))
exp = int(tok.get("expires_at") or 0)
print("  expires_at  :", exp, "→",
      time.strftime("%m-%d %H:%M:%S", time.localtime(exp)),
      "(现在", time.strftime("%H:%M:%S"), ")")
print("  已过期      :", exp < time.time())
print("  access 长度 :", len(str(tok.get("access_token") or "")))
print()

print("=== ensure_token() ===")
try:
    t = g.ensure_token()
    print("  返回长度:", len(t or ""))
    print("  与库里一致:", (t or "") == str(tok.get("access_token") or ""))
except Exception as e:
    print("  失败:", type(e).__name__, str(e)[:200])
    sys.exit(1)
print()

print("=== 通道探测 ===")
try:
    print("  channel =", repr(g.status().get("channel")))
except Exception as e:
    print("  探测失败:", type(e).__name__, str(e)[:150])
print()

print("=== 实际请求（打印状态码与错误体）===")
for top in (100, 10):
    url = f"{OUTLOOK}/me/notes?$select={SEL}&$top={top}"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + g.ensure_token(),
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        print(f"  $top={top:<4} HTTP {r.status}  {len(raw)} 字节  ✅")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        print(f"  $top={top:<4} HTTP {e.code}  ❌  响应体: {body[:300]}")
    except Exception as e:
        print(f"  $top={top:<4} {type(e).__name__}: {str(e)[:200]}")
    time.sleep(0.5)
