# -*- coding: utf-8 -*-
"""回滚到用户之前的可用配置：graph.mode=real + auto_sync=true。

为什么要回滚：
  · fabric 已经验证在这台服务器上跑不通（和 outlook 一样，0/3 全失败）
  · 用户原配置是 real + auto_sync=true，链路恢复后应当自动继续
    （非致命错误不暂停，这是既有的设计）
  · 我不该把一个"切换失败"的中间状态留在服务器上

幂等。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import time

CFG = pathlib.Path("/app/data/config.json")
before = json.loads(CFG.read_text(encoding="utf-8"))

bak = CFG.with_suffix(".json.bak-%d" % int(time.time()))
shutil.copy2(CFG, bak)

old_mode = before.get("graph", {}).get("mode")
old_auto = before.get("auto_sync")
before.setdefault("graph", {})["mode"] = "real"
before["auto_sync"] = True
CFG.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")

print("原配置备份 :", bak)
print("graph.mode :", repr(old_mode), "→ 'real'")
print("auto_sync  :", repr(old_auto), "→ True")
