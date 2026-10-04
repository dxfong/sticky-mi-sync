#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真实浏览器取小米凭据 —— 免粘贴、免扫码、长期不过期。

为什么需要它
------------
小米把「可信设备」记在 `deviceId` 上：
  - 扫码 / 终端登录产生的是**新设备** → 读笔记接口时被要求做一次交互式安全验证
    （响应里的 `isSecondValidation: true`，绕不过去）
  - 手工复制浏览器 Cookie 之所以能用，是因为那份凭据来自**已经验证过的设备**
所以真正干净的做法不是"再想一个登录方式"，而是：**直接用你的浏览器**。

怎么做到的
----------
用 Playwright 开一个**持久化 profile** 的浏览器：
  1. 第一次：弹出窗口，你登录一次（任何安全验证都在这个窗口里完成）
     登录态存进 `data/browser_profile/`
  2. 之后：**同一个 profile 里已经带着登录态**，headless 也能把 cookie 读出来
     → 静默刷新，不需要你参与，也不需要粘贴

因为登录态在浏览器 profile 里，只要它还在，cookie 就永远有效。
比任何"复制粘贴"都稳。

实现要点（照抄 mi-note-cli 的做法，这几条是决定成败的）
-------------------------------------------------------
`https://github.com/ceynri/mi-note-cli` 的 `src/auth.ts` 里这几条，缺一条就会
出现「cookie 拿到了，但调接口 401」：

1. `ignoreDefaultArgs: ["--no-sandbox", "--enable-automation"]`
   Playwright 默认会加 `--enable-automation`，于是 `navigator.webdriver === true`。
   小米登录页据此判定"自动化环境"，给你一份**看起来正常、实际打不通**的凭据。
2. 优先 `channel: "chrome"`（系统 Chrome，真签名），最后才回退内置 chromium。
3. 登录页用 **`https://i.mi.com/note/h5#/`**（H5 版）。桌面版是服务端渲染，
   不会触发前端去换 serviceToken；H5 版是纯前端应用，打开就会拿长效登录态
   去换新的 `serviceToken`。**这是"静默续期"能work的真正机制。**
4. 静默续期要**轮询等待**（mi-note-cli 给 15 秒），不是 goto 完读一次。
   换发是异步的，读早了就是空的 —— 之前只等 3 秒，就是这么失败的。

用法
----
    python -m notesync.browser_auth              # 交互：弹窗口让你登录（一次，之后永久）
    python -m notesync.browser_auth --silent     # 静默：只尝试从已有 profile 取
    python -m notesync.browser_auth --json       # 只把 cookie 打到 stdout（供后端调用）
    python -m notesync.browser_auth --seed       # 把库里现有凭据种进 profile
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 小米这边真正需要的键。其余的一并带上也不会有坏处，但这两个是核心。
CORE = ("serviceToken", "userId", "passToken", "deviceId", "cUserId",
        "i.mi.com_slh", "i.mi.com_ph", "passInfo", "pass_ua", "uLocale",
        "i.mi.com_istrudev", "i.mi.com_isvalid_servicetoken", "iplocale")

# 这几个键属于账号域，换服务凭据时要用 account.xiaomi.com 下的那一份
ACCOUNT_SCOPE = {"passToken", "deviceId", "pass_ua", "passInfo", "cUserId"}

NOTE_PROBE = "https://i.mi.com/note/full/page?limit=1"

# 登录 / 静默续期都用 **H5 版**。
# 桌面版是服务端渲染，页面加载完不会去换 serviceToken；
# H5 版是纯前端应用，打开时会拿 profile 里的长效登录态换一份新的 serviceToken。
# 这就是"无交互续期"的机制所在 —— 用错 URL 就永远续不上。
NOTE_URL = "https://i.mi.com/note/h5#/"

# 静默续期最多等多久（秒）。换发是异步的，读早了就是空的。
SILENT_WAIT = 25


def _pick(cookies: list[dict]) -> dict[str, str]:
    """把 Playwright 的 cookie 列表压成一个扁平的 name->value。

    同一个名字可能出现在不同域下（serviceToken 在 .mi.com 和 account.xiaomi.com 都有）。
    规则：
      - passToken / deviceId: 优先 account.xiaomi.com（换服务凭据要用它）
      - 其它: 优先 i.mi.com / .mi.com（调笔记接口要用它）
    """
    out: dict[str, str] = {}
    for c in cookies:
        name = c.get("name") or ""
        if not name:
            continue
        if name not in CORE:
            continue
        dom = (c.get("domain") or "").lower()
        prefer_account = name in ("passToken", "deviceId")
        is_account = "account.xiaomi.com" in dom
        if name in out and prefer_account != is_account:
            # 已经有一个，且域优先级更高，就不覆盖
            continue
        out[name] = c.get("value") or ""
    return out


