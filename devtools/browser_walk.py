#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟浏览器操作，跑一遍新用户首次使用的完整流程。

为什么要有这个：API 级测试能证明接口通，**证明不了"人点得下去"** ——
按钮是不是灰的、点完有没有反馈、文字看不看得懂、布局有没有崩，
这些只有真开一个浏览器才知道。用户明确要求过"最好是模拟浏览器操作测试"。

用法：
    python devtools/browser_walk.py                      # 默认打 127.0.0.1:8787
    python devtools/browser_walk.py --base http://192.168.31.66:8787
    python devtools/browser_walk.py --password xxx       # 已设过密码时

用系统已装的 Edge（channel="msedge"），不额外下载 Chromium。
截图存到 devtools/_shots/。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "devtools" / "_shots"

PASSED: list[str] = []
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> bool:
    (PASSED if cond else FAILED).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("\n         " + str(detail)[:200]) if detail else ""))
    return cond


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--password", default="walker-test-123")
    ap.add_argument("--shot", action="store_true", default=True)
    args = ap.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)

        print("=" * 62)
        print("浏览器走查：模拟新用户首次使用")
        print(f"  目标: {args.base}")
        print("=" * 62)

        # ---- 1. 打开
        print("\n[1] 打开页面")
        page.goto(args.base, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        title = page.title()
        ok("页面标题正确", "便笺" in title or "sync" in title.lower(), title)
        page.screenshot(path=str(SHOTS / "01-打开.png"))

        # ---- 2. 登录闸门
        print("\n[2] 登录闸门")
        gate = page.locator("#loginGate")
        gate_visible = gate.is_visible()
        ok("显示登录闸门", gate_visible)
        body_text = page.inner_text("#gateBody") if gate_visible else ""
        ok("闸门文案可读", ("密码" in body_text), body_text[:100].replace("\n", " "))

        pw_input = page.locator("#gatePw")
        if pw_input.count() and pw_input.is_visible():
            # 有 gatePw2 说明是"首次设置"
            setup_mode = page.locator("#gatePw2").count() > 0
            print(f"        模式: {'首次设置密码' if setup_mode else '登录'}")
            pw_input.fill(args.password)
            if setup_mode:
                page.locator("#gatePw2").fill(args.password)
            page.screenshot(path=str(SHOTS / "02-闸门填写.png"))
            page.locator("#gateGo").click()
            page.wait_for_timeout(3000)

        ok("登录后闸门消失", not page.locator("#loginGate").is_visible())
        page.screenshot(path=str(SHOTS / "03-进入主界面.png"))

        # ---- 3. 主界面元素
        print("\n[3] 主界面")
        for sel, label in [
            ("#btnAuto", "自动同步按钮"),
            ("#btnFetch", "只读拉取按钮"),
            ("#btnAcct", "账号与登录按钮"),
            ("#readyBox", "就绪提示区"),
            ("#subline", "状态副标题"),
        ]:
            cnt = page.locator(sel).count()
            ok(f"{label}存在", cnt > 0, f"{sel} count={cnt}")

        sub = page.inner_text("#subline")
        ok("副标题显示映射数", "映射" in sub, sub)
        acct_text = page.inner_text("#btnAcct")
        ok("账号按钮有文字", bool(acct_text.strip()), acct_text)

        # ---- 4. 账号面板开合
        print("\n[4] 账号与登录面板")
        panel = page.locator("#acctPanel")
        open_before = "open" in (panel.get_attribute("class") or "")
        print(f"        初始状态: {'展开' if open_before else '收起'}")
        page.locator("#btnAcct").click()
        page.wait_for_timeout(700)
        open_after = "open" in (panel.get_attribute("class") or "")
        ok("点击后开合状态翻转", open_after != open_before,
           f"{open_before} -> {open_after}")
        acct_after = page.inner_text("#btnAcct")
        ok("按钮箭头跟随状态", ("▴" in acct_after) or ("▾" in acct_after), acct_after)
        page.screenshot(path=str(SHOTS / "04-账号面板.png"))

        # 确保展开，看两张卡片
        if not open_after:
            page.locator("#btnAcct").click()
            page.wait_for_timeout(600)
        gtext = page.inner_text("#gLoginBox")
        xtext = page.inner_text("#xLoginBox")
        print(f"        微软卡片: {gtext[:70].replace(chr(10),' ')}")
        print(f"        小米卡片: {xtext[:70].replace(chr(10),' ')}")

        # ---- 4b. 小米账号密码登录区（容器里唯一能完整走通的登录方式）
        print("\n[4b] 小米账号密码登录区")
        for sel, label in [("#xPwUser", "账号输入框"), ("#xPwPass", "密码输入框"),
                           ("#xPwGo", "登录按钮"), ("#xPwMsg", "提示区")]:
            ok(f"有{label}", page.locator(sel).count() > 0, sel)
        ok("默认隐藏图形验证码区", not page.locator("#xPwCaptcha").is_visible())
        ok("默认隐藏二次验证区", not page.locator("#xPwVerify").is_visible())
        ok("密码框是 password 类型",
           page.locator("#xPwPass").get_attribute("type") == "password")

        # 空提交应给出提示而不是静默
        page.locator("#xPwGo").click()
        page.wait_for_timeout(500)
        m = page.inner_text("#xPwMsg")
        ok("空提交给出提示", bool(m.strip()), m[:80])
        page.screenshot(path=str(SHOTS / "04b-小米密码登录区.png"))

        # ---- 5. 列表区
        print("\n[5] 记录列表")
        rows = page.locator("#mergedList li")
        n = rows.count()
        ok("列表容器存在", page.locator("#mergedList").count() > 0)
        print(f"        当前行数: {n}")
        rc = page.inner_text("#rowCount") if page.locator("#rowCount").count() else ""
        print(f"        行数标签: {rc.strip()}")

        # ---- 5b. 「按内容配对」按钮（两侧各自已有数据时的一次性整理）
        print("\n[5b] 配对相关按钮")
        ok("按内容配对按钮存在", page.locator("#btnPair").count() > 0)
        tip = page.locator("#btnPair").get_attribute("title") or ""
        ok("按内容配对有说明性提示", "未配对" in tip or "内容相同" in tip, tip[:70])
        ok("配对选中按钮存在", page.locator("#btnPairPicked").count() > 0)
        tip2 = page.locator("#btnPairPicked").get_attribute("title") or ""
        ok("配对选中提示了「1 条便笺 + 1 条小米」", "1 条便笺" in tip2, tip2[:70])

        # ---- 6. 设置区
        print("\n[6] 设置区")
        ok("设置默认收起", not page.locator("#cfgBox").is_visible())
        page.locator("#cfgToggle").click()
        page.wait_for_timeout(500)
        ok("点开展开设置", page.locator("#cfgBox").is_visible())
        ok("有间隔输入框", page.locator("#interval").count() > 0)
        ok("有保存按钮", page.locator("#btnSaveCfg").count() > 0)
        # 三个下拉都要在，而且选项数要对
        for sel, name, want in [
            ("#conflict", "冲突策略", 2),
            ("#pairing", "首次配对", 2),
            ("#diffPref", "细微差异以哪侧为准", 3),
        ]:
            n = page.locator(f"{sel} option").count()
            ok(f"{name} 有 {want} 个选项", n == want, f"实际 {n} 个")
        ok("预演模式已移除", page.locator("#dryToggle").count() == 0)
        page.screenshot(path=str(SHOTS / "05-设置展开.png"))

        # ---- 7. 回收站 & 日志
        print("\n[7] 回收站 / 备份")
        ok("回收站存在", page.locator("#trashToggle").count() > 0)
        ok("备份按钮存在", page.locator("#btnBackup").count() > 0)

        # ---- 8. JS 错误
        print("\n[8] 浏览器控制台")
        # favicon 的 404 不算问题（浏览器自动请求，缺失不影响任何功能）
        real_errors = [e for e in errors
                       if "favicon" not in e.lower()
                       and "404 (Not Found)" not in e]
        ok("没有 JS 报错", not real_errors,
           "\n         ".join(real_errors[:4]) if real_errors else "")

        page.screenshot(path=str(SHOTS / "06-最终.png"), full_page=True)
        browser.close()

    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for f in FAILED:
            print("  -", f)
        return 1
    print("✅ 浏览器走查全过")
    print(f"   截图在 {SHOTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
