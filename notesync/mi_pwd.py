#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米账号密码登录 —— 纯 HTTP，**容器里可以完整跑通，包括二次验证**。

为什么需要它
------------
扫码登录在容器里有个绕不过去的坎：**扫码产生的是「新设备」**，
小米会对新设备要求一次**交互式安全验证**（响应里的 `notificationUrl`
/ `isSecondValidation`）。而扫码那条链路（migate 的 `handle_browser_qr`）
**根本没有处理它** —— 拿到 cookie 就算成功，于是读笔记接口直接被 401 拒掉。
（用户实测踩到过：日志写「扫码登录成功」，但列表永远是空的。）

「浏览器登录」也不行：它要弹**有头**浏览器，容器里没显示器。

所以容器里唯一能完整走通的登录方式是**账号密码 + 网页版二次验证**：

    1. POST serviceLoginAuth2 {user, hash(md5(密码))}
       → 可能要求图形验证码（87001）
       → 可能返回 notificationUrl（要二次验证）
    2. 二次验证：拿可选方式（手机/邮箱）→ 发验证码 → 用户输验证码
       → 完成后**重新登录一次**，这次的 session 才是「已验证」的
    3. cookie 里拿到 deviceId / passToken / userId → 落库

整条链路都是普通 HTTPS 请求，没有终端、没有浏览器、没有交互界面 ——
所以能直接搬到网页上（本模块只做 HTTP，界面在 server.py + 前端）。

复用 migate 的 URL 常量与请求层，不重复实现协议细节。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()

# 单步网络超时
STEP_TIMEOUT = 25

STATE: dict[str, Any] = {
    "phase": "idle",       # idle | captcha | need_method | sent | success | error
    "message": "",
    # 图形验证码（登录前那一步，以及"发短信前"那一步都用它）
    "captcha_b64": "",
    # 二次验证
    "context": "",
    "options": [],         # 4 = 手机, 8 = 邮箱
    "address_type": "",    # PH | EM
    "account_mask": "",    # 脱敏后的手机号/邮箱（给用户确认发给谁）
    "quota": None,         # 剩余可发次数
    "sent_to": "",
    # 通用
    "started_at": 0.0,
    "user": "",
}


def status() -> dict[str, Any]:
    with LOCK:
        return dict(STATE)


def reset() -> None:
    """清空本轮登录的所有字段，但保留 user（重试时不用重新输账号）"""
    with LOCK:
        user = STATE.get("user") or ""
        STATE.update(
            phase="idle", message="", captcha_b64="",
            context="", options=[], address_type="", account_mask="",
            quota=None, sent_to="", started_at=time.time(), user=user,
        )


def _set(**kw: Any) -> None:
    with LOCK:
        STATE.update(**kw)


# ------------------------------------------------------------------ 底层

def _body(text: str) -> dict[str, Any]:
    """小米的 JSON 响应前面有 11 个字符的前缀（`&&&START&&&`），要剥掉"""
    try:
        return json.loads(text[11:])
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            return {"_raw": text[:300]}


def _http():
    """用 migate 的 requester —— **必须和登录共用同一个 session**，
    因为 cookie(登录态) 就存在里面，二次验证要靠它接上。"""
    from migate.requester import get, post, session
    return get, post, session


def _md5_upper(pwd: str) -> str:
    return hashlib.md5(pwd.encode()).hexdigest().upper()


def _fetch_captcha_b64(captcha_url: str) -> str:
    """把图形验证码取回来转成 base64，直接给前端 <img src="data:...">"""
    get, _, _ = _http()
    from migate.config import BASE_URL
    url = captcha_url if captcha_url.startswith("http") else BASE_URL + captcha_url
    r = get(url, timeout=STEP_TIMEOUT)
    return base64.b64encode(r.content).decode()


# ------------------------------------------------------------------ 第 1 步：账号密码

