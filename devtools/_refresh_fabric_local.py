# -*- coding: utf-8 -*-
"""在本机抓一份**新鲜的** NotesFabric 凭据，用掉它做两件事：
  1. 存进本地 data/state.db（下次本地导出就能直接用）
  2. 写一份 `data/_fabric_new.json`，供推送到服务器导入

为什么要走浏览器 profile：NotesFabric 的 `MSAuth1.0 usertoken` 只能由
便笺网页版在页面里生成，OAuth / 设备码都拿不到。本机 `data/onenote_profile`
已经是登录态，所以能直接截到。

**不要把 token 打到终端**（会进 shell 历史 / 日志），只打掩码与长度。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "_fabric_new.json"

exe = sys.executable
print("用解释器:", exe)
r = subprocess.run(
    [exe, "-u", str(ROOT / "capture_owa_auth.py"), "--json",
     "--data-dir", str(DATA)],
    capture_output=True, text=True, timeout=420, cwd=str(ROOT),
    encoding="utf-8", errors="replace")

payload = None
for line in reversed((r.stdout or "").strip().splitlines()):
    line = line.strip()
    if line.startswith("{"):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue

if not payload or not payload.get("authorization"):
    print("✖ 没截到 Authorization")
    print((r.stdout or "")[-400:])
    print((r.stderr or "")[-400:])
    sys.exit(1)

auth = {
    "authorization": payload["authorization"],
    "anchormailbox": payload.get("anchormailbox") or "",
    "sdkversion": payload.get("sdkversion") or "StickyNotes-Web/11.5.10",
}
az = auth["authorization"]
print("来源      :", payload.get("from"))
print("邮箱      :", auth["anchormailbox"])
print("token 长度:", len(az))
print("token 掩码:", az[:34] + "……" + az[-14:])

# 1) 存进本地 store（走应用自己的存取，保证格式一致）
sys.path.insert(0, str(ROOT))
from notesync.store import Store            # noqa: E402
from notesync.notesfabric import FabricAuth  # noqa: E402

store = Store(str(DATA))
FabricAuth(store).save(auth)
print("已写入本地 data/state.db 的 cred.fabric_auth ✅")

# 2) 写一份给服务器导入
OUT.write_text(json.dumps(auth, ensure_ascii=False), encoding="utf-8")
print("已写:", OUT)
print()
print("（该文件在 data/ 里，已被 .gitignore 忽略，不会提交）")
