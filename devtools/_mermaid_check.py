# -*- coding: utf-8 -*-
"""把 README 里的 mermaid 块真正渲染一遍，确认语法无误。

用 CDN 的 mermaid（走代理），渲染成功后截图。
"""
import json
import pathlib
import re

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
md = (ROOT / "README.md").read_text(encoding="utf-8")
blocks = re.findall(r"```mermaid\n(.*?)```", md, re.S)
print("README 里的 mermaid 块数量:", len(blocks))
for i, b in enumerate(blocks, 1):
    print(f"--- 块 {i} ---\n{b}")

TPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{background:#0d1117;color:#c9d1d9;font-family:sans-serif;padding:18px;margin:0}
  .err{color:#f85149;font-family:monospace;white-space:pre-wrap}</style></head>
<body><div id="out"></div>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
mermaid.initialize({startOnLoad:false, theme:'dark'});
const blocks = __BLOCKS__;
const out = document.getElementById('out');
let bad = 0;
for (let i = 0; i < blocks.length; i++) {
  const d = document.createElement('div');
  out.appendChild(d);
  try {
    const {svg} = await mermaid.render('g'+i, blocks[i]);
    d.innerHTML = svg;
  } catch(e) {
    bad++;
    d.innerHTML = '<div class="err">RENDER ERROR: '+ (e && e.message) + '</div>';
  }
}
document.title = bad === 0 ? 'OK' : ('FAIL:' + bad);
</script></body></html>"""

html = TPL.replace("__BLOCKS__", json.dumps(blocks))
SHOT = ROOT / "devtools" / "_shots" / "readme_mermaid.png"
SHOT.parent.mkdir(exist_ok=True)

with sync_playwright() as p:
    b = p.chromium.launch(
        channel="msedge", headless=True,
        proxy={"server": "http://192.168.31.96:4067"},   # CDN 走代理
    )
    pg = b.new_page(viewport={"width": 900, "height": 420})
    pg.set_content(html)
    try:
        pg.wait_for_function("() => document.title.startsWith('OK') "
                             "|| document.title.startsWith('FAIL')", timeout=30000)
    except Exception as e:
        print("等待渲染超时（可能是 CDN 没加载出来）:", str(e)[:120])
    print("页面标题（OK=渲染成功 / FAIL:n=有 n 块失败）:", pg.title())
    body = pg.inner_text("body")
    if "RENDER ERROR" in body:
        print("!!! 渲染报错内容:")
        for line in body.splitlines():
            if "RENDER ERROR" in line:
                print("   ", line[:200])
    pg.screenshot(path=str(SHOT), full_page=True)
    print("截图:", SHOT)
    b.close()