def login(user: str, password: str, capt_code: str = "") -> dict[str, Any]:
    """账号密码登录。会处理：图形验证码 → 二次验证 → 落库。

    `capt_code`：上一次调用如果返回了 `need_captcha`，把用户填的图形验证码带回来重试。
    """
    get, post, session = _http()
    from migate.config import SERVICELOGINAUTH2_URL, SERVICELOGIN_URL

    user = (user or "").strip()
    if not user or not password:
        return {"ok": False, "error": "账号和密码都要填"}

    reset()
    _set(user=user, started_at=time.time(), message="正在登录…")

    try:
        # 每次都重新拿一份干净的会话参数（和自带的 sid 等）
        auth_data: dict[str, Any] = {"sid": "i.mi.com", "_json": True}
        r = get(SERVICELOGIN_URL, params=auth_data, timeout=STEP_TIMEOUT)
        head = _body(r.text)
        auth_data.update({
            "serviceParam": head["serviceParam"],
            "qs": head["qs"],
            "callback": head["callback"],
            "_sign": head["_sign"],
        })
    except Exception as e:
        _set(phase="error", message=f"连不上小米登录服务：{e}")
        return {"ok": False, "error": str(e)}

    payload = dict(auth_data)
    payload.update({"user": user, "hash": _md5_upper(password)})
    if capt_code:
        payload["captCode"] = capt_code

    try:
        r = post(SERVICELOGINAUTH2_URL, data=payload, timeout=STEP_TIMEOUT)
        d = _body(r.text)
    except Exception as e:
        _set(phase="error", message=f"登录请求失败：{e}")
        return {"ok": False, "error": str(e)}

    code = d.get("code")

    # 图形验证码
    if code == 87001:
        b64 = ""
        try:
            b64 = _fetch_captcha_b64(d.get("captchaUrl") or "")
        except Exception as e:
            _set(phase="error", message=f"取图形验证码失败：{e}")
            return {"ok": False, "error": str(e)}
        _set(phase="captcha", captcha_b64=b64,
             message="需要输入图形验证码（看不清可点重试）")
        return {"ok": False, "need_captcha": True, "captcha_b64": b64}

    if code == 70016:
        _set(phase="error", message="账号或密码不对")
        return {"ok": False, "error": "账号或密码不对"}

    # ★ 二次验证：这是扫码那条路会漏掉的一步
    nurl = d.get("notificationUrl")
    if nurl:
        try:
            ctx = parse_qs(urlparse(nurl).query)["context"][0]
        except Exception:
            _set(phase="error", message=f"小米要求安全验证，但没能解析上下文：{nurl[:120]}")
            return {"ok": False, "error": "小米要求安全验证，但解析失败"}
        _set(context=ctx, message="小米要求安全验证")
        return _begin_verify(auth_data)

    if code == 0:
        return {"ok": True, **_harvest()}

    msg = d.get("tips") or d.get("desc") or str(d)
    _set(phase="error", message=f"登录失败：{msg}")
    return {"ok": False, "error": msg}


# ------------------------------------------------------------------ 第 2 步：二次验证

def _begin_verify(auth_data: dict[str, Any]) -> dict[str, Any]:
    """问小米支持哪些验证方式（手机 / 邮箱）"""
    get, _, _ = _http()
    from migate.config import LIST_URL

    with LOCK:
        ctx = STATE["context"]
    try:
        r = get(LIST_URL, params={"sid": "i.mi.com", "supportedMask": "0",
                                  "context": ctx}, timeout=STEP_TIMEOUT)
        d = _body(r.text)
    except Exception as e:
        _set(phase="error", message=f"取验证方式失败：{e}")
        return {"ok": False, "error": str(e)}

    options = d.get("options") or []
    if 4 not in options and 8 not in options:
        _set(phase="error",
             message=f"小米没有给出可用的验证方式（options={options}）。"
                     f"可能需要先在手机上完成一次安全验证。")
        return {"ok": False, "error": f"无可用验证方式，options={options}"}

    kinds = []
    if 4 in options:
        kinds.append({"type": "PH", "label": "手机短信"})
    if 8 in options:
        kinds.append({"type": "EM", "label": "邮箱"})

    _set(phase="need_method", options=options,
         message="请选择验证方式，然后点「发送验证码」")
    return {"ok": False, "need_verify": True, "methods": kinds,
            "message": "小米要求安全验证 —— 选一种方式收验证码"}


def send_code(address_type: str, capt_code: str = "") -> dict[str, Any]:
    """发验证码。可能先要过一道图形验证码（87001）。"""
    get, post, _ = _http()
    from migate.config import (SEND_EM_TICKET, SEND_PH_TICKET,
                               USERQUOTA_URL, BASE_URL)

    at = "EM" if address_type == "EM" else "PH"
    label = "邮箱" if at == "EM" else "手机"

    # 先看配额 —— 发太多次会被小米临时封掉，提前告诉用户比事后报错好
    try:
        rq = post(USERQUOTA_URL, data={"addressType": at, "contentType": "160040",
                                       "_json": "true"}, timeout=STEP_TIMEOUT)
        q = _body(rq.text)
        left = q.get("info")
        left = int(left) if left is not None else None
    except Exception:
        left = None
    if left == 0:
        _set(phase="error", message=f"今天给{label}发得太多了，明天再试")
        return {"ok": False, "error": f"今天给{label}发得太多了，明天再试"}
    _set(quota=left, address_type=at)

    send_url = SEND_EM_TICKET if at == "EM" else SEND_PH_TICKET
    payload = {"icode": capt_code, "_json": "true"} if capt_code else None

    try:
        r = post(send_url, data=payload, timeout=STEP_TIMEOUT) if payload \
            else post(send_url, timeout=STEP_TIMEOUT)
        d = _body(r.text)
    except Exception as e:
        _set(phase="error", message=f"发验证码失败：{e}")
        return {"ok": False, "error": str(e)}

    code = d.get("code")

    if code == 87001:
        b64 = ""
        try:
            b64 = _fetch_captcha_b64(d.get("captchaUrl") or "")
        except Exception as e:
            _set(phase="error", message=f"取图形验证码失败：{e}")
            return {"ok": False, "error": str(e)}
        _set(phase="captcha", captcha_b64=b64,
             message=f"发{label}验证码前需要图形验证码")
        return {"ok": False, "need_captcha": True, "captcha_b64": b64,
                "address_type": at}

    if code == 20024:
        wt = ((d.get("data") or {}).get("wt")) or 60
        _set(phase="error", message=f"发得太快了，请等约 {wt} 秒再试")
        return {"ok": False, "error": f"发得太快了，请等约 {wt} 秒再试"}

    if code == 0:
        _set(phase="sent", sent_to=label, message=f"验证码已发到{label}，请查看并填入")
        return {"ok": True, "sent": True, "to": label, "quota": left}

    msg = d.get("tips") or str(d)
    _set(phase="error", message=f"发验证码失败：{msg}")
    return {"ok": False, "error": msg}


