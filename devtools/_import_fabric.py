# -*- coding: utf-8 -*-
"""把 `/tmp/_fabric_new.json` 里的 NotesFabric 凭据导入服务端 data/state.db。

导入后**立刻验证**（发一次真实请求确认 token 能用）——
"保存了"和"能用"是两件事，不验证的话会拿着一份过期令牌对着空列表猜。

只写 cred 表，不动 link / 不触发同步。
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, "/app")
from notesync.store import Store              # noqa: E402
from notesync.notesfabric import FabricAuth   # noqa: E402

SRC = pathlib.Path("/tmp/_fabric_new.json")
if not SRC.exists():
    sys.exit("找不到 /tmp/_fabric_new.json —— 先把凭据文件推上去")

auth = json.loads(SRC.read_text(encoding="utf-8"))
az = auth.get("authorization") or ""
print("待导入 :", auth.get("anchormailbox"), f"token 长度 {len(az)}")
print("掩码   :", az[:34] + "……" + az[-14:])

store = Store("/app/data")
FabricAuth(store).save(auth)
print("已写入 cred.fabric_auth ✅")
print()

# ---- 立刻验证：真发一次请求 ----
print("验证（真实请求，只读）")
from notesync.graph import NotesFabricGraph   # noqa: E402
g = NotesFabricGraph(store)
st = g.status()
print("  connected =", st.get("connected"))
print("  account   =", st.get("account"))
print("  error     =", st.get("error"))
if not st.get("connected"):
    sys.exit("✖ 凭据不可用")

notes = g.list_notes()
print(f"  ✅ fabric 便笺数 = {len(notes)}")

# ---- 顺手比对 id 与 link 表（安全阀，和 _diag_fabric_compare 同一逻辑）----
import sqlite3  # noqa: E402
c = sqlite3.connect("/app/data/state.db")
link_ids = {str(r[0]) for r in c.execute(
    "SELECT graph_id FROM link WHERE graph_id IS NOT NULL")}
fab_ids = {str(n.get("id")) for n in notes}
both = link_ids & fab_ids
print()
print(f"link 里的便笺 id = {len(link_ids)}")
print(f"两边都有         = {len(both)}")
if link_ids:
    rate = len(both) / len(link_ids) * 100
    print(f"一致率           = {rate:.1f}%  → "
          + ("✅ 可以切" if rate >= 95 else
             "⚠ 需要人工确认" if rate >= 50 else "❌ 不要切"))
print()
print("清掉临时文件…")
SRC.unlink(missing_ok=True)
print("完成")
