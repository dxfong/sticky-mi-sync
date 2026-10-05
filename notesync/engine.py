"""同步引擎：三方比较 → 四方决策。

每一轮做四件事
--------------
1. 拉两侧全量（当前状态）
2. 拿 link 表里的 base_hash 当基线，判断每条映射"谁改了"
3. 按四方决策表执行
4. 成功执行后才更新 base_hash（失败的不更新，下一轮重试）

四方决策
--------
  便笺未改 + 小米未改  -> 跳过
  便笺未改 + 小米已改  -> PATCH 便笺（带 If-Match）
  便笺已改 + 小米未改  -> 更新小米笔记
  便笺已改 + 小米已改  -> 冲突，保留双方 + 告警

删除
----
  一侧删除 + 另一侧未改 -> 删另一侧
  一侧删除 + 另一侧已改 -> **以修改为准，把它重新建回来**（保留数据，不丢）
                          （这一步同时解决了"不收敛"的问题：重建后就有了新的映射）

首次配对
--------
link 表为空时按 initial_pairing 策略走，默认 graph_authoritative：
只把便笺推到小米，小米侧已有的笔记不动（否则两边会互相复制一遍）。
配对完成后就把 meta.paired 置 1，之后新增的笔记双向处理。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
from typing import Any

from .textutil import content_hash, first_line

# 日志里给"这次拉列表是谁触发的"起的中文名。
# 用户要求把**手动**和**自动**的拉取在日志里分开 ——
# 否则看到一行"拉取微软便笺列表：165 条"根本不知道是点了按钮还是后台自己在跑。
PULL_LABEL = {
    "manual": "手动",
    "auto": "自动",
    "startup": "启动",
    "switch": "开关",
}

# 小米写接口的配额异常。写在 xiaomi.py 里（那一层才知道 HTTP 细节），
# 这里只用来**分拣**：配额耗尽要停轮，不能当成偶发错误继续硬试。
# 用 try 包一层只是为了防循环导入，正常情况下走不到 except 分支。
try:
    from .xiaomi import QuotaExceeded
except Exception:                      # pragma: no cover
    class QuotaExceeded(RuntimeError):
        pass

# 连续失败几轮就自动暂停自动同步。
# 凭据一过期，5 秒一轮的循环会变成 5 秒一条错误日志 —— 真正有用的那条信息
# 会被自己刷掉，还白打接口。3 轮（默认 15 秒）足够区分"偶发抖动"和"真的坏了"。
FAIL_STREAK_LIMIT = 3

# 日志里给两侧起的中文名（"拉取微软便笺列表：165 条"比 "graph: 165" 好读）
SIDE_LABEL = {"graph": "微软便笺", "xiaomi": "小米笔记"}


# ---------------------------------------------------------------- 时间转换
# 两端的时间格式不一样，同步时必须换算，否则排序会乱：
#   便笺 documentModifiedAt → ISO 字符串（有时带 7 位小数 + Z）
#   小米 modifyDate        → 毫秒时间戳
# 关键用途：把**内容原本的修改时间**写给另一端，而不是写 now。
# 否则一批记录会在同一秒落地，另一端按时间排序时顺序全乱（用户报过）。

def _now_iso() -> str:
    """当前 UTC 时间，便笺那种 ISO 形状（`...Z`，秒级）。

    只在"本地补列表项"时当占位用 —— 真实时间以服务端为准。
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def iso_to_ms(iso: str | None) -> int | None:
    """便笺的 ISO 时间 → 小米要的毫秒时间戳。解不出来返回 None（调用方会退回 now）。"""
    if not iso:
        return None
    try:
        s = str(iso).strip().replace("Z", "+00:00")
        # .NET 风格 7 位小数（2026-09-12T17:31:34.1234567Z）→ Python 只认 6 位
        s = re.sub(r"\.(\d{6})\d+", r".\1", s)
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def ms_to_iso(ms) -> str:
    """小米的毫秒时间戳 → 便笺要的 ISO（UTC，秒精度）。"""
    try:
        v = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    if v < 10**11:      # 万一给的是秒级
        v *= 1000
    try:
        return datetime.datetime.utcfromtimestamp(v / 1000).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError, OverflowError):
        return ""


