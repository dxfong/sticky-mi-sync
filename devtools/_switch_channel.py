# -*- coding: utf-8 -*-
"""把微软侧通道切到 fabric，并**先把自动同步关掉**（等验证过再开）。

为什么必须先关自动同步：
  切通道后便笺 id 体系若不同，`link` 里的 164 条会全部"对不上"，
  引擎会认为便笺侧整体被删 → 走删除传播。安全阀会拦下整轮，
  但"拦下"不等于"正确" —— 会一直拦、同步彻底停摆。
  所以顺序是：备份 ✅ → 切通道 + 关自动同步 → 粘 token → **只读比对 id**
  → 确认一致再开自动同步。

幂等，可重复跑。打完就打印前后对比。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import time

CFG = pathlib.Path("/app/data/config.json")

before = json.loads(CFG.read_text(encoding="utf-8"))
old_mode = before.get("graph", {}).get("mode")
old_auto = before.get("auto_sync")

# 留一份原配置，便于回滚
bak = CFG.with_suffix(".json.bak-%d" % int(time.time()))
shutil.copy2(CFG, bak)

before.setdefault("graph", {})["mode"] = "fabric"
before["auto_sync"] = False

CFG.write_text(json.dumps(before, ensure_ascii=False, indent=2),
               encoding="utf-8")

after = json.loads(CFG.read_text(encoding="utf-8"))
print("原配置备份 :", bak)
print("graph.mode :", repr(old_mode), "→", repr(after["graph"]["mode"]))
print("auto_sync  :", repr(old_auto), "→", repr(after["auto_sync"]))
print()
print("当前 config.json：")
print(json.dumps(after, ensure_ascii=False, indent=2))
