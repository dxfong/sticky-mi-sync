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
    end = time.time() + seconds
    while time.time() < end:
        d, _ = ctx.call("/api/auth/status", timeout=3)
        if d.get("ok"):
            return True
        time.sleep(0.4)
    return False


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--keep", action="store_true", help="跑完保留数据目录，便于排查")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sms-smoke-"))
    base = f"http://127.0.0.1:{args.port}"
    ctx = Ctx(base)
    proc = None

    print("=" * 60)
    print("全新部署冒烟测试（模拟容器首次启动）")
    print("=" * 60)
    print(f"  数据目录: {tmp}")
    print(f"  端口    : {args.port}")
    print()

    try:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            [sys.executable, "server.py", "--port", str(args.port),
             "--data-dir", str(tmp)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        # ---- 1. 自举
        print("[1] 空目录自举")
        up = wait_port(ctx)
        ok("服务能在空目录上启动并响应", up)
        if not up:
            raise SystemExit(1)
        cfg_file = tmp / "config.json"
        ok("自动创建了 config.json", cfg_file.exists())
        ok("自动创建了 state.db", (tmp / "state.db").exists())

        # ---- 2. ★ 默认通道
        print("\n[2] 默认通道（最关键）")
        cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
        gm = cfg.get("graph", {}).get("mode")
        ok("graph.mode 默认是 fabric", gm == "fabric",
           f"实际是 {gm!r} —— 若是 mock/real，全新部署会拿到缺 2026 便笺的残缺数据")

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
        ok("运行态通道也是 fabric", g.get("mode") == "fabric", f"实际 {g.get('mode')!r}")

        # ---- 4. 登录端点：未配置时应给「能看懂的提示」
        print("\n[4] 登录端点返回值是否可读（不能 500 / 不能挂死）")
        r, dt = ctx.call("/api/graph/login/start", {})
        ok("设备码端点在 fabric 下给出明确指引",
           r.get("status") == "error" and "NotesFabric" in str(r.get("error", "")),
           f"{dt:.1f}s -> {str(r)[:150]}")
        ok("设备码端点没有挂死", dt < 20, f"耗时 {dt:.1f}s")

        # ---- 5. ★ fabric 的粘贴入口必须能用（至少要"接受 payload"）
        print("\n[5] fabric 粘贴入口（旧版会返回「通道不支持」）")
        r, dt = ctx.call("/api/graph/token", {"payload": "MSAuth1.0 usertoken=\"bogus-token-for-smoke-test\""})
        err = str(r.get("error", ""))
        ok("粘贴端点在 fabric 下被受理（不是「通道不支持」）",
           "不支持" not in err and "先切到 real" not in err,
           f"{dt:.1f}s -> {err[:180]}")
        ok("粘错内容会给出可照做的提示（列出常见原因）",
           ("常见原因" in err) or ("重新导出" in err) or r.get("ok") is True,
           err[:180])
        ok("验证失败时不把坏凭据留在库里（应回滚）",
           r.get("ok") is not True, "假令牌不该被当成登录成功")

        # ---- 6. 小米扫码不能挂住
        print("\n[6] 小米扫码（缺 migate 或网络受限时必须快速返回）")
        r, dt = ctx.call("/api/xiaomi/qr/start", {}, timeout=40)
        ok("扫码端点快速返回（未超时）", dt < 35, f"耗时 {dt:.1f}s -> {str(r)[:140]}")
        ok("扫码端点给出可读的提示",
           r.get("ok") is False and bool(r.get("error")), str(r)[:140])

        # ---- 7. 受保护端点
        print("\n[7] 鉴权边界")
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
