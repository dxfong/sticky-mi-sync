# -*- coding: utf-8 -*-
"""对照测量：改动前(e3ff822) vs 改动后(工作区) 顶栏按钮的漂移量。

前提：两版都用 set_content 注入（file:// 会被页面自己跳走）。
"""
import pathlib
import subprocess

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHOT = pathlib.Path(__file__).resolve().parent / "_shots"
SHOT.mkdir(exist_ok=True)

OLD_HTML = subprocess.run(
    ["git", "show", "e3ff822:web/index.html"],
    cwd=str(ROOT), capture_output=True, check=True,
).stdout.decode("utf-8")
NEW_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

# 倒计时的取值跨度：秒 → 两位数 → 分:秒 → 无值
COUNTDOWNS = ["4s", "10s", "1:30", "–"]

# --- 旧版：状态散在 #autoHint / #countdown / #syncProg 三个元素上 ---
OLD_SETTER = """c => {
  const h = document.getElementById('autoHint');
  const n = document.getElementById('countdown');
  const p = document.getElementById('syncProg');
  if (h) h.textContent = c.hint;
  if (n) n.textContent = c.cd;
  if (p) p.textContent = c.prog || '';
}"""
OLD_CASES = [{"hint": "运行中", "cd": cd} for cd in COUNTDOWNS]
OLD_CASES += [
    {"hint": "运行中", "cd": "4s", "prog": "同步中…"},
    {"hint": "运行中", "cd": "4s", "prog": "有断点待续"},
    {"hint": "尚不可用", "cd": "已关闭"},
]

# --- 新版：状态全在一个 #autoStatus 上 ---
NEW_SETTER = """t => {
  const el = document.getElementById('autoStatus');
  if (el) el.textContent = t;
}"""
NEW_CASES = [
    " · 运行中 · 下次同步 4s",
    " · 运行中 · 下次同步 10s",
    " · 运行中 · 下次同步 1:30",
    " · 运行中 · 下次同步 –",
    " · 同步中…",
    " · 有断点待续",
    " · 可以打开",
    " · 尚不可用",
    "",
]


def run(pg, html, setter, cases, label):
    pg.set_content(html, wait_until="load")
    pg.wait_for_timeout(600)
    if pg.query_selector("#btnAuto") is None:
        print(f"{label}: #btnAuto 不在 DOM 里，跳过")
        return
    xs = []
    for c in cases:
        pg.evaluate(setter, c)
        pg.wait_for_timeout(50)
        xs.append(round(pg.query_selector("#btnAuto").bounding_box()["x"], 2))
    drift = max(xs) - min(xs)
    print(f"{label}")
    print(f"  按钮 x 取值: {sorted(set(xs))}")
    print(f"  漂移量 = {drift:.2f}px  {'✅' if drift < 0.5 else '❌ 仍会抖'}")
    return drift


with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    # ★ 抖动只在**窄屏**出现：宽屏时 .row 里的 <span flex:1> 吸收全部余量，
    #   按钮被 space-between 钉在右边，怎么变都不动；
    #   窄屏才会触发 .row 的 flex-wrap 换行 —— 那才是排版被挤动的真实场景。
    for w in (390, 768, 1280):
        pg.set_viewport_size({"width": w, "height": 900})
        print("=" * 62)
        print(f"视口宽度 = {w}px")
        d_old = run(pg, OLD_HTML, OLD_SETTER, OLD_CASES, "  【改动前 e3ff822】状态在按钮那一行")
        print("  " + "-" * 58)
        d_new = run(pg, NEW_HTML, NEW_SETTER, NEW_CASES, "  【改动后】状态并入标题下方副标题行")
        if d_old is not None and d_new is not None:
            print(f"  → 漂移 {d_old:.2f}px  →  {d_new:.2f}px")

    # 新版截图
    pg.set_content(NEW_HTML, wait_until="load")
    pg.wait_for_timeout(600)
    pg.evaluate(NEW_SETTER, " · 运行中 · 下次同步 10s")
    pg.wait_for_timeout(100)
    pg.screenshot(path=str(SHOT / "topbar_after.png"),
                  clip={"x": 0, "y": 0, "width": 1280, "height": 105})
    print("截图:", SHOT / "topbar_after.png")
    b.close()
