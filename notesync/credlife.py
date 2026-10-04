"""凭据寿命取证：读出"这条登录到底能撑多久"的硬数据。

问题背景
--------
用户问的是：**"不能验证自动续期，是不是代表现在登录了也撑不久？"**

这是两个被混在一起的问题，必须分开回答：

1. **登录态本身能撑多久** —— 有硬数据。浏览器 profile 里
   `deviceId`（可信设备标识）的有效期写在 Cookie 库里，换算即得。
   这是"身份"的寿命，通常是年量级。

2. **接口凭据（serviceToken）能撑多久** —— 短效，几天到几十天，
   必然会过期。这是唯一需要"续"的东西。

续期有两条路，风险完全不同：

* **路 A：从浏览器 profile 静默读取**（`browser_auth.py`）
  只读本地 Edge profile 的 cookie，**不向小米发任何登录请求**，
  不触发风控，零风险。profile 在就能用。
* **路 B：passToken + serviceLogin 换证**（`mi_auth.acquire_service`）
  会真的发登录请求。如果小米认为设备不可信，会返回 `captchaUrl`
  要求人工过验证码 —— 这就是"不能全自动"的来源。

所以正确的判断是：**路 A 存在 => 续期就是自动的**，路 B 只是兜底。

**纯只读**：拷贝数据库副本再读，绝不碰原文件，绝不发网络请求。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Chrome/Edge 时间戳基准：1601-01-01 UTC，单位微秒
CHROME_EPOCH_OFFSET = 11644473600

CST = timezone(timedelta(hours=8))

# 决定"能不能长期免维护"的关键字段
KEY_NAMES = {
    "deviceId", "passToken", "serviceToken", "userId", "cUserId",
    "i.mi.com_istrudev", "i.mi.com_isvalid_servicetoken",
    "i.mi.com_slh", "i.mi.com_ph",
}


def chrome_to_unix(us: int) -> int:
    return int(us / 1_000_000) - CHROME_EPOCH_OFFSET


def fmt_ts(ts: int, now: int | None = None) -> str:
    if ts <= 0:
        return "session（无过期时间）"
    now = now or int(time.time())
    d = datetime.fromtimestamp(ts, tz=CST)
    left = ts - now
    if left <= 0:
        return f"{d:%Y-%m-%d %H:%M} 【已过期】"
    days = left / 86400
    tag = f"剩 {days:.0f} 天" if days >= 1 else f"剩 {left / 3600:.1f} 小时"
    return f"{d:%Y-%m-%d %H:%M}（{tag}）"


def cookie_db(profile: Path) -> Path | None:
    for c in (profile / "Default" / "Network" / "Cookies",
              profile / "Default" / "Cookies"):
        if c.exists():
            return c
    return None


def profile_has_login(profile: str | Path) -> dict:
    """profile 里到底有没有 i.mi.com 的登录态？

    **只看目录存在是不够的** —— 一次失败的尝试就会把 profile 目录建出来，
    于是"profile 存在"和"profile 里有登录态"变成两件完全不同的事。
    这正是之前误判的来源：告诉用户"能静默续期"，其实 profile 是空的。

    i.mi.com 的登录态落在 **localStorage**（leveldb），不在 cookie 里
    （cookie 库里只有 deviceId / pass_ua / uLocale 这几个，是账号域的）。
    所以直接去 leveldb 的原始字节里找 i.mi.com 的来源标记。
    """
    profile = Path(profile)
    out = {"has_login": False, "evidences": [], "files": 0}
    ls = profile / "Default" / "Local Storage" / "leveldb"
    if not ls.is_dir():
        return out

    # leveldb 的 .log / .ldb 是半明文，直接搜来源前缀即可
    markers = (b"_https://i.mi.com", b"i.mi.com\x00", b"https://i.mi.com")
    # 账号域不算 —— 光有 account.xiaomi.com 只能说明访问过登录页
    for f in sorted(ls.iterdir()):
        if not f.is_file() or f.suffix.lower() not in (".log", ".ldb"):
            continue
        out["files"] += 1
        try:
            b = f.read_bytes()
        except Exception:
            continue
        for m in markers:
            if m in b:
                out["has_login"] = True
                out["evidences"].append(f"{f.name}: {m.decode('utf-8', 'replace')}")
                break
    return out


def read_mi_cookies(profile: str | Path) -> list[dict]:
    """读 profile 里所有 mi.com 域的 cookie。

    copy-then-read：Edge 运行时会锁住 Cookies 文件，直接打开会 database is locked。
    """
    profile = Path(profile)
    db = cookie_db(profile)
    if db is None:
        raise FileNotFoundError(f"没找到 Cookies 库（profile={profile}）")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "Cookies"
        shutil.copy2(db, tmp)
        for suf in ("-wal", "-shm"):
            side = Path(str(db) + suf)
            if side.exists():
                shutil.copy2(side, Path(str(tmp) + suf))
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT host_key, name, expires_utc, is_secure, is_httponly "
                "FROM cookies WHERE host_key LIKE '%mi.com%' "
                "ORDER BY host_key, name").fetchall()
        finally:
            con.close()
    now = int(time.time())
    return [{
        "host": r["host_key"], "name": r["name"],
        "expires_unix": chrome_to_unix(int(r["expires_utc"] or 0)),
        "expires_text": fmt_ts(chrome_to_unix(int(r["expires_utc"] or 0)), now),
        "http_only": bool(r["is_httponly"]), "secure": bool(r["is_secure"]),
    } for r in rows]


def profile_life(profile: str | Path) -> dict:
    """把"这个 profile 的登录态还能撑多久"浓缩成一条结论。

    返回结构刻意做成前端能直接渲染的样子。
    """
    profile = Path(profile)
    now = int(time.time())
    out: dict = {
        "profile_exists": profile.is_dir(),
        "db_mtime_text": "",
        "cookies": [],
        "device_id_until": 0,
        "device_id_text": "",
        "device_days_left": 0,
        "has_login": False,
        "login_evidences": [],
        "conclusion": "",
    }
    if not profile.is_dir():
        out["conclusion"] = ("没有浏览器 profile —— 续期只能走 passToken 换证那条路，"
                             "而那条路可能被验证码拦住。建议先跑一次 browser-login.bat。")
        return out

    db = cookie_db(profile)
    if db is not None:
        mt = datetime.fromtimestamp(db.stat().st_mtime, tz=CST)
        out["db_mtime"] = int(db.stat().st_mtime)
        out["db_mtime_text"] = mt.strftime("%Y-%m-%d %H:%M")

    try:
        cookies = read_mi_cookies(profile)
    except Exception as e:
        out["conclusion"] = f"读取 profile cookie 失败：{type(e).__name__}: {e}"
        return out

    out["cookies"] = cookies
    dev = next((c for c in cookies if c["name"] == "deviceId"), None)
    if dev:
        out["device_id_until"] = dev["expires_unix"]
        out["device_id_text"] = dev["expires_text"]
        out["device_days_left"] = round((dev["expires_unix"] - now) / 86400, 1)

    out["trusted_marker"] = any(
        c["name"] == "i.mi.com_istrudev" for c in cookies)

    # 真正决定"能不能静默续期"的，是 profile 里有没有 i.mi.com 的登录态。
    # 目录存在不算数 —— 失败的尝试也会留下目录。
    ls = profile_has_login(profile)
    out["has_login"] = ls["has_login"]
    out["login_evidences"] = ls["evidences"]

    if not ls["has_login"]:
        out["conclusion"] = (
            "profile 里**没有** i.mi.com 的登录态（localStorage 里找不到痕迹）"
            "—— 静默续期不可用。需要跑一次 browser-login.bat 登录，"
            "只需一次，之后就能一直静默续期。"
        )
    elif out["device_days_left"] > 0:
        out["conclusion"] = (
            f"profile 里有登录态，且可信设备标识还有 {out['device_days_left']:.0f} 天"
            "有效期 —— 凭据过期时可以无头打开浏览器自动换新，"
            "**不需要重新登录**。"
        )
    else:
        out["conclusion"] = ("profile 里有登录态痕迹，但没读到 deviceId 有效期。"
                             "实际点一次「静默续期」即可确认。")
    return out


def cached_profile_life(store, ttl: int = 300) -> dict:
    """带缓存的版本。

    读 cookie 库要拷 SQLite（几 MB），不适合放进 5 秒一轮的循环里。
    缓存 5 分钟，够用且几乎无开销。

    **但有一个陷阱**：如果期间发生过登录或续期，缓存必须立刻作废 ——
    否则用户刚登录成功，页面还在显示"profile 里没有登录态"，
    看起来就像登录没生效（实际踩过这个）。
    判断依据：`xiaomi_cred_at`（凭据写入时间）比缓存时间新。
    """
    now = int(time.time())
    try:
        last = int(store.get_meta("cred_life_at", "0") or 0)
        cred_at = int(store.get_meta("xiaomi_cred_at", "0") or 0)
        stale = cred_at > last
        if not stale and now - last < ttl:
            import json as _json
            blob = store.get_meta("cred_life_json", "") or ""
            if blob:
                return _json.loads(blob)
    except Exception:
        pass
    info = profile_life(Path(store.data_dir) / "browser_profile")
    try:
        import json as _json
        store.set_meta("cred_life_at", str(now))
        store.set_meta("cred_life_json", _json.dumps(info, ensure_ascii=False))
    except Exception:
        pass
    return info


def invalidate_profile_cache(store) -> None:
    """显式清掉凭据寿命缓存（登录/续期之后调一次）"""
    try:
        store.set_meta("cred_life_at", "0")
    except Exception:
        pass
