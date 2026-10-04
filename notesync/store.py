"""状态库：SQLite 保存映射关系、同步基线、凭据与日志。

设计要点
--------
link 表是双向同步的心脏。`base_hash` 记录"上次同步完成后双方一致的内容指纹"，
每一轮拿两侧的当前指纹和它比，就能判断出到底是谁改了、还是都改了。

  base_hash 是唯一能可靠区分"对方改的"和"自己上轮写进去的"的东西。
  用时间戳判断变化一定会出错（时钟不同步 + 自己写入也会改时间戳）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "port": 8787,
    # 自动同步默认关闭：条件不满足时不应该偷偷跑，必须由用户显式打开
    "auto_sync": False,
    "sync_interval_sec": 5,
    "dry_run": False,
    "conflict_policy": "keep_both",
    # 首次配对策略：
    #   graph_authoritative  只把便笺推到小米，小米侧已有的笔记保持不动（默认）
    #   adopt_both           两边都收（两边本来就是干净的才用，否则会重复一遍）
    "initial_pairing": "graph_authoritative",
    "graph": {
        # ★ 默认走 **real（OAuth）+ Outlook REST** —— 这是唯一同时满足
        #   「数据完整（含 2026 便笺）」和「refresh_token 自动续期」的通道。
        #   fabric 数据也全，但它的令牌拿不到 refresh_token，一过期就得
        #   人工从浏览器重抓 —— 在容器里根本没法长期跑。
        "mode": "real",              # real（推荐，走 Outlook REST）| fabric
        "client_id": "",             # Azure 应用注册的 Application (client) ID
        "tenant": "consumers",       # 个人微软账号必须 consumers；组织账号用 common
        # ★ 这个 scope 指向 Outlook REST —— 便笺真正的接口。
        #   别改成 Graph 的 ShortNotes：个人账号上那个端点根本不存在（400）。
        "scope": "https://outlook.office.com/notes.readwrite offline_access",
    },
    "xiaomi": {
        "mode": "real",              # mock 已停用；real 是唯一在线模式
        "base_url": "https://i.mi.com",
        "folder_name": "微软便笺",    # 目标文件夹（按名字找，找不到会告警）
        "folder_id": "",             # 也可以直接填 folderId 跳过按名查找
        "write_enabled": True,       # 写入接口已实测可用（2026-09 验证）
        "warn_interval_sec": 30,     # 低于这个间隔会告警（小米风控）

        # ---- 写入节流（防风控）----
        # 写都是串行的，但本地循环一秒能发十几个请求 ——
        # 实测就是这个节奏打爆了写配额（503 over user rate quota）。
        # 下面三项是**强制**的，写在 RealXiaomi._post_form 里，
        # 所有写入都绕不过去。
        "write_interval_sec": 1.0,      # 两次写之间的最小间隔（秒）。0 = 不限速
        "max_writes_per_round": 40,     # 每轮最多写几条。0 = 不限制
        # 命中配额后暂停写入多久（秒）。连续命中会翻倍退避（上限 30 分钟）
        "quota_cooldown_sec": 300,

        # ---- 原始时间回填 ----
        # 实测：小米**新建**接口会把 createDate/modifyDate 覆盖成当前时间，
        # 但**更新**接口认 modifyDate。所以"把原始时间写进去"要建完再改一次。
        #   true  = 每条 2 次写，小米侧时间与便笺一致，排序也一致（默认）
        #   false = 每条 1 次写（写入量减半），小米侧时间是同步时刻；
        #           **列表排序仍然正确**（因为我们按原始时间升序写入）
        # 配额吃紧时关掉这个最划算。
        "honor_original_time": True,
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS link (
    mi_id           TEXT PRIMARY KEY,
    graph_id        TEXT UNIQUE,
    base_hash       TEXT,
    graph_changekey TEXT,
    mi_modifydate   INTEGER,
    last_synced_at  INTEGER,
    tombstone       INTEGER DEFAULT 0,
    conflict        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cred (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
    ts    INTEGER NOT NULL,
    level TEXT NOT NULL,
    msg   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
-- 两端列表的**持久化缓存**。
-- 为什么需要：列表原来只存在 engine 的内存里（last_notes），
-- 服务一重启或重建连接器就没了 —— 用户看到列表被"重置"，得重新拉一次；
-- 而拉一次微软侧要翻 16 页、约 20 秒。落到库里之后：启动即可显示上次的列表，
-- 后台再拉新的来对比。
-- 操作（删除/恢复/新建/同步）时也先在缓存里做**局部更新**，
-- 界面立刻反映变化，不用等全量重拉。
CREATE TABLE IF NOT EXISTS notes_cache (
    side    TEXT NOT NULL,          -- graph | xiaomi
    id      TEXT NOT NULL,
    payload TEXT NOT NULL,          -- 整条 note 的 JSON
    at      INTEGER,                -- 写入时间
    PRIMARY KEY (side, id)
);

-- 回收站。删除时**不真删，只搬进来**；恢复时按删除前两端的实际状态还原。
-- 之所以把两端的字段分列存（graph_* / xiaomi_*），而不是只存一份内容：
-- 删除前可能"只有微软有"或"只有小米有"或"两端都有且内容不同"，
-- 恢复必须还原成**当时的样子**，不能一律按一份内容重建两边 ——
-- 那会凭空多出用户当时并没有的条目。
CREATE TABLE IF NOT EXISTS trash (
    tid          INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT,                  -- 删除时的合并 key，便于溯源
    deleted_at   INTEGER NOT NULL,
    graph_id     TEXT,                  -- 为 NULL 表示删除时这一侧没有
    graph_title  TEXT,
    graph_text   TEXT,
    graph_time   TEXT,
    xiaomi_id    TEXT,
    xiaomi_title TEXT,
    xiaomi_text  TEXT,
    xiaomi_time  TEXT,
    was_linked   INTEGER DEFAULT 0,     -- 删除时是否已配对（决定恢复后要不要重建映射）
    mi_id        TEXT,                  -- 原映射的小米 id
    restored_tid INTEGER DEFAULT 0      -- 指向恢复后新建的那条（0=尚未恢复）
);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.data_dir / "config.json"
        self._lock = threading.RLock()
        # check_same_thread=False：HTTP 线程和同步线程都要访问
        self.db = sqlite3.connect(self.data_dir / "state.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.cfg = self._load_config()
        # ★ 首次启动就把默认配置落盘。
        #   不写的话 config.json 压根不存在 —— 用户"找不到配置文件"，
        #   发布前的冒烟测试也读不到默认通道（实测踩到）。
        #   落盘还有个好处：用户能直接看到有哪些可改项，不用去翻代码。
        if not self.cfg_path.exists():
            self.save_config()

    # ---------------------------------------------------------------- 配置
    def _load_config(self) -> dict[str, Any]:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值
        if self.cfg_path.exists():
            try:
                user = json.loads(self.cfg_path.read_text(encoding="utf-8"))
                for k, v in user.items():
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
            except (json.JSONDecodeError, OSError):
                pass
        return cfg

    def save_config(self) -> None:
        with self._lock:
            self.cfg_path.write_text(
                json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def patch_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """局部更新配置。只接受已存在的键，防止前端塞进任意字段"""
        with self._lock:
            for k, v in patch.items():
                if k not in self.cfg:
                    continue
                if isinstance(v, dict) and isinstance(self.cfg[k], dict):
                    for kk, vv in v.items():
                        if kk in self.cfg[k]:
                            self.cfg[k][kk] = vv
                else:
                    self.cfg[k] = v
            self.save_config()
            return self.cfg

    # ---------------------------------------------------------------- 凭据
    def set_cred(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO cred(k, v) VALUES(?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self.db.commit()

    def get_cred(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT v FROM cred WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except json.JSONDecodeError:
            return default

    def del_cred(self, key: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM cred WHERE k=?", (key,))
            self.db.commit()

    # ---------------------------------------------------------------- 日志
    def log(self, msg: str, level: str = "info") -> None:
        """写日志。**自动去重**：同样的内容在 60 秒内只写一条。

        守卫意义：凭据过期时同步循环会每几秒失败一次，如果不去重，
        日志会被同一条错误刷屏，用户真正需要看的信息全被埋掉（这个现象出现过）。
        """
        with self._lock:
            now = int(time.time())
            row = self.db.execute(
                "SELECT ts FROM log WHERE level=? AND msg=? ORDER BY ts DESC LIMIT 1",
                (level, msg),
            ).fetchone()
            if row and now - int(row["ts"]) < 60:
                return
            self.db.execute(
                "INSERT INTO log(ts, level, msg) VALUES(?,?,?)", (now, level, msg)
            )
            # 只留最近 500 条，别让日志把库撑大
            self.db.execute(
                "DELETE FROM log WHERE ts < (SELECT ts FROM log ORDER BY ts DESC LIMIT 1 OFFSET 500)"
            )
            self.db.commit()

    def recent_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT ts, level, msg FROM log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"ts": r["ts"], "level": r["level"], "msg": r["msg"]} for r in rows]

    # ---------------------------------------------------------------- 映射表
    def all_links(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM link").fetchall()
        return [dict(r) for r in rows]

    def link_by_side(self, mi_id: str = "", graph_id: str = "") -> dict[str, Any] | None:
        if mi_id:
            row = self.db.execute("SELECT * FROM link WHERE mi_id=?", (mi_id,)).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM link WHERE graph_id=?", (graph_id,)
            ).fetchone()
        return dict(row) if row else None

    def upsert_link(
        self,
        mi_id: str,
        graph_id: str,
        base_hash: str,
        graph_changekey: str = "",
        mi_modifydate: int = 0,
        tombstone: int = 0,
        conflict: int = 0,
    ) -> None:
        with self._lock:
            self.db.execute(
                """
                INSERT INTO link(mi_id, graph_id, base_hash, graph_changekey,
                                 mi_modifydate, last_synced_at, tombstone, conflict)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(mi_id) DO UPDATE SET
                    graph_id=excluded.graph_id,
                    base_hash=excluded.base_hash,
                    graph_changekey=excluded.graph_changekey,
                    mi_modifydate=excluded.mi_modifydate,
                    last_synced_at=excluded.last_synced_at,
                    tombstone=excluded.tombstone,
                    conflict=excluded.conflict
                """,
                (mi_id, graph_id or None, base_hash, graph_changekey,
                 mi_modifydate, int(time.time()), tombstone, conflict),
            )
            self.db.commit()

    def delete_link(self, mi_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM link WHERE mi_id=?", (mi_id,))
            self.db.commit()

    def clear_links(self) -> int:
        """清空全部映射，返回删掉的条数。

        用途：切换数据源（尤其是涉及 mock）时，旧映射必然失效 ——
        mock 的便笺 id 是 `AAMkMOCK0001` 这种，真实的是
        `AAkALgAAAAAAHYQ…`（MAPI EntryID），本地库又是 RemoteId，
        **四套 id 体系互不相干**。留着它们只会让界面显示一堆
        "已映射"，而另一边其实根本没有对应条目。
        """
        with self._lock:
            n = self.db.execute("DELETE FROM link").rowcount
            self.db.commit()
            return n

    # ---------------------------------------------------------------- 列表缓存
    def cache_save(self, side: str, notes: list[dict[str, Any]]) -> None:
        """整侧列表覆盖写入缓存。

        拉取成功后调一次。用「先删后插」而不是逐条 upsert ——
        这样**服务端已经删掉的条目会自动从缓存里消失**，
        不会留一堆再也不会更新的僵尸行。
        """
        with self._lock:
            now = int(time.time())
            self.db.execute("DELETE FROM notes_cache WHERE side=?", (side,))
            self.db.executemany(
                "INSERT INTO notes_cache(side, id, payload, at) VALUES(?,?,?,?)",
                [(side, str(n.get("id") or ""),
                  json.dumps(n, ensure_ascii=False), now) for n in notes],
            )
            self.db.commit()

    def cache_load(self) -> dict[str, list[dict[str, Any]]]:
        """读回缓存的列表（按写入时的顺序没法保证，所以靠 modified_iso 排序）。"""
        out: dict[str, list[dict[str, Any]]] = {"graph": [], "xiaomi": []}
        rows = self.db.execute(
            "SELECT side, payload FROM notes_cache").fetchall()
        for r in rows:
            try:
                n = json.loads(r["payload"])
            except json.JSONDecodeError:
                continue
            out.setdefault(r["side"], []).append(n)
        for side in out:
            # 两侧都是"越新越前"。缓存写的是原始顺序，这里重新排一次保险。
            out[side].sort(key=lambda n: (n.get("modified_iso") or ""),
                           reverse=True)
        return out

    def cache_upsert(self, side: str, note: dict[str, Any]) -> None:
        """局部更新单条 —— 新建/恢复/同步后立刻反映到缓存，不用全量重拉。"""
        with self._lock:
            self.db.execute(
                "INSERT INTO notes_cache(side, id, payload, at) VALUES(?,?,?,?) "
                "ON CONFLICT(side, id) DO UPDATE SET payload=excluded.payload, "
                "at=excluded.at",
                (side, str(note.get("id") or ""),
                 json.dumps(note, ensure_ascii=False), int(time.time())),
            )
            self.db.commit()

    def cache_del(self, side: str, note_id: str) -> None:
        """局部删除单条 —— 删掉之后界面该立刻少一行。"""
        with self._lock:
            self.db.execute("DELETE FROM notes_cache WHERE side=? AND id=?",
                            (side, str(note_id)))
            self.db.commit()

    def cache_clear(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM notes_cache")
            self.db.commit()

    # ---------------------------------------------------------------- 回收站
    def trash_add(self, item: dict[str, Any]) -> int:
        """把一条记录搬进回收站，返回回收站条目 id。

        存的是**删除前两端的实际状态**（各自有无、各自的正文），
        而不是"一条内容"。恢复时要靠它还原成当时的样子。
        """
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO trash(key, deleted_at, graph_id, graph_title,"
                " graph_text, graph_time, xiaomi_id, xiaomi_title, xiaomi_text,"
                " xiaomi_time, was_linked, mi_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.get("key") or "", int(time.time()),
                 item.get("graph_id"), item.get("graph_title"),
                 item.get("graph_text"), item.get("graph_time"),
                 item.get("xiaomi_id"), item.get("xiaomi_title"),
                 item.get("xiaomi_text"), item.get("xiaomi_time"),
                 1 if item.get("was_linked") else 0, item.get("mi_id") or ""),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def trash_list(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM trash ORDER BY deleted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def trash_get(self, tid: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM trash WHERE tid=?", (tid,)).fetchone()
        return dict(r) if r else None

    def trash_mark_restored(self, tid: int, new_tid: int = -1) -> None:
        """标记为已恢复。`new_tid` 存恢复后新建的便笺 id 的哈希不现实，
        这里只记 -1 表示"已处理"，避免重复恢复出多份。"""
        with self._lock:
            self.db.execute("UPDATE trash SET restored_tid=? WHERE tid=?",
                            (new_tid, tid))
            self.db.commit()

    def trash_del(self, tid: int) -> None:
        """从回收站彻底移除（不可恢复）。"""
        with self._lock:
            self.db.execute("DELETE FROM trash WHERE tid=?", (tid,))
            self.db.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO meta(k, v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )
            self.db.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row and row["v"] is not None else default


def masked(cfg: dict[str, Any]) -> dict[str, Any]:
    """给前端看的配置副本：绝不带凭据"""
    out = json.loads(json.dumps(cfg))
    return out
