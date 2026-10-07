# -*- coding: utf-8 -*-
"""对照组：拿一个已知带 mermaid 图的公开仓库，看它能不能渲染。
   能渲染 => 我的网络没问题，问题在语法
   不能渲染 => 我的网络把 GitHub 的 mermaid 脚本挡了，本轮观测作废
"""
import pathlib

from playwright.sync_api import sync_playwright

SHOT = pathlib.Path(__file__).resolve().parent / "_shots"

TARGETS = [
    ("对照（mermaid 官方仓库）", "https://github.com/mermaid-js/mermaid"),
    ("我的探针页",             "https://github.com/dxfong/sticky-mi-sync/blob/main/docs/_mermaid_probe.md"),
]

PROBE = """() => {
  const svgs = document.querySelectorAll(
    'svg[aria-roledescription="flowchart-v2"], svg[id^="mermaid"], .mermaid svg').length;
  const fails = document.querySelectorAll('[class*="render-plaintext"]').length;
  const scripts = [...document.querySelectorAll('script[src]')]
    .map(s => s.src).filter(s => /mermaid|githubassets/.test(s)).length;
  return {svgs, fails, scriptCount: scripts,
          err: document.body.innerText.includes('Unable to render rich display')};
}"""

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True,
                          proxy={"server": "http://192.168.31.96:4067"})
    for name, url in TARGETS:
        pg = b.new_page(viewport={"width": 1280, "height": 1200})
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=90000)
            for _ in range(10):
                pg.mouse.wheel(0, 2500)
                pg.wait_for_timeout(350)
            pg.wait_for_timeout(9000)
            r = pg.evaluate(PROBE)
            print(f"{name}")
            print(f"   渲染成功的 svg = {r['svgs']} | 失败兜底 = {r['fails']} "
                  f"| script 数 = {r['scriptCount']} | 页面含错误提示 = {r['err']}")
        except Exception as e:
            print(f"{name}: 打开失败 {str(e)[:120]}")
        pg.close()
    b.close()
