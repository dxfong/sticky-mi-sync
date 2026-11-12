# -*- coding: utf-8 -*-
"""验证：小米的自定义标题（extraInfo.title）能不能**单独写**。

要写就必须保留 content / modifyDate 原值 —— 只换 extraInfo 里的 title。
如果服务端忽略 extraInfo，标题不会变（我们立刻就能看出来）。

可回滚：改完读回校验，然后把原 extraInfo 写回去。
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "/app")
from notesync.store import Store        # noqa: E402
from notesync.xiaomi import RealXiaomi  # noqa: E402

MI = "52389627160707840"
PROBE = "PROBE-MI-TITLE"

st = Store("/app/data")
x = RealXiaomi(st)


def entry():
    return x._entry(MI)


def post_title(cur: dict, title: str) -> dict:
    """只改 extraInfo.title，content / modifyDate / 其它字段全部原样。"""
    e = dict(cur)
    raw = e.get("extraInfo") or "{}"
    try:
        ei = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        ei = {}
    ei["title"] = title
    e["extraInfo"] = json.dumps(ei, ensure_ascii=False)
    return x._post_form(f"/note/note/{MI}", {
        "entry": json.dumps(e, ensure_ascii=False),
        "serviceToken": x._service_token(),
    })


cur0 = entry()
orig_ei = cur0.get("extraInfo")
orig_md = cur0.get("modifyDate")
orig_len = len(cur0.get("content") or "")


def show(tag: str, e: dict):
    ei = e.get("extraInfo") or ""
    try:
        title = (json.loads(ei) or {}).get("title")
    except Exception:
        title = "?"
    print(f"  {tag}: title={title!r}  modifyDate={e.get('modifyDate')}  "
          f"contentLen={len(e.get('content') or '')}")


print("=" * 70)
print("① 原状")
print("=" * 70)
show("当前", cur0)
print(f"  原 extraInfo = {orig_ei!r}")

print()
print("=" * 70)
print("② 只改 extraInfo.title →", PROBE)
print("=" * 70)
r = post_title(cur0, PROBE)
print("  POST result =", r.get("result"))
time.sleep(1.0)
cur1 = entry()
show("读回", cur1)

ok = False
try:
    ok = (json.loads(cur1.get("extraInfo") or "{}").get("title") == PROBE)
except Exception:
    ok = False

print()
print("=" * 70)
print("③ 还原 extraInfo")
print("=" * 70)
e = dict(entry())
e["extraInfo"] = orig_ei
r2 = x._post_form(f"/note/note/{MI}", {
    "entry": json.dumps(e, ensure_ascii=False),
    "serviceToken": x._service_token(),
})
print("  POST result =", r2.get("result"))
time.sleep(1.0)
cur2 = entry()
show("还原后", cur2)

print()
print("=" * 70)
if ok:
    print("✅ 小米标题**可写**（extraInfo.title 生效）")
    print(f"   content 长度 {orig_len} → {len(cur1.get('content') or '')}"
          f"（{'没变 ✅' if orig_len == len(cur1.get('content') or '') else '变了 ⚠'}）")
    print(f"   modifyDate {orig_md} → {cur1.get('modifyDate')}"
          f"（{'没变 ✅' if str(orig_md) == str(cur1.get('modifyDate')) else '变了 ⚠'}）")
else:
    print("❌ 小米标题不可写（extraInfo 被忽略）→ 只能做单向")
