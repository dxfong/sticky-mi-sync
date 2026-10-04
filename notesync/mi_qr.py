#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米扫码登录（在网页里完成，不需要开终端）。

为什么单独写一个模块
--------------------
migate 自带三种登录方式，但它的实现是**面向终端的**：
浏览器模式会调 `webbrowser.open()`，扫码模式用 `qr.print_ascii()` 把二维码
打在终端里。我们的后端是个无头 HTTP 服务，没有 stdin，也没法"在终端里等用户扫码"。

但看穿它的实现就会发现：扫码登录**本质上是个两阶段 HTTP 流程**，跟终端无关 ——
  A. GET longPolling/loginUrl  -> 拿到一个 loginUrl（就是一串 URL）
  B. GET lp（长轮询，阻塞）     -> 用户在手机上有动作后返回，随后 cookie 里就有登录态
终端只是把 A 拿到的 URL 画成了二维码。所以把 A 的结果渲染成图片发给浏览器，
B 放到后台线程里跑，整件事就能在页面上完成。

复用 migate 的请求层与 URL 常量，不重复实现协议细节。
"""

from __future__ import annotations

import io
import json
import threading
import time
from typing import Any

LOCK = threading.Lock()

# 阶段 A（拿 loginUrl）的网络超时（秒）。
# 这两步要连 i.mi.com；容器/服务器网络受限时会一直挂住，
# 而它是跑在 HTTP 请求线程里的 —— 不设限就等于把接口挂死，前端只能干等。
QR_TIMEOUT = 20

STATE: dict[str, Any] = {
    "phase": "idle",      # idle | waiting | success | error
    "message": "",
    "qr_svg": "",
    "qr_url": "",
    "tips": "",
    "started_at": 0.0,
    "expires_at": 0.0,
    "account": "",
}


def _qr_svg(url: str) -> str:
    """把 URL 渲染成 SVG 二维码。用 migate 已经依赖的 qrcode 库，纯 Python，无额外依赖"""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # 去掉固定宽高，让它在页面里自适应
    svg = svg.replace("width=", "data-w=", 1).replace("height=", "data-h=", 1)
    return svg


def status() -> dict[str, Any]:
    with LOCK:
        return {k: v for k, v in STATE.items() if k != "qr_svg"}


def snapshot() -> dict[str, Any]:
    """给前端用的完整状态（含二维码图）"""
    with LOCK:
        return dict(STATE)


def cancel() -> None:
    with LOCK:
        if STATE["phase"] == "waiting":
            STATE.update(phase="idle", message="已取消", qr_svg="", qr_url="")
        STATE["generation"] = STATE.get("generation", 0) + 1


def _finish(store, pass_token: dict) -> dict[str, Any]:
    """登录态拿到了 —— 换出 i.mi.com 的服务 cookie 并落库。

    这里**故意不用 migate.get_service**：它依赖 migate 的模块级全局 session，
    在我们这种多线程服务里会被残留 cookie 和并发清空搞坏（实测在干净进程里能成功、
    在服务进程里返回 None）。改用 mi_auth.acquire_service —— 自有会话，完全隔离。
    """
    from . import mi_auth

    # 先把登录态存起来 —— 即使后面失败，也不会白扫一次码
    mi_auth.save_session_file(pass_token)

    try:
        svc = mi_auth.acquire_service(pass_token, "i.mi.com")
    except Exception as e:
        raise RuntimeError(
            f"{e}　（登录态已保存，可直接双击 login-xiaomi.bat 完成剩余步骤）")

    cookies = svc.get("cookies") or {}
    merged = dict(cookies)
    for k in ("cUserId", "slh", "ph", "deviceId", "uLocale"):
        if k in pass_token and k not in merged:
            merged[k] = pass_token[k]

    # 两个都存：serviceToken 短效（日常用），passToken 长效（过期时自动续期用）
    store.set_cred("xiaomi_cookie", merged)
    store.set_cred("xiaomi_pass_token", pass_token)
    store.log(f"小米扫码登录成功（userId={merged.get('userId')}），"
              f"cookie 键={sorted(merged)}，已保存 passToken，之后可自动续期")
    return merged


def _worker(store, auth_data: dict, lp: str, timeout: int, generation: int) -> None:
    """后台长轮询：等用户在手机上确认扫码"""
    import migate
    from migate.requester import get, session

    try:
        resp = get(lp, timeout=timeout + 15)
    except Exception as e:
        with LOCK:
            if STATE.get("generation") == generation:
                STATE.update(phase="error", message=f"等待扫码超时或失败：{e}")
        return

    with LOCK:
        if STATE.get("generation") != generation:
            return          # 已经被取消/重新发起了，丢弃这次结果

    cookies = session.cookies.get_dict()
    required = {"deviceId", "passToken", "userId"}
    missing = required - cookies.keys()
    if missing:
        with LOCK:
            STATE.update(phase="error",
                         message=f"登录返回缺少字段 {', '.join(sorted(missing))}；"
                                 f"响应片段：{resp.text[:200]}")
        return

    pass_token = {k: cookies[k] for k in required}
    try:
        merged = _finish(store, pass_token)
    except Exception as e:
        with LOCK:
            STATE.update(phase="error", message=str(e))
        return
    finally:
        try:
            session.cookies.clear()
        except Exception:
            pass

    with LOCK:
        STATE.update(phase="success", message="登录成功",
                     account=str(merged.get("userId") or ""))


def start(store) -> dict[str, Any]:
    """阶段 A：拿 loginUrl 并渲染二维码，同时把阶段 B 丢到后台线程"""
    import migate
    from migate.config import LONGPOLLING_URL, SERVICELOGIN_URL
    from migate.requester import get

    with LOCK:
        if STATE["phase"] == "waiting" and time.time() < STATE["expires_at"]:
            return {"ok": True, "reused": True, **{k: v for k, v in STATE.items()}}
        STATE["generation"] = STATE.get("generation", 0) + 1
        generation = STATE["generation"]
        STATE.update(phase="waiting", message="正在获取二维码…", qr_svg="",
                     qr_url="", tips="", account="",
                     started_at=time.time(), expires_at=time.time() + 300)

    # ★ 阶段 A 的两步都要连小米服务器（也就是这个函数会阻塞住 HTTP 请求线程）。
    #   旧代码直接在这里同步调 get()，**没有任何超时** —— 容器里访问 i.mi.com
    #   慢或被墙时就会一直挂住，前端停在"获取二维码…"不动（用户实测反馈）。
    #   整段包进带超时的线程：超时就返回明确错误并提示改用粘贴 Cookie。
    box: dict[str, Any] = {}

    def _stage_a() -> None:
        try:
            # migate 的 session 是模块级共享的，上一次尝试（或别处调用）留下的 cookie
            # 会污染这次登录。每次开始前先清干净，保证是一次干净的握手。
            try:
                from migate.requester import session as _s
                _s.cookies.clear()
            except Exception:
                pass

            # 第 1 步：拿 serviceLogin 的会话参数
            auth_data: dict[str, Any] = {"sid": "i.mi.com", "_json": True}
            r = get(SERVICELOGIN_URL, params=auth_data)
            head = json.loads(r.text[11:])
            auth_data.update({
                "serviceParam": head["serviceParam"],
                "qs": head["qs"],
                "callback": head["callback"],
                "_sign": head["_sign"],
                "_json": False,          # 长轮询这一步必须关掉 _json
            })

            # 第 2 步：拿 loginUrl（扫码用）与长轮询地址 lp
            r2 = get(LONGPOLLING_URL, params=auth_data)
            info = json.loads(r2.text[11:])
            box.update(
                ok=True,
                auth_data=auth_data,
                login_url=info["loginUrl"],
                lp=info["lp"],
                timeout=int(info.get("timeout") or 120),
                tips=info.get("qrTips", ""),
            )
        except Exception as e:
            box.update(ok=False, err=e)

    th = threading.Thread(target=_stage_a, daemon=True)
    th.start()
    th.join(timeout=QR_TIMEOUT)

    if th.is_alive():
        with LOCK:
            STATE.update(phase="error", message="连接小米服务器超时")
        return {"ok": False, "error":
                f"连接小米服务器超时（{QR_TIMEOUT} 秒无响应）。"
                "请确认这台机器能访问 i.mi.com；"
                "若网络受限，改用「粘贴 Cookie」登录。"}

    if not box.get("ok"):
        err = box.get("err")
        with LOCK:
            STATE.update(phase="error", message=f"获取二维码失败：{err}")
        return {"ok": False, "error": str(err)}

    auth_data = box["auth_data"]
    lp = box["lp"]
    login_url = box["login_url"]
    timeout = box["timeout"]
    tips = box["tips"]
    try:
        svg = _qr_svg(login_url)
    except Exception as e:
        with LOCK:
            STATE.update(phase="error", message=f"二维码渲染失败：{e}")
        return {"ok": False, "error": str(e)}

    with LOCK:
        STATE.update(phase="waiting", message="请用小米手机扫码",
                     qr_svg=svg, qr_url=login_url, tips=tips,
                     expires_at=time.time() + timeout)

    threading.Thread(target=_worker,
                     args=(store, auth_data, lp, timeout, generation),
                     daemon=True).start()

    return {"ok": True, "reused": False, **snapshot()}
