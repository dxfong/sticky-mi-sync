# -*- coding: utf-8 -*-
"""端到端验证「标题同步」真的会写。

步骤（可回滚、净效果无变化）：
  1. 把便笺 Subject 故意改成 'PROBE-TMP-XYZ'
  2. 把 link 的 m_title 基线清空（模拟"小米标题刚变化"）
  3. 等一轮同步
  4. 期望：引擎把便笺 Subject 改回小米的标题 f8727aaeb2bbaff2
     （即回到原值 —— 不留任何痕迹）
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/app")
from notesync.store import Store                    # noqa: E402
from notesync.graph import RealGraph, OUTLOOK, _request  # noqa: E402

MI = "52389627160707840"
GOOD = "f8727aaeb2bbaff2"
BAD = "PROBE-TMP-XYZ"

st = Store("/app/data")
g = RealGraph(st)
tok = g.ensure_token()
lk = next((l for l in st.all_links() if str(l.get("mi_id")) == MI), None)
if not lk:
    sys.exit("找不到映射")
gid = lk["graph_id"]


def subject_of():
    code, data = _request("GET", f"{OUTLOOK}/me/notes/{gid}?$select=Subject", tok)
    return (data.get("Subject") or "") if code == 200 else f"<HTTP {code}>"


def patch(sub):
    ck = lk.get("graph_changekey") or ""
    code, data = _request("PATCH", f"{OUTLOOK}/me/notes/{gid}", tok,
                          {"Subject": sub}, {"If-Match": ck} if ck else None)
    return code


print("① 当前便笺 Subject =", repr(subject_of()))
print("② 故意改成", repr(BAD), "→ HTTP", patch(BAD))
print("   读回 =", repr(subject_of()))
print("③ 清空 m_title 基线（模拟小米标题变化）")
st.set_link_titles(MI, subject_of(), "")

print("④ 等 40 秒让引擎跑一轮...")
time.sleep(40)

now = subject_of()
print("   便笺 Subject 现在 =", repr(now))
lm = next((l for l in st.all_links() if str(l.get("mi_id")) == MI), {})
print("   link.m_title 现在 =", repr(lm.get("m_title")))
print("   link.g_title 现在 =", repr(lm.get("g_title")))
print()
if now == GOOD:
    print("✅ 引擎把标题纠回了小米侧的值 —— 标题同步链路端到端可用")
else:
    print("❌ 没纠回来，需要继续查")
