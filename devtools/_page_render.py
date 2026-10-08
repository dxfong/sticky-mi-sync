# -*- coding: utf-8 -*-
"""静态页面渲染回归 —— 抓「语法没问题、一跑就抛异常」的 JS bug。

为什么必须单独有这一个
----------------------
`check_js.py`   只做**语法**检查；
`_btn_width.py` / `_mobile_actions.py` 只量**布局**。
它们都发现不了 `busy is not defined` 这种**引用错误** ——
因为 `busy` 在语法上完全合法（看着像个全局变量），Node 的 `--check` 也放行。

2026-10-08 就这么炸过一次（用户截图反馈「打开页面就停在这个状态，
记录也没了、日志也没了」）：
  删「中止本轮」时顺手删了 `renderSwitch()` 里的 `const busy = ...`，
  但下面写状态文字那行还在用它 →
  `ReferenceError: busy is not defined` → `refresh()` 在 **renderSwitch 处中断** →
  **它之后的所有区域全部空着**（日志列表、记录列表一个都不渲染）。
  因为副标题在 renderSwitch 之前写，所以标题照样显示 —— 界面看起来"活着但空"，
  非常具有迷惑性。

本脚本的做法
------------
注入**真实的** index.html，把 `window.fetch` 换成桩数据，
主动调 `startApp()` 跑一次 refresh，然后断言**每个面板都真的渲染出了内容**，
并且全程没有 JS 异常 / 未处理的 Promise 拒绝。

**跑多个状态场景**（关键）：同一个 renderSwitch 里有多条分支，
只测"就绪 + 空闲"这一种，别的分支里的引用错误照样漏掉。
所以下面 SCENARIOS 逐个走：空闲 / 正在跑 / 被自动暂停 / 未就绪 / 已配对未反查。

用法
----
    python devtools/_page_render.py                  # 测当前工作区的 index.html
    python devtools/_page_render.py --rev db194a2    # 测某个历史版本
                                                     # （用于验证本脚本确实抓得住）

退出码 0 = 全过；非 0 = 有失败项。
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHOTS = ROOT / "devtools" / "_shots"

PASSED: list[str] = []
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> bool:
    (PASSED if cond else FAILED).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  —— " + str(detail)[:300]) if detail else ""))
    return cond


NOW = int(time.time())
_H = "a" * 40   # 两侧一致的 content_hash（双绿灯）

# ---- 桩数据：形状严格照抄后端真实返回（字段名错一个字就会渲染成空白） ----
BASE_STATE = {
    "ok": True,
    "now": NOW,
    "auto_sync": False,
    "busy": False,
    "aborting": False,
    "resume_pending": False,
    "paused": {},
    "next_sync_at": NOW + 5,
    "last_ok_at": NOW - 20,
    "last_fail_at": 0,
    "last_fail_msg": "",
    "counts": {"links": 164, "conflicts": 0},
    "readiness": {"ready": True, "blockers": []},
    "graph": {
        "mode": "real", "channel": "fabric", "logged_in": True,
        "account": "dxfong@hotmail.com", "folder": "便笺",
        "configured": True, "connected": True,
    },
    "xiaomi": {
        "mode": "real", "logged_in": True, "account": "315436288",
        "folder": "便笺", "folder_id": "folder-1",
        "write_enabled": True, "connected": True,
    },
    "config": {
        "sync_interval_sec": 5,
        "initial_pairing": "graph_authoritative",
        "diff_pref": "",
        "conflict_policy": "keep_both",
        "auto_sync": False,
    },
    "logs": [
        {"ts": NOW - 30, "level": "info", "msg": "同步完成：0 个动作"},
        {"ts": NOW - 60, "level": "warn",
         "msg": "拉取微软便笺列表遇到网络抖动，2 秒后重试…"},
        {"ts": NOW - 90, "level": "info",
         "msg": "自动拉取列表：便笺 164 条 / 小米 164 条"},
    ],
    "snapshot_dirty": False,
}

NOTES = {
    "ok": True,
    "counts": {"total": 2, "graph": 2, "xiaomi": 2, "merged": 2},
    "rows": [
        {
            "key": "link:1001", "linked": True, "conflict": False,
            "graph": {"title": "测试笔记 A", "chars": 12, "hash": _H,
                      "time": "2026-10-08T01:00:00Z"},
            "xiaomi": {"title": "测试笔记 A", "chars": 12, "hash": _H,
                       "time": "2026-10-08T01:00:00Z"},
        },
        {
            "key": "link:1002", "linked": True, "conflict": False,
            "graph": {"title": "一条比较长的标题用来验证列表不会被撑破",
                      "chars": 88, "hash": "b" * 40,
                      "time": "2026-10-08T02:00:00Z"},
            "xiaomi": {"title": "一条比较长的标题用来验证列表不会被撑破",
                       "chars": 88, "hash": "b" * 40,
                       "time": "2026-10-08T02:00:00Z"},
        },
    ],
}


def scenario(name, *, expect_btn, **patch):
    """造一个状态场景。patch 直接盖在 BASE_STATE 上（支持嵌套 dict 合并）。"""
    st = copy.deepcopy(BASE_STATE)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(st.get(k), dict):
            st[k].update(v)
        else:
            st[k] = v
    return {"name": name, "state": st, "expect_btn": expect_btn}


SCENARIOS = [
    scenario("空闲 · 就绪", expect_btn="自动同步：关"),
    scenario("正在跑一轮", expect_btn="自动同步：开",
             auto_sync=True, busy=True),
    # ★ 被服务自己暂停 —— 这条分支里有 Math.round / 字符串拼接，单独走一遍
    scenario("被自动暂停", expect_btn="已暂停 · 恢复",
             auto_sync=False,
             paused={"at": NOW - 600, "every_sec": 300,
                     "next_probe_at": NOW + 240,
                     "reason": "凭据已失效，需要重新登录"}),
    # ★ 未就绪 —— readyBox 走 blocker 拼接分支，按钮应置灰
    scenario("未就绪（缺文件夹）", expect_btn="自动同步：关",
             readiness={"ready": False, "blockers": ["小米：未选定文件夹"]},
             xiaomi={"folder": "", "folder_id": "", "logged_in": True}),
    # ★ 有断点待续 —— autoStatus 走 resume_pending 分支
    scenario("有断点待续", expect_btn="自动同步：关", resume_pending=True),
    # ★ 有冲突计数 —— 副标题走 ` · 冲突 N` 拼接分支
    scenario("有冲突计数", expect_btn="自动同步：开", auto_sync=True,
             counts={"links": 164, "conflicts": 3}),
    # ★ 一端未登录 —— 按钮文案走"⚠ X未登录"分支（renderLoginBoxes）
    scenario("小米未登录", expect_btn="自动同步：关",
             readiness={"ready": False, "blockers": ["小米笔记：未登录"]},
             xiaomi={"logged_in": False, "folder": "", "folder_id": ""}),
]


def make_stub(state: dict, notes: dict) -> str:
    return """