def _probe(cookies: dict[str, str], timeout: int = 15) -> bool:
    """用这组 cookie 打一次笔记接口，确认真的能用。只读。"""
    import urllib.error
    import urllib.request

    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    req = urllib.request.Request(NOTE_PROBE, headers={
        "Cookie": header,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Referer": "https://i.mi.com/note/",
    })
    try:
        with direct.open(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def _launch(pw, profile: Path, headless: bool):
    """启动持久化浏览器。

    **优先系统 Chrome**（真签名），回退 Edge，最后才是 Playwright 自带的 chromium。

    并且必须摘掉 Playwright 默认追加的自动化标记 —— 带着 `--enable-automation`
    启动时，页面里 `navigator.webdriver` 为 true，小米会给你一份
    "cookie 看着齐全、调接口却 401" 的凭据。这是前面反复踩的坑。
    """
    common = {
        "user_data_dir": str(profile),
        "headless": headless,
        # 注意 Python 版是 snake_case（JS 版才是 chromiumSandbox），写错会直接
        # 抛 "unexpected keyword argument" 把三个候选渠道全试失败。
        "chromium_sandbox": True,
        # 这一行是关键：把自动化痕迹去掉
        "ignore_default_args": ["--no-sandbox", "--enable-automation"],
        "args": ["--no-first-run", "--no-default-browser-check",
                 "--disable-blink-features=AutomationControlled"],
    }
    last = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = dict(common)
            if channel:
                kw["channel"] = channel
            ctx = pw.chromium.launch_persistent_context(**kw)
            ctx._sticky_notes_channel = channel or "bundled-chromium"
            return ctx
        except Exception as e:
            last = e
    raise RuntimeError(
        f"起不来浏览器（试过系统 Chrome / Edge / 内置 chromium）：{last}\n"
        f"请至少装一个 Microsoft Edge 或 Google Chrome。")


def silent_refresh(profile: Path, wait: int = SILENT_WAIT) -> dict:
    """静默续期：无头打开 profile 里的 i.mi.com，**等它自己把 serviceToken 换出来**。

    这一步就是用户要的"不用再登录也能一直用"。
    全程不发登录请求、不弹窗、不需要人参与。

    为什么是"等"而不是"读"
    ----------------------
    profile 里存的是**长效登录态**，而调接口要的是**短效 serviceToken**。
    短效那个是浏览器打开 H5 页面时，前端拿长效的去找服务端换来的。
    所以流程必须是：打开页面 → 等前端完成换发 → **轮询**读 cookie。

    换发是异步的，读早了什么都拿不到 —— 之前只在 goto 之后读一次（等 3 秒），
    就是这么失败的。mi-note-cli 在这里给的是 15 秒轮询，这里给 25 秒。
    """
    from playwright.sync_api import sync_playwright

    deadline = time.time() + wait
    last: dict = {}
    channel = ""
    try:
        with sync_playwright() as pw:
            ctx = _launch(pw, profile, headless=True)
            channel = getattr(ctx, "_sticky_notes_channel", "")
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto(NOTE_URL, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    pass

                while time.time() < deadline:
                    ck = _pick(ctx.cookies())
                    last = ck
                    if ck.get("serviceToken") and ck.get("userId") and _probe(ck):
                        return {"ok": True, "cookies": ck, "channel": channel}
                    time.sleep(2)
            finally:
                ctx.close()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "channel": channel}

    have = sorted(last)
    if not have:
        hint = ("浏览器 profile 里没有登录态 —— 先跑一次 browser-login.bat 登录"
                "（只需一次，之后就一直免登录了）")
    elif "serviceToken" not in last:
        hint = (f"profile 里有 {have}，但没有 serviceToken："
                "长效登录态可能已失效，需要重新登录一次")
    else:
        hint = (f"profile 里有 serviceToken，但打不通笔记接口（等满 {wait} 秒仍未换发成功）："
                "长效登录态大概率已失效，需要重新登录一次")
    return {"ok": False, "error": hint, "cookies": last, "keys": have,
            "channel": channel}


def interactive(store, profile: Path, timeout: int = 600) -> int:
    """交互式登录一次，并**当场验证 profile 是否真的持久化**。

    验证这一步很重要：登录成功 != 以后不用再登录。
    只有"关掉浏览器、重开一个无头的、还能拿到可用凭据"，才算真的持久。
    所以登录完立刻用 `silent_refresh()` 自证一遍，把结论直接告诉用户。
    """
    from playwright.sync_api import sync_playwright

    print("=" * 68)
    print("小米登录（浏览器方式 · 只需做这一次）")
    print("=" * 68)
    print("会弹出一个浏览器窗口，请在里面登录 i.mi.com：")
    print("  · 可以用手机号/邮箱 + 密码登录，也可以扫码")
    print("  · 如果弹出安全验证/短信验证码，就在这里做完")
    print("  · 登录成功后窗口会自动关闭，凭据自动保存")
    print()
    wait_txt = (f"{timeout // 60} 分钟" if timeout >= 60 else f"{timeout} 秒")
    print(f"最多等 {wait_txt}。窗口没弹出的检查一下任务栏。")
    print()

    with sync_playwright() as pw:
        ctx = _launch(pw, profile, headless=False)
        print(f"浏览器：{getattr(ctx, '_sticky_notes_channel', '?')}")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(NOTE_URL, wait_until="domcontentloaded")
        except Exception:
            pass

        deadline = time.time() + timeout
        start = time.time()
        ok = False
        beat = 0
        cookies: dict = {}
        while time.time() < deadline:
            ck = _pick(ctx.cookies())
            if ck.get("serviceToken") and ck.get("userId") and _probe(ck):
                ok = True
                cookies = ck
                break
            if time.time() - start > beat * 15:
                beat += 1
                left = int(deadline - time.time())
                print(f"  …还在等待登录（剩余 {left} 秒）"
                      f"，当前 cookie：{sorted(ck) or '（空）'}", flush=True)
            time.sleep(2)

        if ok:
            # 关键：多等一会再关闭。
            # 登录态除了 cookie 还有 localStorage，浏览器要一点时间把它落盘。
            # 立刻 close 有可能丢掉这部分 —— 那 profile 下次打开就还是"未登录"。
            print("  已检测到可用凭据，等待登录态写入 profile（8 秒）…", flush=True)
            page.wait_for_timeout(8000)
            cookies = _pick(ctx.cookies())

        ctx.close()

    if not ok:
        print()
        print("=" * 68)
        print("[x] 没有等到可用的登录态")
        print("=" * 68)
        print(f"  当前拿到的 cookie：{sorted(cookies) or '（空）'}")
        if not cookies:
            print("  → 浏览器可能没打开成功，或页面没加载出来")
        elif "serviceToken" not in cookies:
            print("  → 页面到了，但没有登录态的 cookie：说明登录还没完成")
            print("    请在窗口里把登录走完（含任何验证码/安全验证）")
        else:
            print("  → 有 serviceToken 但打不通笔记接口：可能仍需要一次安全验证")
        return 3

    store.set_cred("xiaomi_cookie", cookies)
    store.set_meta("xiaomi_cred_at", str(int(time.time())))
    if cookies.get("passToken") and cookies.get("deviceId"):
        store.set_cred("xiaomi_pass_token", {
            "deviceId": cookies["deviceId"],
            "passToken": cookies["passToken"],
            "userId": cookies.get("userId", ""),
        })
    store.log(f"小米登录成功（浏览器方式，userId={cookies.get('userId')}），"
              f"键={sorted(cookies)}")
    print()
    print("=" * 68)
    print("登录成功，凭据已保存")
    print("=" * 68)
    print(f"  userId : {cookies.get('userId')}")
    print(f"  键     : {sorted(cookies)}")
    print(f"  Profile: {profile}")
    print()

    # 自证：关掉浏览器、无头重开，看还能不能换到可用凭据。
    # 这一步过了，才能对用户说"以后不用再登录"。
    print("正在验证 profile 持久化（关掉浏览器后能否静默续期）…")
    r = silent_refresh(profile)
    if r.get("ok"):
        print("[OK] 验证通过 —— profile 已持久化，以后凭据过期会自动静默续期，"
              "不用再登录。")
        store.log("已自证：浏览器 profile 可静默续期（以后无需重新登录）")
        return 0
    print(f"[!] 登录本身成功了，但持久化自证没通过：{r.get('error')}")
    print("    凭据现在能用；等它过期时如果续不上，重跑一次 browser-login.bat 即可。")
    store.log(f"登录成功但持久化自证未通过：{r.get('error')}", "warn")
    return 0


def silent_json(profile: Path, timeout: int = SILENT_WAIT) -> int:
    """静默模式：不弹窗口，把结果以 JSON 打到 stdout。供后端调用。"""
    r = silent_refresh(profile, timeout)
    if r.get("ok"):
        print(json.dumps({"ok": True, "cookies": r["cookies"],
                          "channel": r.get("channel", "")}, ensure_ascii=False))
        return 0
    print(json.dumps({"ok": False, "error": r.get("error", "未知原因"),
                      "cookies": r.get("cookies") or {},
                      "keys": r.get("keys") or [],
                      "channel": r.get("channel", "")}, ensure_ascii=False))
    return 3


_PW_PY: str | None = None
_PW_PY_TRIED = False


def find_python_with_playwright() -> str | None:
    """找一个**真的装了 playwright** 的 python 解释器。

    为什么不能直接用 `sys.executable`
    --------------------------------
    同步服务可能跑在一个没装 playwright 的解释器里（比如系统自带的 python），
    而 playwright 装在另一个 venv 里。那样静默续期会以 ModuleNotFoundError 收场，
    而且错误埋在子进程输出里，从页面上只看到"续期失败"，很难看出真正原因。

    所以这里逐个候选探测，谁 import 得动就用谁。结果缓存，只探测一次。
    """
    global _PW_PY, _PW_PY_TRIED
    if _PW_PY_TRIED:
        return _PW_PY
    _PW_PY_TRIED = True

    import shutil
    import subprocess

    cands: list[str] = []
    for p in (Path.home() / ".workbuddy" / "binaries" / "python" / "envs"
              / "default" / "Scripts" / "python.exe",
              Path.home() / ".workbuddy" / "binaries" / "python" / "envs"
              / "default" / "bin" / "python"):
        if p.exists():
            cands.append(str(p))
    cands.append(sys.executable)
    for name in ("python", "python3"):
        w = shutil.which(name)
        if w:
            cands.append(w)

    for exe in cands:
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "-c", "import playwright"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                _PW_PY = exe
                return exe
        except Exception:
            continue
    return None


def launch_options_note() -> str:
    """给前端/日志用的一句话，说明当前这套环境能不能静默续期。"""
    exe = find_python_with_playwright()
    if not exe:
        return "没找到装了 playwright 的 python —— 静默续期不可用（pip install playwright）"
    return f"静默续期可用（用 {exe}）"


def seed_from_creds(store, profile: Path, timeout: int = 90) -> dict:
    """把库里**现有**凭据"种"进浏览器 profile，让 profile 恢复登录态。

    为什么必须有这一步
    ------------------
    静默续期（`--silent`）的前提是 profile 里带登录态。但 profile 里的
    serviceToken / passToken 是 **session cookie**，不落盘 —— 浏览器一关就没了。
    结果是一个很别扭的局面：

        库里还有一份能用的凭据（手工粘贴来的），
        可 profile 已经空了，续期这条路**已经断了**。
        等这份凭据过期，人就只能重新登录。

    这个函数就是**趁凭据还能用的时候**，把它种回 profile：
        注入 cookie -> 打开 i.mi.com（就是正常浏览行为）-> 服务端认下这个会话
        -> 读回 cookie -> 验证能不能用

    种完之后 profile 自带登录态，`--silent` 静默续期就通了，
    而且之后凭据过期时浏览器会拿 profile 里的 passToken 自动换新，
    不需要人再管。

    安全约定
    --------
    * 只**添加** cookie，绝不删除 profile 里已有的（尤其别动那个 390 天有效期的
      `deviceId` —— 它是"可信设备"的身份证，扔了就得重新做安全验证）
    * 全程只是浏览 i.mi.com，不调用任何登录接口，不触发风控
    * 返回值只是报告，写库由调用方决定
    """
    from playwright.sync_api import sync_playwright

    creds = store.get_cred("xiaomi_cookie", {}) or {}
    if not (creds.get("serviceToken") and creds.get("userId")):
        return {"ok": False,
                "error": "库里没有可用的凭据（缺 serviceToken/userId），没有东西可种"}

    report: dict = {"seeded": [], "before": [], "after": [], "ok": False}
    try:
        with sync_playwright() as pw:
            ctx = _launch(pw, profile, headless=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # 先在目标站点上，add_cookies 用 url 形式才稳
                try:
                    page.goto("https://i.mi.com/", wait_until="domcontentloaded")
                except Exception:
                    pass

                report["before"] = sorted(_pick(ctx.cookies()))

                jar = []
                for k, v in creds.items():
                    if not v:
                        continue
                    # 同一份值同时投到笔记域和账号域 —— 小米两处都会读。
                    # 注意 Playwright 的规矩：**给了 url 就不能再给 path/domain**
                    # （两者互斥，同时给会报 "Cookie should have either url or path"）。
                    jar.append({"name": k, "value": str(v),
                                "url": "https://i.mi.com/"})
                    if k in ACCOUNT_SCOPE:
                        jar.append({"name": k, "value": str(v),
                                    "url": "https://account.xiaomi.com/"})

                # 逐条注入：某一条被浏览器拒了（域/属性不合法）不该拖垮全部
                added, failed = 0, []
                for ck in jar:
                    try:
                        ctx.add_cookies([ck])
                        added += 1
                    except Exception as e:
                        failed.append(f"{ck['name']}@{ck['url']}: {e}")
                report["added"] = added
                if failed:
                    report["add_errors"] = failed[:5]
                report["seeded"] = sorted(creds)

                # 带着种进去的 cookie 重访一次 H5 页 —— 这一步让服务端把会话"认下来"，
                # 并且让前端把登录态写进 localStorage。之后再打开浏览器，
                # profile 里就自带登录态了。
                try:
                    page.goto(NOTE_URL, wait_until="domcontentloaded")
                    page.wait_for_timeout(8000)
                except Exception:
                    pass

                after = _pick(ctx.cookies())
                report["after"] = sorted(after)
                ok = bool(after.get("serviceToken") and after.get("userId")) \
                    and _probe(after)
                report["ok"] = ok
                report["cookies"] = after
                if not ok:
                    report["error"] = ("种进去之后 profile 里仍读不到可用的登录态 —— "
                                       "说明这份凭据可能已经不允许继续用了。")
            finally:
                ctx.close()
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="用真实浏览器取小米凭据")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--profile", default="", help="浏览器 profile 目录（默认 data/browser_profile）")
    ap.add_argument("--silent", action="store_true", help="不弹窗口，只尝试静默取")
    ap.add_argument("--seed", action="store_true",
                    help="把库里现有凭据种进 profile（趁还没过期，把续期这条路修通）")
    ap.add_argument("--json", action="store_true", help="只把 cookie 打到 stdout")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    profile = Path(args.profile) if args.profile else Path(args.data_dir) / "browser_profile"
    profile.mkdir(parents=True, exist_ok=True)

    # 静默类操作不需要"最多等 10 分钟"，用 SILENT_WAIT
    silent_wait = args.timeout if args.timeout != 600 else SILENT_WAIT

    if args.seed:
        from notesync.store import Store
        store = Store(Path(args.data_dir))
        r = seed_from_creds(store, profile, silent_wait)
        if args.json:
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r.get("ok") else 3
        print("=" * 68)
        print("把现有凭据种进浏览器 profile")
        print("=" * 68)
        print(f"  profile  : {profile}")
        print(f"  种之前   : {r.get('before') or '（空）'}")
        print(f"  注入的键 : {r.get('seeded') or '（无）'}（成功 {r.get('added', 0)} 条）")
        print(f"  种之后   : {r.get('after') or '（空）'}")
        print()
        if r.get("ok"):
            print("[OK] profile 已恢复登录态。以后凭据过期可以静默续期，不用再登录。")
        else:
            print(f"[x] 没成功：{r.get('error')}")
        return 0 if r.get("ok") else 3

    if args.json:
        return silent_json(profile, silent_wait)

    from notesync.store import Store
    store = Store(Path(args.data_dir))

    if args.silent:
        return silent_json(profile, silent_wait)

    try:
        return interactive(store, profile, args.timeout)
    except ImportError:
        print("[x] 没装 playwright：pip install playwright", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
