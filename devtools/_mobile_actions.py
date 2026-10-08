# -*- coding: utf-8 -*-
"""移动端 / 窄屏下的按钮布局回归。

用户要求（2026-10-08）：
    1. 按钮应**等宽**
    2. 按钮**不在同一行时，应右对齐**
    3. 上部「两端已登录…」等文字**不应与按钮处于同一列**（挪到标题那列）
    4. 自动同步按钮不必转换成「中止本轮」

这份脚本把前三条变成可测的数字。第 4 条由 check_js + 代码断言保证。

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
}"""

# 手机竖屏 / 手机横屏 / 窄桌面窗口 / 平板 / 宽屏。前三个必须吃 @media(<=720px)。
VIEWPORTS = [(360, 740), (390, 844), (430, 932), (700, 900),
             (768, 1024), (1180, 900)]

print("=" * 72)
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)

    for w, h in VIEWPORTS:
        print(f"\n视口 {w}×{h}")
        pg = b.new_page(viewport={"width": w, "height": h})
        pg.set_content(HTML, wait_until="load")
        pg.wait_for_timeout(600)
        pg.evaluate(HIDE_ERR)

        info = pg.evaluate("""() => {
          const btns = ['btnAcct', 'btnFetch', 'btnPair', 'btnAuto']
            .map(id => document.getElementById(id))
            .filter(Boolean)
            .map(el => {
              const r = el.getBoundingClientRect();
              return {id: el.id, x: r.x, y: r.y, w: r.width, h: r.height,
                      text: el.textContent};
            });
          const rb = document.getElementById('readyBox').getBoundingClientRect();
          const acts = document.querySelector('.actions').getBoundingClientRect();
          return {btns, readyBox: {x: rb.x, y: rb.y, w: rb.width,
                                   right: rb.x + rb.width},
                  actions: {x: acts.x, y: acts.y, w: acts.width,
                            right: acts.x + acts.width},
                  winW: window.innerWidth};
        }""")

        btns = info["btns"]
        rb = info["readyBox"]
        acts = info["actions"]

        # ---- ① 等宽：同一行（y 相同）的按钮宽度差 ----
        # ★ 按 y 分组要用**容差**，不能直接用 round()：
        #   实测同一行的按钮 y 会是 94 和 95（亚像素/抗锯齿），
        #   round 之后被拆成两"行"，于是"等宽"和"右对齐"两项都误判。
        by_row = []
        for t in sorted(btns, key=lambda v: (v["y"], v["x"])):
            if by_row and abs(t["y"] - by_row[-1][0]["y"]) <= 3:
                by_row[-1].append(t)
            else:
                by_row.append([t])
        max_row_spread = 0.0
        for row in by_row:
            if len(row) > 1:
                ws = [t["w"] for t in row]
                max_row_spread = max(max_row_spread, max(ws) - min(ws))
        check(f"① 同行按钮等宽（最大差 {max_row_spread:.2f}px）",
              max_row_spread <= 1.0)

        # ---- ② 换行时右对齐 ----
        if len(by_row) > 1:
            # 每一行的右边缘都应贴近按钮组的右边缘
            worst = 0.0
            for row in by_row:
                r_right = max(t["x"] + t["w"] for t in row)
                worst = max(worst, acts["right"] - r_right)
            check(f"② 换行后每行右对齐（最大离右边 {worst:.2f}px）",
                  worst <= 2.0,
                  f"{len(by_row)} 行")
        else:
            print(f"  ·       未换行（{len(by_row)} 行），跳过右对齐检查")

        # ---- ③ 就绪提示不与按钮同列 ----
        # 判据：readyBox 的右边缘必须在按钮组左边缘的**左边**（宽屏），
        #       或者整行落在按钮组**上方**（窄屏竖排）。
        # 两者都不满足 = 它和按钮挤在同一列（用户截图里的问题）。
        same_col = (rb["right"] > acts["x"] + 1) and (rb["y"] > acts["y"] - 1)
        check("③ 就绪提示不与按钮同列",
              not same_col,
              f"readyBox.right={rb['right']:.0f} actions.x={acts['x']:.0f} "
              f"y: {rb['y']:.0f} vs {acts['y']:.0f}")

        # ---- ④ 按钮组铺满可用宽度（右对齐的前提）----
        # 窄屏竖排时按钮组应该和容器同宽，这样"右对齐"才有参照系。
        if w <= 720:
            gap_right = info["winW"] - acts["right"]
            check(f"④ 按钮组贴住右边缘（离窗口右 {gap_right:.1f}px）",
                  gap_right <= 20)

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