class RoundStopped(Exception):
    """本轮**提前收尾**（不是失败，不回滚）。

    三种原因，都不该被当成"同步出错"，所以不触发 fail_streak、不暂停自动同步：
      · `abort`     —— 用户点了中止
      · `budget`    —— 本轮写入条数已达上限（防风控），下一轮接着做
      · `cooldown`  —— 小米写接口命中配额，处于冷却期，本轮不再写
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class Aborted(RoundStopped):
    """同步被用户中止。

    **中止点只设在"每条记录之间"**，绝不设在某个动作的中途 ——
    写便笺写到一半被打断会留下一半的内容，比不中止更糟。
    所以中止的语义是：**当前这条做完，下一条不再开始**。
    已经写完的动作不会被回滚（同步本身是幂等的，下一轮会接着收敛）。
    """

    def __init__(self, detail: str = ""):
        super().__init__("abort", detail)

ACTION_SKIP = "skip"
ACTION_TO_XIAOMI = "to_xiaomi"
ACTION_TO_GRAPH = "to_graph"
ACTION_CONFLICT = "conflict"
ACTION_DELETE_XIAOMI = "delete_xiaomi"
ACTION_DELETE_GRAPH = "delete_graph"
ACTION_LINK_NEW_MI = "link_new_mi"
ACTION_LINK_NEW_GRAPH = "link_new_graph"

LABELS = {
    ACTION_TO_XIAOMI: "便笺 -> 小米",
    ACTION_TO_GRAPH: "小米 -> 便笺",
    ACTION_CONFLICT: "冲突：保留双方",
    ACTION_DELETE_XIAOMI: "删除小米笔记",
    ACTION_DELETE_GRAPH: "删除便笺",
    ACTION_LINK_NEW_MI: "新建小米笔记",
    ACTION_LINK_NEW_GRAPH: "新建便笺",
    ACTION_SKIP: "跳过",
}


class SyncEngine:
    def __init__(self, store, graph, xiaomi):
        self.store = store
        self.graph = graph
        self.xiaomi = xiaomi
        self.last_result: dict[str, Any] = {}
        self.last_notes: dict[str, list] = {"graph": [], "xiaomi": []}
        self.last_notes_at = 0
        # 列表是不是"来自上次的缓存"（还没被真实拉取覆盖过）
        self._from_cache = False
        # 本轮同步的进度，供前端轮询显示（{done, total, at}）
        self.progress: dict[str, int] = {"done": 0, "total": 0, "at": 0}
        # 上一次**全量**拉取时两侧列表的指纹 + 时刻。
        # 用途：日志里能明确指出"这次拉取发现了变化"，并算出"距上次拉取多少秒" ——
        # 用户要看的正是"内容改变以后，后台多久才把列表拉下来"。
        self._pull_fp: tuple[str, str] | None = None
        self._pull_at: int = 0
        # ★ 界面快照是否被**本地补丁**改过（publish/unpublish）。
        # 为什么需要它：「就地补丁」只是把本轮改动并进快照，**它不等价于
        # 和远端真实状态完全一致**（重建/改链等分支会改变行的构成）。
        # 而同步开关关着时，后台**根本不会重拉列表** ——
        # 于是补丁一旦和真实状态有出入，界面就**永久**显示错，
        # 只能靠用户手动点「拉取列表」纠正（实测被这么反馈过）。
        # 所以打了这个标记，让前端在空闲时自己补一次全量拉取。
        self.snapshot_dirty = False
        # ★ 上一轮快照，按 id 索引：{side: {id: note}}。
        # 用途只有一个，但很关键：**记录被删掉之后就查不到了** ——
        # 而用户要求"云端删掉的记录也要能恢复"，恢复必须知道它原来是什么。
        # 所以在覆盖 last_notes 之前先把旧的按 id 存一份，删的时候再去这里捞。
        self._prev: dict[str, dict[str, dict]] = {"graph": {}, "xiaomi": {}}
        # 用户点"中止"后置 True。只在下一条记录开始前生效（见 Aborted 的说明）
        self.abort = False
        self.busy = False
        # 连续失败计数：凭据过期后如果任由循环跑下去，会每几秒失败一次。
        # 达到阈值就**自动暂停自动同步**，把控制权交回给人 —— 比默默刷屏日志好得多。
        # 存在 meta 里，服务重启后仍然延续（重启本身不该被当成"问题已解决"）。
        try:
            self.fail_streak = int(self.store.get_meta("fail_streak", "0") or 0)
        except (TypeError, ValueError):
            self.fail_streak = 0

        # **启动时先把上次的列表从库里读回来。**
        # 列表原来只在内存里，服务一重启就空了 —— 用户看到"列表被重置、
        # 要重新拉取"，而拉一次微软侧要翻 16 页（约 20 秒）。
        # 先拿缓存顶上，界面立刻有内容；随后后台拉取会刷新它。
        try:
            cached = self.store.cache_load()
            if cached.get("graph") or cached.get("xiaomi"):
                self.last_notes = {"graph": cached.get("graph") or [],
                                   "xiaomi": cached.get("xiaomi") or []}
                self.last_notes_at = int(self.store.get_meta("notes_cache_at", "0") or 0)
                self._from_cache = True
        except Exception:
            pass

    # ------------------------------------------------------------------ 工具
    def _cooldown_left(self) -> int:
        """小米写接口还剩多少秒冷却（0 = 不在冷却）。

        用 getattr 取，而不是直接调 —— 假 provider、旧 provider
        可能没这个方法，不该因此让整轮挂掉。
        """
        fn = getattr(self.xiaomi, "cooldown_left", None)
        if not callable(fn):
            return 0
        try:
            return max(0, int(fn() or 0))
        except Exception:
            return 0

    def _xiaomi_writable(self) -> bool:
        cfg = self.store.cfg["xiaomi"]
        if cfg.get("mode") == "mock":
            return True
        return bool(cfg.get("write_enabled"))

    # ------------------------------------------------------------------ 就绪检查
    def readiness(self) -> dict[str, Any]:
        """自动同步能不能开。不满足就返回原因，前端据此禁用开关。

        两条硬门槛：
          1. 两端都要登录（没登录跑同步只会每轮报错刷日志）
          2. **必须指定小米侧的目标文件夹** —— 否则会变成"全盘同步"，
             这正是要避免的
        """
        blockers: list[str] = []
        try:
            gst = self.graph.status()
            if not gst.get("connected"):
                blockers.append("微软便笺未登录")
        except Exception as e:
            blockers.append(f"微软侧不可用：{e}")
        try:
            xst = self.xiaomi.status()
            if not xst.get("connected"):
                blockers.append("小米笔记未登录（还没提供 Cookie）")
            elif xst.get("cred_state") == "expired":
                # "有 cookie 字段"和"凭据真能用"是两件事。失效了就必须拦下来，
                # 否则开自动同步 = 每 5 秒失败一次。
                #
                # ★ 这里**不能**再引导用户点「浏览器登录」——
                #   那个按钮要弹有头浏览器（Windows 专有），容器里根本用不了。
                #   而且小米对**新设备**（扫码 = 新设备）会要求交互式安全验证
                #   （响应里的 isSecondValidation: true，绕不过去），
                #   所以容器里扫码也过不了这一关。正确路径是：
                #   在**有浏览器的机器**上完成一次登录（顺手把安全验证做掉），
                #   再把凭据搬过来 —— 那台机器就成了"可信设备"。
                blockers.append(
                    "小米凭据已失效 —— 请在**有浏览器的电脑**上跑一次 "
                    "`python -m notesync.browser_auth`（会弹窗口，"
                    "把登录和任何安全验证做完），然后 "
                    "`python -m notesync.export_creds` 导出，"
                    "把小米那一段粘到本页的小米卡片里。"
                    "（容器里扫码会被当成「新设备」，小米必然要求安全验证，过不去。）")
            elif not str(self.store.cfg["xiaomi"].get("folder_id") or "").strip():
                blockers.append("未指定小米笔记的同步文件夹")
        except Exception as e:
            blockers.append(f"小米侧不可用：{e}")
        return {"ready": not blockers, "blockers": blockers}

    # ------------------------------------------------------------------ 拉取留痕
    @staticmethod
    def _list_fp(notes: list, time_key: str) -> str:
        """给一份列表算指纹：id + 修改时间。

        为什么不只看条数：**内容改了但条数没变**是最常见的情况
        （改一条便笺的正文），只看条数会漏掉。
        也不看正文本身 —— 那要额外拉详情，太重。
        id + 修改时间的组合足够了：任一侧有增删改，它就会变。
        """
        h = hashlib.sha1()
        for part in sorted("%s@%s" % (n.get("id"), n.get(time_key) or "")
                           for n in notes):
            h.update(part.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()[:16]

    def note_pull(self, source: str, g_all: list, m_all: list,
                  partial: str = "") -> None:
        """把"拉了一次列表"记进日志。

        - `source` 区分手动 / 自动 / 启动（见 PULL_LABEL）。用户明确要求分开，
          否则一行"拉取…列表：N 条"看不出是谁触发的。
        - **检测到内容变化时单独用一句话点出来**，并附上"距上次拉取多少秒"，
          这样"改完之后后台多久才拉到"一眼可见。
        - 没变化时记的是一条**内容固定**的消息（只有条数），
          会被 store.log 的 60 秒去重自然压掉 —— 5 秒一轮也不会刷屏；
          而一旦有变化，消息内容不同，立刻就能在日志里看到。
        - `partial`：这次拿到的**不是全量**（受限模式只拉一页 / 窄模式只取已配对），
          此时**不能**拿它算指纹 —— 部分列表相比全量必然"处处不同"，
          会把"截断"误判成"大量删除"。给个非空字符串就只记一条带原因的日志。
        """
        label = PULL_LABEL.get(source, source)
        now = int(time.time())
        n_g, n_m = len(g_all), len(m_all)

        if partial:
            self.store.log(f"{label}拉取列表（{partial}）："
                           f"便笺 {n_g} 条 / 小米 {n_m} 条")
            return

        fp = (self._list_fp(g_all, "modified_iso"),
              self._list_fp(m_all, "modify_date"))
        prev = self._pull_fp
        if prev and prev != fp:
            which = []
            if prev[0] != fp[0]:
                which.append("便笺")
            if prev[1] != fp[1]:
                which.append("小米")
            gap = (now - self._pull_at) if self._pull_at else 0
            self.store.log(
                f"{label}拉取列表 —— **{'/'.join(which)}侧有变化**："
                f"便笺 {n_g} 条 / 小米 {n_m} 条（距上次拉取 {gap} 秒）")
        else:
            self.store.log(f"{label}拉取列表：便笺 {n_g} 条 / 小米 {n_m} 条")
        self._pull_fp = fp
        self._pull_at = now

    def _fetch_one(self, side: str, note_id: str) -> dict[str, Any] | None:
        """取**单条**记录，形状与 list_notes 的单条一致。

        优先用 provider 的单条读取接口（便笺侧 FabricClient.get、
        小米侧详情接口）—— 一条请求 vs 全量十几页。
        **provider 没有这个方法时**才退回全量列表再挑 —— 宁可慢一点，
        也不要因为这个优化让功能失效。

        `get_note` 返回 None 一律当作"这一侧没有这条"（它内部已经把
        网络异常和"已删除"都收敛成 None）。窄操作里"这条这轮先跳过"是安全的：
        它不增不删，下一轮或下次拉取自然会再看到。
        """
        nid = str(note_id or "")
        if not nid:
            return None
        prov = self.graph if side == "graph" else self.xiaomi
        fn = getattr(prov, "get_note", None)
        if callable(fn):
            try:
                return fn(nid) or None
            except Exception as e:
                self.store.log(
                    f"单条读取失败（{side} {nid[:12]}）：{type(e).__name__}: {e}",
                    "warn")
                return None
        # 兜底：provider 不支持单条读取
        try:
            lst = [n for n in prov.list_notes() if not n.get("deleted")]
        except Exception:
            return None
        return next((n for n in lst if str(n.get("id")) == nid), None)

    # ------------------------------------------------------------------ 只读拉取
    def fetch_only(self, source: str = "manual") -> dict[str, Any]:
        """只读拉两侧列表，**不执行任何同步动作**。

        用途：同步开关还没打开时，也要能先看到两边的列表和条数对不对。
        两侧各自独立容错 —— 一侧失败不影响另一侧。

        `source`：是谁触发的（manual / auto / startup / switch），会写进日志。
        """
        errors: list[str] = []
        for name, src in (("graph", self.graph), ("xiaomi", self.xiaomi)):
            label = SIDE_LABEL.get(name, name)
            try:
                notes = [n for n in src.list_notes() if not n.get("deleted")]
                self.last_notes[name] = notes
                # 拉成功就把这一侧落盘。
                # **逐侧处理**：一侧失败时另一侧照样更新，
                # 失败那侧的缓存保持原样（不清空）—— 否则一次网络抖动
                # 就会让用户的列表凭空变空。
                try:
                    self.store.cache_save(name, notes)
                except Exception:
                    pass
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")
                self.store.log(f"拉取{label}列表失败：{type(e).__name__}: {e}",
                               "error")
        # 统一在最后记一条（含来源与变化检测），而不是每侧各记一条 ——
        # 两侧分开记会导致"便笺有变化"和"小米有变化"割裂成两条读不出因果的消息。
        self.note_pull(source, self.last_notes.get("graph") or [],
                       self.last_notes.get("xiaomi") or [])
        # 刚全量重拉过，快照与远端一致 —— 清掉脏标记
        self.snapshot_dirty = False
        self.last_notes_at = int(time.time())
        self._from_cache = False
        try:
            self.store.set_meta("notes_cache_at", str(self.last_notes_at))
        except Exception:
            pass
        # 只读拉取能成，就说明两边的凭据**确实可用**（而不是"有 token"）。
        # 所以这里也记一次健康 —— 用户点一下"刷新列表"就能看到
        # 健康档案更新，不用等到下一轮自动同步。
        if not errors:
            self._mark_ok()
        return {
            "ok": not errors,
            "errors": errors,
            "counts": {k: len(v) for k, v in self.last_notes.items()},
            "at": self.last_notes_at,
        }

    # ------------------------------------------------------------------ 主流程
    def run_once(self, dry_run: bool | None = None, limit: int | None = None,
                 resume: bool = True, force: bool = False,
                 source: str = "manual",
                 changed_only: bool = False) -> dict[str, Any]:
        """跑一轮同步。

        `limit` 是**测试用的安全模式**：只处理最新的 N 条便笺/笔记。

        ⚠ 为什么不能简单截断列表就完事：
        同步的判断依据是"这条在本轮列表里出现了没有"——没出现就认为它被删了。
        如果我们只喂进去 N 条，剩下 150 多条全"没出现"，
        就会被判成删除，触发重建（便笺被删但小米改了 → 重建便笺）甚至删数据。
        所以 limit 模式下**必须跳过所有"只有一边在"的分支**，
        只做两端都在的条目：正常比较 + 双向同步。
        """
        if self.busy:
            return {"skipped": True, "reason": "上一轮还没跑完"}
        self.busy = True
        started = time.time()
        self.abort = False          # 每轮开始都清掉上一轮可能残留的中止标记
        dry = self.store.cfg["dry_run"] if dry_run is None else dry_run
        limit_n = max(0, int(limit or 0))
        actions: list[dict[str, str]] = []
        errors: list[str] = []

        # ★ 本轮的改动登记，**必须在 try 之前就建好**。
        # 因为"列表局部更新"要放在 finally 里（每条退出路径都要跑），
        # 而 finally 在拉取就失败那条路径上也会执行 —— 那时它们还没被赋值过。
        # 本轮主动删掉的条目。必须记下来 ——
        # 否则后面的"未关联条目"循环用的还是本轮开始时的快照，
        # 会把刚删掉的那条又当成新条目建回去，来回震荡。
        dropped: dict[str, set[str]] = {"graph": set(), "xiaomi": set()}
        # 本轮新建/更新过的条目（写成功时就地登记，见下面的 _touch）。
        touched: dict[str, list] = {"graph": [], "xiaomi": []}

        def publish(side: str, item: dict) -> None:
            """写成功一条就**立刻**并进界面列表（不等轮末）。

            为什么不等轮末：一轮可能要 40 秒（40 次写入 × 1 秒节流），
            期间界面完全不动；更要命的是这一轮**中途失败**时
            （例如凭据出问题写不进去了），用户看到的是
            "同步报错了，而且状态灯再也不更新"—— 实测被这么反馈过。
            逐条并进去，界面就跟着写入进度实时变，出错时也已经反映出
            已经成功的那部分。
            """
            touched[side].append(item)
            try:
                self.cache_add(side, item)
            except Exception as e:
                self.store.log(f"更新界面列表失败（{side}）：{e}", "warn")

        def unpublish(side: str, note_id: str) -> None:
            """删掉一条就**立刻**从界面列表里摘掉（同上，不等轮末）。"""
            dropped[side].add(note_id)
            try:
                self.cache_drop(side, note_id)
            except Exception as e:
                self.store.log(f"更新界面列表失败（{side}）：{e}", "warn")


        try:
            # 受限模式下只需要**第一页**就够 —— 省掉十几次翻页。
            # 便笺每页固定 19 条，拉全 165 条要 16 个请求（约 24 秒），
            # 而"只看最新一条"根本用不着那么多。
            if changed_only:
                # ★★★ 「只同步改动过内容的」：**只取 link 表里那些已配对的记录**。
                #   逐条取（便笺侧 FabricClient.get、小米侧详情接口），
                #   全量翻页要十几页约 20 秒，而这里通常只有几条。
                #   更关键的是：这样构造出来的快照里**只有已配对记录**，
                #   于是下面两个"未关联"循环自然一条都不会新建 ——
                #   这正是这个模式的语义：**不含新建**。
                mp = None
                g_all, m_all = [], []
                for _l in self.store.all_links():
                    _g = self._fetch_one("graph", _l.get("graph_id") or "")
                    _m = self._fetch_one("xiaomi", _l.get("mi_id") or "")
                    if _g:
                        g_all.append(_g)
                    if _m:
                        m_all.append(_m)
                if self.store.all_links():
                    self.store.log(
                        f"只同步改动过内容的：已配对 {len(self.store.all_links())} 条，"
                        f"逐条取回 {len(g_all)} 便笺 / {len(m_all)} 小米笔记"
                        f"（**不新建、不删除**）")
            else:
                mp = 1 if limit_n else None
                g_all = [n for n in self.graph.list_notes(max_pages=mp)
                         if not n.get("deleted")]
                m_all = [n for n in self.xiaomi.list_notes()
                         if not n.get("deleted")]

            # ★★★ 在**截断处**按时间倒序排，不依赖各 provider 的实现。★★★
            #
            # 「只同步最新 N 条」是靠 `list[:N]` 实现的 —— 这个写法**默认
            # 列表已经按时间倒序**。但那是不能假设的：
            # 小米的 /note/full/page 返回的是**最旧在前**（时间升序），
            # 于是 [:1] 取到的是**最旧的那条**。
            # 用户实测踩到：新建「哈哈哈」后点"只同步最新一条"，
            # 引擎拿到的是最旧的「916」，「哈哈哈」压根没进视野。
            #
            # 两个 provider 现在都会自己排（便笺侧本来就有，小米侧已补），
            # 但这里再排一次 —— 截断的正确性不该寄希望于上游实现细节。
            # 顺便：last_notes（界面快照）也直接吃这个顺序，界面才两端一致。
            g_all.sort(key=lambda n: (n.get("modified_iso") or ""), reverse=True)
            m_all.sort(key=lambda n: int(n.get("modify_date") or 0), reverse=True)

            # 拉完就地留痕（含来源 + 变化检测）。
            # **自动同步时这一句会在每轮都走一遍** —— 没变化的那条消息内容固定，
            # 会被 store.log 的 60 秒去重压掉；一旦有变化就立刻出现，
            # 正好回答"内容改了之后，后台多久才拉到"。
            self.note_pull(source, g_all, m_all,
                           partial=("只取已配对记录" if changed_only
                                    else ("受限模式·只拉第一页" if limit_n else "")))
            if changed_only:
                self.store.log(
                    f"窄同步：只对齐**已配对且内容有差异**的记录"
                    f"（不新建、不删除、不重建）")
            if limit_n:
                self.store.log(f"受限同步：只处理最新 {limit_n} 条"
                               f"（只拉第一页，跳过翻页）")
            # **受限模式绝不能拿这半页数据覆盖前端快照。**
            # 它只拉了一页（19 条），覆盖上去前端就从 165 条缩成 1 条 ——
            # 用户点了"只同步最新一条"后发现整个列表没了，就是这个原因。
            # 保留上一次全量拉取的快照；想看受控操作的最新结果点"拉取列表"即可。
            # 受限模式且**快照还是空的**（刚启动、还没拉过列表）时仍要填一次，
            # 否则前端会一直空着；有快照就保留更全的那份。
            has_snap = bool((self.last_notes.get("graph") or [])
                            or (self.last_notes.get("xiaomi") or []))
            # 窄模式的列表是**部分的**（只有已配对那几条），
            # 无论如何都不能拿去覆盖界面快照 —— 否则列表会从 167 条缩成 3 条。
            if changed_only:
                pass
            elif not limit_n or not has_snap:
                # **先留住上一轮**：被删的记录只在旧快照里还有内容。
                for _side in ("graph", "xiaomi"):
                    for _n in (self.last_notes.get(_side) or []):
                        if _n.get("id"):
                            self._prev[_side][str(_n["id"])] = _n
                self.last_notes = {"graph": list(g_all), "xiaomi": list(m_all)}
                self.snapshot_dirty = False   # 刚全量拉过
                self.last_notes_at = int(time.time())
            g_notes = {n["id"]: n for n in (g_all[:limit_n] if limit_n else g_all)}
            m_notes = {n["id"]: n for n in (m_all[:limit_n] if limit_n else m_all)}
            # 注意：这里**不要再覆盖一次 self.last_notes**。
            # 曾经这里有一句 `self.last_notes = {g_notes…}`，用的是上面被截断的
            # g_notes —— 结果受限同步一跑，前端列表就从 165 条缩成 1 条。
            # 快照的赋值统一在上面那一处做，且只用完整列表。
            cfg = self.store.cfg

            links = {l["mi_id"]: l for l in self.store.all_links()}
            first_run = not links and self.store.get_meta("paired") != "1"
            policy = cfg.get("initial_pairing", "graph_authoritative")
            xiaomi_ok = self._xiaomi_writable()

            # ---- 首次配对的"不收养"保护：改成按**时间**判定 ----
            #
            # 原来的写法是 `if first_run and policy == graph_authoritative: 跳过`，
            # 靠 meta.paired 标志。它有两个问题：
            #   1. 配对完成后 paired 就被置 1，**保护只持续一轮** ——
            #      下一轮会把小米侧所有既有笔记一次性全收养过来，很突然。
            #   2. 用户"清空小米后重新配对"时，paired 被清成空，
            #      于是刚在手机上新建的那条也被当成"既有笔记"跳过 ——
            #      用户实测踩到：小米新建的「916」没同步到微软。
            #
            # 改成记住**配对起点**：只有"配对开始之前就存在的小米笔记"才不动，
            # 之后新建的（比如用户刚在手机上写的）正常收过来。
            # 这个保护是**长期有效**的，不再依赖那一轮性的标志。
            try:
                pairing_since = int(self.store.get_meta("pairing_since", "0") or 0)
            except (TypeError, ValueError):
                pairing_since = 0
            if policy == "graph_authoritative" and not pairing_since:
                # 没记过就现在记 —— 此刻之前存在的小米笔记视作"既有笔记"
                pairing_since = int(time.time() * 1000)
                self.store.set_meta("pairing_since", str(pairing_since))
                self.store.log(
                    "首次配对起点已记录：此刻之前就存在的小米笔记保持不动"
                    "（策略 graph_authoritative）；之后新建的会正常同步")

            # ---- 写入节流相关（防风控）----
            # 每轮开始把"本轮已写条数"归零。
            try:
                self.xiaomi.reset_write_budget()
            except AttributeError:
                pass
            # 本轮写入上限。首轮全量推送是最容易打爆配额的场景，
            # 靠这个把"一口气 165 条"摊成几轮。
            # 0 = 不限制。
            try:
                _write_budget = int(cfg.get("xiaomi", {}).get(
                    "max_writes_per_round", 40) or 0)
            except (TypeError, ValueError):
                _write_budget = 40
            # 命中配额后进入冷却时，本轮直接不做事（连读都不必做）——
            # 用户此刻要的是"别再打了"，不是"再拉一次列表"。
            if xiaomi_ok and not dry and not force and not limit_n:
                left = self._cooldown_left()
                if left > 0:
                    cooldown = {
                        "ok": False, "quota_cooldown": True,
                        "wait_sec": left,
                        "actions": [], "errors": [],
                        "at": int(time.time()),
                        "counts": self._counts(),
                        "hint": (f"小米写接口仍在配额冷却中，还需 {left} 秒"
                                 f"（约 {max(1, left // 60)} 分钟）。"
                                 f"本轮已跳过 —— 冷却结束后会自动接着同步。"),
                    }
                    self.last_result = cooldown
                    return cooldown

            # （dropped / touched 已在 try 之前声明 —— finally 里要用。）

            # 本轮因"配对前既有"而跳过的小米笔记条数。
            # **必须单独计数并写进日志** —— ACTION_SKIP 是不进日志的，
            # 否则用户会看到"点了同步但小米那条没动"，完全不知道为什么。
            preexisting_skipped = 0


            def plan(kind: str, detail: str) -> None:
                actions.append({"action": kind, "label": LABELS.get(kind, kind),
                                "detail": detail})

            # ---------------------------------------------------- 进度上报
            # 用户要求同步时能看到"1/166 → 2/166"这样的进度。
            # 两条通道都要有：
            #   1) 日志里按批次记（**不能每条都记** —— 166 条就是 166 行，
            #      把有用信息全冲走；这里按总量的 ~10% 分档，约 10 条）
            #   2) 内存里放一个 progress 对象，前端轮询时能读到实时数字
            total_units = len(links) + len(g_notes) + len(m_notes)
            step = max(1, total_units // 10)
            done = 0
            self.progress = {"done": 0, "total": total_units, "at": int(time.time())}

            # 断点续传：上次中止时留下的"已完成"集合。
            # resume=False 时忽略它（用户想强制全量重跑）。
            done_keys = self._load_done() if resume else set()
            if done_keys:
                self.store.log(f"接着上次的断点继续（上次已完成 {len(done_keys)} 条）")

            # ---------------------------------------------------- 大规模删除安全阀
            #
            # 为什么必须有这一道：删除传播是**双向**的 ——
            # "小米侧删了 + 便笺没改" → 删便笺；"便笺删了 + 小米没改" → 删小米笔记。
            # 日常使用里这是对的（一边删、另一边跟着删）。
            #
            # 但它有个致命副作用：**用户清空小米笔记打算重同步时**，
            # 本地映射表还留着上百条 link，每一条都会走成
            #   "小米侧没了 + 便笺未改" → 删便笺
            # 于是这些便笺被从微软云端删除，并同步到用户其它设备 ——
            # 用户真正的笔记就这么没了。这不是推测，是本项目真实存在的路径。
            #
            # 所以：**先预扫，再动手**。预扫是纯计算（不发任何请求），
            # 一旦本轮要删的条数超过阈值，就拦下整轮（一条都不执行），
            # 让用户显式选"确实要删（force）"还是"先清映射再重配对"。
            #
            # 阈值 max(5, 映射总数 20%)：日常删一两条绝不触发，
            # 只有"一次删掉一大片"这种几乎必然是误操作的情况才命中。
            # 窄模式（changed_only）不删任何东西，跳过这道闸 ——
            # 否则会给出"已拦截"的误导性提示，用户以为出了事。
            if not dry and not force and not limit_n and not changed_only:
                will_del_g, will_del_m = 0, 0
                for _mid, _lk in links.items():
                    if ("link:" + str(_mid)) in done_keys:
                        continue
                    _gid = _lk.get("graph_id") or ""
                    _bh = _lk.get("base_hash") or ""
                    _g = g_notes.get(_gid)
                    _m = m_notes.get(_mid)
                    if _g and not _m:
                        # 小米没了；便笺"未改"才会被删 —— 改了反而会重建（保数据）
                        if content_hash(_g["text"]) == _bh:
                            will_del_g += 1
                    elif _m and not _g:
                        if content_hash(_m["text"]) == _bh:
                            will_del_m += 1
                _thr = max(5, int(len(links) * 0.2))
                if (will_del_g + will_del_m) > _thr:
                    self.store.log(
                        f"**已拦截**：本轮将删除 {will_del_g} 条便笺 + "
                        f"{will_del_m} 条小米笔记（阈值 {_thr}），疑似误操作 —— "
                        f"未执行任何动作", "warn")
                    guarded = {
                        "ok": False,
                        "guarded": True,
                        "dry_run": False,
                        "will_delete": {"graph": will_del_g, "xiaomi": will_del_m},
                        "threshold": _thr,
                        "actions": [], "errors": [],
                        "at": int(time.time()),
                        "counts": self._counts(),
                        "hint": ("本轮要删除的条目过多，已全部拦下（一条都没删）。"
                                 "确实想删就带 force 强制；"
                                 "只是想重来，请先「清空映射并重配对」再同步。"),
                    }
                    self.last_result = guarded
                    return guarded      # finally 里会复位 busy

            def tick(key: str = "") -> None:
                nonlocal done
                # **所有"收尾检查"都必须在登记之前做。**
                # 曾经这里先 `done_keys.add(key)` 再检查中止，
                # 于是被中止打断的那一条已经被记成"已完成"，
                # 下次续传会直接跳过它 —— 这条就永远同步不到了。
                # 顺序反过来就对了：先决定要不要停，再登记这一条。
                if self.abort:
                    self._save_done(done_keys)
                    raise Aborted()
                # 本轮写入条数已达上限：收尾，下一轮接着做。
                # 这是**防风控的主力** —— 首轮全量 165 条一口气写完，
                # 实测就是这个节奏把小米的写配额打爆的。
                if xiaomi_ok and not dry and _write_budget:
                    if getattr(self.xiaomi, "writes_this_round", 0) >= _write_budget:
                        self._save_done(done_keys)
                        raise RoundStopped(
                            "budget",
                            f"本轮已写入 {_write_budget} 条（上限），"
                            f"先收尾，下一轮继续")
                # 命中配额后的冷却期内不再写 —— 硬试只会把窗口越拖越长。
                if xiaomi_ok and not dry:
                    left = self._cooldown_left()
                    if left > 0:
                        self._save_done(done_keys)
                        raise RoundStopped(
                            "cooldown",
                            f"小米写入冷却中（还需 {left} 秒）")
                if key:
                    done_keys.add(key)
                done += 1
                self.progress["done"] = done
                self.progress["at"] = int(time.time())
                if done % step == 0 or done == total_units:
                    self.store.log(f"同步进度 {done}/{total_units}")
                    # 分档落盘 —— 每条都写 DB 太费；分档既能续传，
                    # 最坏情况也只是重复处理十几条（同步是幂等的，无害）
                    self._save_done(done_keys)

            # ---------------------------------------------------- 既有关联
            #
            # **按"两侧里较新的那个时间"倒序处理** —— 最近改过的先做，
            # 用户先看到自己关心的那条被同步过去（和写入方向保持一致）。
            def _link_recency(_l: dict) -> str:
                _g = g_notes.get(str(_l.get("graph_id") or ""))
                _m = m_notes.get(str(_l.get("mi_id") or ""))
                return max((_g or {}).get("modified_iso") or "",
                           ms_to_iso((_m or {}).get("modify_date")) or "")

            for mi_id, link in sorted(links.items(),
                                      key=lambda kv: _link_recency(kv[1]),
                                      reverse=True):
                key_l = "link:" + str(mi_id)
                if key_l in done_keys:
                    continue          # 上次已经处理过，续传时直接跳过
                tick(key_l)
                gid = link.get("graph_id") or ""
                base = link.get("base_hash") or ""
                g = g_notes.get(gid)
                m = m_notes.get(mi_id)

                g_hash = content_hash(g["text"]) if g else None
                m_hash = content_hash(m["text"]) if m else None
                g_changed = bool(g) and g_hash != base
                m_changed = bool(m) and m_hash != base

                # 两边都在
                if g and m:
                    if not g_changed and not m_changed:
                        continue
                    if g_changed and not m_changed:
                        if not xiaomi_ok:
                            errors.append(f"便笺 {gid[:8]} 有改动，但小米写入已关闭"
                                          "（xiaomi.write_enabled=false）")
                            continue
                        plan(ACTION_TO_XIAOMI, f"便笺改动 -> 小米 {mi_id}")
                        if not dry:
                            self.xiaomi.update_note(
                                mi_id, g["text"],
                                when_ms=iso_to_ms(g.get("modified_iso")))
                            publish("xiaomi", 
                                self._m_item(mi_id, g["text"],
                                             self._mi_patch_time(iso_to_ms(g.get("modified_iso")))))
                            self._relink(mi_id, gid, g["text"], g, m_notes, mi_id)
                    elif m_changed and not g_changed:
                        plan(ACTION_TO_GRAPH, f"小米 {mi_id} 改动 -> 便笺 {gid[:8]}")
                        if not dry:
                            res = self.graph.update_note(
                                gid, m["text"],
                                link.get("graph_changekey") or "",
                                when_iso=ms_to_iso(m.get("modify_date")))
                            publish("graph", 
                                self._g_item(gid, m["text"],
                                             res.get("change_key", ""),
                                             ms_to_iso(m.get("modify_date"))))
                            self._relink(mi_id, gid, m["text"], None, None,
                                         gid, res.get("change_key", ""))
                    else:
                        # 冲突：微软便笺是权威源，把小米侧的版本另存为新便笺，谁也不丢
                        plan(ACTION_CONFLICT, f"两边都改了：{gid[:8]} / {mi_id}")
                        if not dry:
                            # 冲突副本保留小米侧**原本的修改时间**，
                            # 否则它会排到列表最前，破坏便笺侧的时间顺序
                            dup = self.graph.create_note(
                                "[冲突副本] " + (m["text"] or "")[:2000],
                                when_iso=ms_to_iso(m.get("modify_date")))
                            self.store.log(
                                f"冲突：小米侧版本已另存为便笺 {dup['id'][:8]}", "warn")
                            publish("graph", 
                                self._g_item(dup["id"],
                                             "[冲突副本] " + (m["text"] or "")[:2000],
                                             dup.get("change_key", ""),
                                             ms_to_iso(m.get("modify_date"))))
                            if xiaomi_ok:
                                self.xiaomi.update_note(
                                    mi_id, g["text"],
                                    when_ms=iso_to_ms(g.get("modified_iso")))
                                publish("xiaomi", 
                                self._m_item(mi_id, g["text"],
                                             self._mi_patch_time(iso_to_ms(g.get("modified_iso")))))
                            self._relink(mi_id, gid, g["text"], g, m_notes, mi_id,
                                         conflict=0)
                    continue

                # ↓↓ limit 模式的安全阀 ↓↓
                # 截断后的列表必然让大量条目"在本轮缺失"，
                # 而下面的分支会把"缺失"解读成"被删了"并触发重建/删除。
                # 所以受限同步只做两端都在的条目，缺一边的一律不碰。
                # **窄模式（changed_only）同理**：它的快照里只有已配对那几条，
                # 其余条目全都"缺失"，不走这道闸就会把整库判成被删。
                if limit_n or changed_only:
                    continue

                # 便笺没了（被删）
                if not g and m:
                    if m_changed:
                        plan(ACTION_LINK_NEW_GRAPH, f"便笺被删但小米改了，重建便笺 <- {mi_id}")
                        if not dry:
                            created = self.graph.create_note(
                                m["text"],
                                when_iso=ms_to_iso(m.get("modify_date")))
                            publish("graph", 
                                self._g_item(created["id"], m["text"],
                                             created.get("change_key", ""),
                                             ms_to_iso(m.get("modify_date"))))
                            self.store.delete_link(mi_id)
                            self.store.upsert_link(mi_id, created["id"],
                                                   content_hash(m["text"]),
                                                   created.get("change_key", ""),
                                                   int(m.get("modify_date") or 0))
                    else:
                        if not xiaomi_ok:
                            errors.append(f"便笺已删，但小米写入已关闭，跳过删除 {mi_id}")
                            continue
                        plan(ACTION_DELETE_XIAOMI, f"便笺已删 -> 删小米 {mi_id}")
                        if not dry:
                            # ★ 先入回收站再删 —— 云端（便笺侧）删掉的那条，
                            # 内容取自"最后已知"；小米侧内容取当前值。
                            # 这样恢复时两端都能建回来。
                            _tid = self._trash_before_delete(
                                "link:" + str(mi_id),
                                self._last_known("graph", gid), m, mi_id)
                            self.xiaomi.delete_note(mi_id)
                            self.store.delete_link(mi_id)
                            unpublish("xiaomi", mi_id)
                            if _tid > 0:
                                self.store.log(
                                    f"便笺侧已删 → 小米侧同步删除，"
                                    f"已存入回收站 #{_tid}（可恢复，两端都会还原）",
                                    "warn")
                    continue

                # 小米笔记没了（被删）
                if g and not m:
                    if g_changed:
                        if not xiaomi_ok:
                            errors.append(f"小米笔记已删但便笺改了，需重建小米笔记"
                                          f"（write_enabled=false）")
                            continue
                        plan(ACTION_LINK_NEW_MI, f"小米侧被删但便笺改了，重建小米 <- {gid[:8]}")
                        if not dry:
                            created = self.xiaomi.create_note(
                                g["text"], self._folder_id(),
                                when_ms=iso_to_ms(g.get("modified_iso")))
                            if created.get("id"):
                                publish("xiaomi", 
                                    self._m_item(created["id"], g["text"],
                                                 self._mi_patch_time(iso_to_ms(g.get("modified_iso")))))
                            self.store.delete_link(mi_id)
                            self.store.upsert_link(created["id"], gid,
                                                   content_hash(g["text"]),
                                                   g.get("change_key", ""))
                            unpublish("xiaomi", mi_id)
                    else:
                        plan(ACTION_DELETE_GRAPH, f"小米已删 -> 删便笺 {gid[:8]}")
                        if not dry:
                            # ★ 同上：小米侧是"云端删的"，内容取自"最后已知"。
                            _tid = self._trash_before_delete(
                                "link:" + str(mi_id), g,
                                self._last_known("xiaomi", mi_id), mi_id)
                            self.graph.delete_note(gid)
                            self.store.delete_link(mi_id)
                            unpublish("graph", gid)
                            if _tid > 0:
                                self.store.log(
                                    f"小米侧已删 → 便笺侧同步删除，"
                                    f"已存入回收站 #{_tid}（可恢复，两端都会还原）",
                                    "warn")
                    continue

                # 两边都没了
                plan(ACTION_SKIP, f"两边都已删除，清理映射 {mi_id}")
                if not dry:
                    # 两边都被删了 —— 也存一份回收站，这样"删干净了"还能找回。
                    # **只在确实留有内容时才存**：映射残留（例如清过映射又重建）
                    # 也会走到这里，那时两侧都没有内容，存进去只会多一条空记录。
                    _pg = self._last_known("graph", gid)
                    _pm = self._last_known("xiaomi", mi_id)
                    if (_pg and _pg.get("text")) or (_pm and _pm.get("text")):
                        self._trash_before_delete(
                            "link:" + str(mi_id), _pg, _pm, mi_id)
                    self.store.delete_link(mi_id)
                    unpublish("graph", gid)
                    unpublish("xiaomi", mi_id)

            # ---------------------------------------------------- 未关联的笔记
            #
            # ★★ 顺序很关键：**先做「小米侧新增 → 便笺」，再做「便笺 → 小米」。**
            #
            # 两个循环共用同一个写入预算（`max_writes_per_round`），而预算是靠
            # **抛异常提前收尾**的。"便笺 → 小米"是历史存量（首轮要建 140+ 条），
            # 预算会在它里面用光、整轮直接结束，后面那个循环**一轮都轮不到**。
            # 后果：用户在手机上新建一条，得等整个存量推完才可能被收养 ——
            # 表现成"这条明明没配对，同步却一直不管它"。
            # （实测踩到：小米侧的「哈哈哈」建好很久都还是未配对。）
            #
            # 反过来先收小米侧的：新笔记下一轮开头立刻就被收过来；
            # 而"便笺 → 小米"本来就按**最新优先**排，新建的便笺也在队首，不会饿着。
            #
            # 另外：**每个循环开跑前都重新取一次映射表** ——
            # 前一个循环刚建好关联，用旧集合会把"刚建好的"又当成未关联，
            # 于是重复创建一份。

            # ---------------- ① 小米侧新增 → 便笺
            linked_m = {l.get("mi_id") for l in self.store.all_links()}
            for mi_id, m in m_notes.items():
                key_m = "m:" + str(mi_id)
                if key_m in done_keys:
                    continue
                tick(key_m)
                if mi_id in linked_m or mi_id in dropped["xiaomi"]:
                    continue
                # ★ 判定必须用**时间**，不能用"是否首次配对" ——
                #   要保护的是"配对开始之前就存在的小米笔记"；
                #   配对之后用户新写的（比如刚在手机上记的）必须收过来。
                #   用 first_run 判定会因为 paired 标志而漏掉这种情况：
                #   用户清空小米后重新配对，新写的那条也被当成"既有笔记"跳过了。
                if policy == "graph_authoritative":
                    _mt = int(m.get("modify_date") or 0)
                    if not _mt or _mt < pairing_since:
                        plan(ACTION_SKIP,
                             f"配对前既有的小米笔记 {mi_id[:8]} 保持不动"
                             f"（策略 graph_authoritative）")
                        preexisting_skipped += 1
                        continue
                plan(ACTION_LINK_NEW_GRAPH, f"新小米笔记 -> 新建便笺 {mi_id}")
                if not dry:
                    created = self.graph.create_note(
                        m["text"], when_iso=ms_to_iso(m.get("modify_date")))
                    self.store.upsert_link(mi_id, created["id"],
                                           content_hash(m["text"]),
                                           created.get("change_key", ""),
                                           int(m.get("modify_date") or 0))
                    if created.get("id"):
                        # 便笺侧**认**客户端传的 documentModifiedAt（已实测），
                        # 所以这里补列表项要用**小米原本的修改时间**，与服务端一致。
                        publish("graph", self._g_item(
                            created["id"], m["text"],
                            created.get("change_key", ""),
                            ms_to_iso(m.get("modify_date"))))

            if preexisting_skipped:
                self.store.log(
                    f"小米侧有 {preexisting_skipped} 条**配对前就存在**的笔记，"
                    f"按 graph_authoritative 策略保持不动（这是设计行为，不是失败）。"
                    f"想让它们也过来，把「首次配对策略」改成 adopt_both 后重跑",
                    "warn")

            # ---------------- ② 便笺 → 小米
            linked_g = {l.get("graph_id") for l in self.store.all_links()
                        if l.get("graph_id")}

            # ★ 写入方向：默认**从最新的一条开始**（用户要求"最新优先"）。
            #
            # 前提：小米**新建**接口不认客户端传的 modifyDate（会被覆盖成当前时间），
            # 所以"写入顺序"曾经是保证另一端排序的**唯一手段**，那要求
            # **按原始时间升序**（最旧先写）。
            # 现在有了"新建后立即回填"（`honor_original_time`），时间戳是
            # **精确写进去**的，顺序不再影响最终排序 —— 于是可以放心改成
            # 最新优先，用户先看到最近的内容被同步过去。
            # **回填关掉时必须退回升序**，否则另一端的排序会乱。
            _honor = False
            try:
                _honor = bool(self.xiaomi._honor_original_time())
            except Exception:
                _honor = False
            _pending = [(gid, g) for gid, g in g_notes.items()
                        if gid not in linked_g and gid not in dropped["graph"]]
            if _honor:
                # 最新优先。空时间戳用 "" 兜底 —— 倒序时它自然落到**最后**。
                _unlinked_g = sorted(
                    _pending,
                    key=lambda kv: (kv[1].get("modified_iso") or ""),
                    reverse=True)
                _order_txt = "最新优先"
            else:
                # 没开回填：必须升序（最旧先写），顺序才是另一端的排序依据。
                # 空时间戳用 "9999" 兜底，同样落到最后。
                _unlinked_g = sorted(
                    _pending,
                    key=lambda kv: (kv[1].get("modified_iso") or "9999"))
                _order_txt = ("按原始时间升序（未开启原始时间回填，"
                              "顺序是另一端的排序依据）")
            if _unlinked_g:
                self.store.log(
                    f"待新建的小米笔记 {len(_unlinked_g)} 条 —— {_order_txt}")

            for gid, g in _unlinked_g:
                key_g = "g:" + str(gid)
                if key_g in done_keys:
                    continue
                tick(key_g)
                if gid in linked_g or gid in dropped["graph"]:
                    continue
                if not xiaomi_ok:
                    errors.append(f"便笺 {gid[:8]} 未关联，但小米写入已关闭，跳过")
                    continue
                plan(ACTION_LINK_NEW_MI, f"新便笺 -> 新建小米笔记 {gid[:8]}")
                if not dry:
                    created = self.xiaomi.create_note(
                        g["text"], self._folder_id(),
                        when_ms=iso_to_ms(g.get("modified_iso")))
                    self.store.upsert_link(created["id"], gid,
                                           content_hash(g["text"]),
                                           g.get("change_key", ""))
                    if created.get("id"):
                        # 回填开启时服务端存的就是原始时间（新建后立刻改了一次），
                        # 所以本地也记原始时间，UI 才和服务端一致。
                        # 关掉回填时服务端用当前时间，_mi_patch_time 会返回 None。
                        publish("xiaomi", self._m_item(
                            created["id"], g["text"],
                            self._mi_patch_time(
                                iso_to_ms(g.get("modified_iso")))))

            if not dry:
                self.store.set_meta("paired", "1")

            # （列表局部更新已移到 finally —— 每条退出路径都要跑，见那里的说明。）

            summary = {
                "ok": True,
                "dry_run": dry,
                "duration_ms": int((time.time() - started) * 1000),
                "counts": self._counts(),
                "actions": actions,
                "errors": errors,
                "at": int(time.time()),
            }
        except RoundStopped as e:
            # **提前收尾：不算失败。** 不触发 fail_streak、不用红字报错 ——
            # 否则中止几次、或者配额冷却几轮，就把自动同步给"暂停"了，
            # 那不是用户的本意。
            summary = {
                "ok": False, "stopped": True, "reason": e.reason,
                "aborted": e.reason == "abort",      # 兼容前端的旧判断
                "dry_run": dry,
                "actions": actions, "errors": errors, "at": int(time.time()),
                "counts": self._counts(),
            }
            if e.detail:
                summary["detail"] = e.detail
            if e.reason == "abort":
                self.store.log(
                    f"同步已中止（处理到 {self.progress.get('done', 0)}/"
                    f"{self.progress.get('total', 0)}）—— "
                    f"已完成的动作不回滚，下一轮会接着收敛", "warn")
            elif e.reason == "budget":
                self.store.log(
                    f"本轮写入已达上限（{e.detail}）—— 这是防风控的限速，"
                    f"不是错误。已完成的动作不回滚，下一轮继续", "info")
            elif e.reason == "cooldown":
                self.store.log(f"小米写入冷却中，本轮提前收尾 —— {e.detail}", "warn")
        except QuotaExceeded as e:
            # **配额耗尽：立刻停轮，不算失败。**
            # 这一条必须挡在通用 except 之前 —— 否则会被当成普通异常，
            # 记成"同步出错"并推进 fail_streak，几轮就把自动同步暂停了，
            # 而实际上只是需要等一会儿。
            left = self._cooldown_left()
            summary = {
                "ok": False, "stopped": True, "reason": "quota",
                "quota": True, "wait_sec": left, "dry_run": dry,
                "actions": actions, "errors": errors, "at": int(time.time()),
                "counts": self._counts(),
                "detail": str(e)[:200],
                "hint": (f"小米写接口配额已耗尽，已停轮并进入 {left // 60} 分钟冷却。"
                         f"**这不是失败**，冷却结束会自动接着同步；"
                         f"也可以在设置里调大写入间隔或减小每轮写入上限。"),
            }
            self.store.log(
                f"小米写接口配额耗尽 —— 停轮并冷却 {left // 60} 分钟"
                f"（已处理 {self.progress.get('done', 0)}/"
                f"{self.progress.get('total', 0)}）", "warn")
        except Exception as e:  # 单轮失败不能把循环搞死
            summary = {
                "ok": False, "dry_run": dry, "error": f"{type(e).__name__}: {e}",
                "actions": actions, "errors": errors, "at": int(time.time()),
                "counts": self._counts(),
            }
        finally:
            self.busy = False
            # ★★★ 列表局部更新必须放在 finally ★★★
            #
            # 它原来放在 try 的末尾，于是**只有"整轮跑到底"才会执行**。
            # 而"本轮写入达上限""用户中止""配额冷却"这些提前收尾的路径
            # 都是靠抛异常跳出去的 —— 直接跳到 except，补丁一行都没跑。
            # 后果正是用户实测到的："自动同步跑着，界面上的灯一直不变，
            # 手动拉取一次才更新"。
            # 放进 finally 后，无论怎么退出都会把本轮改动并进列表缓存。
            if not dry:
                try:
                    for _side in ("graph", "xiaomi"):
                        for _id in dropped[_side]:
                            self.cache_drop(_side, _id)
                        for _it in touched[_side]:
                            self.cache_add(_side, _it)
                    if (touched["graph"] or touched["xiaomi"]
                            or dropped["graph"] or dropped["xiaomi"]):
                        self.last_notes_at = int(time.time())
                        self.store.set_meta("notes_cache_at",
                                            str(self.last_notes_at))
                except Exception as _e:
                    self.store.log(f"刷新界面列表缓存失败：{_e}", "warn")

        self.last_result = summary
        for e in errors:
            self.store.log(e, "warn")

        # **逐条记录本轮的实际动作。**
        # 原来只写一条汇总（"同步完成：N 个动作"），结果用户反馈"更新后日志没有输出"。
        # 两个原因叠加：
        #   1. `store.log()` 有 60 秒去重（同样的 level+msg 只写一条）
        #   2. 汇总消息内容固定，连续几轮的文本一模一样 → 全被去重吞掉
        # 逐条日志里带便笺 id 和动作详情，文本天然不同，能如实留痕。
        real_actions = [a for a in actions if a["action"] != ACTION_SKIP]
        if not dry:
            for a in real_actions:
                lv = "warn" if a["action"] == ACTION_CONFLICT else "info"
                self.store.log(f"{a['label']}：{a['detail']}", lv)
        elif real_actions:
            # 预演也要留痕，否则用户点了预演却看不到任何记录
            self.store.log(f"预演（未执行）："
                           f"{'；'.join(a['label'] + ' ' + a['detail'][:60] for a in real_actions[:5])}"
                           f"{'…' if len(real_actions) > 5 else ''}")
        if summary.get("ok"):
            self._mark_ok()
            # 整轮跑完就把断点记录作废 —— 下一轮是新的一轮，
            # 期间两端可能有新变化，必须重新逐条看，不能沿用旧集合。
            self._clear_done()
            n = len([a for a in actions if a["action"] != ACTION_SKIP])
            self.store.log(
                f"{'预演' if dry else '同步'}完成：{n} 个动作"
                f"{'，' + str(len(errors)) + ' 条告警' if errors else ''}",
                "warn" if errors else "info",
            )
        elif summary.get("stopped"):
            # **提前收尾 —— 全都不算失败**（用户中止 / 本轮写入达上限 / 配额冷却）。
            #
            # 这里原来只挡了 aborted，于是"本轮写入已达上限"会掉进下面的 else：
            #   1. 日志打出一条"同步失败："，**后面什么都没有**（stopped 的分支
            #      本来就不写 error 字段）—— 用户看到的就是这些空错误
            #   2. 更糟的是还会走 `_mark_fail()`，把 fail_streak 往上推。
            #      而"写入达上限"是**每轮都会发生**的正常限速，
            #      连跑 3 轮就够资格触发"连续失败自动暂停自动同步" ——
            #      等于自动同步会把自己跑死。
            # 保留断点记录，下一轮接着跑。
            pass
        else:
            err = str(summary.get("error") or "")
            self.store.log(f"同步失败：{err}", "error")
            self._mark_fail(err)
        return summary

    # ------------------------------------------------------------ 列表局部更新
    # 操作（删除 / 恢复 / 新建 / 同步）之后**不去全量重拉** ——
    # 微软侧拉一次要翻 16 页、约 20 秒，交互上完全不可接受。
    # 改成先在内存和缓存里改掉那几条，界面立刻反映变化；
    # 想核对时点"拉取列表"，或者等后台轮询自然刷新。

    def _last_known(self, side: str, note_id: str) -> dict[str, Any] | None:
        """这条记录的**最后已知内容**（来自上一轮快照）。

        为什么需要：一条记录被删掉之后，本轮的列表里就没有它了 ——
        但"云端删除也要能恢复"要求我们必须知道它原来是什么。
        所以轮首覆盖快照之前会先按 id 留一份。
        """
        if not note_id:
            return None
        return (self._prev.get(side) or {}).get(str(note_id))

    def _trash_before_delete(self, key: str, g: dict | None, m: dict | None,
                             mi_id: str = "") -> int:
        """删之前先把这条**存进回收站**，返回回收站条目 id（失败返回 -1）。

        两个来源的内容会合并：
          · 现在还活着的那一侧 —— 直接用当前内容（最准）；
          · 已经被删掉的那一侧 —— 用**最后已知内容**（`_last_known`）。
        这样"便笺删了 → 我们删小米"这种传播，回收站里**两端内容都有**，
        恢复时能把两边都建回来，而不是只捞回一半。

        为什么云端删除也必须走这一步：以前只有页面上的删除会进回收站，
        而用户在便笺 App / 小米 App 里删掉之后，同步把另一侧也删了 ——
        那一侧就**永久没了、无从恢复**。用户明确要求两边都能捞回来。
        """
        try:
            return self.store.trash_add({
                "key": key,
                "graph_id": (g or {}).get("id"),
                "graph_title": first_line((g or {}).get("text")),
                "graph_text": (g or {}).get("text"),
                "graph_time": (g or {}).get("modified_iso"),
                "xiaomi_id": (m or {}).get("id"),
                "xiaomi_title": first_line((m or {}).get("subject")
                                           or (m or {}).get("text")),
                "xiaomi_text": (m or {}).get("text"),
                "xiaomi_time": ms_to_iso((m or {}).get("modify_date")),
                "was_linked": True,
                "mi_id": mi_id or "",
            })
        except Exception as e:
            self.store.log(f"写入回收站失败（{key}）：{e}", "warn")
            return -1

    def _mi_patch_time(self, when_ms):
        """本地补**小米侧**列表项时该记什么时间。

        取决于「原始时间回填」开没开（`xiaomi.honor_original_time`）：
          · 开 —— 服务端存的就是我们回填的原始时间，本地要跟着记原始时间，
                  否则 UI 里小米列的时间和服务端不一致
          · 关 —— 服务端用的是它自己的当前时间，本地记 now
        """
        fn = getattr(self.xiaomi, "_honor_original_time", None)
        try:
            honor = bool(fn()) if callable(fn) else False
        except Exception:
            honor = False
        return when_ms if honor else None

    def _g_item(self, gid: str, text: str, change_key: str = "",
                when_iso: str = "") -> dict[str, Any]:
        """构造一条"便笺列表项"，形状与 graph.list_notes() 的返回**保持一致**。

        形状不一致会让 cache_add 的排序和字段读取出问题：
        例如原来 list_notes 不返回 modified_iso，cache_add 按它排序时就全是空值。
        """
        return {
            "id": str(gid or ""),
            "text": text or "",
            "change_key": change_key or "",
            "modified": 0,
            "modified_iso": when_iso or _now_iso(),
            "deleted": False,
        }

    def _m_item(self, mid: str, text: str,
                when_ms: int | None = None) -> dict[str, Any]:
        """构造一条"小米笔记列表项"，形状与 xiaomi.list_notes() 保持一致。"""
        try:
            ms = int(when_ms or time.time() * 1000)
        except (TypeError, ValueError):
            ms = int(time.time() * 1000)
        try:
            folder = self._folder_id()
        except Exception:
            folder = ""
        return {
            "id": str(mid or ""),
            "text": text or "",
            "subject": first_line(text or "") or "",
            "folder_id": folder,
            "modify_date": ms,
            "modified_iso": ms_to_iso(ms),
            "deleted": False,
        }

    def cache_drop(self, side: str, note_id: str) -> None:
        """从列表里移除一条（它已经被删掉了）。

        **这里会置 `snapshot_dirty`** —— 本地补丁一律经过 cache_add / cache_drop，
        所以把标记放在这一层，就不需要每个调用点各自记得打标（那样迟早漏一处）。
        """
        self.snapshot_dirty = True
        try:
            self.last_notes[side] = [
                n for n in (self.last_notes.get(side) or [])
                if str(n.get("id")) != str(note_id)
            ]
        except Exception:
            pass
        try:
            self.store.cache_del(side, note_id)
        except Exception:
            pass

    def cache_add(self, side: str, note: dict[str, Any]) -> None:
        """往列表里加一条（新建或恢复出来的）。

        **同样置 `snapshot_dirty`**：这是本地补丁，快照不再等于远端真实状态。
        """
        self.snapshot_dirty = True
        nid = str(note.get("id") or "")
        if not nid:
            return
        lst = [n for n in (self.last_notes.get(side) or [])
               if str(n.get("id")) != nid]
        lst.append(note)
        # 保持"越新越前"，和拉取后的顺序一致
        lst.sort(key=lambda n: (n.get("modified_iso") or ""), reverse=True)
        self.last_notes[side] = lst
        try:
            self.store.cache_upsert(side, note)
        except Exception:
            pass

    # ------------------------------------------------------------ 断点续传
    # 中止之后要能"从断点继续"，所以得记住**本轮已经处理过哪些条目**。
    #
    # 为什么不记"处理到第 N 条"（游标式）：
    # 那要求遍历顺序绝对稳定，而 links / g_notes / m_notes 都是 dict，
    # 顺序会随两端列表的增删变化 —— 一旦错位，断点就指到别的条目上去了，
    # 比不续传更危险。记 id 集合虽然多占几 KB，但**永远精确**：
    # 条目后来被删了也无所谓，它本来就不会再出现。

    def _load_done(self) -> set[str]:
        try:
            raw = self.store.get_meta("sync_done", "") or ""
            return set(json.loads(raw)) if raw else set()
        except Exception as e:
            # **不静默吞**：这里一失败，"断点续传"就悄悄失灵，
            # 而界面上完全看不出来。踩过的坑：engine 少 import json，
            # NameError 被 except 吃掉，断点永远存不进去，排查了很久。
            self.store.log(f"读取同步断点失败：{type(e).__name__}: {e}", "error")
            return set()

    def _save_done(self, keys: set[str]) -> None:
        try:
            self.store.set_meta("sync_done", json.dumps(sorted(keys)))
        except Exception as e:
            self.store.log(f"保存同步断点失败：{type(e).__name__}: {e}", "error")

    def _clear_done(self) -> None:
        try:
            self.store.set_meta("sync_done", "")
        except Exception as e:
            self.store.log(f"清除同步断点失败：{type(e).__name__}: {e}", "error")

    # ------------------------------------------------------------------ 健康档案
    def _mark_ok(self) -> None:
        """本轮成功：清零失败计数，盖一个"最近一次确认可用"的时间戳。

        `ok_since` **不每轮刷新** —— 它记录的是"从什么时候开始一直没坏过"。
        这样页面上那句"已连续可用 X 天"才是个有意义的数字，
        而不是永远显示 0。

        **写库是有节制的**：同步每 5 秒一轮，如果每轮都无条件写两三次 meta，
        磁盘就在做完全无谓的活。所以只在值真的变了的时候写。
        """
        now = int(time.time())
        if self.fail_streak:
            self.fail_streak = 0
            self.store.set_meta("fail_streak", "0")
        try:
            prev = int(self.store.get_meta("last_ok_at", "0") or 0)
        except (TypeError, ValueError):
            prev = 0
        # 这个时间戳只是给人看"凭据多久前验过一次"，60 秒精度足够
        if now - prev >= 60:
            self.store.set_meta("last_ok_at", str(now))
        if not (self.store.get_meta("ok_since", "") or "").strip():
            self.store.set_meta("ok_since", str(now))

    def _mark_fail(self, err: str = "") -> None:
        """本轮失败：累加计数，到阈值就自动关掉自动同步。

        关掉之后把控制权交回给人 —— 既不会刷屏，也不会拿着坏凭据空转。
        """
        self.fail_streak += 1
        now = int(time.time())
        self.store.set_meta("fail_streak", str(self.fail_streak))
        self.store.set_meta("last_fail_at", str(now))
        self.store.set_meta("last_fail_msg", (err or "")[:300])
        # 一旦失败，"连续可用"的计时就归零重来
        self.store.set_meta("ok_since", "")

        if self.fail_streak >= FAIL_STREAK_LIMIT and self.store.cfg.get("auto_sync"):
            interval = self.store.cfg.get("sync_interval_sec") or 5
            self.store.cfg["auto_sync"] = False
            self.store.save_config()
            self.store.log(
                f"已连续 {self.fail_streak} 轮同步失败，自动同步已自动暂停"
                f"（否则会每 {interval} 秒往日志里写一条同样的错误，"
                "把有用信息淹掉）。问题修好后，手动把开关重新打开即可。",
                "error")

    def health(self) -> dict[str, Any]:
        """凭据健康档案 —— 专门用来回答"登录到底能撑多久"。

        两个维度分开看，别混：
          * `fail_streak` / `last_ok_at` —— 现在好不好用
          * `ok_since` / `ok_days`     —— 已经连续好了多久（这才是"能撑多久"的实证）
        """
        now = int(time.time())

        def num(key: str) -> int:
            try:
                return int(self.store.get_meta(key, "0") or 0)
            except (TypeError, ValueError):
                return 0

        ok_since = num("ok_since")
        return {
            "fail_streak": self.fail_streak,
            "fail_limit": FAIL_STREAK_LIMIT,
            "last_ok_at": num("last_ok_at"),
            "last_fail_at": num("last_fail_at"),
            "last_fail_msg": self.store.get_meta("last_fail_msg", "") or "",
            "ok_since": ok_since,
            "ok_days": round((now - ok_since) / 86400, 2) if ok_since else 0.0,
            "now": now,
        }

    # ------------------------------------------------------------------ 辅助
    def _folder_id(self) -> str:
        try:
            return self.xiaomi.resolve_folder()
        except Exception:
            return ""

    def _relink(self, mi_id: str, graph_id: str, text: str,
                g: dict | None, m_notes: dict | None, mid: str,
                change_key: str | None = None, conflict: int = 0) -> None:
        """写入成功后更新基线，防止下一轮把自己写的内容当成"对方改了" """
        ck = change_key
        md = 0
        if g is not None:
            ck = g.get("change_key", "")
        row = self.store.link_by_side(mi_id=mi_id)
        if row:
            ck = ck or row.get("graph_changekey") or ""
        try:
            notes = self.xiaomi.list_notes()
            for n in notes:
                if n["id"] == mid:
                    md = int(n.get("modify_date") or 0)
                    break
        except Exception:
            pass
        self.store.upsert_link(mi_id, graph_id, content_hash(text), ck or "", md,
                               conflict=conflict)

    def _counts(self) -> dict[str, int]:
        links = self.store.all_links()
        return {
            "links": len(links),
            "linked": len([l for l in links if l.get("graph_id") and not l.get("tombstone")]),
            "conflicts": len([l for l in links if l.get("conflict")]),
        }

    def state(self) -> dict[str, Any]:
        try:
            graph_status = self.graph.status()
        except Exception as e:
            graph_status = {"mode": "?", "connected": False, "error": str(e)}
        try:
            xiaomi_status = self.xiaomi.status()
        except Exception as e:
            xiaomi_status = {"mode": "?", "connected": False, "error": str(e)}
        return {
            "graph": graph_status,
            "xiaomi": xiaomi_status,
            "counts": self._counts(),
            "busy": self.busy,
            # 同步进度，供前端显示"1/166"和决定要不要显示「中止」按钮
            "progress": dict(getattr(self, "progress", {}) or {}),
            "aborting": bool(getattr(self, "abort", False)),
            # 有断点没跑完（上次中止过）—— 前端可以提示"点同步会接着跑"
            "resume_pending": len(self._load_done()),
            "auto_sync": bool(self.store.cfg.get("auto_sync")),
            "readiness": self.readiness(),
            "health": self.health(),
            "last_result": self.last_result,
        }
