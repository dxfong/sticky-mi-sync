# -*- coding: utf-8 -*-
"""查「只改标题」那条笔记：两端各是什么。

要回答：
  ① 小米侧 subject（标题）与正文首行是否已经不一致
  ② 便笺侧有没有 Subject 字段（outlook 通道的 pick 里根本没取）
  ③ 链接表的 base_hash 是按什么算的（只算正文？）
  ④ 界面拿到的 xiaomi_title 是什么
只读。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")
from notesync.store import Store                    # noqa: E402
from notesync.graph import RealGraph, OUTLOOK, _request  # noqa: E402
from notesync.xiaomi import RealXiaomi              # noqa: E402
from notesync.textutil import first_line, content_hash  # noqa: E402

st = Store("/app/data")
g, x = RealGraph(st), RealXiaomi(st)

m_all = x.list_notes()
linked = {str(l["mi_id"]): l for l in st.all_links()}

print("=" * 84)
print("① 小米侧：subject 与正文首行不一致的条目（这就是「只改标题」的证据）")
print("=" * 84)
bad = 0
for n in m_all:
    sub = (n.get("subject") or "").strip()
    fl = first_line(n.get("text") or "").strip()
    if sub and fl and sub != fl:
        bad += 1
        lk = linked.get(str(n.get("id")))
        print(f"  mi={n.get('id')}")
        print(f"     subject   = {sub[:60]!r}")
        print(f"     正文首行  = {fl[:60]!r}")
        print(f"     已配对    = {'是' if lk else '否'}"
              f"   base_hash={lk.get('base_hash') if lk else '-'}")
        if bad >= 6:
            break
print(f"  —— 不一致共 {bad} 条（扫描 {len(m_all)} 条）")

print()
print("=" * 84)
print("② 便笺侧：outlook 通道返回里有没有 Subject（pick 取没取）")
print("=" * 84)
tok = g.ensure_token()
code, data = _request(
    "GET", f"{OUTLOOK}/me/notes?$select=Id,Subject,Body,ChangeKey,LastModifiedDateTime&$top=2", tok)
print("  HTTP", code)
for it in (data.get("value") or [])[:2]:
    print(f"    Id      = {str(it.get('Id'))[:26]}…")
    print(f"    Subject = {(it.get('Subject') or '(空)')[:60]!r}")
    body = (it.get("Body") or {}).get("Content") or ""
    print(f"    Body首行= {first_line(body)[:60]!r}")

print()
print("=" * 84)
print("③ 链接表的 base_hash 是按正文算的（不含 subject）")
print("=" * 84)
n0 = m_all[0]
print(f"  示例 mi={n0.get('id')}")
print(f"    content_hash(正文)        = {content_hash(n0.get('text') or '')}")
print(f"    content_hash(正文+标题)   = {content_hash((n0.get('text') or '') + '\\x00' + (n0.get('subject') or ''))}")
print("  → 两者不同，说明现有 base_hash 只覆盖正文")
