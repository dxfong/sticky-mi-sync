"""微软便笺的**真实云端**数据源（NotesFabric）。

为什么不是 Graph
----------------
Graph 上没有便笺这个资源 —— 实测：

    GET https://graph.microsoft.com/v1.0/me/notes   → 400 Resource not found
    GET https://graph.microsoft.com/beta/me/notes   → 403 ErrorAccessDenied

官方文档写着个人账户支持 `ShortNotes.ReadWrite`，**实际没开通**。
便笺真正在的地方是微软的 substrate 服务：

    https://substrate.office.com/NotesFabric/api/v2.0/me/notes

认证也不是 Bearer JWT，而是 MSA 的 usertoken：

    Authorization: MSAuth1.0 usertoken="EwBIBOl3BAAU…"
    x-anchormailbox: MSA:your-alias@outlook.com
    stickynotes-sdkversion: StickyNotes-Web/11.5.10

`MSAuth1.0` 和 `Bearer` 是两套完全不同的认证体系 —— 这是之前所有 Graph 尝试
都失败的根本原因：**不是权限不够，是找错服务了**。

数据形态
--------
一条便笺长这样（实测）：

    {
      "id":   "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0A…",   ← 稳定主键，跨设备一致
      "title": "测试 2026\\n",                              ← 便笺全文（纯文本）
      "document": {                                     ← ★ 权威内容：结构化 blocks
         "blocks": [ { "type": "par",
                       "content": [{"styles": [], "text": "测试 2026"}],
                       "id": "937b3162-…" } ] },
      "createdAt": "2020-04-23T05:32:27Z",              ← ⚠ 占位值，不可排序
      "documentModifiedAt": "2026-09-12T17:31:34Z",     ← ★ 真实修改时间
      "changeKey": "CQAAABYAAABCzSE7cSGeQKlYzwj181NtAAmN0iVs"   ← ★ 更新必带（乐观锁）
    }

`createdAt` 是占位值这一点，与 Windows 本地库 `plum.sqlite` 的 `CreatedAt`
**完全一致** —— 两个独立来源互相印证：排序只能用 `documentModifiedAt`。

分页
----
响应顶层是 `{"skipToken": "...", "value": [...]}`，**一页 19 条**。
追着 `skipToken` 翻页，直到它为空 —— 165 条约 16 页。
注意是自定义的 `skipToken`，**不是** OData 的 `$skiptoken`，
传 `$top` / `top` / `pageSize` 都无效。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE = "https://substrate.office.com/NotesFabric/api/v2.0/me/notes"
TIMEOUT = 45
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

# substrate 是境外服务，和 graph 一样需要代理时走系统代理；这里保持默认行为
OPENER = urllib.request.build_opener()


class FabricError(RuntimeError):
    """NotesFabric 调用失败。带上状态码，便于上层区分 401/404/412。"""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class Conflict(FabricError):
    """412：changeKey 不匹配，说明别处改过了。"""


# --------------------------------------------------------------------- 文档结构


def blocks_to_text(doc: dict | None) -> str:
    """`document.blocks` → 纯文本。

    每个 block 一行；block 里的 `content[]` 是若干 run（带 styles 的文本片段），
    拼起来就是这一行的内容。空 block 就是空行。

    这样处理的好处：往返可逆 —— `text_to_blocks()` 生成的 blocks 再解析回来
    能得到同样的文本，不会每同步一轮就多/少空行。
    """
    if not doc:
        return ""
    blocks = doc.get("blocks") or []
    lines: list[str] = []
    for b in blocks:
        parts = []
        for c in (b.get("content") or []):
            parts.append(str(c.get("text") or ""))
        lines.append("".join(parts))
    return "\n".join(lines)


def text_to_blocks(text: str | None) -> dict:
    """纯文本 → `document` 结构。

    两个类型标注都是**抓包实测**的，缺一个服务端就回
    `400 InvalidJson: invalid/missing types` 而不说缺什么：

      * `document.type = "document"`
      * `blocks[].type = "paragraph"`   ← 注意**不是** `"par"`

    （GET 返回里这个字段被日志截断成过 `"par..."`，所以一度误用了 `"par"`。）
    """
    import uuid

    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    for line in lines:
        blocks.append({
            "id": str(uuid.uuid4()),
            "type": "paragraph",
            "blockStyles": {"textDirection": "ltr"},
            "content": [{"styles": [], "text": line}],
        })
    return {"type": "document", "blocks": blocks}


def note_text(n: dict) -> str:
    """从一条便笺里取出纯文本内容。

    优先用 `document.blocks`（结构最完整）；退化时才用 `title`
    —— 因为 `title` 是服务端从内容里派生的，长便笺可能被截断。
    """
    t = blocks_to_text(n.get("document"))
    if t.strip():
        return t
    return n.get("title") or ""


def note_time(n: dict) -> str:
    """取真实修改时间。**绝不能用 createdAt**（那是占位值）。"""
    return (n.get("documentModifiedAt") or n.get("lastModified")
            or n.get("createdAt") or "")


# --------------------------------------------------------------------- 认证


class FabricAuth:
    """管理 `MSAuth1.0 usertoken`。

    这个 token **没法用 OAuth 流程直接拿** —— 它由 OWA / 便笺网页版在页面里生成。
    所以获取方式是在 Playwright 里打开 `outlook.live.com/mail/notes`，
    让页面自己发一次请求，把它的 `Authorization` 头截下来。

    设计成"缓存 + 过期刷新"：
      * token 存到 store.cred['fabric_auth']
      * 调用返回 401 时自动刷新一次并重试
      * 刷新走 headless（可进 Docker，不需要 GUI）
    """

    # token 本身的真实过期时间拿不到（是加密串），所以按经验值保守处理，
    # 并在**每次 401 时**强制刷新 —— 不靠猜。
    DEFAULT_TTL = 1800

    def __init__(self, store):
        self.store = store
        self.last_error = ""

    # ---- 存取
    def load(self) -> dict:
        return self.store.get_cred("fabric_auth", {}) or {}

    def save(self, headers: dict) -> None:
        data = dict(headers)
        data["at"] = int(time.time())
        self.store.set_cred("fabric_auth", data)

    def clear(self) -> None:
        self.store.del_cred("fabric_auth")

    def usable(self) -> bool:
        d = self.load()
        return bool(d.get("authorization") and d.get("anchormailbox"))

    # ---- 刷新
    def refresh(self) -> dict:
        """用 Playwright 打开便笺网页，截下它自己的 Authorization 头。

        刷新会顺带更新 `x-anchormailbox`（账号标识），所以换账号也能自动跟上。
        """
        import subprocess
        import sys

        root = Path(__file__).resolve().parent.parent
        profile = Path(self.store.data_dir) / "onenote_profile"
        if not profile.exists():
            self.last_error = ("还没有网页版 profile —— 先跑一次 "
                               "probe_onenote.py 登录（只需一次）")
            return {}

        from .browser_auth import find_python_with_playwright
        exe = find_python_with_playwright() or sys.executable
        try:
            r = subprocess.run(
                [exe, "-u", str(root / "capture_owa_auth.py"), "--json",
                 "--data-dir", str(self.store.data_dir)],
                capture_output=True, text=True, timeout=300,
                cwd=str(root), encoding="utf-8", errors="replace")
        except Exception as e:
            self.last_error = f"刷新 token 失败：{type(e).__name__}: {e}"
            return {}

        payload = None
        for line in reversed((r.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                break
        if not payload or not payload.get("authorization"):
            self.last_error = (f"没截到 Authorization 头。"
                               f"{(payload or {}).get('error') or (r.stderr or '')[-160:]}")
            return {}

        hdrs = {
            "authorization": payload["authorization"],
            "anchormailbox": payload.get("anchormailbox") or "",
            "sdkversion": payload.get("sdkversion") or "StickyNotes-Web/11.5.10",
        }
        self.save(hdrs)
        self.store.log("微软便笺凭据已刷新（MSAuth1.0 usertoken）")
        return hdrs


# --------------------------------------------------------------------- 客户端


class FabricClient:
    """NotesFabric 的读写客户端。只依赖 urllib，无第三方库。"""

    def __init__(self, store, auth: FabricAuth | None = None):
        self.store = store
        self.auth = auth or FabricAuth(store)

    # ---- 底层
    def _headers(self, extra: dict | None = None) -> dict:
        d = self.auth.load()
        h = {
            "Authorization": d.get("authorization") or "",
            "x-anchormailbox": d.get("anchormailbox") or "",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://outlook.live.com/",
            "scenario-notescomponent": "NotesFolder",
            "stickynotes-sdkversion": d.get("sdkversion") or "StickyNotes-Web/11.5.10",
            "User-Agent": UA,
        }
        if extra:
            h.update(extra)
        return h

    def _call(self, method: str, url: str, body: dict | None = None,
              extra: dict | None = None, _retry: bool = True) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(extra))
        try:
            with OPENER.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                if not raw:
                    return resp.status, {}
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, {"_raw": raw[:400]}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            # 401 = token 过期，刷新一次再重试（只允许一次，避免递归）
            if e.code == 401 and _retry:
                self.store.log("便笺接口 401，正在刷新凭据…", "warn")
                if self.auth.refresh():
                    return self._call(method, url, body, extra, _retry=False)
                raise FabricError(401, f"凭据刷新后仍 401：{raw[:180]}") from None
            if e.code == 412:
                raise Conflict(412, "便笺在别处被改过（412 Precondition Failed）") from None
            raise FabricError(e.code, f"HTTP {e.code}：{raw[:220]}") from None
        except Exception as e:
            raise FabricError(-1, f"{type(e).__name__}: {e}") from None

    # ---- 读
    def list_all(self, page_limit: int = 40) -> list[dict]:
        """拉全部便笺（自动翻页）。

        分页靠响应里的 `skipToken`，一页 19 条，165 条约 16 页。
        """
        out: list[dict] = []
        token = ""
        for _ in range(page_limit):
            url = BASE + (("?skipToken=" + urllib.parse.quote(token)) if token else "")
            code, j = self._call("GET", url)
            if code not in (200, 201):
                raise FabricError(code, f"拉列表失败：{str(j)[:200]}")
            out.extend(j.get("value") or [])
            token = j.get("skipToken") or ""
            if not token:
                break
        return out

    def get(self, note_id: str) -> dict:
        code, j = self._call("GET", f"{BASE}/{urllib.parse.quote(note_id)}")
        if code not in (200, 201):
            raise FabricError(code, f"读单条失败：{str(j)[:200]}")
        return j

    # ---- 写
    def create(self, text: str, color: int = 0, when_iso: str = "") -> dict:
        """新建便笺。

        请求体是**抓包实测**的（不是猜的）：

            POST /NotesFabric/api/v2.0/me/notes
            {
              "title": "全文",
              "document": {"type": "document", "blocks": [...]},
              "color": 0,
              "createdWithLocalId": "<uuid>",     ← 客户端生成的本地 id
              "createdByApp": "OWA",              ← 来源标识
              "documentModifiedAt": "<ISO>"
            }

        **`document.type` 和 `blocks[].type` 都必须有** —— 缺了服务端只回
        `invalid/missing types` 加一段回显，完全看不出缺哪个字段。

        `when_iso` 是**内容原本的修改时间**（从另一端带过来）。
        必须用它而不是 now —— 否则一批记录会在同一秒写进来，
        另一端按时间排序时**顺序全乱**（这个现象用户报过）。
        注意服务端的 `createdAt` 是占位值、不参与排序，排序看的就是
        `documentModifiedAt`，所以这里改它是有效的。
        """
        import uuid
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
            + "%06dZ" % datetime.now(timezone.utc).microsecond
        body = {
            "title": text or "",
            "document": text_to_blocks(text),
            "color": color,
            "createdWithLocalId": str(uuid.uuid4()),
            "createdByApp": "OWA",
            # 有原始时间就用原始时间，这才是排序该依据的字段
            "documentModifiedAt": when_iso or now,
        }
        code, j = self._call("POST", BASE, body)
        if code not in (200, 201):
            raise FabricError(code, f"新建便笺失败：{str(j)[:220]}")
        return j

    def update(self, note_id: str, text: str, change_key: str = "",
               when_iso: str = "") -> dict:
        """更新便笺。

        请求体是**抓包实测**的（在网页版改一条便笺截下来的），
        和创建比多了几个"原样带回"的字段：

            PATCH /NotesFabric/api/v2.0/me/notes/{id}
            {
              "id": "…",                        ← body 里也要
              "document": {"type":"document","blocks":[{"type":"paragraph",…}]},
              "color": 0,
              "changeKey": "…",                 ← ★ body 里必须有（不只是 If-Match 头）
              "createdByApp": "OWA",
              "createdAt": "…", "lastModified": "…",   ← ★ 原值带回
              "documentModifiedAt": "…",        ← ★ 本次修改时间
              "title": "全文"
            }

        `changeKey` 同时放进 body 和 `If-Match` 头：前者是这个接口的要求，
        后者是标准的乐观并发语义 —— 别处改过就 412，我们不动它，
        交给冲突处理，**绝不硬覆盖**（这是用户数据的底线）。
        """
        from datetime import datetime, timezone

        cur = self.get(note_id)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%06dZ" % now_dt.microsecond
        ck = change_key or str(cur.get("changeKey") or "")
        body = {
            "id": note_id,
            "document": text_to_blocks(text),
            "color": int(cur.get("color") or 0),
            "changeKey": ck,
            "createdByApp": cur.get("createdByApp") or "OWA",
            "createdAt": cur.get("createdAt") or now,
            "lastModified": cur.get("lastModified") or cur.get("createdAt") or now,
            # 和 create 同理：有原始时间就用它，排序才不会被同步动作打乱
            "documentModifiedAt": when_iso or now,
            "title": text or "",
        }
        extra = {"If-Match": f'"{ck}"'} if ck else None
        code, j = self._call("PATCH", f"{BASE}/{urllib.parse.quote(note_id)}",
                             body, extra)
        if code == 412:
            raise Conflict(412, "便笺在别处被改过（412 Precondition Failed）")
        if code not in (200, 201, 204):
            raise FabricError(code, f"更新便笺失败：{str(j)[:220]}")
        return j if isinstance(j, dict) else {}

    def delete(self, note_id: str, change_key: str = "") -> None:
        extra = {"If-Match": f'"{change_key}"'} if change_key else None
        code, j = self._call("DELETE", f"{BASE}/{urllib.parse.quote(note_id)}",
                             None, extra)
        if code not in (200, 202, 204):
            raise FabricError(code, f"删除便笺失败：{str(j)[:220]}")
