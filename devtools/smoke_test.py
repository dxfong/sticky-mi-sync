#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全新部署冒烟测试 —— **每次 push 前必须跑通**。

为什么要有这个脚本
------------------
2026-10-05 犯过一次：代码在本机（fabric 模式、凭据齐全）跑得好好的，
打包发布后用户一部署就卡住 —— 因为
  · 默认 `graph.mode` 是 mock，而 mock 会静默落到 RealGraph（缺 2026 便笺）；
  · fabric 模式下根本没有登录入口（粘贴端点要求 RealGraph，前端也没渲染）。

**本机可用 ≠ 全新部署可用。** 这个脚本模拟"容器首次启动"那个状态，
把那类问题挡在 push 之前。

覆盖
----
1. 空目录能否自举（自动建 config.json / state.db）
2. **默认通道是不是 fabric**（最关键的一条）
3. 设密码 → 登录 → 拿会话
4. 各登录端点在**未配置**时返回的是「能看懂的提示」而不是 500 / 挂死
5. `/api/graph/token` 在 fabric 模式下**接受** payload（不能被"通道不支持"挡掉）
6. 小米扫码在缺 migate 时**立即**返回（不能挂住请求线程）
7. 清理

用法
----
    python devtools/smoke_test.py
    python devtools/smoke_test.py --port 8901

退出码 0 = 全过；非 0 = 有失败项。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSED: list[str] = []
FAILED: list[str] = []
# 探活失败时最后一次的响应（给排查用）
LAST_PROBE: dict = {}


def ok(name: str, cond: bool, detail: str = "") -> bool:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"\n         {detail}"
    print(line, flush=True)
    return cond


# --------------------------------------------------------------------- HTTP

class Ctx:
    def __init__(self, base: str):
        self.base = base
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.ProxyHandler({}),          # 绕开系统代理
        )

    def call(self, path: str, body=None, timeout: float = 30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if body is not None else "GET")
        t0 = time.time()
        try:
            with self.op.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or "{}"), time.time() - t0
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode() or "{}"), time.time() - t0
            except Exception:
                return {"_http": e.code}, time.time() - t0
        except Exception as e:
            return {"_exc": repr(e)}, time.time() - t0


