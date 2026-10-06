"""访问密码与登录会话。

这是一台机器上的**单用户个人工具**，没有邮件服务器 ——
所以"忘记密码 → 收邮件重置"这条路根本不存在，也不该假装存在。
**唯一的重置方式是从宿主机 shell 跑 CLI**：

    python server.py --set-password          # 交互输入（推荐）
    docker compose exec sticky-mi-sync python server.py --set-password

这条约束是**刻意的**，不是偷懒：能跑上面那条命令的人，
本来就能读 `data/` 里的数据库、改配置、拿到小米凭据。
所以它只是给"本来就有的权限"加个便利，**不是新开一个网络可达的后门**。
（自托管产品的通行做法：Capstan / Portainer / Dify / n8n 全都是容器内 CLI 重置，
 没有一家做网络侧的密码重置端点。）

实现上的几个硬要求：

· **PBKDF2-HMAC-SHA256**（标准库 hashlib，不引第三方依赖）。
  存成 `pbkdf2_sha256$迭代数$盐$哈希`，前缀可升级。
· **会话令牌只存哈希**。cookie 里放随机 32 字节，库里只留它的 sha256 ——
  数据库被看到也不能直接拿去登录。
· **改密码 = 立刻吊销所有会话**（靠 `epoch` 递增）。
  否则改完密码，旧 cookie 还能继续用 —— 那"重置"就白做了。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

#: cookie 名
COOKIE = "sms_session"
#: 会话有效期（秒）。30 天，够长；改密码会立刻全吊销。
SESSION_TTL = 30 * 24 * 3600
#: PBKDF2 迭代次数。写进哈希串里，以后可以单独升级老哈希。
ITERATIONS = 260_000
#: 密码最短长度。个人自用工具，不做复杂度硬要求（那只会逼出 "abc123!"）。
MIN_LEN = 8


# ---------------------------------------------------------------- 密码哈希

def hash_password(pw: str) -> str:
    """把明文密码变成可存储的哈希串。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(pw: str, stored: str) -> bool:
    """校验密码。用 compare_digest 做常数时间比较，别用 ==。"""
    try:
        algo, iters, salt_b64, hash_b64 = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                  salt, int(iters))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def check_strength(pw: str) -> str | None:
    """返回不合格的原因；合格返回 None。"""
    if not pw:
        return "密码不能为空"
    if len(pw) < MIN_LEN:
        return f"密码至少 {MIN_LEN} 位"
    if len(pw) > 200:
        return "密码过长"
    return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 鉴权

