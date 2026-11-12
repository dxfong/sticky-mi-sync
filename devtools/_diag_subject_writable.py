# -*- coding: utf-8 -*-
"""验证：便笺（Outlook REST notes）的 Subject 能不能**显式设置**。

为什么必须验：小米的标题在 `extraInfo.title`（独立字段），
而便笺的 Subject 看起来是"正文首行派生的"。如果 Subject 可写，
就能把小米标题同步过去而**不动正文**；如果不可写，只能走"把标题并进正文首行"。

做法（可回滚）：对**用户那条笔记**（mi=52389627160707840）的便笺对应条目：
  1. 记录原 Subject
  2. PATCH {"Subject": "f8727aaeb2bbaff2"}
  3. GET 读回，看 Subject 是否保留、Body 有没有被清空
  4. 如果没保留 → 把 Subject 还原成原值
只改这一个字段，不碰正文。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")
from notesync.store import Store                    # noqa: E402
from notesync.graph import RealGraph, OUTLOOK, _request, _err_text  # noqa: E402

MI = "52389627160707840"
NEW_TITLE = "f8727aaeb2bbaff2"

st = Store("/app/data")
g = RealGraph(st)
tok = g.ensure_token()

links = {str(l["mi_id"]): l for l in st.all_links()}
lk = links.get(MI)
if not lk:
    sys.exit(f"mi={MI} 不在映射表里")
gid = lk["graph_id"]
print("便笺 id =", gid[:40], "…")


def fetch():
    code, data = _request(
        "GET", f"{OUTLOOK}/me/notes/{gid}?$select=Id,Subject,Body,ChangeKey", tok)
    if code != 200:
        raise SystemExit(f"读取失败 {code}：{_err_text(code, data)}")
    body = ((data.get("Body") or {}).get("Content") or "")
    return data.get("Subject") or "", body, data.get("ChangeKey") or ""


sub0, body0, ck0 = fetch()
print(f"\n原 Subject = {sub0[:60]!r}")
print(f"原 Body 长度 = {len(body0)}")

print("\n--- PATCH Subject ---")
code, data = _request("PATCH", f"{OUTLOOK}/me/notes/{gid}", tok,
                      {"Subject": NEW_TITLE},
                      {"If-Match": ck0} if ck0 else None)
print("  HTTP", code, "" if code in (200, 201) else _err_text(code, data)[:160])

import time
time.sleep(1.5)
sub1, body1, ck1 = fetch()
print(f"\n改后 Subject = {sub1[:60]!r}")
print(f"改后 Body 长度 = {len(body1)}  （原 {len(body0)}）")

kept = sub1.strip() == NEW_TITLE
print()
print("=" * 76)
if kept:
    print("✅ Subject **可写**：设置后读回仍是新值")
    print(f"   Body 是否被清空：{'是（危险）' if len(body1) < len(body0) * 0.9 else '否 ✅'}")
    print("   → 方案：小米 extraInfo.title → 便笺 Subject，不动正文")
else:
    print("❌ Subject **不可写**（被服务端重算回原值）")
    print(f"   说明它是「正文首行派生」的 → 想让标题出现在微软，只能把它并进正文首行")
    if sub1.strip() != sub0.strip():
        code, data = _request("PATCH", f"{OUTLOOK}/me/notes/{gid}", tok,
                              {"Subject": sub0}, {"If-Match": ck1} if ck1 else None)
        print(f"   已尝试还原 Subject（HTTP {code}）")
    else:
        print("   值与原值一致，无需还原")
