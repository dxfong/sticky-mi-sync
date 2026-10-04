#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米服务凭据换取（passToken -> serviceToken）。

为什么不用 migate.get_service
-----------------------------
migate 的 `requester.session` 是**模块级共享的**，而 `get_service` 内部会
`session.cookies.clear()`（在 finally 里）。这在单线程 CLI 里没问题，
但我们的后端是**多线程 HTTP 服务**：QR 登录阶段已经在同一个 session 上跑过几次请求，
残留 cookie + 可能的并发清空，会让 serviceLogin 拿不到 nonce/ssecurity。

实测对比：
  干净进程里用同一份 passToken 走这两步 -> 稳定成功（拿到 serviceToken）
  服务进程里走 migate.get_service     -> 返回 None

所以这里**用我们自己的 requests.Session 重新实现这两步**，与 migate 的全局状态完全隔离，
只借用它的 URL 常量。协议本身很简单：

  1. GET {SERVICELOGIN_URL}?_json=True&sid=i.mi.com
     （带上 deviceId / passToken / userId 作为 cookie）
     -> 返回 nonce / ssecurity / location / cUserId / psecurity
  2. clientSign = base64(sha1("nonce={nonce}&{ssecurity}"))
     GET {location}&clientSign={clientSign}
     -> 这一跳的 Set-Cookie 就是 serviceToken / i.mi.com_slh / i.mi.com_ph / userId

注意：小米会在 JSON 前面加 `&&&START&&&` 前缀，解析时要先剥掉。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

SID = "i.mi.com"
# migate 的会话文件默认在用户主目录下（~/.migatesession）。
# 这在容器里是个坑：主目录**不在数据卷里** → 容器重建就丢了，
# 而"数据全部持久化"是明确要求。所以允许用环境变量把它指到数据卷里
# （Dockerfile 里设 MIGATE_SESSION_DIR=/app/data/migatesession）。
# 不设时行为与以前完全一致，本机使用不受影响。
SESSION_DIR = Path(os.environ.get("MIGATE_SESSION_DIR") or (Path.home() / ".migatesession"))
UA = "offici5l/migate"
TIMEOUT = 30


def session_file(sid: str = SID) -> Path:
    return SESSION_DIR / sid / "session.json"


def save_session_file(pass_token: dict, sid: str = SID) -> None:
    """把登录态写进 migate 的会话文件，这样 CLI 也能复用（不用重新扫码）"""
    try:
        f = session_file(sid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(pass_token), encoding="utf-8")
    except Exception:
        pass


def load_session_file(sid: str = SID) -> dict | None:
    f = session_file(sid)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _strip_prefix(text: str) -> str:
    """剥掉小米的 &&&START&&& 前缀。不能无脑切 11 个字符 —— 万一前缀格式变了会切坏 JSON"""
    return text[11:] if text.startswith("&&&") else text


def acquire_service(pass_token: dict, sid: str = SID) -> dict[str, Any]:
    """用 passToken 换 i.mi.com 的服务凭据。失败时抛带诊断信息的异常。

    返回 {"servicedata": {...}, "cookies": {...}}
    """
    import requests

    need = {"deviceId", "passToken", "userId"}
    missing = need - set(pass_token or {})
    if missing:
        raise RuntimeError(f"登录态缺字段：{sorted(missing)}")

    # 独立会话 —— 与 migate 的全局 session 彻底隔离，这是修好这个 bug 的关键
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    for k, v in pass_token.items():
        s.cookies.set(k, v)

    # 第 1 步
    try:
        r = s.get("https://account.xiaomi.com/pass/serviceLogin",
                  params={"sid": sid, "_json": True}, timeout=TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"serviceLogin 请求异常：{type(e).__name__}: {e}") from None

    try:
        data = json.loads(_strip_prefix(r.text))
    except json.JSONDecodeError:
        raise RuntimeError(
            f"serviceLogin 返回不是 JSON（HTTP {r.status_code}）：{r.text[:200]}") from None

    nonce = data.get("nonce")
    ssecurity = data.get("ssecurity")
    location = data.get("location")
    if not nonce or not ssecurity or not location:
        raise RuntimeError(
            f"serviceLogin 没返回 nonce/ssecurity/location。"
            f"返回字段={sorted(data)}；code={data.get('code')}；"
            f"desc={data.get('desc') or data.get('description')}") from None

    # 第 2 步
    client_sign = quote(base64.b64encode(
        hashlib.sha1(f"nonce={nonce}&{ssecurity}".encode()).digest()))
    try:
        r2 = s.get(f"{location}&clientSign={client_sign}", timeout=TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"取服务 cookie 请求异常：{type(e).__name__}: {e}") from None

    # 两处都收：这一跳的 Set-Cookie + 会话里非登录态的新 cookie（不同版本行为不同）
    cookies: dict[str, str] = {}
    for k, v in s.cookies.get_dict().items():
        if k not in pass_token:
            cookies[k] = v
    cookies.update(r2.cookies.get_dict())

    if not cookies.get("serviceToken"):
        raise RuntimeError(
            f"没拿到 serviceToken（HTTP {r2.status_code}）。"
            f"这一跳 cookie 键={sorted(r2.cookies.get_dict())}；"
            f"会话 cookie 键={sorted(s.cookies.get_dict())}；"
            f"响应前 200 字={r2.text[:200]}")

    return {
        "servicedata": {
            "nonce": nonce,
            "ssecurity": ssecurity,
            "cUserId": data.get("cUserId"),
            "psecurity": data.get("psecurity"),
            "deviceId": pass_token.get("deviceId"),
        },
        "cookies": cookies,
    }