class Auth:
    """密码 + 会话。状态都存在 store 里，所以重启不丢登录态。"""

    def __init__(self, store, log=None):
        self.store = store
        self._log = log or (lambda msg, level="info": None)
        # 登录失败节流：内存计数即可（重启归零无所谓，重启本身要宿主机权限）
        self._fails: dict[str, list[float]] = {}
        # ★ 会话表是"读-改-写"的：`s = self._sessions(); s[k] = ...; _save_sessions(s)`。
        #   不加锁的话，两个并发登录会各自读到同一份快照，
        #   后写的把先写的整个盖掉 —— 表现就是**另一个设备的登录会莫名失效**。
        #   server 是 ThreadingHTTPServer，并发是常态。
        self._lock = threading.Lock()
        self._last_why = ""

    # ---------------------------------------------------------- 密码

    def has_password(self) -> bool:
        return bool(self.store.get_cred("auth_password"))

    def set_password(self, pw: str, *, revoke: bool = True) -> None:
        """设置/重置密码。**默认吊销所有会话** —— 重置的意义就在于此。"""
        self.store.set_cred("auth_password", hash_password(pw))
        if revoke:
            self.revoke_all()
        self._log("访问密码已更新（所有已登录会话已失效）", "warn")

    def verify(self, pw: str) -> bool:
        stored = self.store.get_cred("auth_password") or ""
        if not stored:
            return False
        return verify_password(pw, stored)

    def clear_password(self) -> None:
        """清掉密码 —— 下次打开页面会回到"首次设置"状态。"""
        self.store.del_cred("auth_password")
        self.revoke_all()
        self._log("访问密码已清除（页面会要求重新设置）", "warn")

    # ---------------------------------------------------------- 会话

    def _epoch(self) -> int:
        try:
            return int(self.store.get_meta("auth_epoch") or "0")
        except Exception:
            return 0

    def _sessions(self) -> dict[str, dict]:
        """{token_hash: {"exp": 过期时间, "epoch": 签发时的 epoch}}"""
        raw = self.store.get_cred("auth_sessions") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        now = time.time()
        out: dict[str, dict] = {}
        for k, v in raw.items():
            try:
                # 兼容老格式（直接存过期时间戳）
                rec = v if isinstance(v, dict) else {"exp": float(v), "epoch": 0}
                if float(rec.get("exp") or 0) > now:
                    out[k] = rec
            except Exception:
                continue
        return out

    def _save_sessions(self, s: dict[str, dict]) -> None:
        self.store.set_cred("auth_sessions", s)

    def new_session(self) -> str:
        """签发一个新会话，返回要放进 cookie 的令牌（明文只出现这一次）。

        epoch **写在会话记录里**，而不是让客户端带上来 ——
        放 cookie 里等于多一个客户端可伪造的字段，还得额外校验一致性。
        """
        token = secrets.token_urlsafe(32)
        with self._lock:                 # 读-改-写必须原子，否则并发登录互相覆盖
            s = self._sessions()
            s[token_hash(token)] = {"exp": time.time() + SESSION_TTL,
                                    "epoch": self._epoch()}
            self._save_sessions(s)
        return token

    def validate(self, token: str | None) -> bool:
        """cookie 里的令牌是否有效。

        **会话记录里的 epoch 必须等于当前 epoch** ——
        这正是"改密码/重置立刻踢掉所有旧会话"的实现方式。

        失败时把**具体原因**记下来（真实归因）：
        用户反馈过"有时会突然退出、要重新输密码"，而这条路径有三种
        完全不同的原因（cookie 没带上 / 会话不在表里 / epoch 被吊销），
        不区分的话只能靠猜。这里只在**原因变化时**记一条，不刷屏。
        """
        if not token:
            self._note_why("cookie 里没有会话令牌")
            return False
        rec = self._sessions().get(token_hash(token))
        if not rec:
            # 区分"从来没签发过"和"签发了但已过期被过滤掉"
            raw = self.store.get_cred("auth_sessions") or {}
            expired = token_hash(token) in (raw if isinstance(raw, dict) else {})
            self._note_why("会话已过期（30 天）" if expired else "会话不在服务端记录里"
                           "（可能被吊销，或本地会话表被覆盖）")
            return False
        if int(rec.get("epoch") or 0) != self._epoch():
            self._note_why("会话的 epoch 与服务端不一致（改过密码/重置过）")
            return False
        return True

    def _note_why(self, why: str) -> None:
        """只在原因变化时记一次 —— 前端每 2 秒轮询，不去重会刷爆日志。

        级别区分：
          · **cookie 里没有会话令牌** → `info`。这是**正常的首次访问**
            （页面刚打开、还没登录过），不是异常。按 warn 记会让日志看起来
            像出了问题，用户实测反馈过"我没被要求输密码，却总有这条"。
          · 其余（会话不在表里 / epoch 不一致）→ `warn`。那些才是真的异常：
            要么会话被吊销，要么服务端记录被覆盖。
        """
        if getattr(self, "_last_why", "") == why:
            return
        self._last_why = why
        try:
            lv = "info" if "没有会话令牌" in why else "warn"
            self._log(f"未登录：{why}", lv)
        except Exception:
            pass

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            s = self._sessions()
            s.pop(token_hash(token), None)
            self._save_sessions(s)

    def revoke_all(self) -> None:
        """吊销全部会话：清空列表 + epoch+1（双保险，防止旧表被写回）。"""
        with self._lock:
            self.store.set_cred("auth_sessions", {})
            self.store.set_meta("auth_epoch", str(self._epoch() + 1))
        self._fails.clear()

    # ---------------------------------------------------------- 失败节流

    def throttled(self, ip: str) -> int:
        """返回还要等几秒；0 表示可以尝试。"""
        now = time.time()
        hits = [t for t in self._fails.get(ip, []) if now - t < 300]
        self._fails[ip] = hits
        if len(hits) < 5:
            return 0
        wait = int(300 - (now - hits[0]))
        return max(wait, 1)

    def note_fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())

    def note_ok(self, ip: str) -> None:
        self._fails.pop(ip, None)
