# -*- coding: utf-8 -*-
"""量 #btnAuto 在几种文案下的宽度，以及它左边元素被推动的距离。

问题：按钮文案会随状态切换，文案长度不同 → 按钮宽度变 →
同一行左侧的 readyBox 被推来推去。所以宽度必须**由 CSS 定死**，
不能让它跟着文案走。
"""
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

HIDE_ERR = """() => {
  ['errBanner', 'mockBanner'].forEach(id => {
    const e = document.getElementById(id);
    if (e) { e.style.display = 'none'; e.textContent = ''; }
  });
}"""

# 真实文案（见 renderSwitch）。注意 2026-10-08 之后「中止本轮 / 正在中止…」
# 已经**不存在**了 —— 自动同步按钮不再兼职中止（开着时点它 = 关掉，
# 而"关掉"本身就会让本轮收尾，同一件事不需要两个入口）。
# 留在表里的是「已暂停 · 恢复」，它是当前**最长**的一条，必须一起量。
CASES = ["自动同步：关", "自动同步：开", "已暂停 · 恢复"]

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    for w in (1280, 390):
        pg = b.new_page(viewport={"width": w, "height": 700})
        pg.set_content(HTML, wait_until="load")
        pg.wait_for_timeout(700)
        pg.evaluate(HIDE_ERR)
        print("=" * 64)
        print(f"视口 {w}px")
        print(f"{'按钮文案':<14}{'btnAuto.x':>11}{'btnAuto.w':>11}{'readyBox.x':>12}")
        xs, ws, rx = [], [], []
        for t in CASES:
            pg.evaluate("t => { document.getElementById('btnAuto').textContent = t }", t)
            pg.wait_for_timeout(50)
            a = pg.query_selector("#btnAuto").bounding_box()
            r = pg.query_selector("#readyBox").bounding_box()
            xs.append(round(a["x"], 2)); ws.append(round(a["width"], 2))
            rx.append(round(r["x"], 2) if r else None)
            print(f"{t:<14}{a['x']:>11.2f}{a['width']:>11.2f}"
                  f"{(r['x'] if r else float('nan')):>12.2f}")
        print(f"→ 按钮宽度差 {max(ws) - min(ws):.2f}px ;"
              f" readyBox 位移 {max(v for v in rx if v is not None) - min(v for v in rx if v is not None):.2f}px")
        pg.close()
    b.close()