<script>
// 把 fetch 换成桩 —— 必须在页面自己的 <script> 之前跑，所以插在 <head> 后面。
window.__errors = [];
window.addEventListener('error', function (e) {
  window.__errors.push('error: ' + (e.message || e.type));
});
window.addEventListener('unhandledrejection', function (e) {
  var r = e.reason;
  window.__errors.push('rejection: ' + ((r && (r.message || r.stack)) || String(r)));
});
var __RES = %s;
window.fetch = function (url, opt) {
  var p = String(url).split('?')[0];
  var hit = null;
  for (var k in __RES) { if (p.slice(-k.length) === k) { hit = k; break; } }
  var body = hit ? __RES[hit] : { ok: true };
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' }
  }));
};
</script>
""" % json.dumps({"/api/state": state, "/api/notes": notes},
                 ensure_ascii=False)


def load_html(rev: str | None) -> str:
    if not rev:
        return (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    out = subprocess.run(["git", "show", f"{rev}:web/index.html"],
                         cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8")
    if out.returncode != 0:
        sys.exit(f"取不到 {rev} 的 index.html：{out.stderr.strip()}")
    return out.stdout


def run_scenario(browser, html: str, sc: dict, tag: str) -> None:
    """跑一个场景：注入 → 过闸 → 断言各面板有内容 + 无 JS 异常。"""
    page_html = html.replace(
        "<head>", "<head>" + make_stub(sc["state"], NOTES), 1)
    nm = sc["name"]
    print(f"\n-- 场景：{nm} --")

    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    pw_errors: list[str] = []
    pg.on("pageerror", lambda e: pw_errors.append(str(e)))

    pg.set_content(page_html, wait_until="load")
    pg.wait_for_timeout(500)
    # boot() 会问鉴权状态；桩数据让它到此为止。这里手动过闸，
    # 走的就是用户点完登录后的那条路径（startApp → refresh → 轮询）。
    pg.evaluate("() => { if (typeof startApp === 'function') startApp(); }")
    pg.wait_for_timeout(1200)

    errs = pg.evaluate("() => window.__errors || []")
    n_merged = pg.eval_on_selector_all("#mergedList > li", "e => e.length")
    n_logs = pg.eval_on_selector_all("#logList > li", "e => e.length")

    def txt(sel: str) -> str:
        return (pg.evaluate(
            "s => { const e = document.querySelector(s);"
            " return e ? (e.innerText || '').trim() : ''; }", sel) or "")

    subline, auto_status = txt("#subline"), txt("#autoStatus")
    ready_box, row_count, btn_auto = txt("#readyBox"), txt("#rowCount"), txt("#btnAuto")
    first_title = pg.evaluate(
        "() => { const e = document.querySelector('#mergedList .t');"
        " return e ? e.textContent : ''; }")
    shot = SHOTS / f"page_render_{tag}_{nm.replace(' ', '_').replace('（','_').replace('）','')}.png"
    pg.screenshot(path=str(shot))
    pg.close()

    all_errs = list(errs) + [f"pageerror: {e}" for e in pw_errors]
    pre = f"{nm}｜"
    ok(pre + "无 JS 异常", not all_errs, "｜".join(all_errs)[:300])
    ok(pre + "副标题有映射数", "已建立映射" in subline, subline)
    # ★ 关键：renderSwitch 是"分水岭" —— 它一抛异常，后面的日志和记录全空。
    ok(pre + "按钮状态文字非空（renderSwitch 跑到最后）",
       bool(auto_status), auto_status)
    ok(pre + "就绪提示非空", bool(ready_box), ready_box)
    ok(pre + "日志列表有内容", n_logs > 0, f"{n_logs} 行")
    ok(pre + "记录列表有内容", n_merged > 0 and bool(first_title),
       f"{n_merged} 行｜首行={first_title}")
    ok(pre + "记录计数非空", row_count.startswith("("), row_count)
    ok(pre + "按钮文案正确", sc["expect_btn"] in btn_auto,
       f"期望含「{sc['expect_btn']}」，实际「{btn_auto}」")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default=None,
                    help="从 git 取该版本的 web/index.html（默认用工作区文件）")
    ap.add_argument("--only", default=None, help="只跑名字里含该子串的场景")
    args = ap.parse_args()

    html = load_html(args.rev)
    if "<head>" not in html:
        sys.exit("index.html 里找不到 <head>，无法注入桩")
    tag = args.rev or "working"

    print("=" * 72)
    print(f"页面渲染回归 —— 版本：{tag}｜场景数 "
          f"{len([s for s in SCENARIOS if not args.only or args.only in s['name']])}")
    print("=" * 72)

    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        for sc in SCENARIOS:
            if args.only and args.only not in sc["name"]:
                continue
            run_scenario(b, html, sc, tag)
        b.close()

    print("\n" + "=" * 72)
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过（通过 {len(PASSED)} 项）")
        for f in FAILED:
            print("   · " + f)
        return 1
    print(f"✅ 全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
