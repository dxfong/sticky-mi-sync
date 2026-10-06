"""小米笔记数据源。

两个实现：
  MockXiaomi  内存假笔记
  RealXiaomi  i.mi.com 的内部接口（Cookie 认证）

接口形态说明
------------
读取接口是**已验证**的（社区多处交叉印证）：
  GET /note/full/page?ts=<ms>&syncTag=<游标>&limit=200
      返回 data.entries[]（笔记）+ data.folders[]（文件夹）+ data.syncTag + data.lastPage
      每条笔记带 folderId，所以「只同步某个文件夹」= 按 folderId 过滤
  GET /note/note/{id}/?ts=<ms>      返回 data.entry.content（HTML）

写入接口**未验证**：社区项目（mi_note_mcp）证明「创建/修改/删除/移动笔记」
和「文件夹增删改」都能做，但公开资料里没有给出请求体格式。
所以这里按 i.mi.com 一贯的表单风格实现了一份，并用 write_enabled 开关默认关闭。
打开之前请先按 README 里的方法抓一次真实请求，核对字段名。
"""

from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .credlife import cached_profile_life
from .textutil import (first_line, first_nonempty_line, mi_content_to_text,
                       text_to_mi_xml)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 30

# 凭据刚失败后的"重试宽限"（秒）。
#
# 在这段时间内不算"已失效"，而是"正在重试" —— 因为 serviceToken 本来就会过期，
# 续期偶尔会撞上网络抖动或小米限流，而下一轮往往就自愈了。
# 不给宽限期的话，一次失败就会在页面顶部弹「小米凭据已失效，请重新登录」的大红字，
# 用户实测碰上过：白紧张一场，几秒后自己又好了。
RETRY_GRACE = 300

# i.mi.com 是境内站点，直连比走代理稳。环境里如果设了 HTTP_PROXY，
# urllib 默认会把请求也塞进代理，所以这里显式绕开。
# （graph.microsoft.com 相反，需要代理时保持默认行为即可。）
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class QuotaExceeded(RuntimeError):
    """小米写接口的**配额/限流**被触发（HTTP 503 + over user rate quota）。

    单独一个类型，是为了让它和"偶发网络错误"区分开：
      · 偶发错误 → 记一条告警，继续处理下一条
      · 配额耗尽 → **立刻停掉整轮**。继续硬试只会让配额窗口越拖越长，
                   而且 165 条会连着报 165 次同样的错，把日志刷满。
    """

    def __init__(self, body: str = ""):
        super().__init__(f"小米写接口配额已耗尽（限流）：{body[:200]}")
        self.body = body


