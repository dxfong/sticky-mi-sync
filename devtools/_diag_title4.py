# -*- coding: utf-8 -*-
"""精确核对：小米笔记的"标题"到底存在哪个字段、正文里有没有它。只读。

上一轮我判成「标题就是正文第一行」，依据是 `content` 前 160 字里出现了标题文本。
但那只打印了一行，可能是我截断/转义看漏了。这次逐字段量清楚：
  · content 的长度、repr（前 200 字，保留原生转义）
  · subject / snippet / extraInfo 分别是什么
  · 正文里到底有没有出现标题那段文字
对 3 类样本各取一例：
  A. 用户改过标题那条（folderId=0，不在同步范围）
  B. 同步范围内「subject 为空」的（3 条）
  C. 同步范围内「subject 非空」的（162 条）
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")
from notesync.store import Store                  # noqa: E402
from notesync.xiaomi import RealXiaomi            # noqa: E402
from notesync.textutil import mi_content_to_text  # noqa: E402

USER_NOTE = "51115357469950656"

st = Store("/app/data")
x = RealXiaomi(st)


def dump_detail(nid: str, title: str):
    print("=" * 86)
    print(f"{title}   id={nid}")
    print("=" * 86)
    d = x._get(f"/note/note/{nid}/")
    data = (d or {}).get("data") or {}
    ent = data.get("entry") if isinstance(data, dict) else None
    if ent is None:
        print("  取不到 entry，顶层键 =", list((d or {}).keys()))
        return
    c = ent.get("content") or ""
    print(f"  len(content) = {len(c)}")
    print(f"  content[:200] repr = {c[:200]!r}")
    print(f"  subject      = {ent.get('subject')!r}")
    print(f"  snippet      = {(ent.get('snippet') or '')[:80]!r}")
    print(f"  extraInfo    = {ent.get('extraInfo')!r}")
    sub = (ent.get("subject") or "").strip()
    if sub:
        print(f"  正文里是否出现 subject？ {sub in c}")
    txt = mi_content_to_text(c)
    print(f"  → mi_content_to_text 结果前 120 字：{txt[:120]!r}")
    print(f"  → 结果里是否含 subject？ {bool(sub) and sub in txt}")
    # 有没有 <title> 之类的标签
    import re
    tags = re.findall(r"<([a-zA-Z]+)[\s>]", c[:400])
    print(f"  content 前 400 字里出现的标签：{sorted(set(tags))[:8]}")
    print()


dump_detail(USER_NOTE, "A. 用户改过标题那条（未分类）")

ns = x.list_notes()
empty = [n for n in ns if not (n.get("subject") or "").strip()]
full = [n for n in ns if (n.get("subject") or "").strip()]
print(f"范围内 subject 为空 {len(empty)} 条 / 非空 {len(full)} 条\n")
if empty:
    dump_detail(str(empty[0]["id"]), "B. 范围内 subject 为空（拿它当标题）")
if full:
    dump_detail(str(full[0]["id"]), "C. 范围内 subject 非空")
