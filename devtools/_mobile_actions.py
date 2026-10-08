# -*- coding: utf-8 -*-
"""移动端 / 窄屏下的按钮布局回归。

用户要求（2026-10-08 第一轮）：
    1. 按钮应**等宽**
    2. 按钮**不在同一行时，应右对齐**
    3. 上部「两端已登录…」等文字**不应与按钮处于同一列**（挪到标题那列）
    4. 自动同步按钮不必转换成「中止本轮」

用户要求（2026-10-08 第二轮，**修正了第 1 轮的实现**）：
    按钮应该处于**同一列**且**与标题顶对齐**，而不是分成两列。

用户要求（2026-10-08 第三轮，**最终确认**）：
    「横向空间足够时，所有按钮在**一行内、靠右**，和原来一样；
     当空间被挤压时，按钮可以换成**四行一列、靠右**。」

★ 这里有一次真实的需求误读，值得记下来：
  第 1 轮我把"按钮等宽 + 换行右对齐"实现成了**2 列网格**，
  等宽和右对齐都达标了（0.00px），但用户要的其实是
  **一行一个、竖成一列**。数字全绿 ≠ 需求满足 ——
  「等宽」和「几列」是两件事，不能拿前者推导后者。

所以本脚本按**两种状态**分别断言：
  · 宽屏（>720px）：一行放下全部 4 个按钮、靠右（维持原有外观）
  · 窄屏（≤720px）：单列 4 行、等宽、与标题顶对齐、整体靠右

为什么要测这么多视口：这类问题**只在窄屏暴露**。
之前就吃过亏 —— 在 1280px 下测"按钮会不会被推动"，永远测不出来
（宽屏有 `flex:1` 空档吸收余量）；一到 390px 就原形毕露。
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

FAILS = []


def check(name, ok, detail=""):
    if not ok:
        FAILS.append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


HIDE_ERR = """() => {
  ['errBanner', 'mockBanner'].forEach(id => {
    const e = document.getElementById(id);
    if (e) { e.style.display = 'none'; e.textContent = ''; }
  });
  const rb = document.getElementById('readyBox');
  if (rb) rb.textContent = '✓ 两端已登录且已选定文件夹，可以开启自动同步';
}"""

# 手机竖屏 / 窄桌面窗口 / 平板 / 宽屏。前三个必须吃 @media(<=720px)。
VIEWPORTS = [(360, 740), (390, 844), (430, 932),
             (700, 900), (768, 1024), (1180, 900)]

NARROW = 720     # 断点，和 CSS 里的 @media 保持一致

print("=" * 72)
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)

    for w, h in VIEWPORTS:
        print(f"\n视口 {w}×{h}")
        pg = b.new_page(viewport={"width": w, "height": h})
        pg.set_content(HTML, wait_until="load")
        pg.wait_for_timeout(600)
        pg.evaluate(HIDE_ERR)
        pg.wait_for_timeout(100)

        info = pg.evaluate("""() => {
          const btns = ['btnAcct', 'btnFetch', 'btnPair', 'btnAuto']
            .map(id => document.getElementById(id))
            .filter(Boolean)
            .map(el => {
              const r = el.getBoundingClientRect();
              return {id: el.id, x: r.x, y: r.y, w: r.width, h: r.height,
                      right: r.x + r.width, text: el.textContent};
            });
          const rb = document.getElementById('readyBox').getBoundingClientRect();
          const hl = document.querySelector('h1').getBoundingClientRect();
          const acts = document.querySelector('.actions').getBoundingClientRect();
          return {btns,
                  readyBox: {x: rb.x, y: rb.y, right: rb.x + rb.width},
                  h1: {x: hl.x, y: hl.y, bottom: hl.y + hl.height},
                  actions: {x: acts.x, y: acts.y, w: acts.width,
                            right: acts.x + acts.width},
                  winW: window.innerWidth};
        }""")

        btns = info["btns"]
        rb, acts, h1 = info["readyBox"], info["actions"], info["h1"]
        narrow = w <= NARROW

        # ---- ① 等宽：所有按钮（不分行/列）宽度必须一致 ----
        # ★ 这里改成"全体"而不是"同行" —— 用户要的是**同一列**，
        #   同一列里任何两个按钮都必须等宽，跨行比较才有意义。
        ws = [t["w"] for t in btns]
        spread = max(ws) - min(ws)
        check(f"① 按钮全部等宽（最大差 {spread:.2f}px）", spread <= 1.0,
              " / ".join(f"{v:.0f}" for v in ws))

        # ---- ② 窄屏：必须**单列**（每个按钮占一行，x 全相等）----
        if narrow:
            xs = [t["x"] for t in btns]
            col_spread = max(xs) - min(xs)
            check(f"② 窄屏是单列（x 最大差 {col_spread:.2f}px）",
                  col_spread <= 1.0,
                  "每行一个按钮，对齐同一条左边缘")

            # 顺带确认 y 是**严格递增**的（真的是竖着一列，而不是被网格摊开）
            ys = [t["y"] for t in sorted(btns, key=lambda t: t["y"])]
            gaps = [round(ys[i + 1] - ys[i]) for i in range(len(ys) - 1)]
            check("②b 按钮竖着依次排列", len(ys) == 4 and all(g > 0 for g in gaps),
                  f"y 间距 {gaps}")

        # ---- ③ 按钮组整体贴右 ----
        gap_right = info["winW"] - acts["right"]
        if narrow:
            check(f"③ 按钮组贴住右边缘（离窗口右 {gap_right:.1f}px）",
                  gap_right <= 20)
        else:
            # 宽屏靠 .flexsp 的 space-between 贴右，同样应该几乎无缝
            check(f"③ 宽屏按钮组也贴右（离窗口右 {gap_right:.1f}px）",
                  gap_right <= 20)

        # ---- ④ 宽屏：必须**一行**放下全部按钮 ----
        # 用户明确：「横向空间足够时，所有按钮在一行内、靠右，和原来一样」。
        # 所以宽屏不能因为等宽改造而把按钮挤成两行。
        if not narrow:
            ys = {round(t["y"]) for t in btns}
            check(f"④ 宽屏全部按钮在一行（{len(btns)} 个按钮占 {len(ys)} 种 y）",
                  len(ys) == 1)
            xs = sorted(t["x"] for t in btns)
            check("④b 宽屏按钮按 x 依次排列（真的一行，不是叠在一起）",
                  all(xs[i] < xs[i + 1] for i in range(len(xs) - 1)),
                  " → ".join(f"{v:.0f}" for v in xs))

        # ---- ⑤ 窄屏：与标题**顶对齐** ----
        # 用户要求「按钮应与标题顶对齐」。宽屏不受此约束
        # （宽屏靠 space-between + align-items:center 垂直居中，是原有外观，
        #  用户说"和原来一样"，所以不动它）。
        if narrow:
            rb_top = rb["y"]
            dy = acts["y"] - h1["y"]
            check(f"⑤ 按钮组与标题顶对齐（垂直差 {dy:.1f}px）",
                  abs(dy) <= 8, f"h1.y={h1['y']:.0f} actions.y={acts['y']:.0f}")

        # ---- ⑥ 就绪提示不与按钮抢同一列 ----
        # 判据：readyBox 的右边缘不能越过按钮组的左边缘（否则就是挤在一起）。
        check("⑥ 就绪提示不与按钮同列",
              rb["right"] <= acts["x"] + 1,
              f"readyBox.right={rb['right']:.0f} actions.x={acts['x']:.0f}")

        print("     按钮位置：" + " | ".join(
            f"{t['id']}={t['x']:.0f},{t['y']:.0f} w={t['w']:.0f}"
            for t in btns))
        pg.close()

    b.close()

print()
print("=" * 72)
if FAILS:
    print(f"❌ {len(FAILS)} 项未通过: {FAILS}")
    sys.exit(1)
print("✅ 全部通过")