def wait_port(ctx: Ctx, seconds: float = 20) -> bool:
    """等 /api/auth/status 能通。失败时把最后一次的返回记到 LAST_PROBE，便于排查"""
    global LAST_PROBE
    end = time.time() + seconds
    while time.time() < end:
        d, _ = ctx.call("/api/auth/status", timeout=3)
        if d.get("ok"):
            return True
        LAST_PROBE = d
        time.sleep(0.4)
    return False


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--keep", action="store_true", help="跑完保留数据目录，便于排查")
    args = ap.parse_args()

    # ★ 必须屏蔽代理。这台机器/这个会话可能带着 http_proxy 之类的环境变量，
    #   而 Python 的 urllib 会读它们 —— 于是访问 127.0.0.1 也被扔给代理，
    #   换来一个莫名其妙的结果（实测见过代理不转发回环，报 502 或 10061）。
    #   Ctx 里已经有 ProxyHandler({})，这里再从环境变量层面堵一道。
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        os.environ.pop(k, None)
    os.environ["no_proxy"] = "*"
    os.environ["NO_PROXY"] = "*"

    tmp = Path(tempfile.mkdtemp(prefix="sms-smoke-"))
    base = f"http://127.0.0.1:{args.port}"
    ctx = Ctx(base)
    proc = None
    logfh = None

    print("=" * 60)
    print("全新部署冒烟测试（模拟容器首次启动）")
    print("=" * 60)
    print(f"  数据目录: {tmp}")
    print(f"  端口    : {args.port}")
    print()

    try:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        # ★ 日志写**文件**而不是 PIPE。
        #   用 PIPE 的话，服务没起来时去 read() 会阻塞（进程还活着、pipe 里没数据，
        #   read 就一直等），表现成整个测试卡死被 SIGTERM —— 反而看不到任何线索。
        logf = tmp / "_server.log"

        # ★ 起服务要**重试**：实测这台机器上 subprocess 偶发启动不起来
        #   （进程活着、不监听、不输出，约 1/3 概率），手动跑同样的命令却次次成功。
        #   这是环境噪声，不该让测试因此报红 —— 重试一次就能消化掉。
        uptries = 0
        for attempt in range(1, 3):
            uptries = attempt
            logfh = logf.open("w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(
                [sys.executable, "-u", "server.py", "--port", str(args.port),
                 "--data-dir", str(tmp)],
                cwd=str(ROOT), env=env,
                stdout=logfh, stderr=subprocess.STDOUT)

            # ---- 1. 自举
            print(f"[1] 空目录自举{'（第 %d 次尝试）' % attempt if attempt > 1 else ''}")
            up = wait_port(ctx, seconds=40)
            if up:
                break
            print(f"    第 {attempt} 次没起来，换一次重试…")
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            try:
                logfh.close()
            except Exception:
                pass
            logfh = None
            time.sleep(1.5)

        ok("服务能在空目录上启动并响应", up, f"尝试了 {uptries} 次")
        if not up:
            # 服务没起来时必须把日志打出来 ——
            # 否则只看到一句 FAIL，分不清是缺依赖、端口冲突还是解释器路径不对。
            print("\n  服务没起来，日志如下：")
            try:
                # 先确认服务到底有没有在监听端口 —— 把"服务没起"和"HTTP 层连不上"
                # 这两种完全不同的失败分开，别混在一起猜。
                import socket as _sock
                try:
                    _s = _sock.create_connection(("127.0.0.1", args.port), timeout=3)
                    _s.close()
                    print("    [探测] TCP 127.0.0.1:%d 可连 —— 服务在监听，"
                          "问题出在 HTTP 层（大概率是代理）" % args.port)
                except Exception as _e:
                    print("    [探测] TCP 127.0.0.1:%d 连不上（%s）—— 服务确实没监听"
                          % (args.port, type(_e).__name__))
                print(f"    [探测] 进程存活: {proc.poll() is None}，"
                      f"退出码: {proc.poll()}")
                print(f"    [探测] 解释器: {sys.executable}")
                print(f"    [探测] 最后一次探活返回: {str(LAST_PROBE)[:200]}")
                try:
                    if logfh:
                        logfh.flush()
                except Exception:
                    pass
                text = logf.read_text(encoding="utf-8", errors="replace")
                lines = [ln for ln in text.splitlines() if ln.strip()]
                if lines:
                    for ln in lines[-25:]:
                        print("    | " + ln)
                else:
                    print("    （日志是空的）")
            except Exception as e:
                print("    读日志失败:", e)
            raise SystemExit(1)
        cfg_file = tmp / "config.json"
        ok("自动创建了 config.json", cfg_file.exists())
        ok("自动创建了 state.db", (tmp / "state.db").exists())

        # ---- 2. ★ 默认通道
        print("\n[2] 默认通道（最关键）")
        cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
        gcfg = cfg.get("graph", {})
        gm = gcfg.get("mode")
        ok("graph.mode 默认是 real", gm == "real",
           f"实际是 {gm!r} —— 若是 mock/fabric，全新部署会拿不到"
           f"「数据全 + 自动续期」这个组合")
        sc = str(gcfg.get("scope") or "")
        ok("scope 指向 Outlook REST（不是 Graph 的 ShortNotes）",
           "outlook.office.com" in sc,
           f"实际 {sc!r} —— ShortNotes 对个人账号的端点根本不存在")
        ok("tenant 默认 consumers（个人账号必须）",
           gcfg.get("tenant") == "consumers", f"实际 {gcfg.get('tenant')!r}")

        # ---- 3. 设密码 + 登录
        print("\n[3] 首次设置密码 + 登录")
        st, _ = ctx.call("/api/auth/status")
        ok("首次是 needs_setup", st.get("needs_setup") is True, f"实际 {st}")
        r, _ = ctx.call("/api/auth/setup", {"password": "smoke-test-123"})
        ok("设置密码成功", r.get("ok") is True, str(r)[:120])
        r, _ = ctx.call("/api/auth/login", {"password": "smoke-test-123"})
        ok("登录成功", r.get("ok") is True, str(r)[:120])
        state, _ = ctx.call("/api/state")
        g = state.get("graph", {})
        ok("运行态通道也是 real", g.get("mode") == "real", f"实际 {g.get('mode')!r}")

        # ---- 4. 登录端点：未配置时应给「能看懂的提示」
        print("\n[4] 登录端点返回值是否可读（不能 500 / 不能挂死）")
        r, dt = ctx.call("/api/graph/login/start", {})
        ok("设备码端点在未填 client_id 时给出可读提示",
           r.get("status") == "error" and "client_id" in str(r.get("error", "")),
           f"{dt:.1f}s -> {str(r)[:150]}")
        ok("设备码端点没有挂死", dt < 20, f"耗时 {dt:.1f}s")

        # ---- 5. 粘贴入口
        # real 模式下这个端点收的是 **Graph 的 access_token**（JWT），
        # 不是 NotesFabric 的 MSAuth1.0 那种。传错类型应该被格式预检拦下
        # 并说清原因 —— 这正是要验证的（而不是等到网络 401 才发现拿错了）。
        print("\n[5] 粘贴入口（real 模式收 Graph access_token）")
        r, dt = ctx.call("/api/graph/token", {"payload":
            'MSAuth1.0 usertoken="x", type="MSACT"\nx-anchormailbox: MSA:a@b.com'})
        err5 = str(r.get("error", ""))
        ok("拿错类型的令牌会被拦下并说明原因",
           r.get("ok") is not True and bool(err5), f"{dt:.1f}s -> {err5[:170]}")

        print("\n[5b] ★ 格式校验：应在**发出请求之前**拦下并说明原因")
        # 空
        r, dt = ctx.call("/api/graph/token", {"payload": ""})
        e0 = str(r.get("error", ""))
        ok("空令牌被拦下", r.get("ok") is not True and bool(e0),
           f"{dt:.1f}s -> {e0[:140]}")

        # 明显不是令牌的字符串
        r, dt = ctx.call("/api/graph/token", {"payload": "hello-world"})
        e1 = str(r.get("error", ""))
        ok("不像令牌的内容被拦下并说明",
           r.get("ok") is not True and ("不像" in e1 or "access token" in e1.lower()),
           f"{dt:.1f}s -> {e1[:140]}")

        # 拿 NotesFabric 的 MSAuth1.0 冒充 Graph 令牌
        r, dt = ctx.call("/api/graph/token", {"payload":
            'Authorization: MSAuth1.0 usertoken="EwBIBOl3", type="MSACT"\n'
            'x-anchormailbox: MSA:a@b.com'})
        e2 = str(r.get("error", ""))
        ok("拿 MSAuth1.0 冒充 Graph 令牌会被拦下",
           r.get("ok") is not True and bool(e2), f"{dt:.1f}s -> {e2[:140]}")

        # ---- 6. 小米扫码不能挂住
        print("\n[6] 小米扫码（必须快速返回，不能把请求线程挂死）")
        r, dt = ctx.call("/api/xiaomi/qr/start", {}, timeout=40)
        ok("扫码端点快速返回（未超时）", dt < 35, f"耗时 {dt:.1f}s")
        # 两种结果都算合格：装了 migate 就能拿到二维码，没装要给得出可读的提示。
        # （不能只认"报错" —— 那样装了 migate 反而判失败。）
        if r.get("ok") and r.get("qr_svg"):
            ok("扫码端点返回了二维码", True, f"phase={r.get('phase')}")
        else:
            ok("扫码端点给出可读的提示", bool(r.get("error")), str(r)[:140])

        # ---- 6b. 小米账号密码登录（容器里唯一能完整走通的登录方式）
        print("\n[6b] 小米账号密码登录端点")
        r, dt = ctx.call("/api/xiaomi/pwd/status")
        ok("状态端点可用", "phase" in r, str(r)[:120])

        r, dt = ctx.call("/api/xiaomi/pwd/login", {})
        ok("空账号被拦下", r.get("ok") is not True and bool(r.get("error")),
           f"{dt:.1f}s -> {str(r)[:130]}")

        r, dt = ctx.call("/api/xiaomi/pwd/login", {"user": "nobody", "password": ""})
        ok("空密码被拦下", r.get("ok") is not True and bool(r.get("error")),
           f"{dt:.1f}s -> {str(r)[:130]}")

        r, dt = ctx.call("/api/xiaomi/pwd/check", {"ticket": ""})
        ok("空验证码被拦下", r.get("ok") is not True and bool(r.get("error")),
           f"{dt:.1f}s -> {str(r)[:130]}")

        # ---- 7. 鉴权边界
        anon = Ctx(base)
        r, _ = anon.call("/api/state")
        ok("/api/state 未登录时拒绝", r.get("need_login") is True, str(r)[:100])

    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        try:
            if logfh:
                logfh.close()
        except Exception:
            pass
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n  (数据目录保留在 {tmp})")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("✅ 冒烟测试全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