def _ms_to_iso(ms) -> str:
    """小米的毫秒时间戳 → 和便笺侧同形状的 ISO 字符串（`...Z`）。

    放在这一层是为了让 list_notes() 返回的字段形状与便笺侧一致 ——
    engine.cache_add() 和前端都按 `modified_iso` 排序，
    小米侧缺这个字段会导致排序全落空。
    这里刻意不从 engine 导入（会形成循环依赖）。
    """
    try:
        v = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    base = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return (base + datetime.timedelta(milliseconds=v)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _looks_like_quota(code: int, body: str) -> bool:
    """判这是不是"限流/配额"而不是别的错误。

    依据实测响应体：`503 user api quota is reached. over user rate quota`
    （Resin/4.0.61，Server: MicloudWeb）。
    用**宽松匹配**：宁可把一次 503 误判成配额（停下来等一等），
    也不要把配额错误当成偶发错误去硬试。
    """
    b = (body or "").lower()
    if "quota" in b or "rate" in b and "exceed" in b:
        return True
    if code == 503:
        return True
    if code == 429:
        return True
    return False


# --------------------------------------------------------------------- Mock


class MockXiaomi:
    """内存假笔记。create/update/delete 给前端 mock 面板用。

    种子里故意放一条"别的文件夹"的笔记 —— 用来验证同步只认指定文件夹，
    不会把小米笔记全盘搬过去。
    """

    TARGET = "F_MOCK"          # 目标文件夹
    OTHER = "F_MOCK_OTHER"     # 另一个文件夹（不该被同步）

    def __init__(self, store, seed: list[str] | None = None):
        self.store = store
        self.notes: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, str] = {self.TARGET: "微软便笺",
                                        self.OTHER: "其他笔记"}
        self._seq = 0
        # 与 RealXiaomi 对齐的节流接口。mock 不发请求，所以全是空实现 ——
        # 但**必须存在**：引擎会无差别调用这两个方法（`reset_write_budget`
        # 和 `cooldown_left`），缺了就会 AttributeError。
        self.writes_this_round = 0
        # 用一看就知道是假的标题，绝不跟真实便笺的标题撞车
        for text in (seed or ["【示例】小米侧原有笔记\n首次配对时保持不动，不会被复制成便笺"]):
            self.create(text, self.TARGET)
        self.create("【示例】别的文件夹里的笔记\n这条绝对不该被同步", self.OTHER)

    # ---- 与 RealXiaomi 对齐的节流接口（mock 不节流，只保证能调）----
    def reset_write_budget(self) -> None:
        self.writes_this_round = 0

    def cooldown_left(self) -> int:
        return 0

    def _new_id(self) -> str:
        self._seq += 1
        return f"MINOTE{self._seq:04d}"

    def create(self, text: str, folder_id: str = "") -> dict[str, Any]:
        nid = self._new_id()
        fid = folder_id or self.TARGET
        self.notes[nid] = {
            "id": nid, "text": text, "folder_id": fid,
            "subject": text.split("\n")[0][:60] if text else "",
            "modify_date": int(time.time() * 1000), "deleted": False,
        }
        return self.notes[nid]

    def update(self, note_id: str, text: str) -> dict[str, Any]:
        n = self.notes[note_id]
        n["text"] = text
        n["subject"] = text.split("\n")[0][:60] if text else ""
        n["modify_date"] = int(time.time() * 1000)
        return n

    def delete(self, note_id: str) -> None:
        self.notes[note_id]["deleted"] = True

    def selected_folder(self) -> str:
        """当前选中的目标文件夹（配置优先）"""
        fid = str(self.store.cfg["xiaomi"].get("folder_id") or "").strip()
        return fid if fid in self.folders else self.TARGET

    # ---- 数据源接口
    def status(self) -> dict[str, Any]:
        # 注意：这里报的是**配置里选中**的文件夹，不是 selected_folder() 的回退值。
        # 否则配置为空时卡片会显示"目标文件夹：微软便笺"，让人误以为已经指定过了，
        # 而 readiness 又会说"未指定文件夹" —— 两边对不上。
        cfg = self.store.cfg["xiaomi"]
        fid = str(cfg.get("folder_id") or "").strip()
        return {"mode": "mock", "connected": True, "account": "mock-mi-id",
                "folder": self.folders.get(fid, ""), "folder_id": fid,
                "folder_effective": self.folders.get(self.selected_folder(), "")}

    def list_folders(self) -> list[dict[str, Any]]:
        return [{"id": k, "subject": v} for k, v in self.folders.items()]

    def folder_stats(self) -> dict[str, int]:
        fid = self.selected_folder()
        alive = [n for n in self.notes.values() if not n.get("deleted")]
        return {"total": len(alive),
                "scoped": len([n for n in alive if n["folder_id"] == fid])}

    def resolve_folder(self) -> str:
        return self.selected_folder()

    def list_notes(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        """**只返回目标文件夹内的笔记** —— 与 RealXiaomi 行为保持一致。

        同样按修改时间**倒序**返回（和 RealXiaomi 一致，也和便笺侧一致）。
        """
        fid = self.selected_folder()
        out = [dict(n) for n in self.notes.values()
               if not n.get("deleted") and n["folder_id"] == fid]
        # 和 RealXiaomi 一样按修改时间倒序，别让 mock 的行为跟真实的不一致
        out.sort(key=lambda n: int(n.get("modify_date") or 0), reverse=True)
        return out

    def create_note(self, text: str, folder_id: str,
                    when_ms: int | None = None) -> dict[str, Any]:
        return self.create(text, folder_id or self.selected_folder())

    def update_note(self, note_id: str, text: str,
                    when_ms: int | None = None) -> dict[str, Any]:
        return self.update(note_id, text)

    def delete_note(self, note_id: str) -> None:
        self.delete(note_id)


# --------------------------------------------------------------------- Real


class RealXiaomi:
    def __init__(self, store):
        self.store = store
        self.last_error = ""
        # 正文缓存：id -> (modifyDate, 纯文本)
        # 列表接口拿不到正文，但每条笔记都要单独取一次详情。
        # 用 modifyDate 当版本号，只有变动过的才重新取 ——
        # 否则 5 秒一轮 × 165 条 = 每轮 165 个请求，接口和风控都受不了。
        self._detail: dict[str, tuple[int, str]] = {}
        # 最近一次接口请求的成败。用来判「凭据到底还能不能用」。
        # 放在**最底层**记录，而不是等上层 fetch_only 汇总 ——
        # 否则微软侧一挂，小米侧明明好好的也会被判成"状态未知"。
        self._last_req_ok: bool | None = None

        # ---- 写入节流（防风控）----
        # 所有写都是**串行**的（一个 for 循环挨个发），HTTP 层面没有并发。
        # 但"串行"不等于"慢"：本地循环可以一秒发十几个请求，
        # 实测就是这个节奏把配额打爆的（503 over user rate quota）。
        # 所以这里给**写操作**之间强制拉开最小间隔。
        self._last_write_at: float = 0.0
        # 配额耗尽后的冷却截止时间（Unix 秒）。冷却期内不再发起任何写入。
        # **从 meta 恢复** —— 只有内存的话，服务一重启冷却就丢了，
        # 于是刚被打爆的配额立刻又挨一轮硬打，是最糟的组合。
        self._cooldown_until: float = 0.0
        try:
            self._cooldown_until = float(
                self.store.get_meta("xiaomi_cooldown_until", "0") or 0)
        except (TypeError, ValueError):
            self._cooldown_until = 0.0
        if self._cooldown_until > time.time():
            self.store.log(
                f"小米写入仍在配额冷却中（还剩 "
                f"{int(self._cooldown_until - time.time()) // 60} 分钟），"
                f"冷却期内不会发起写入", "warn")
        # 本轮已写入次数（由引擎按轮重置）
        self.writes_this_round: int = 0
        # ★ 凭据续期的**全局防重入**标志。
        # 任何一条续期路径（401 触发的 _refresh_token、手动的静默续期、
        # 以及 _require_cred 的自动补齐）在跑的时候都把它置 True。
        # 为什么必须有：续期过程中自己也要发请求，而那条请求失败会再触发续期 ——
        # 嵌套下去会反复开 profile、反复覆盖又回滚 cookie，
        # 实测把本来能用的登录态搅成了半空状态（缺 serviceToken）。
        self._refreshing = False
        # 上次自动补齐的时刻（节流用：失败后别反复去动 profile）
        self._reaquire_at = 0.0

    # ---------------------------------------------------------- 写入节流
    def _write_interval(self) -> float:
        """两次写之间至少间隔多少秒。默认 1.0 —— 即用户说的"每秒写一条"。"""
        try:
            v = float(self._cfg().get("write_interval_sec", 1.0))
        except (TypeError, ValueError):
            v = 1.0
        return max(0.0, v)

    def cooldown_left(self) -> int:
        """距离冷却结束还有几秒（0 表示不在冷却中）。"""
        return max(0, int(self._cooldown_until - time.time()))

    def _throttle_write(self) -> None:
        """发起写入前调用：先看冷却，再补足最小间隔。"""
        left = self.cooldown_left()
        if left > 0:
            raise QuotaExceeded(
                f"仍在配额冷却中（还需 {left} 秒）—— 本轮不再尝试写入")
        gap = self._write_interval()
        if gap > 0 and self._last_write_at:
            wait = gap - (time.time() - self._last_write_at)
            if wait > 0:
                time.sleep(wait)

    def _mark_written(self) -> None:
        self._last_write_at = time.time()
        self.writes_this_round += 1

    def _mark_quota_hit(self, body: str) -> None:
        """命中配额：进入冷却。**冷却时长随连续命中次数递增**（退避）。"""
        try:
            base = int(self._cfg().get("quota_cooldown_sec", 300) or 300)
        except (TypeError, ValueError):
            base = 300
        self._quota_hits = getattr(self, "_quota_hits", 0) + 1
        # 5 分钟 → 10 分钟 → 20 分钟 → 30 分钟封顶
        cool = min(base * (2 ** (self._quota_hits - 1)), 1800)
        self._cooldown_until = time.time() + cool
        self.store.set_meta("xiaomi_cooldown_until", str(int(self._cooldown_until)))
        self.store.log(
            f"小米写接口命中配额（第 {self._quota_hits} 次）—— "
            f"写入暂停 {cool // 60} 分钟。{body[:120]}", "warn")

    def reset_write_budget(self) -> None:
        """每轮同步开始时调用：写入计数归零。"""
        self.writes_this_round = 0

    def _note_req(self, ok: bool) -> None:
        """记录一次接口请求的成败（只在**状态翻转**时写库，避免每轮刷写）"""
        if self._last_req_ok is ok:
            return
        self._last_req_ok = ok
        key = "xiaomi_ok_at" if ok else "xiaomi_fail_at"
        try:
            self.store.set_meta(key, str(int(time.time())))
        except Exception:
            pass

    def _cfg(self) -> dict[str, Any]:
        return self.store.cfg["xiaomi"]

    def _cookie(self) -> dict[str, Any]:
        return self.store.get_cred("xiaomi_cookie", {}) or {}

    # ---------------------------------------------------------- 底层请求
    def _cookie_header(self) -> str:
        """把保存的 cookie 全部发出去。

        之前只发白名单里的几个键，漏掉了 `i.mi.com_slh` / `i.mi.com_ph` ——
        这两个是 i.mi.com 这个 sid 专属的辅助字段，缺了会被判未登录。
        教训：**别用白名单猜服务端要什么，手上有什么就发什么**（cookie 本来就是个不透明袋子）。
        """
        c = self._cookie()
        return "; ".join(f"{k}={v}" for k, v in c.items() if v not in (None, ""))

    def _require_cred(self) -> None:
        """没有凭据（或不完整）就别发请求。

        否则会出现很误导的现象：用户明明已经清空/还没登录，页面上却显示一条
        "小米凭据不可用（401）" —— 那其实是"没凭据还去请求"造成的，
        看起来像"凭据坏了"，让人以为是清空没生效。
        """
        c = self._cookie()
        if c.get("serviceToken") and c.get("userId"):
            return
        # ★ 凭据不完整时**先自己试着补齐**，再报错。
        #
        # 顺序有讲究：**先试 passToken 换证，再试浏览器 profile**。
        #   · passToken 换证是纯 HTTP —— 容器 / 服务器里就能做，约 3 秒；
        #    · 浏览器 profile 只有本机 Windows 才有，容器里必然拿不到。
        # 以前只试了后者，于是容器里每次都是"尝试补齐失败 → 报错让你重新登录"，
        # 而其实第一条路就能自愈（用户实测质疑过"难道每隔几天都要重登"）。
        if not self._refreshing:
            healed = False
            try:
                if self.store.get_cred("xiaomi_pass_token"):
                    healed = self._refresh_token()
            except Exception:
                healed = False
            if not healed and self._try_silent_reaquire():
                healed = True
            if healed:
                c = self._cookie()
                if c.get("serviceToken") and c.get("userId"):
                    return
        # ★ 续期正在进行时，cookie 可能处于"已清旧值、还没写新值"的中间态 ——
        #   这时**不能报成"凭据坏了、请重新登录"**（用户实测被这条误导过：
        #   日志刷出"请用账号密码登录"，而同一秒凭据其实自己就换好了）。
        #   它是**临时状态**，下一轮自然就恢复，如实说清楚即可。
        if self._refreshing:
            raise RuntimeError(
                "小米凭据正在续期中（本轮先跳过，下一轮会自动恢复）。"
                "若长时间一直如此，再用页面的「账号密码登录」重登一次。")
        miss = [k for k in ("serviceToken", "userId") if not c.get(k)]
        raise RuntimeError(
            f"小米凭据不完整（缺 {'、'.join(miss)}），自动补齐也没成功。"
            f"请到页面「账号与登录 → 小米笔记」卡片里，"
            f"用最上方的**账号密码登录**重新登录一次 —— "
            f"全程在这个页面完成（含短信/邮箱验证码），不需要本机浏览器。")

    def _try_silent_reaquire(self) -> bool:
        """凭据不完整时的一次静默补齐（带 60 秒节流，避免反复起浏览器）。"""
        now = time.time()
        if now - float(getattr(self, "_reaquire_at", 0.0) or 0.0) < 60:
            return False
        self._reaquire_at = now
        self._refreshing = True
        try:
            ok = self.refresh_from_browser_safe()
            if ok:
                self.store.log(
                    "小米凭据不完整 —— 已从浏览器 profile 自动补齐（免粘贴、免扫码）",
                    "info")
            else:
                self.store.log(
                    "小米凭据不完整，尝试自动补齐失败："
                    + (self.last_error or "未知原因"), "warn")
            return ok
        except Exception as e:
            self.store.log(f"自动补齐凭据异常：{type(e).__name__}: {e}", "warn")
            return False
        finally:
            self._refreshing = False

    def _get(self, path: str, params: dict[str, Any] | None = None,
             _retry: bool = True) -> dict[str, Any]:
        self._require_cred()
        base = self._cfg().get("base_url") or "https://i.mi.com"
        p = {"ts": int(time.time() * 1000)}
        if params:
            p.update(params)
        url = f"{base}{path}?{urllib.parse.urlencode(p)}"
        req = urllib.request.Request(url, headers={
            "Cookie": self._cookie_header(),
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{base}/note/",
        })
        try:
            with DIRECT.open(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._note_req(True)
            return data
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            # 401/403 基本就是 serviceToken 过期：用 passToken 自动换一次再重试。
            # **_retry 必须只允许一次** —— 否则续期成功但接口仍返 401 时会无限递归，
            # 把进程挂死（这个坑踩过：表现为命令被超时杀掉、没有任何输出）。
            if e.code in (401, 403):
                if _retry and self._refresh_token():
                    return self._get(path, params, _retry=False)
                self._note_req(False)
                raise RuntimeError(
                    f"小米凭据不可用（{e.code}）。"
                    + ("已自动续期但仍失败；" if _retry else "本次只做只读检查、未尝试续期；")
                    + f"响应：{body}") from None
            self._note_req(False)
            raise RuntimeError(f"小米接口 {path} 返回 {e.code}：{body}") from None
        except Exception:
            # 网络层异常也算不健康，但它不是"凭据坏了"，所以不写 fail 时间戳
            raise

    def save_credentials(self, cookies: dict, pass_token: dict | None = None) -> None:
        """保存凭据。pass_token 用来在 serviceToken 过期时自动换新"""
        self.store.set_cred("xiaomi_cookie", cookies)
        if pass_token:
            self.store.set_cred("xiaomi_pass_token", pass_token)

    def _refresh_from_browser(self) -> bool:
        """兜底：用 Playwright 从持久化浏览器 profile 里**静默**读一次 cookie。

        这条路不依赖 passToken —— 只要那个浏览器 profile 里的登录态还在，
        headless 打开 i.mi.com 的 H5 页面就会自动换发一份新的 serviceToken
        （详见 browser_auth 模块头的说明）。

        特意用子进程而不是进程内调用：Playwright 的同步 API 在服务这种多线程环境里
        容易打架，子进程完全隔离，还自带超时。
        """
        import subprocess

        root = Path(__file__).resolve().parent.parent
        profile = Path(self.store.data_dir) / "browser_profile"
        if not profile.exists():
            self.last_error = ("还没有浏览器 profile —— 这条路走不通。"
                               "（容器/服务器里本来就没有浏览器，属正常；"
                               "凭据过期后用页面上的「账号密码登录」重登即可）")
            return False

        from .browser_auth import find_python_with_playwright
        exe = find_python_with_playwright()
        if not exe:
            self.last_error = ("没找到装了 playwright 的 python —— 静默续期不可用。"
                               "（容器里本来就没有，属正常；凭据过期后用页面上的"
                               "「账号密码登录」重登即可）")
            return False

        try:
            r = subprocess.run(
                [exe, "-u", str(root / "notesync" / "browser_auth.py"),
                 "--json", "--data-dir", str(self.store.data_dir)],
                capture_output=True, text=True, timeout=180,
                cwd=str(root), encoding="utf-8", errors="replace",
            )
        except Exception as e:
            self.last_error = f"浏览器取凭据失败：{type(e).__name__}: {e}"
            return False

        payload = None
        for line in reversed((r.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                break
        if not payload or not payload.get("ok"):
            err = (payload or {}).get("error") or (r.stderr or "").strip()[-200:] \
                  or "未知原因"
            self.last_error = f"浏览器 profile 取凭据失败：{err}"
            return False

        cookies = payload.get("cookies") or {}
        merged = dict(self._cookie())
        merged.update(cookies)
        self.store.set_cred("xiaomi_cookie", merged)
        self.store.set_meta("xiaomi_cred_at", str(int(time.time())))
        if cookies.get("passToken") and cookies.get("deviceId"):
            self.store.set_cred("xiaomi_pass_token", {
                "deviceId": cookies["deviceId"],
                "passToken": cookies["passToken"],
                "userId": cookies.get("userId", ""),
            })
        self.store.log("小米凭据已从浏览器 profile 静默续期（免粘贴、免扫码）")
        return True

    def refresh_from_browser_safe(self) -> bool:
        """从浏览器 profile 静默续期的**入口**（带全局防重入）。

        加这层包装的理由见 `_refreshing` 的说明：续期过程自己也会发请求，
        失败又会触发续期，不挡住就会嵌套着反复开 profile、反复覆盖又回滚 cookie
        —— 实测把本来能用的登录态搅成了半空（缺 serviceToken）。
        """
        if self._refreshing:
            # ★ 不要写进 last_error —— 这**不是故障**，只是"另一次续期正在进行，
            #   这一轮不需要重复做"。写进去会被界面和日志当成错误显示
            #   （用户实测看到过「尝试自动补齐失败：已有一次凭据续期在进行中」，
            #    看起来像坏了，其实下一轮就正常）。
            self.store.log("另一次凭据续期正在进行，这一轮跳过（正常现象）", "info")
            return False
        self._refreshing = True
        try:
            return self._refresh_from_browser_safe()
        finally:
            self._refreshing = False

    def _refresh_from_browser_safe(self) -> bool:
        """从浏览器 profile **安全续期**：备份 -> 读取 -> 验证 -> 失败回滚。

        这条路**不向小米发任何登录请求**，只是读本地 Edge profile 里现成的 cookie，
        所以不会触发风控 —— 它才是"测试续期"应该走的默认路径。

        "不发登录请求"不等于"不可能弄坏凭据"：profile 里读出来的 cookie 有可能
        比库里已有的更旧（比如 profile 后来被清理过）。所以照样先备份，
        验证不通过立刻回滚。
        """
        backup_cookie = dict(self._cookie())
        backup_pt = self.store.get_cred("xiaomi_pass_token")
        if not self._refresh_from_browser():
            self._restore(backup_cookie, backup_pt)
            return False
        if self._verify_usable():
            return True
        self.store.log(f"从 profile 读到的凭据不可用（已回滚，未影响现有凭据）："
                       f"{self.last_error}", "warn")
        self._restore(backup_cookie, backup_pt)
        return False

    def _verify_usable(self) -> bool:
        """拿新凭据真打一次接口，确认"能读"，而不是"换到了 token"。

        之前只检查 `serviceToken` 有没有拿到就报成功 —— 但小米对"不可信设备"换出来的
        token 照样给，只是调接口时 401。两个概念差很远：
        **续期成功的定义是"接口能用"，不是"拿到了字段"。**
        注意 `_retry=False`：这里的 401 不能再去触发续期，否则无限递归。
        """
        try:
            self._get("/note/full/page", {"limit": 1}, _retry=False)
            return True
        except Exception as e:
            msg = str(e)
            # 续期是自己调起来的，此时 cookie 可能正处于中间态 ——
            # 那种"正在续期中"的话术不是真的失败原因，照抄进日志只会更误导。
            if "正在续期中" in msg:
                msg = "凭据还在换发中（中间态）"
            self.last_error = f"续期拿到的凭据仍然读不了笔记：{msg[:160]}"
            return False

    def _refresh_token(self) -> bool:
        """凭据失效时续期的**入口**（带全局防重入）。见 `_refreshing` 的说明。"""
        if self._refreshing:
            # ★ 不要写进 last_error —— 这**不是故障**，只是"另一次续期正在进行，
            #   这一轮不需要重复做"。写进去会被界面和日志当成错误显示
            #   （用户实测看到过「尝试自动补齐失败：已有一次凭据续期在进行中」，
            #    看起来像坏了，其实下一轮就正常）。
            self.store.log("另一次凭据续期正在进行，这一轮跳过（正常现象）", "info")
            return False
        self._refreshing = True
        try:
            return self._refresh_token_locked()
        finally:
            self._refreshing = False

    def _refresh_token_locked(self) -> bool:
        """凭据失效时的续期。**任何一步失败都必须回滚**，绝不留下坏凭据。

        血泪教训：之前这里只要"换到了 serviceToken"就写库，哪怕那个 token 是
        不可信设备换出来的（调接口必 401）。结果用户本来能用的浏览器凭据
        被一个不能用的 token 覆盖掉 —— **自动续期把好凭据降级成了坏凭据**，
        从"能读"变成"什么都读不到"。

        现在的规矩：
          1. 动手前先备份当前凭据
          2. 每个候选凭据都要 `_verify_usable()` 真打一次接口
          3. 验证不过 → **立刻回滚**，并且不写库
          4. 返回 False，让上层如实报错（不要假报成功）
        """
        backup_cookie = dict(self._cookie())
        backup_pt = self.store.get_cred("xiaomi_pass_token")
        errors: list[str] = []

        def try_candidate(name: str, apply) -> bool:
            """apply() 负责把候选凭据写进库；随后验证，不通过就回滚"""
            if not apply():
                errors.append(f"{name}：{(self.last_error or '取凭据失败').strip()}")
                self._restore(backup_cookie, backup_pt)
                return False
            if self._verify_usable():
                return True
            errors.append(f"{name}：{(self.last_error or '凭据不可用').strip()}")
            self._restore(backup_cookie, backup_pt)
            return False

        profile = Path(self.store.data_dir) / "browser_profile"
        if profile.exists():
            if try_candidate("浏览器 profile", self._refresh_from_browser):
                return True
        else:
            errors.append("浏览器 profile：没有（容器/服务器里属正常）")

        pt = self.store.get_cred("xiaomi_pass_token")
        if pt:
            def use_passtoken() -> bool:
                try:
                    from . import mi_auth
                    svc = mi_auth.acquire_service(pt, "i.mi.com")
                except Exception as e:
                    self.last_error = f"换凭据失败：{e}"
                    return False
                cookies = (svc or {}).get("cookies") or {}
                if not cookies.get("serviceToken"):
                    self.last_error = f"换回来的 cookie 不完整：{str(cookies)[:150]}"
                    return False
                merged = dict(self._cookie())
                merged.update(cookies)
                self.store.set_cred("xiaomi_cookie", merged)
                return True

            if try_candidate("passToken 换证", use_passtoken):
                return True
        else:
            errors.append("passToken：库里没有保存")

        self.last_error = ("所有续期方式都失败，已回滚到原凭据（未破坏你现有的凭据）。"
                           + " ｜ ".join(errors))
        # 失败也必须写日志 —— 否则用户点了"测试续期"却什么都看不到，
        # 只能靠一闪而过的弹窗猜（这个反馈缺口是用户直接提出来的）。
        self.store.log("小米凭据续期失败（已回滚，现有凭据未受影响）："
                       + " ｜ ".join(errors), "error")
        return False

    def _restore(self, cookie: dict, pass_token) -> None:
        """回滚到续期之前的凭据"""
        try:
            if cookie:
                self.store.set_cred("xiaomi_cookie", cookie)
            else:
                self.store.del_cred("xiaomi_cookie")
            if pass_token:
                self.store.set_cred("xiaomi_pass_token", pass_token)
        except Exception:
            pass

    def _post_form(self, path: str, data: dict[str, Any],
                   _retry: bool = True) -> dict[str, Any]:
        """写入用的表单 POST（`entry` 是一个 JSON 字符串作为单个表单字段）。

        **所有写入都必须走这里**，因为节流、配额退避都在这一层做，
        绕过去就等于绕过了防风控。
        """
        self._require_cred()
        # 节流放在**发请求之前**：
        # 冷却期内直接抛 QuotaExceeded（不发请求），
        # 否则就补足最小间隔。这样"每秒写一条"是强制的，不靠调用方自觉。
        self._throttle_write()
        base = self._cfg().get("base_url") or "https://i.mi.com"
        form = {k: v for k, v in data.items() if v is not None}
        body = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(f"{base}{path}", data=body, headers={
            "Cookie": self._cookie_header(),
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": f"{base}/note/",
        }, method="POST")
        try:
            with DIRECT.open(req, timeout=TIMEOUT) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
            self._note_req(True)
            self._mark_written()
            # 写成功一次就把连续命中计数清零 —— 退避是给"持续打爆"用的，
            # 不该在恢复正常后还一路指数涨上去。
            self._quota_hits = 0
            return resp_json
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")[:300]
            # ---- 配额/限流：单独处理，交给上层停轮 ----
            if _looks_like_quota(e.code, err_body):
                self._note_req(False)
                self._mark_quota_hit(err_body)
                raise QuotaExceeded(err_body) from None
            if e.code in (401, 403):
                if _retry and self._refresh_token():
                    # 注意这里传的是**原始表单** form，不是响应体 ——
                    # 之前参数名和下面对响应体的赋值重名，虽然碰巧没出事，
                    # 但改个名字免得以后埋雷。
                    return self._post_form(path, form, _retry=False)
                self._note_req(False)
                raise RuntimeError(
                    f"小米写入凭据不可用（{e.code}）：{err_body}") from None
            self._note_req(False)
            raise RuntimeError(f"小米写入 {path} 返回 {e.code}：{err_body}") from None

    # ---------------------------------------------------------- 认证
    def verify_cookie(self, cookie: dict[str, Any]) -> dict[str, Any]:
        """用给定 cookie 打一次列表接口，验证是否有效"""
        backup = self._cookie()
        self.store.set_cred("xiaomi_cookie", cookie)
        try:
            data = self._get("/note/full/page", {"limit": 1})
            if data.get("result") != "ok":
                raise RuntimeError(f"接口返回异常：{str(data)[:200]}")
            return {"ok": True, "note_total": len(data.get("data", {}).get("entries", []))}
        finally:
            self.store.set_cred("xiaomi_cookie", backup)

    # ---------------------------------------------------------- 文件夹
    def list_folders(self) -> list[dict[str, Any]]:
        """读文件夹列表。

        ★★ `folders` 只在**最后一页**返回，所以**必须翻页**。★★

        实测（2026-09-18，账号 202 条 = 两页）：
            第 1 页: entries=200  folders=0   lastPage=False
            第 2 页: entries=2    folders=1   lastPage=True → '便笺'
        也就是说：**只读第一页时，账号一旦超过一页（每页上限 200）就再也拿不到文件夹。**
        这解释了一个很迷惑的现象 —— "以前明明是好的"：
        那时笔记不到 200 条，第一页恰好**就是**最后一页，所以 folders 有值。

        参考实现（ceynri/mi-note-cli 的 `src/client.ts` → `getAllNotes`）
        就是这么做的：循环取页、把每页的 `folders` 累积起来、直到 `lastPage`。
        之前只抄了它的接口形态，漏掉了这个循环。
        """
        cursor = ""
        found: dict[str, dict[str, Any]] = {}
        for _ in range(50):                      # 防御：最多 50 页
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["syncTag"] = cursor
            data = self._get("/note/full/page", params)
            if data.get("result") != "ok":
                raise RuntimeError(f"读取文件夹失败：{str(data)[:200]}")
            body = data.get("data", {}) or {}
            for f in (body.get("folders") or []):
                fid = str(f.get("id") or "")
                if fid:
                    found[fid] = {"id": fid,
                                  "subject": f.get("subject") or "(未命名)"}
            if body.get("lastPage"):
                break
            cursor = body.get("syncTag") or ""
            if not cursor:
                break
        if found:
            return list(found.values())

        # ---------------- 兜底 ------------------------------------------
        # 万一接口以后真的不返回 folders 了，就从第一页的 entries 里
        # 把用到的 folderId 推出来 —— 至少让选择器还能用（只有 id 没有名字）。
        data = self._get("/note/full/page", {"limit": 200})
        body = data.get("data", {}) or {}
        cfg = self._cfg()
        cur_id = str(cfg.get("folder_id") or "")
        cur_name = (cfg.get("folder_name") or "").strip()
        seen: dict[str, int] = {}
        for e in (body.get("entries") or []):
            if int(e.get("deleteTime") or 0) > 0:
                continue
            fid = str(e.get("folderId") or "")
            if fid:
                seen[fid] = seen.get(fid, 0) + 1
        out = []
        for fid, n in sorted(seen.items(), key=lambda kv: -kv[1]):
            if fid == cur_id and cur_name:
                name = f"{cur_name}（当前目标 · 首页 {n} 条）"
            elif fid == "0":
                name = f"未分类（首页 {n} 条）"
            else:
                name = f"文件夹 …{fid[-6:]}（首页 {n} 条）"
            out.append({"id": fid, "subject": name})
        if out:
            self.store.log(
                f"小米列表接口未返回文件夹名 —— 已从笔记里推出 {len(out)} 个在用的"
                f"文件夹（只有 id）。同步目标不受影响（用配置里存的 id）", "warn")
        return out

    def folder_stats(self) -> dict[str, int]:
        """目标文件夹内有多少条 / 小米笔记总共多少条。

        接口不支持按文件夹过滤，元数据只能全量拿（很轻），
        但**正文只取目标文件夹内的**，写入也只进这个文件夹。

        ★ **必须翻页**。这个账号的笔记数已经超过接口每页上限（200），
        原来只取第一页 → 界面上显示"小米笔记总计 200 条"，
        而这个数字恰好等于每页上限 —— 看起来非常像一个真实的数字，
        最容易骗过人（用户就是照着它发现不对劲的）。
        """
        fid = self.resolve_folder()
        cursor = ""
        n_total = 0
        n_scoped = 0
        for _ in range(50):                      # 防御：最多 50 页
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["syncTag"] = cursor
            data = self._get("/note/full/page", params)
            if data.get("result") != "ok":
                break
            body = data.get("data", {}) or {}
            for e in (body.get("entries") or []):
                if int(e.get("deleteTime") or 0) > 0:
                    continue
                if (e.get("status") or "") not in ("", "normal"):
                    continue
                n_total += 1
                if str(e.get("folderId") or "") == fid:
                    n_scoped += 1
            if body.get("lastPage"):
                break
            cursor = body.get("syncTag") or ""
            if not cursor:
                break
        return {"total": n_total, "scoped": n_scoped}

    def resolve_folder(self) -> str:
        """把配置里的文件夹名解析成 folderId（有缓存就直接用）"""
        cfg = self._cfg()
        if cfg.get("folder_id"):
            return str(cfg["folder_id"])
        want = (cfg.get("folder_name") or "").strip()
        folders = self.list_folders()
        for f in folders:
            if f["subject"].strip() == want:
                fid = f["id"]
                if fid:
                    self.store.cfg["xiaomi"]["folder_id"] = fid
                    self.store.save_config()
                    return fid
        names = ", ".join(f["subject"] for f in folders[:10])
        raise RuntimeError(f"找不到文件夹「{want}」。现有文件夹：{names or '（无）'}")

    # ---------------------------------------------------------- 数据源接口
    def status(self) -> dict[str, Any]:
        c = self._cookie()
        has_pt = bool(self.store.get_cred("xiaomi_pass_token"))
        connected = bool(c.get("serviceToken") and c.get("userId"))
        prof = cached_profile_life(self.store)
        # 只有 profile 里**真的有登录态**，静默续期才成立。
        # 光有目录不算 —— 失败的尝试也会留下目录（这个误判踩过）。
        profile_ok = bool(prof.get("profile_exists") and prof.get("has_login"))

        # 凭据是不是**真的能用**，跟"cookie 字段在不在"是两件事。
        # 用最底层请求记录（xiaomi_ok_at / xiaomi_fail_at）判断，不掺别的侧。
        def _n(key: str) -> int:
            try:
                return int(self.store.get_meta(key, "0") or 0)
            except (TypeError, ValueError):
                return 0
        ok_at, fail_at = _n("xiaomi_ok_at"), _n("xiaomi_fail_at")
        if fail_at > ok_at:
            # ★ 别"一次失败就宣称凭据已失效"。
            #   serviceToken 本来就会过期，续期偶尔会撞上网络抖动 / 小米限流 ——
            #   而下一轮往往就自愈了。用户实测见过：页面弹出大红字
            #   「小米凭据已失效，请重新登录」，几秒后自己又好了 —— 白紧张一场。
            #   所以给一个**重试窗口**：刚失败不久算"正在重试"，超时才算真失效。
            cred_state = ("retrying" if (time.time() - fail_at) < RETRY_GRACE
                          else "expired")
        elif ok_at > 0:
            cred_state = "ok"
        else:
            cred_state = "unknown" if connected else "none"

        return {
            "mode": "real",
            "connected": connected,
            "account": str(c.get("userId") or ""),
            "folder": self._cfg().get("folder_name", ""),
            "folder_id": self._cfg().get("folder_id", ""),
            "write_enabled": bool(self._cfg().get("write_enabled")),
            # 凭据可用性：ok / expired / unknown / none
            "cred_state": cred_state,
            "cred_ok_at": ok_at,
            "cred_fail_at": fail_at,
            # 能不能自动续期，取决于**有没有"原料"**：
            #   profile 里有登录态 -> 无头打开浏览器就能换新（零请求、零风控）
            #   passToken 在手     -> 也能换，但小米可能要求验证码
            "auto_refresh": profile_ok or has_pt,
            "refresh_paths": (["浏览器 profile（静默、零风控）"] if profile_ok else [])
                             + (["passToken 换证"] if has_pt else []),
            "cred_kind": ("浏览器 profile 承载（可静默续期）" if profile_ok
                          else ("已存 passToken（换证时小米可能要求验证码）" if has_pt
                                else "手工 Cookie（过期需重贴）")),
            # profile 里 deviceId 的有效期 = "这条登录能撑多久"的硬数据
            "profile": prof,
            "cred_keys": sorted(k for k, v in c.items() if v),
            # 记录 serviceToken 是**什么时候**拿到的，用来实测它能活多久
            "cred_at": int(self.store.get_meta("xiaomi_cred_at", "0") or 0),
            # 没登录时不要把历史错误挂在这儿 —— 否则用户会以为是"凭据坏了"，
            # 而这多半只是"没凭据还去请求"留下的噪音
            "error": self.last_error if connected else "",
        }

    def list_notes(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        """拉全量并过滤出目标文件夹里的笔记。

        用 syncTag 游标分页，直到 lastPage 为真。
        content 只在列表接口里拿不到，所以对每条笔记再取一次详情。
        为控制请求量，只对目标文件夹内的笔记取详情。
        """
        folder_id = self.resolve_folder()
        cursor = ""
        entries: list[dict[str, Any]] = []
        for _ in range(50):  # 防御：最多 50 页
            data = self._get("/note/full/page",
                             {"limit": 200, "syncTag": cursor})
            if data.get("result") != "ok":
                self.last_error = f"列表接口异常：{str(data)[:200]}"
                raise RuntimeError(self.last_error)
            body = data.get("data", {})
            for e in body.get("entries") or []:
                if str(e.get("folderId") or "") == folder_id:
                    entries.append(e)
            if body.get("lastPage"):
                break
            cursor = body.get("syncTag") or ""
            if not cursor:
                break

        out = []
        alive: set[str] = set()
        for e in entries:
            nid = str(e.get("id") or "")
            alive.add(nid)
            mtime = int(e.get("modifyDate") or 0)
            cached = self._detail.get(nid)
            if cached and cached[0] == mtime:
                text = cached[1]                      # 没变过，直接用缓存
            else:
                detail = self._get(f"/note/note/{nid}/")
                entry = (detail.get("data") or {}).get("entry") or {}
                # 小米的 content 是 **XML**（<text>/<bullet>/<order>…），不是 HTML。
                # mi_content_to_text 会按 XML 解析，认不出来才回退 HTML 分支。
                text = mi_content_to_text(entry.get("content") or "")
                self._detail[nid] = (mtime, text)
            out.append({
                "id": nid,
                "text": text,
                "subject": e.get("subject") or "",
                "folder_id": folder_id,
                "modify_date": mtime,
                # 和便笺侧保持**同样的字段形状**。前端和 engine.cache_add()
                # 会按 modified_iso 排序，缺了它小米侧的排序就全成了空值。
                "modified_iso": _ms_to_iso(mtime),
                "deleted": int(e.get("deleteTime") or 0) > 0
                           or (e.get("status") or "") not in ("", "normal"),
            })
        # 清掉已经不存在的笔记，避免缓存无限增长
        for gone in set(self._detail) - alive:
            self._detail.pop(gone, None)

        # ★★★ 必须自己按修改时间**倒序**排，不能沿用服务端顺序。★★★
        #
        # 实测：/note/full/page 返回的顺序是**最旧在前**（时间升序）。
        # 例：916(03:29) → 测试(03:30) → 哈哈哈(次日 01:05)，正好是升序。
        # 这和"最新在前"的直觉相反，而且便笺侧是显式按时间倒序排的
        # （NotesFabricGraph.list_notes 里有 out.sort(..., reverse=True)），
        # 两边不一致。不补这一刀有两个后果：
        #   1. 界面上小米列的顺序和便笺列**正好相反**
        #   2. **致命**：「只同步最新 N 条」是靠 `list[:N]` 截断实现的，
        #      顺序反了就等于"只同步最旧的 N 条"。
        #      用户实测踩到：新建「哈哈哈」后点"只同步最新一条"，
        #      引擎拿到的却是最旧的「916」，「哈哈哈」压根没进入视野。
        out.sort(key=lambda n: int(n.get("modify_date") or 0), reverse=True)
        self.last_error = ""
        return out

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        """只取**这一条**笔记（一条详情请求），不走全量列表。

        给「只同步改动过内容的」这类窄操作省时间：全量列表虽然只要一页，
        但每条都要取一次详情；而窄操作只涉及 link 表里那几条。
        返回形状与 `list_notes()` 的单条**完全一致**，上层无差别处理。
        取不到（或已删除）返回 None。
        """
        if not note_id:
            return None
        try:
            e = self._entry(note_id)
        except Exception:
            return None          # 交给调用方的全量兜底
        if not e:
            return None
        mtime = int(e.get("modifyDate") or 0)
        deleted = (int(e.get("deleteTime") or 0) > 0
                   or (e.get("status") or "") not in ("", "normal"))
        if deleted:
            return None          # 已删除的当作"这一侧没有"，窄操作不碰
        return {
            "id": str(e.get("id") or note_id),
            "text": mi_content_to_text(e.get("content") or ""),
            "subject": e.get("subject") or "",
            "folder_id": str(e.get("folderId") or ""),
            "modify_date": mtime,
            "modified_iso": _ms_to_iso(mtime),
            "deleted": False,
        }

    # ---- 写入（格式来自 ceynri/mi-note-cli，已对照其 client.ts 核实）

    def _service_token(self) -> str:
        """写操作的**表单里**必须另外带 serviceToken，光有 Cookie 不够。

        这一点最容易漏：读接口只认 Cookie，写接口还要求把 token 也放在 body 里，
        缺了会被当成未登录（表现为 401 或 result != ok，很难从响应看出原因）。
        """
        tok = self._cookie().get("serviceToken")
        if not tok:
            raise RuntimeError("凭据里没有 serviceToken，无法写入")
        return str(tok)

    def _entry(self, note_id: str) -> dict[str, Any]:
        """GET 笔记详情。写（更新/删除）之前**必须**先拿一次 tag（乐观锁）。"""
        d = self._get(f"/note/note/{note_id}/")
        if d.get("result") != "ok":
            raise RuntimeError(f"读笔记详情失败：{str(d)[:200]}")
        entry = (d.get("data") or {}).get("entry") or {}
        if not entry:
            raise RuntimeError(f"笔记 {note_id} 详情为空")
        return entry

    @staticmethod
    def _extra_info(cur: dict | None, title: str | None = None) -> str:
        """构造 extraInfo。

        更新时要**保留原字段**（尤其 title），只按需覆盖 —— 直接覆盖会让
        用户在小米客户端设的标题丢掉。
        """
        base: dict = {}
        if cur:
            raw = cur.get("extraInfo")
            if isinstance(raw, dict):
                base = dict(raw)
            elif isinstance(raw, str) and raw.strip():
                try:
                    base = json.loads(raw)
                except json.JSONDecodeError:
                    base = {}
        base.setdefault("note_content_type", "common")
        if title is not None:
            base["title"] = title
        base = {k: v for k, v in base.items() if v not in (None, "")}
        return json.dumps(base, ensure_ascii=False) if base else ""

    def create_note(self, text: str, folder_id: str,
                    when_ms: int | None = None) -> dict[str, Any]:
        """创建笔记。

        接口形态（**已验证**，来自 mi-note-cli 的 client.ts）：
            POST /note/note            ← 路径**没有**尾部斜杠
            body: entry=<JSON 字符串>&serviceToken=<token>

        关键是 entry 是**一个 JSON 字符串**作为一个表单字段提交，
        不是把 colorId/folderId/content 平铺成表单字段。
        之前按平铺实现，字段名全不对，所以一直写不进去。

        `when_ms`（毫秒时间戳）是**内容原本的修改时间**，从便笺带过来。

        ★★ 关键：**新建接口不认 when_ms，但更新接口认。**（两条路径都实测过）
          · 新建（这里，POST /note/note）：服务端把 createDate/modifyDate
            **一律覆盖成它自己的当前时间** —— 传 2023-11-15 过去，
            读回来是当前时刻。写进去也没用。
          · 更新（update_note，POST /note/note/{id}）：**认 modifyDate**。
            传 2023-11-15，读回来就是 2023-11-15；且 createDate 会被保留。

        所以"把原始时间写进小米"必须**两步**：新建拿到 id 后**立刻再更新一次**
        改 modifyDate。见下面 `honor_when` 的处理。
        （这一步会让写入量翻倍，所以给了开关：`xiaomi.honor_original_time`。）
        """
        now = int(when_ms or time.time() * 1000)
        xml = text_to_mi_xml(text)
        entry = {
            "colorId": 0,
            "folderId": str(folder_id or "0"),
            "createDate": now,
            "modifyDate": now,
            "content": xml,
            "alertDate": 0,
            "setting": {"themeId": 0, "stickyTime": 0, "version": 0},
            "extraInfo": self._extra_info(None),
            # **`subject` 不能省。** 它才是小米笔记列表里显示的"标题"，
            # 省掉的话新建出来的笔记在 App 里全是**空白条目** ——
            # 实测踩过：一次全量同步推了 132 条过去，subject 全空，
            # 小米侧看过去就是一片没有标题的空白。
            # 这里取首行（和便笺的 first_line 一致，保持两端观感统一）。
            "subject": first_line(text),
            "snippet": first_nonempty_line(xml),
        }
        r = self._post_form("/note/note", {
            "entry": json.dumps(entry, ensure_ascii=False),
            "serviceToken": self._service_token(),
        })
        if r.get("result") != "ok":
            raise RuntimeError(
                f"新建小米笔记失败：{r.get('description') or str(r)[:200]}")
        new_entry = (r.get("data") or {}).get("entry") or {}
        nid = str(new_entry.get("id") or "")
        if not nid:
            raise RuntimeError(f"新建成功但没拿到 id：{str(r)[:200]}")

        # ★ 第二步：把 modifyDate 改成内容原本的修改时间。
        # 复用新建响应里返回的 entry（它带 tag / createDate / setting 等），
        # 这样**省掉一次详情 GET** —— 全量 165 条能省 165 个请求。
        # 失败**不抛**：笔记已经建好了，只是时间不对，不该让整条同步失败
        # （时间不对只是排序问题，内容是对的；下一轮也还有机会）。
        if when_ms and self._honor_original_time():
            try:
                self.update_note(nid, text, when_ms=int(when_ms), _cur=new_entry)
            except Exception as e:
                self.store.log(
                    f"新建后回填原始时间失败（{nid}）：{str(e)[:120]}"
                    f" —— 内容已写入，仅时间戳是当前时间", "warn")

        self._detail.pop(nid, None)     # 自己写的，缓存作废，下轮重新取
        return {"id": nid, "tag": new_entry.get("tag", "")}

    def _honor_original_time(self) -> bool:
        """是否"新建后再更新一次"把原始时间写进去。

        默认开。关掉可以**把写入量减半**（每条 1 次写而不是 2 次），
        代价是小米侧每条的时间都是同步时刻 —— 但**列表排序仍然正确**，
        因为写入顺序本身就是按原始时间升序的。
        配额吃紧时把它关掉是最划算的降载手段。
        """
        v = self._cfg().get("honor_original_time", True)
        if isinstance(v, str):
            return v.strip().lower() not in ("0", "false", "no", "")
        return bool(v)


    def update_note(self, note_id: str, text: str,
                    when_ms: int | None = None,
                    _cur: dict[str, Any] | None = None) -> dict[str, Any]:
        """更新笔记：先 GET 拿最新 tag，再提交（乐观锁）。

            POST /note/note/{id}
            body: entry=<JSON>&serviceToken=<token>

        ★ **这条路径认 `modifyDate`**（新建那条不认）—— 已实测：
        传 2023-11-15，读回来就是 2023-11-15，且 `createDate` 会被保留。
        所以 `create_note` 建完会再调这里一次，把原始时间回填进去。

        `_cur`：调用方**已经拿到**该笔记的 entry 时直接传进来，
        省掉一次详情 GET（create_note 就是这么用的 ——
        新建的响应体里已经带了完整 entry，没必要再取一遍）。

        `when_ms` 是内容原本的修改时间；`createDate` 保持原值不动 ——
        改创建时间会让笔记跳到列表的另一个位置。
        """
        # `_cur` 有值就不去 GET —— 调用方（create_note）刚从新建响应里
        # 拿到了完整 entry，再取一次纯属浪费一个请求。
        cur = _cur if _cur is not None else self._entry(note_id)
        now = int(when_ms or time.time() * 1000)
        xml = text_to_mi_xml(text)
        entry = {
            "id": str(cur.get("id") or note_id),
            "tag": cur.get("tag"),
            "status": cur.get("status") or "normal",
            "createDate": int(cur.get("createDate") or now),
            "modifyDate": now,
            "colorId": int(cur.get("colorId") or 0),
            "content": xml,
            "setting": cur.get("setting") or {"themeId": 0, "stickyTime": 0, "version": 0},
            "folderId": str(cur.get("folderId") or "0"),
            "alertDate": int(cur.get("alertDate") or 0),
            "extraInfo": self._extra_info(cur),
            # 和 create_note 同理：subject 是列表显示的标题，必须跟随内容更新。
            # 用 `or cur.get(...)` 兜底 —— 万一内容首行为空（整条是空白），
            # 就保留原来的标题，不要把它清成空串。
            "subject": first_line(text) or cur.get("subject"),
            "snippet": first_nonempty_line(xml),
        }
        r = self._post_form(f"/note/note/{note_id}", {
            "entry": json.dumps(entry, ensure_ascii=False),
            "serviceToken": self._service_token(),
        })
        if r.get("result") != "ok":
            raise RuntimeError(
                f"更新小米笔记失败：{r.get('description') or str(r)[:200]}")
        self._detail.pop(note_id, None)
        return {"id": note_id, "tag": (r.get("data") or {}).get("tag", "")}

    def delete_note(self, note_id: str) -> None:
        """删除笔记（软删除，进回收站）。

            POST /note/full/{id}/delete
            body: tag=<tag>&purge=false&serviceToken=<token>

        小米的删除是两步状态机：normal --(purge=false)--> deleted --(purge=true)--> 物理删除。
        **不能跳步** —— 对 normal 实体直接 purge 会返回「便签未被删除」(51006)。
        同步场景只需要软删除（用户还能从回收站恢复），所以这里永远传 false。
        """
        cur = self._entry(note_id)
        tag = cur.get("tag")
        if not tag:
            raise RuntimeError(f"笔记 {note_id} 详情里没有 tag，无法删除")
        r = self._post_form(f"/note/full/{note_id}/delete", {
            "tag": str(tag),
            "purge": "false",
            "serviceToken": self._service_token(),
        })
        if r.get("result") != "ok":
            raise RuntimeError(
                f"删除小米笔记失败：{r.get('description') or str(r)[:200]}")
        if (r.get("data") or {}).get("conflict"):
            raise RuntimeError("删除时 tag 已过期（conflict），下一轮会重试")
        self._detail.pop(note_id, None)
