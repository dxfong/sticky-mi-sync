# -*- coding: utf-8 -*-
"""dump GitHub 端 mermaid 兜底元素里的真实源码 + 控制台错误。"""
import pathlib

from playwright.sync_api import sync_playwright

URL = "https://github.com/dxfong/sticky-mi-sync/blob/main/README.md"
SHOT = pathlib.Path(__file__).resolve().parent / "_shots" / "github_blob.png"

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True,
                          proxy={"server": "http://192.168.31.96:4067"})
    pg = b.new_page(viewport={"width": 1280, "height": 1200})

    logs = []
    pg.on("console", lambda m: logs.append(f"[{m.type}] {m.text[:400]}"))
    pg.on("pageerror", lambda e: logs.append(f"[pageerror] {str(e)[:400]}"))

    pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    for _ in range(10):
        pg.mouse.wheel(0, 3000)
        pg.wait_for_timeout(400)
    pg.wait_for_timeout(7000)

    dumped = pg.evaluate("""() => {
      const nodes = [...document.querySelectorAll('[class*="render-plaintext"], [class*="mermaid"]')];
      return nodes.map(n => ({
        cls: n.className,
        text: (n.textContent || '').slice(0, 800),
        html: (n.innerHTML || '').slice(0, 400),
      }));
    }""")
    print("===== GitHub 端兜底元素 =====")
    for d in dumped:
        print("class:", d["cls"])
        print("textContent >>>")
        print(d["text"])
        print("innerHTML >>>")
        print(d["html"])
        print("-" * 60)

    print()
    print("===== 控制台里与 mermaid / parse / error 有关的 =====")
    hit = [l for l in logs if any(k in l.lower() for k in
                                  ("mermaid", "parse", "error", "syntax", "lexical"))]
    for l in hit[:15]:
        print(" ", l)
    if not hit:
        print("  （没有相关日志，共捕获", len(logs), "条）")

    pg.evaluate("""() => {
      const el = document.querySelector('[class*="render-plaintext"], [class*="mermaid"]');
      if (el) el.scrollIntoView({block: 'center'});
    }""")
    pg.wait_for_timeout(1500)
    pg.screenshot(path=str(SHOT))
    print("截图:", SHOT)
    b.close()