def check_code(ticket: str) -> dict[str, Any]:
    """提交验证码。成功后**必须重新登录一次** —— 那样 session 才是「已验证」的。"""
    get, post, session = _http()
    from migate.config import (VERIFY_EM, VERIFY_PH, SERVICELOGINAUTH2_URL,
                               SERVICELOGIN_URL)

    ticket = (ticket or "").strip()
    if not ticket:
        return {"ok": False, "error": "请填验证码"}

    at = STATE.get("address_type") or "PH"
    url = VERIFY_EM if at == "EM" else VERIFY_PH

    try:
        r = post(url, data={"ticket": ticket, "trust": "true", "_json": "true"},
                 timeout=STEP_TIMEOUT)
        d = _body(r.text)
    except Exception as e:
        _set(phase="error", message=f"提交验证码失败：{e}")
        return {"ok": False, "error": str(e)}

    if d.get("code") == 70014:
        _set(message="验证码不对，再试一次")
        return {"ok": False, "error": "验证码不对"}

    loc = d.get("location")
    if d.get("code") != 0 or not loc:
        msg = d.get("tips") or str(d)
        _set(phase="error", message=f"验证没通过：{msg}")
        return {"ok": False, "error": msg}

    # ---- 验证过了，把跳转走完，再**重新登录**拿「已验证」的 session
    try:
        r1 = get(loc, allow_redirects=False, timeout=STEP_TIMEOUT)
        loc2 = r1.headers.get("Location")
        if loc2:
            get(loc2, allow_redirects=False, timeout=STEP_TIMEOUT)

        # 重新走一遍登录：这次小米不会再要验证（session 已标记为已验证）
        auth_data: dict[str, Any] = {"sid": "i.mi.com", "_json": True}
        r = get(SERVICELOGIN_URL, params=auth_data, timeout=STEP_TIMEOUT)
        head = _body(r.text)
        auth_data.update({"serviceParam": head["serviceParam"], "qs": head["qs"],
                          "callback": head["callback"], "_sign": head["_sign"]})

        # ★ 只传 auth_data，**不要再传 user/密码** ——
        #   登录态已经在 session cookie 里了，多传反而会被当成新的一次登录。
        #   （migate 的 verify.py 结尾就是这么做的：post(AUTH2, data=auth_data)。）
        r2 = post(SERVICELOGINAUTH2_URL, data=auth_data, timeout=STEP_TIMEOUT)
        d2 = _body(r2.text)
    except Exception as e:
        _set(phase="error", message=f"验证通过了，但后续登录失败：{e}")
        return {"ok": False, "error": f"验证通过但后续失败：{e}"}

    if d2.get("notificationUrl"):
        _set(phase="error",
             message="小米又要求了一次安全验证 —— 请重试，或在手机上先完成一次验证")
        return {"ok": False, "error": "小米又要求了一次验证，请重试"}

    if d2.get("code") != 0:
        msg = d2.get("tips") or str(d2)
        _set(phase="error", message=f"验证通过了，但重新登录失败：{msg}")
        return {"ok": False, "error": f"重新登录失败：{msg}"}

    return {"ok": True, **_harvest()}


# ------------------------------------------------------------------ 收尾

def _harvest() -> dict[str, Any]:
    """从 session cookie 里取出凭据并落库（和扫码流程共用同一套字段）"""
    _, _, session = _http()
    cookies = session.cookies.get_dict()
    required = {"deviceId", "passToken", "userId"}
    missing = required - cookies.keys()
    if missing:
        raise RuntimeError(f"登录返回缺少字段 {', '.join(sorted(missing))}")
    return {"pass_token": {k: cookies[k] for k in required}, "raw": cookies}
