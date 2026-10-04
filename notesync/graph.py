"""微软便笺数据源。

两个实现，接口一致：
  MockGraph  内存里的假便笺，不需要任何凭据，用来跑通全流程
  RealGraph  设备码 OAuth + Microsoft Graph /me/notes

为什么用设备码而不是 MSAL：
  同步器是个无头进程，设备码流程只需要用户在浏览器里输一次码，
  token 存下来之后可以长期用 refresh_token 续期。整个流程用标准库就能写完，
  不用引入 MSAL 这一大坨依赖 —— 正好符合「轻量」的要求。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .textutil import html_to_text, note_html

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{path}"
UA = "sticky-mi-sync/0.1"
TIMEOUT = 30


class OAuthError(Exception):
    """OAuth 端点返回的错误。保留结构化字段，便于上层按 code 分支处理
    （比如 device code 轮询里的 authorization_pending 就不是真错误）。"""

    def __init__(self, code: str, description: str = "", status: int = 0):
        self.code = code
        self.description = description
        self.status = status
        super().__init__(f"{code}：{description}"[:500] if description else code)


def _post_form(url: str, data: dict[str, str], token: str = "") -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 必须把 AAD 的错误体和错误码带出来，否则只剩一句 "HTTP Error 400"，
        # 完全没法定位（client_id 不存在 / 账户类型不支持 / 需要管理员同意 都是 400）
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw)
            raise OAuthError(err.get("error", ""),
                             err.get("error_description", ""), e.code) from None
        except json.JSONDecodeError:
            raise OAuthError("http_error", raw[:300], e.code) from None


def _request(
    method: str, url: str, token: str, data: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """返回 (状态码, 响应体)。4xx 不抛异常，交给调用方判断"""
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
               "User-Agent": UA}
    if extra_headers:
        headers.update(extra_headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _as_json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _as_json(raw)


def _as_json(raw: str) -> dict[str, Any]:
    """把响应体当 JSON 解；解不开就原样塞进 `_raw`。

    **不能直接 json.loads** —— Graph 的 `/$metadata` 返回的是 XML，
    直接解会抛 JSONDecodeError 把调用方炸掉（踩过：诊断脚本崩在 $metadata 上，
    白白绕了一大圈才发现 token 其实是好的）。
    """
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw[:500]}


# --------------------------------------------------------------------- Mock


class MockGraph:
    """内存假便笺。支持被外部增删改，用来观察同步行为。

    **默认不种任何数据**。之前种了 3 条「【示例】」便笺，后果是：
    只要打开同步，这 3 条就会被推进用户真实的小米笔记里。
    测"推一条过去"的场景时，凭空的 3 条示例就是垃圾数据。
    所以现在默认是空的，要测试自己加一条。
    """

    def __init__(self, seed: list[str] | None = None):
        self.notes: dict[str, dict[str, Any]] = {}
        self._seq = 0
        for text in (seed or []):
            self.create(text)

    def _new_id(self) -> str:
        self._seq += 1
        return f"AAMkMOCK{self._seq:04d}"

    # ---- 外部操纵（前端 mock 面板用）
    def create(self, text: str) -> dict[str, Any]:
        nid = self._new_id()
        self.notes[nid] = {
            "id": nid, "text": text, "change_key": f"CK{nid}-1",
            "modified": int(time.time()), "deleted": False,
        }
        return self.notes[nid]

    def update(self, note_id: str, text: str) -> dict[str, Any]:
        n = self.notes[note_id]
        n["text"] = text
        n["modified"] = int(time.time())
        n["change_key"] = n["change_key"].split("-")[0] + "-" + str(int(time.time()))
        return n

    def delete(self, note_id: str) -> None:
        self.notes[note_id]["deleted"] = True

    # ---- 数据源接口
    def status(self) -> dict[str, Any]:
        return {"mode": "mock", "connected": True, "account": "mock-user@example.com"}

    def list_notes(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        return [dict(n) for n in self.notes.values()]

    def create_note(self, text: str, when_iso: str = "") -> dict[str, Any]:
        return self.create(text)

    def update_note(self, note_id: str, text: str, change_key: str = "",
                    when_iso: str = "") -> dict[str, Any]:
        return self.update(note_id, text)

    def delete_note(self, note_id: str) -> None:
        self.delete(note_id)


# --------------------------------------------------------------------- Real


class RealGraph:
    """真实便笺。凭据存在 store.cred['graph_token']"""

    def __init__(self, store):
        self.store = store
        self.token = store.get_cred("graph_token", {}) or {}
        self.last_error = ""
        self._acct_cache = ""
        self._acct_at = 0.0
        # 通道："" 未探测 / "notes" = /me/notes（窄权限 ShortNotes.*）
        #                        / "mail"  = /me/mailfolders/notes/messages（Mail.Read）
        # 便笺在邮箱里就是 Notes 文件夹里的条目，所以两条路都能读到。
        # /me/notes 权限更窄优先；拿不到 ShortNotes 权限时自动退回 mail 通道。
        self.channel = ""
        # 实际可用的 tenant（common / consumers），由 start_device_login 探测后写入
        self.tenant_effective = ""

    # ---------------------------------------------------------- 通道探测
    def _detect_channel(self, token: str) -> str:
        """探测哪条通道可用，结果缓存起来（每轮都探会很浪费）"""
        if self.channel:
            return self.channel
        code, data = _request("GET", f"{GRAPH}/me/notes?$top=1&$select=id", token)
        if code == 200:
            self.channel = "notes"
            return self.channel
        # 403 = 没有 ShortNotes 权限；401 之类的交给上层报错
        code2, data2 = _request(
            "GET", f"{GRAPH}/me/mailfolders/notes/messages?$top=1&$select=id", token)
        if code2 == 200:
            self.channel = "mail"
            self.store.log(
                "ShortNotes 权限不可用，已自动改用 mail 通道"
                "（/me/mailfolders/notes/messages）—— 只读可用，写入需要 ShortNotes.ReadWrite",
                "warn")
            return self.channel
        msg = _err_text(code, data) if code != 403 else _err_text(code2, data2)
        raise RuntimeError(
            f"两条便笺通道都不可用。\n/me/notes 返回 {code}；"
            f"/me/mailfolders/notes/messages 返回 {code2}。\n{msg}")

    def _require_notes_channel(self) -> None:
        if self.channel == "mail":
            raise RuntimeError(
                "当前只有 mail 通道（Mail.Read），只能读。"
                "要写入便笺需要 ShortNotes.ReadWrite 权限 —— "
                "在 Azure 应用注册里加上这个委托权限再重新登录一次即可。")

    # ---------------------------------------------------------- 认证
    def _cfg(self) -> dict[str, Any]:
        return self.store.cfg["graph"]

    def _url(self, path: str) -> str:
        # tenant_effective 是探测出来的实际可用端点（见 start_device_login），
        # 后续的 token 轮询与刷新必须用同一个，否则会 AADSTS 报错。
        tenant = self.tenant_effective or self._cfg().get("tenant") or "common"
        return LOGIN.format(tenant=tenant, path=path)

    def start_device_login(self) -> dict[str, Any]:
        cfg = self._cfg()
        if not cfg.get("client_id"):
            raise RuntimeError("还没填 Azure 应用的 client_id")

        # 纯个人账号注册的应用，tenant 必须是 consumers 而不是 common
        # （否则报 AADSTS9002346 "…Microsoft Account users only…use /consumers"）。
        # 逐个试，成功就用那个，并写回配置 —— 用户不用自己搞清这件事。
        want = cfg.get("tenant") or "common"
        candidates = [want]
        for t in ("common", "consumers"):
            if t not in candidates:
                candidates.append(t)

        errors: list[str] = []
        for tenant in candidates:
            try:
                resp = _post_form(LOGIN.format(tenant=tenant, path="devicecode"), {
                    "client_id": cfg["client_id"],
                    "scope": cfg["scope"],
                })
            except OAuthError as e:
                errors.append(f"{tenant} → {e.description or e.code}")
                continue
            except Exception as e:
                errors.append(f"{tenant} → {type(e).__name__}: {e}")
                continue

            self.tenant_effective = tenant
            if tenant != want:
                self.store.cfg["graph"]["tenant"] = tenant
                self.store.save_config()
                self.store.log(f"tenant 自动改为 {tenant}（{want} 不适用于这个应用）")
            self.store.set_cred("graph_device", {
                "device_code": resp.get("device_code", ""),
                "interval": int(resp.get("interval") or 5),
                "expires_at": int(time.time()) + int(resp.get("expires_in") or 900),
            })
            return {
                "user_code": resp.get("user_code", ""),
                "verification_uri": resp.get("verification_uri", ""),
                "expires_in": resp.get("expires_in", 900),
                "message": resp.get("message", ""),
                "tenant": tenant,
            }

        raise RuntimeError("设备码请求都失败了。\n" + "\n".join(errors))

    def poll_device_login(self) -> dict[str, Any]:
        dev = self.store.get_cred("graph_device", {}) or {}
        if not dev.get("device_code"):
            return {"status": "error", "error": "没有进行中的登录，请先点开始登录"}
        if time.time() > dev.get("expires_at", 0):
            self.store.del_cred("graph_device")
            return {"status": "error", "error": "设备码已过期，请重新开始"}
        try:
            resp = _post_form(self._url("token"), {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self._cfg()["client_id"],
                "device_code": dev["device_code"],
            })
        except OAuthError as e:
            err = e.code
            if err in ("authorization_pending", "slow_down"):
                return {"status": "pending"}
            if err == "authorization_declined":
                self.store.del_cred("graph_device")
                return {"status": "error", "error": "你在授权页拒绝了授权"}
            if err == "expired_token":
                self.store.del_cred("graph_device")
                return {"status": "error", "error": "设备码已过期，请重新开始登录"}
            if err == "bad_verification_code":
                self.store.del_cred("graph_device")
                return {"status": "error", "error": "设备码无效，请重新开始登录"}
            return {"status": "error",
                    "error": f"{err}：{e.description[:300]}"}

        self._save_token(resp)
        self.store.del_cred("graph_device")
        return {"status": "success", "account": self.account()}

    def _save_token(self, resp: dict[str, Any]) -> None:
        old = self.store.get_cred("graph_token", {}) or {}
        self.token = {
            "access_token": resp.get("access_token", ""),
            "refresh_token": resp.get("refresh_token") or old.get("refresh_token", ""),
            "expires_at": int(time.time()) + int(resp.get("expires_in") or 3600) - 60,
        }
        self.store.set_cred("graph_token", self.token)

    def ensure_token(self) -> str:
        """有 token 就返回；快过期或没有就用 refresh_token 换新的"""
        if self.token.get("access_token") and time.time() < self.token.get("expires_at", 0):
            return self.token["access_token"]
        rt = self.token.get("refresh_token")
        if not rt:
            if self.token.get("kind") == "pasted":
                raise RuntimeError("粘贴的访问令牌已过期（Graph Explorer 的令牌约 1 小时），"
                                   "请重新复制一个新的粘进来")
            raise RuntimeError("未登录微软账号")
        resp = _post_form(self._url("token"), {
            "grant_type": "refresh_token",
            "client_id": self._cfg()["client_id"],
            "refresh_token": rt,
            "scope": self._cfg()["scope"],
        })
        self._save_token(resp)
        return self.token["access_token"]

    def use_pasted_token(self, access_token: str) -> dict[str, Any]:
        """快捷通道：直接粘贴一个访问令牌，**不需要 Azure 应用注册**。

        最快的验证方式是 Microsoft Graph Explorer：
        登录 → Modify permissions 勾 ShortNotes.Read → Consent → 复制 Access token。
        令牌约 1 小时过期，过期后重新粘一个即可。
        """
        access_token = (access_token or "").strip()
        if not access_token:
            raise RuntimeError("令牌是空的")
        # 先做个格式预检，免得拿垃圾字符串去请求、换来一个难懂的库报错
        if access_token.count(".") < 2 or len(access_token) < 100:
            raise RuntimeError(
                "这不像一个访问令牌。请整段复制 Graph Explorer 里的 Access token"
                "（很长的一串，通常 800 字符以上，形如 eyJ0eXAi...）")
        exp = _jwt_exp(access_token) or int(time.time()) + 3000
        prev = self.store.get_cred("graph_token", {}) or {}
        self.token = {
            "access_token": access_token,
            "refresh_token": prev.get("refresh_token", ""),   # 别把旧的 refresh_token 弄丢
            "expires_at": exp,
            "kind": "pasted",
        }
        self.store.set_cred("graph_token", self.token)
        # 立刻验一次：自动探测哪条通道能用（/me/notes 或 mail 通道），
        # 只要有一条能读就算登录成功 —— 否则只有 Mail.Read 的令牌会被误拒。
        self.channel = ""
        try:
            self._detect_channel(access_token)
        except Exception as e:
            self.last_error = str(e)
            raise RuntimeError(f"这个令牌读不到便笺：{e}")
        left = max(0, int(exp - time.time()))
        return {"ok": True, "expires_in_min": round(left / 60),
                "channel": self.channel, "account": self.account()}

    def logout(self) -> None:
        self.token = {}
        self._acct_cache = ""
        self._acct_at = 0.0
        self.channel = ""
        self.store.del_cred("graph_token")

    def account(self) -> str:
        """当前登录账号。**带缓存** —— 前端每 2 秒拉一次状态，
        每次都发一次 /me 请求会白白打接口，也会让状态栏抖动。"""
        now = time.time()
        if self._acct_cache and now - self._acct_at < 300:
            return self._acct_cache
        try:
            code, data = _request("GET", f"{GRAPH}/me?$select=userPrincipalName,mail",
                                  self.ensure_token())
            if code == 200:
                self._acct_cache = data.get("mail") or data.get("userPrincipalName") or ""
                self._acct_at = now
        except Exception:
            pass
        return self._acct_cache

    # ---------------------------------------------------------- 数据源接口
    def status(self) -> dict[str, Any]:
        cfg = self._cfg()
        # 粘贴令牌登录时没有 refresh_token，所以不能只看 refresh_token 判断
        has_access = (bool(self.token.get("access_token"))
                      and time.time() < self.token.get("expires_at", 0))
        connected = bool(has_access or self.token.get("refresh_token"))
        left = 0
        if has_access:
            left = max(0, round((self.token.get("expires_at", 0) - time.time()) / 60))
        return {
            "mode": "real",
            "connected": connected,
            "configured": bool(cfg.get("client_id")),
            "account": self.account() if connected else "",
            "token_kind": self.token.get("kind", "oauth"),
            "expires_in_min": left,
            "channel": self.channel,
            "channel_label": {
                "notes": "官方便笺通道（ShortNotes 权限，可读可写）",
                "mail": "邮箱便笺通道（Mail.Read，只读）",
                "": "未探测",
            }.get(self.channel, self.channel),
            "error": self.last_error,
        }

    def list_notes(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        token = self.ensure_token()
        channel = self._detect_channel(token)
        if channel == "mail":
            return self._list_via_mail(token)
        return self._list_via_notes(token)

    def diagnose(self) -> dict[str, Any]:
        """登录后立刻做一次**分层**体检，把"卡在哪一层"直接定位出来。

        为什么需要它
        ------------
        一个 401 至少有三种完全不同的原因，而它们的修法毫不相干：
          A. token 根本不被 Graph 接受
          B. token 有效，但**缺某个权限**
          C. token 和权限都对，但**服务端对这个账号类型没开通该终点**

        靠猜太费时间，每猜错一次还要用户重新登录一次。所以一次把四层都测了：

          第 1 层  token 在不在（以及它是什么格式）
          第 2 层  GET /$metadata —— **不需要任何权限，token 有效就返回 200**。
                  这是"token 层"最干净的判据
          第 3 层  GET /me —— 需要 User.Read
          第 4 层  GET /me/notes（ShortNotes）
                  GET /me/mailfolders/notes/messages（备选通道，需要 Mail.Read）

        **关于 MSA 的 token 格式（吃过一次亏，在这里写清楚）**
        微软官方文档明确写着：面向 Microsoft 服务的 token「可能使用特殊格式，
        不会作为 JWT 通过校验，而且对个人账户（MSA）可能还是加密的」。
        所以个人微软账号拿到 `EwA4…` 这种单段加密串是**正常现象**，
        绝不能因为"不是三段式 JWT"就判定 token 坏了 —— 必须靠 $metadata 实测。
        （之前正是栽在这个误判上，白绕了一大圈。）
        """
        out: dict[str, Any] = {"ok": True, "steps": [], "verdict": ""}

        def step(name: str, ok: bool, detail: str) -> None:
            out["steps"].append({"name": name, "ok": ok, "detail": detail})
            if not ok:
                out["ok"] = False

        # --- 第 1 层：token 本身
        tok = self.store.get_cred("graph_token") or {}
        at = str(tok.get("access_token") or "")
        if not at:
            step("token 存在", False, "库里没有 access_token")
            out["verdict"] = "没有 token —— 需要重新登录"
            return out

        n_seg = at.count(".") + 1
        if n_seg == 3:
            claims = _jwt_claims(at)
            step("token 格式", True,
                 f"三段式 JWT　aud={claims.get('aud')}　"
                 f"scp={claims.get('scp') or claims.get('roles') or '(空)'}")
        else:
            # **别把"不是 JWT"当成错误**。微软官方文档明确说明：
            # 面向 Microsoft 服务的 token「可能使用特殊格式，不会作为 JWT 通过校验，
            # 而且对个人账户（MSA）可能还是加密的」。
            # 所以个人账号拿到这种单段加密串是正常的，能不能用要看下面 $metadata。
            step("token 格式", True,
                 f"单段加密 token（{len(at)} 字符，开头 “{at[:12]}…”）—— "
                 "这是**个人微软账号的正常格式**。是不是有效，看下一层 $metadata 的实测结果。")

        # --- 第 2 层：$metadata —— 不需要任何权限，只要 token 有效就 200
        code, body = _request("GET", f"{GRAPH}/$metadata", at)
        if code != 200:
            step("GET /$metadata", False,
                 f"HTTP {code}：{json.dumps(body, ensure_ascii=False)[:260]}")
            out["verdict"] = (
                "token **不被 Graph 接受** —— 连不需要任何权限的 $metadata 都过不了。"
                "问题出在 token 本身（签发端 / client_id / 账号类型），与 ShortNotes 无关。")
            return out
        step("GET /$metadata", True, "200 —— token 有效，Graph 接受它")

        # --- 第 3 层：/me（需要 User.Read）
        code_me, body_me = _request("GET", f"{GRAPH}/me", at)
        if code_me == 200:
            step("GET /me", True,
                 str(body_me.get("userPrincipalName") or body_me.get("mail")
                     or body_me.get("id") or ""))
        else:
            step("GET /me", False,
                 f"HTTP {code_me}（需要 User.Read）："
                 f"{json.dumps(body_me, ensure_ascii=False)[:200]}")

        # --- 第 3 层：ShortNotes 通道 + 备选通道
        code, body = _request("GET", f"{GRAPH}/me/notes?$top=3", at)
        notes_ok = code == 200
        n = len(body.get("value") or []) if notes_ok else 0
        step("GET /me/notes", notes_ok,
             (f"读到 {n} 条" if notes_ok
              else f"HTTP {code}：{_err_text(code, body)[:300]}"))

        code_m, body_m = _request(
            "GET", f"{GRAPH}/me/mailfolders/notes/messages?$top=3&$select=id,subject", at)
        step("GET /me/mailfolders/notes/messages", code_m == 200,
             f"HTTP {code_m}：{_err_text(code_m, body_m)[:300]}")

        if notes_ok:
            out["verdict"] = f"✅ 全部通过 —— /me/notes 可用，当前读到 {n} 条便笺"
            return out

        if code_me != 200:
            out["verdict"] = (
                "token 有效（$metadata 通过），但**连 /me 都读不了** —— "
                "说明这个 token 里没有 User.Read。把 scope 加上 User.Read "
                "重新登录一次，这样才能区分「权限没给够」和「服务端不支持」。")
        else:
            out["verdict"] = (
                "token 有效、/me 能读，唯独 **/me/notes 被拒**。"
                "在权限确实已授予的前提下，这通常意味着**服务端对这个账号类型"
                "没有开通该终点**。下一步要换数据来源，而不是继续调权限。")
        return out

    def _paged(self, token: str, url: str, pick) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while url:
            code, data = _request("GET", url, token)
            if code != 200:
                self.last_error = f"列取便笺失败 {code}：{_err_text(code, data)}"
                raise RuntimeError(self.last_error)
            for item in data.get("value", []):
                out.append(pick(item))
            url = data.get("@odata.nextLink", "")
        self.last_error = ""
        return out

    def _list_via_notes(self, token: str) -> list[dict[str, Any]]:
        def pick(item: dict) -> dict:
            return {
                "id": item.get("id", ""),
                "text": (item.get("body") or {}).get("content") or "",
                "change_key": item.get("changeKey", ""),
                "modified": 0,
                "modified_iso": item.get("lastModifiedDateTime", ""),
                "deleted": bool(item.get("isDeleted")),
            }
        return self._paged(
            token,
            f"{GRAPH}/me/notes?$select=id,subject,body,changeKey,"
            f"lastModifiedDateTime,isDeleted&$top=100",
            pick)

    def _list_via_mail(self, token: str) -> list[dict[str, Any]]:
        """退回通道：便笺在邮箱里就是 Notes 文件夹里的条目，按 message 读。

        注意：body.content 是 HTML，要转成纯文本再走后面的规范化，
        否则和小米侧比对时会因为标签差异反复判定"有改动"。
        """
        def pick(item: dict) -> dict:
            body = (item.get("body") or {}).get("content") or ""
            text = html_to_text(body) if "<" in body else body
            return {
                "id": item.get("id", ""),
                "text": text,
                "change_key": item.get("changeKey", ""),
                "modified": 0,
                "modified_iso": item.get("lastModifiedDateTime", ""),
                "deleted": False,     # 消息通道不返回软删标记，靠"不在列表里"判断
            }
        return self._paged(
            token,
            f"{GRAPH}/me/mailfolders/notes/messages"
            f"?$select=id,subject,body,changeKey,lastModifiedDateTime&$top=100",
            pick)

    # ------------------------------------------------------ 写操作：按通道分发
    #
    # 为什么必须分发：`/me/notes`（ShortNotes）这个终点**对个人微软账号根本不存在**
    # （实测返回 400 `Resource not found for the segment 'notes'`，不是权限问题）。
    # 官方文档写着支持个人账户，实际没开通。所以 MSA 只能走 Notes 邮件文件夹那条路，
    # 读已经是这样了，写也必须跟上，否则整个同步只能单向。
    def create_note(self, text: str, when_iso: str = "") -> dict[str, Any]:
        if self._detect_channel(self.ensure_token()) == "mail":
            return self._create_via_mail(text)
        return self._create_via_notes(text)

    def update_note(self, note_id: str, text: str,
                    change_key: str = "", when_iso: str = "") -> dict[str, Any]:
        if self._detect_channel(self.ensure_token()) == "mail":
            return self._update_via_mail(note_id, text)
        return self._update_via_notes(note_id, text, change_key)

    def delete_note(self, note_id: str) -> None:
        if self._detect_channel(self.ensure_token()) == "mail":
            return self._delete_via_mail(note_id)
        return self._delete_via_notes(note_id)

    # ------------------------------------------------------ 写操作：ShortNotes 通道
    def _create_via_notes(self, text: str) -> dict[str, Any]:
        self._require_notes_channel()
        code, data = _request("POST", f"{GRAPH}/me/notes", self.ensure_token(), {
            "body": {"contentType": "text", "content": text},
        })
        if code not in (200, 201):
            raise RuntimeError(f"新建便笺失败 {code}：{_err_text(code, data)}")
        return {"id": data.get("id", ""), "change_key": data.get("changeKey", "")}

    def _update_via_notes(self, note_id: str, text: str,
                          change_key: str = "") -> dict[str, Any]:
        self._require_notes_channel()
        headers = {"If-Match": change_key} if change_key else None
        code, data = _request("PATCH", f"{GRAPH}/me/notes/{note_id}",
                              self.ensure_token(),
                              {"body": {"contentType": "text", "content": text}},
                              headers)
        if code == 412:
            raise Conflict("这条便笺在别处被改过了（412），本轮放弃写入")
        if code not in (200, 201):
            raise RuntimeError(f"更新便笺失败 {code}：{_err_text(code, data)}")
        return {"id": note_id, "change_key": data.get("changeKey", "")}

    def _delete_via_notes(self, note_id: str) -> None:
        self._require_notes_channel()
        code, data = _request("DELETE", f"{GRAPH}/me/notes/{note_id}",
                              self.ensure_token())
        if code not in (200, 204):
            raise RuntimeError(f"删除便笺失败 {code}：{_err_text(code, data)}")

    # ------------------------------------------------------ 写操作：mail 通道
    def _create_via_mail(self, text: str) -> dict[str, Any]:
        """通过 Notes 邮件文件夹创建便笺。

        **字段映射是实测出来的，和直觉相反**：
        一条真便笺里，`subject` 装的是**便笺全文**（纯文本），
        `body` 装的是**同一份内容的 HTML 版**。
        便笺没有"标题/正文"的区分 —— 它的整个内容就是一个字段。

        注意 `subject` 有长度上限（约 255 字符），长便笺会被截断；
        所以完整正文要以 `body` 为准（读取时也正是读 body）。

        第一版实现成「subject=首行标题、body=纯文本正文」，
        结果创建出来是一条草稿邮件，形态和真便笺完全不同。
        """
        tok = self.ensure_token()
        code, data = _request("POST", f"{GRAPH}/me/mailfolders/notes/messages",
                              tok, {
                                  "subject": (text or "")[:255],
                                  "body": {"contentType": "html",
                                           "content": note_html(text)},
                              })
        if code not in (200, 201):
            raise RuntimeError(f"新建便笺失败 {code}：{_err_text(code, data)}")
        return {"id": data.get("id", ""), "change_key": data.get("changeKey", "")}

    def _update_via_mail(self, note_id: str, text: str) -> dict[str, Any]:
        """更新便笺。字段映射同 `_create_via_mail`：subject 与 body 都要写，
        而且要写**同一份内容**，否则两个字段会不一致。"""
        tok = self.ensure_token()
        code, data = _request("PATCH", f"{GRAPH}/me/messages/{note_id}", tok, {
            "subject": (text or "")[:255],
            "body": {"contentType": "html", "content": note_html(text)},
        })
        if code == 412:
            raise Conflict("这条便笺在别处被改过了（412），本轮放弃写入")
        if code not in (200, 201):
            raise RuntimeError(f"更新便笺失败 {code}：{_err_text(code, data)}")
        return {"id": note_id, "change_key": data.get("changeKey", "")}

    def _delete_via_mail(self, note_id: str) -> None:
        tok = self.ensure_token()
        code, data = _request("DELETE", f"{GRAPH}/me/messages/{note_id}", tok)
        if code not in (200, 204):
            raise RuntimeError(f"删除便笺失败 {code}：{_err_text(code, data)}")


class Conflict(Exception):
    """乐观并发冲突（HTTP 412）"""


# --------------------------------------------------------------------- Local


class LocalGraph:
    """微软便笺的**本地库**数据源（Windows 便笺 App 的 plum.sqlite）。

    为什么需要它
    ------------
    Windows 便笺 App 的 2026 年数据**两条 API 路都读不到**：
      * `GET /me/notes`（v1.0）  → 400，终点不存在
      * `GET /me/notes`（beta）  → 403，第三方拿不到 ShortNotes 授权
      * `GET /me/mailfolders/notes/messages` → 只有 2020–2025，最后停在 2025-12

    而本地库里**什么都有**（165 条，最新几条正是 App 里的 2026 便笺，
    每条都带 RemoteId，说明确实同步过）。所以本机场景下，本地库是唯一
    能拿到完整数据的地方。

    **只读。** 写入仍然走服务端 —— 直接改 plum.sqlite 风险太高：
    App 有自己的内存缓存和 delta 状态，我们改了它并不知道，
    轻则改动被它覆盖，重则两边打架丢数据。
    """

    def __init__(self, store):
        self.store = store
        self.last_error = ""

    def status(self) -> dict[str, Any]:
        from .plumnotes import find_db, stats
        try:
            db = find_db()
        except Exception as e:
            db, err = None, f"{type(e).__name__}: {e}"
        else:
            err = ""
        if not db:
            return {"mode": "local", "connected": False, "configured": False,
                    "account": "", "token_kind": "local-db", "expires_in_min": 0,
                    "channel": "local", "channel_label": "本地便笺库",
                    "error": err or "没找到 Windows 便笺的本地库（plum.sqlite）"}
        try:
            st = stats(db)
        except Exception as e:
            return {"mode": "local", "connected": False, "configured": True,
                    "account": "", "token_kind": "local-db", "expires_in_min": 0,
                    "channel": "local", "channel_label": "本地便笺库",
                    "error": f"{type(e).__name__}: {e}"}
        return {"mode": "local", "connected": True, "configured": True,
                "account": "本地便笺库",
                "token_kind": "local-db", "expires_in_min": 0,
                "channel": "local", "channel_label": "本地便笺库（plum.sqlite）",
                "db": str(db), "count": st["count"],
                "newest": st["newest"], "oldest": st["oldest"],
                "by_year": st["by_year"], "error": ""}

    def list_notes(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        from .plumnotes import read_notes
        notes = read_notes()
        self.last_error = ""
        return notes

    def _readonly(self, what: str):
        raise RuntimeError(
            f"本地便笺库是**只读**数据源，不能{what}。"
            "要写入请把微软侧切到 real 模式（走服务端）——"
            "直接改 plum.sqlite 会让便笺 App 的缓存和 delta 状态对不上，"
            "轻则改动被覆盖，重则两边打架丢数据。")

    def create_note(self, text: str, when_iso: str = "") -> dict[str, Any]:
        self._readonly("新建便笺")

    def update_note(self, note_id: str, text: str,
                    change_key: str = "", when_iso: str = "") -> dict[str, Any]:
        self._readonly("修改便笺")

    def delete_note(self, note_id: str) -> None:
        self._readonly("删除便笺")


# --------------------------------------------------------------------- Fabric


class NotesFabricGraph:
    """微软便笺的**云端**数据源（NotesFabric）—— 这才是正确的那个。

    三种微软数据源对比：

    | 类 | 数据源 | 内容 | 可部署 |
    |---|---|---|---|
    | MockGraph | 内存假数据 | — | — |
    | RealGraph | Graph / Notes 邮件文件夹 | 只有 2020–2025（缺 2026） | ✅ 但数据不全 |
    | LocalGraph | 本机 plum.sqlite | 全部 165 条 | ❌ 容器里没有该文件 |
    | **NotesFabricGraph** | **substrate NotesFabric** | **全部 165 条** | ✅ **正确方案** |

    实测：`substrate.office.com/NotesFabric/api/v2.0/me/notes` 返回 165 条，
    与本地库按年分布完全一致（2020:17 / 2021:33 / 2022:56 / 2023:18 /
    2024:16 / 2025:14 / 2026:11），两个独立来源互相验证。

    **可读可写**，认证用 `MSAuth1.0 usertoken`（由 OWA 网页版生成，
    从 Playwright 持久化 profile 里自动截取 —— headless，容器里能跑）。
    """

    def __init__(self, store):
        self.store = store
        from .notesfabric import FabricAuth, FabricClient
        self.auth = FabricAuth(store)
        self.client = FabricClient(store, self.auth)
        self.last_error = ""

    @property
    def channel(self) -> str:
        return "fabric"

    def status(self) -> dict[str, Any]:
        from .notesfabric import BASE
        d = self.auth.load()
        has = bool(d.get("authorization"))
        prof = (Path(self.store.data_dir) / "onenote_profile").exists()
        return {
            "mode": "fabric",
            "connected": has,
            "configured": True,
            "account": str(d.get("anchormailbox") or "").replace("MSA:", ""),
            "token_kind": "MSAuth1.0 usertoken",
            "expires_in_min": 0,
            "channel": "fabric",
            "channel_label": "NotesFabric（微软云端便笺 · 可读写）",
            "endpoint": BASE,
            "has_profile": prof,
            "error": self.last_error if has else (
                "" if prof else "还没有网页版 profile —— 先跑 probe_onenote.py 登录一次"),
        }

    def use_pasted_token(self, payload: str) -> dict[str, Any]:
        """粘贴 NotesFabric 凭据（`MSAuth1.0 usertoken`）—— **容器里唯一的登录方式**。

        为什么必须是"粘贴"：
          fabric 的 usertoken 由 OWA / 便笺网页版在页面里生成，
          **OAuth 流程拿不到它**（设备码也不行 —— 那条路换出来的是 Graph 令牌，
          读不到 NotesFabric）。所以在全新环境（容器/服务器）里首次登录只有两条路：
            ① 在容器里跑一次 headless 浏览器截取 —— 首次要交互登录，容器做不到；
            ② 从**已经登录好的环境**（你自己的电脑）把凭据粘过来 —— 就是这个入口。

        接受的写法（做了容错，不必手改成 JSON）：
          1. 完整 JSON：
               {"authorization": "MSAuth1.0 usertoken=\\"...\\"",
                "anchormailbox": "MSA:you@outlook.com"}
          2. 原始请求头（从浏览器 DevTools → 请求头 直接复制）：
               Authorization: MSAuth1.0 usertoken="..."
               x-anchormailbox: MSA:you@outlook.com
          3. 只粘一行 token 字符串（会自动补上 sdkversion）。

        粘完立刻发一次请求验证 —— 「保存了」和「能用」是两件事，
        不验证的话用户会拿着一个过期令牌对着空列表猜。
        """
        payload = (payload or "").strip()
        if not payload:
            raise RuntimeError("内容是空的")

        auth: dict[str, Any] = {}

        # ---- 写法 1：JSON
        if payload.startswith("{"):
            try:
                auth = json.loads(payload)
            except Exception as e:
                raise RuntimeError(f"看着像 JSON 但解析失败：{e}")

        # ---- 写法 2/3：按行解析 HTTP 头（大小写与前缀都容错）
        if not auth:
            for line in payload.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip().lower().replace("_", "-")
                v = v.strip().strip('"').strip("'")
                if k in ("authorization", "authorization-header"):
                    auth["authorization"] = v
                elif k in ("x-anchormailbox", "anchormailbox", "x-anchor-mailbox"):
                    auth["anchormailbox"] = v
                elif k in ("stickynotes-sdkversion", "sdkversion"):
                    auth["sdkversion"] = v
            # ---- 写法 3：整段就是一行裸 token
            if not auth and payload.count("\n") == 0:
                auth["authorization"] = payload

        auth = {k: str(v).strip() for k, v in auth.items() if v}
        auth.setdefault("sdkversion", "StickyNotes-Web/11.5.10")

        # ---- 格式预检：拦掉"粘错东西"，给出能照着做的提示
        az = auth.get("authorization", "")
        if not az:
            raise RuntimeError(
                "没解析出 authorization。请从浏览器 DevTools 的请求头里整段复制 "
                "Authorization: MSAuth1.0 usertoken=\\\"...\\\" 那一行粘过来")
        if "MSAuth1.0" not in az and not az.startswith("Ew"):
            raise RuntimeError(
                "这段 Authorization 不像是 NotesFabric 的令牌（应以 MSAuth1.0 usertoken= 开头）。"
                "⚠ 别拿 Graph 的令牌（eyJ0eXAi... 那种 JWT）来粘 —— "
                "那是另一套通道，读不到 2026 年的便笺。")
        if ", type=" not in az and 'type=' not in az:
            raise RuntimeError(
                "Authorization 不完整 —— 少了结尾的 `, type=\"MSACT\"`。\n"
                "DevTools 里这一行很长，很容易只选中前半段。请从行名开始整行复制：\n"
                '  Authorization: MSAuth1.0 usertoken="Ew...", type="MSACT"')
        if not auth.get("anchormailbox"):
            # 只粘 authorization 一行时最常踩这个 —— 缺了账号标识，
            # usable() 会判定不可用，但用户看不到任何解释。
            raise RuntimeError(
                "只解析出了 Authorization，缺 x-anchormailbox。\n"
                "**请把两行都粘进来**（在 DevTools 里从行名开始整行复制）：\n"
                '  Authorization: MSAuth1.0 usertoken="Ew...", type="MSACT"\n'
                "  x-anchormailbox: MSA:你的账号@outlook.com")

        # ---- 保存后立刻验证；**不过就回滚**。
        #   旧顺序是"已保存 + 报验证失败"，既自相矛盾，又把一份用不了的凭据
        #   留在库里（下次启动还会拿它去试，报一堆 401）。
        #   ★ 日志也必须在**验证通过之后**才记 —— 否则会出现
        #     "日志说导入成功、凭据表里却什么都没有"（用户实测踩到过，非常误导）。
        prev = self.auth.load()
        self.auth.save(auth)
        try:
            self.last_error = ""
            notes = self.client.list_all(page_limit=1)
        except Exception as e:
            if prev.get("authorization"):
                self.auth.save(prev)          # 回滚到导入前的状态
            else:
                self.auth.clear()
            self.last_error = str(e)
            raise RuntimeError(
                f"凭据格式没问题，但微软拒绝了这次请求：{e}\n"
                "**最可能的原因是 token 已经轮换了** —— NotesFabric 的 usertoken\n"
                "换得很勤（分钟级），而你在 DevTools 里看到的往往是**已经发生过的旧请求**。\n"
                "请这样做：在便笺网页上**点开一条便笺**（触发一次新请求）→\n"
                "回到 Network 面板找到那条**最新**的 substrate.office.com 请求 →\n"
                "立刻复制它的 Authorization / x-anchormailbox。\n"
                "其它可能：复制不完整（usertoken 很长）、账号不对、"
                "或那台机器的便笺网页版本身已掉登录。")

        self.store.log(f"微软便笺：已导入 NotesFabric 凭据并验证通过"
                       f"（{auth.get('anchormailbox', '账号未知')}）")

        return {
            "ok": True,
            "account": str(auth.get("anchormailbox", "")).replace("MSA:", ""),
            "verified_count": len(notes),
            "message": f"导入成功，已读到 {len(notes)} 条便笺（首页）",
        }

    def list_notes(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        """拉便笺列表。

        `max_pages` 限制翻页数 —— 便笺每页固定 19 条，拉全 165 条要 16 个请求
        （约 24 秒）。做「只同步最新一条」这类受限操作时，第一页就够了。
        """
        from .notesfabric import note_text, note_time
        raw = self.client.list_all(page_limit=max_pages or 40)
        out = []
        for n in raw:
            out.append({
                "id": str(n.get("id") or ""),
                "text": note_text(n),
                "change_key": str(n.get("changeKey") or ""),
                # modified 是 FILETIME 语义的数字字段，这里用不上（用 ISO 那个），
                # 但 engine 会拿 int() 包一下，所以给 0 而不是 None
                "modified": 0,
                "modified_iso": note_time(n),
                "deleted": False,
            })
        # **按真实修改时间倒序**。服务端返回的顺序不保证，而 createdAt 是占位值
        # 完全不能用 —— 只能靠 documentModifiedAt。
        out.sort(key=lambda n: n.get("modified_iso") or "", reverse=True)
        self.last_error = ""
        return out

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        """只取**这一条**便笺，不走全量翻页。

        给「只同步改动过内容的」这类窄操作省时间：全量要翻十几页约 20 秒，
        而那种操作通常只涉及几条记录（就是 link 表里那几条）。
        返回的形状与 `list_notes()` 的单条**完全一致**，上层可以无差别处理。
        取不到（或已删除）返回 None。
        """
        from .notesfabric import note_text, note_time
        if not note_id:
            return None
        try:
            n = self.client.get(note_id)
        except Exception:
            return None          # 交给调用方的全量兜底
        if not n:
            return None
        if n.get("isDeleted"):
            return None          # 已删除的当作"这一侧没有"，窄操作不碰
        return {
            "id": str(n.get("id") or note_id),
            "text": note_text(n),
            "change_key": str(n.get("changeKey") or ""),
            "modified": 0,
            "modified_iso": note_time(n),
            "deleted": False,
        }

    def create_note(self, text: str, when_iso: str = "") -> dict[str, Any]:
        """`when_iso`：内容原本的修改时间，用来让另一端排序保持原样"""
        r = self.client.create(text, when_iso=when_iso)
        return {"id": str(r.get("id") or ""),
                "change_key": str(r.get("changeKey") or "")}

    def update_note(self, note_id: str, text: str,
                    change_key: str = "", when_iso: str = "") -> dict[str, Any]:
        r = self.client.update(note_id, text, change_key, when_iso=when_iso)
        return {"id": note_id, "change_key": str(r.get("changeKey") or "")}

    def delete_note(self, note_id: str) -> None:
        # 删除也要带 changeKey（服务端一致性检查），所以先取一次当前值
        cur = self.client.get(note_id)
        self.client.delete(note_id, str(cur.get("changeKey") or ""))


# --------------------------------------------------------------------- 工具


def _err_text(code: int, data: Any) -> str:
    """从 Graph 的错误响应里抠出人能看懂的一句话"""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return f"[{err.get('code','')}] {err.get('message','')}"[:300]
        if err:
            return str(err)[:300]
    return str(data)[:300]


def _jwt_claims(token: str) -> dict[str, Any]:
    """解出 JWT 的 payload。解不开就返回空 dict（不抛异常）。

    **不校验签名** —— 这里只是用来看 token 里到底有什么（aud / scp / ver），
    诊断用，不承担任何安全判断。
    """
    try:
        seg = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except Exception:
        return {}


def _jwt_exp(token: str) -> int:
    """从 JWT 里取过期时间。只读 payload，不做签名校验 ——
    这是用户自己提供的令牌，我们只是想知道它什么时候失效。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return int(data.get("exp") or 0)
    except Exception:
        return 0
