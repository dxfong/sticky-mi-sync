# -*- coding: utf-8 -*-
"""定位 Mermaid 在 GitHub 上渲染失败的那一行语法（每个变体独立页面，互不干扰）。"""
import json
import pathlib

from playwright.sync_api import sync_playwright

SHOT = pathlib.Path(__file__).resolve().parent / "_shots"

NODES = '''    W["Windows<br/>便笺"]
    MS["微软便笺云端<br/>（Exchange 笔记）"]
    N["iPhone / iPad / Mac<br/>备忘录"]
    SV["sticky-mi-sync<br/>（你的 NAS / 软路由）"]
    MI["小米笔记云端"]
    M["Android<br/>小米笔记"]
'''

CASES = [
    ("1 当前写法: 双向箭头 + 带引号边标签", '''flowchart LR
    W["Windows<br/>便笺"] <--> MS["微软便笺云端<br/>（Exchange 笔记）"]
    N["iPhone / iPad / Mac<br/>备忘录"] <-->|"加 hotmail 账号"| MS
    MS <--> SV["sticky-mi-sync<br/>（你的 NAS / 软路由）"]
    SV <--> MI["小米笔记云端"]
    MI <--> M["Android<br/>小米笔记"]'''),

    ("2 双向箭头 无标签", '''flowchart LR
    W["Windows<br/>便笺"] <--> MS["微软便笺云端<br/>（Exchange 笔记）"]
    N["iPhone / iPad / Mac<br/>备忘录"] <--> MS
    MS <--> SV["sticky-mi-sync<br/>（你的 NAS / 软路由）"]
    SV <--> MI["小米笔记云端"]
    MI <--> M["Android<br/>小米笔记"]'''),

    ("3 双向箭头 无引号边标签", '''flowchart LR
    W["Windows<br/>便笺"] <--> MS["微软便笺云端<br/>（Exchange 笔记）"]
    N["iPhone / iPad / Mac<br/>备忘录"] <-->|加 hotmail 账号| MS
    MS <--> SV["sticky-mi-sync<br/>（你的 NAS / 软路由）"]
    SV <--> MI["小米笔记云端"]
    MI <--> M["Android<br/>小米笔记"]'''),

    ("4 单向箭头 无引号边标签", '''flowchart LR
    W["Windows<br/>便笺"] --> MS["微软便笺云端<br/>（Exchange 笔记）"]
    N["iPhone / iPad / Mac<br/>备忘录"] -->|加 hotmail 账号| MS
    MS --> SV["sticky-mi-sync<br/>（你的 NAS / 软路由）"]
    SV --> MI["小米笔记云端"]
    MI --> M["Android<br/>小米笔记"]'''),

    ("5 双向箭头 节点先声明", '''flowchart LR
''' + NODES + '''    W <--> MS
    N <--> MS
    MS <--> SV
    SV <--> MI
    MI <--> M'''),

    ("6 无箭头纯连线", '''flowchart LR
''' + NODES + '''    W --- MS
    N --- MS
    MS --- SV
    SV --- MI
    MI --- M'''),

    ("7 单个 br 标签", 'flowchart LR\n    A["Windows<br/>便笺"] --> B["云端"]'),

    ("8 单个 带引号边标签", 'flowchart LR\n    A["甲"] -->|"加 hotmail 账号"| B["乙"]'),

    ("9 单个 无引号边标签", 'flowchart LR\n    A["甲"] -->|加 hotmail 账号| B["乙"]'),

    ("10 单个 双向箭头", 'flowchart LR\n    A["甲"] <--> B["乙"]'),
]

TPL = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div id="out">pending</div>
<script type="module">
const out = document.getElementById('out');
try {
  const m = await import('https://cdn.jsdelivr.net/npm/mermaid@__VER__/dist/mermaid.esm.min.mjs');
  const mermaid = m.default;
  mermaid.initialize({startOnLoad: false, securityLevel: 'strict'});
  const code = __CODE__;
  try {
    await mermaid.parse(code);
    out.textContent = 'OK';
  } catch (e) {
    out.textContent = 'FAIL: ' + String(e && e.message || e).replace(/\\s+/g, ' ').slice(0, 130);
  }
} catch (e) {
  out.textContent = 'IMPORT_FAIL: ' + String(e).slice(0, 100);
}
</script></body></html>"""

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True,
                          proxy={"server": "http://192.168.31.96:4067"})
    for ver in ("10", "11"):
        print(f"================ mermaid@{ver}  securityLevel=strict ================")
        for name, code in CASES:
            html = TPL.replace("__VER__", ver).replace("__CODE__", json.dumps(code))
            pg = b.new_page()
            try:
                pg.set_content(html, timeout=20000)
                pg.wait_for_function("() => document.getElementById('out')"
                                     ".textContent !== 'pending'", timeout=25000)
                res = pg.inner_text("#out")
            except Exception:
                res = "⚠ 超时/无响应（解析器卡住）"
            print(f"  {name:<34} {res}")
            pg.close()
        print()
    b.close()
