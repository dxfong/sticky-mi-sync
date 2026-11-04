# -*- coding: utf-8 -*-
"""直接打开线上页面，看用户到底看到什么。

_page_render.py 是**注入桩数据**跑 JS 逻辑，看不到"真实加载"的问题。
这个脚本不注入任何东西：真请求、真接口、真登录态，收集
  · 控制台错误 / 未捕获异常 / 失败的网络请求
  · 页面上到底显示了什么（登录闸门？空列表？错误横幅？）
  · 截图
用法：
    python devtools/_live_check.py [url]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.31.66:8787/"
SHOT = Path(__file__).resolve().parent.parent / "devtools" / "_live_shot.png"

console: list[str] = []
pageerrors: list[str] = []
failed: list[str] = []

with sync_playwright() as pw:
    # 用系统自带的 Edge（`channel="msedge"`）—— 本机没装 playwright 的 chromium，
    # 而 _page_render.py 也是这么起的，保持一致。
    b = pw.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.on("console", lambda m: console.append(f"[{m.type}] {m.text[:200]}"))
    pg.on("pageerror", lambda e: pageerrors.append(str(e)[:300]))
    pg.on("requestfailed",
          lambda r: failed.append(f"{r.method} {r.url[:110]} — {r.failure}"))

    resp = pg.goto(URL, wait_until="domcontentloaded", timeout=25000)
    pg.wait_for_timeout(4000)

    print("=" * 74)
    print("HTTP", resp.status if resp else "?", "|", URL)
    print("=" * 74)

    def vis(sel: str) -> str:
        return pg.evaluate(
            "s => { const e=document.querySelector(s); if(!e) return '(无此元素)';"
            " const r=e.getBoundingClientRect(); const st=getComputedStyle(e);"
            " return (st.display!=='none' && r.height>0) ? '可见' : '隐藏'; }", sel)

    for sel, name in (("#loginGate", "登录闸门"), ("#errBanner", "错误横幅"),
                      ("#logList", "日志列表"), ("#mergedList", "记录列表"),
                      ("#subline", "副标题"), ("#autoStatus", "同步状态")):
        print(f"  {name:<10} {vis(sel)}")

    print()
    print("  副标题内容 :", (pg.inner_text("#subline") or "")[:90])
    print("  错误横幅   :", (pg.inner_text("#errBanner") or "")[:120])
    print("  日志行数   :", pg.eval_on_selector_all("#logList > li", "e=>e.length"))
    print("  记录行数   :", pg.eval_on_selector_all("#mergedList > li", "e=>e.length"))
    print("  登录卡可见 :", pg.evaluate(
        "() => { const e=document.querySelector('#loginGate');"
        " return e ? getComputedStyle(e).display : '?' }"))
    print()
    print("  JS 异常    :", pageerrors or "无")
    print("  控制台     :", [c for c in console if "error" in c.lower()][:5] or "无 error")
    print("  失败请求   :", failed[:6] or "无")

    pg.screenshot(path=str(SHOT), full_page=False)
    print()
    print("  截图:", SHOT)
    b.close()
