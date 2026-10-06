#!/usr/bin/env python3
"""本地同步服务：HTTP API + 前端托管 + 后台自动同步

纯标准库，零依赖（真实连接器按需装第三方库）。
默认 5 秒一轮同步，间隔可在前端改。

启动：
    python server.py                 # 默认端口 8787
    python server.py --port 9000
然后浏览器打开 http://127.0.0.1:8787
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from notesync import mi_pwd, mi_qr
from notesync.auth import Auth, COOKIE, SESSION_TTL, check_strength
from notesync.engine import SyncEngine, iso_to_ms
from notesync.graph import LocalGraph, MockGraph, NotesFabricGraph, RealGraph
from notesync.store import Store
from notesync.textutil import content_hash, strip_block_markers
from notesync.xiaomi import MockXiaomi, RealXiaomi

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

RUNTIME: dict = {}

#: 不需要登录就能访问的 API —— **只有这几个**，其余一律要求已登录。
#: 页面本身（/）也必须放行，否则登录页都打不开。
AUTH_OPEN = {
    "/api/auth/status",   # 前端用它判断：要不要显示"首次设置"还是"登录"
    "/api/auth/login",    # 登录本身
    "/api/auth/setup",    # 首次设置密码（仅在还没设过密码时有效）
}


# --------------------------------------------------------------------- 组件装配

def build(data_dir: Path):
    store = Store(data_dir)
    RUNTIME["store"] = store
    # 访问密码 / 会话。放在 build 里，CLI 子命令也能共用同一份实现。
    RUNTIME["auth"] = Auth(store,
                           log=lambda m, lv="info": store.log(m, lv))
    engine = rebuild()
    return store, RUNTIME["graph"], RUNTIME["xiaomi"], engine


def rebuild():
    """按当前配置重建连接器与引擎。

    换模式、改 client_id 之后必须重建 —— 连接器是构造时固定的。

    **微软侧只保留在线模式**（2026-09-16 收敛）：
      fabric  读微软云端便笺服务 NotesFabric（数据全 + 可读写，**推荐**）
      real    走 Microsoft Graph（只能读到 Notes 邮件文件夹那部分）

    ⚠ 两个模拟/本地模式**已停用**（代码保留在下方注释里，便于回滚）：
      mock    内存假便笺
      local   读本机 plum.sqlite
      停用理由：它们都是"不碰真实数据"的数据源，留在生产界面里
      只会让人分不清眼前的列表是真数据还是假数据。
    """
    store: Store = RUNTIME["store"]
    gmode = store.cfg["graph"]["mode"]
    xmode = store.cfg["xiaomi"]["mode"]

    # ------------------ 已停用的测试模式（保留，去掉注释即可恢复）------------------
    # if gmode == "mock":
    #     graph = MockGraph()
    # elif gmode == "local":
    #     graph = LocalGraph(store)
    # ---------------------------------------------------------------------------

    if gmode == "fabric":
        graph = NotesFabricGraph(store)
    elif gmode == "real":
        # 用户显式选的 real —— 尊重。但要清楚它**读不到 2026 年的便笺**，
        # 只是有人可能因为设备码登录方便而选它。
        graph = RealGraph(store)
    else:
        # mock / local 这类已停用的值（含老版本配置、旧镜像的 data/）：
        # ★ 一律落到 **fabric**，不是 real。
        #   旧版这里落到 real，结果**全新部署拿到的是「缺 2026 便笺」的残缺数据**，
        #   而且界面只显示「Graph · 缺 2026 便笺」一行小字，很难联想到是默认值的问题
        #   —— 发布后实测踩到的就是这个。fabric 才是数据最全的那个通道。
        graph = NotesFabricGraph(store)
        store.cfg["graph"]["mode"] = "fabric"
        store.save_config()
        store.log(f"微软模式 {gmode!r} 已停用，自动切到 fabric（数据最全）", "warn")

    # 小米侧同理：mock 已停用
    # xiaomi = MockXiaomi(store) if xmode == "mock" else RealXiaomi(store)
    xiaomi = RealXiaomi(store)
    if xmode == "mock":
        store.cfg["xiaomi"]["mode"] = "real"
        store.save_config()
        store.log("小米模式 'mock' 已停用，自动切到 real", "warn")

    RUNTIME["graph"] = graph
    RUNTIME["xiaomi"] = xiaomi
    RUNTIME["engine"] = SyncEngine(store, graph, xiaomi)
    return RUNTIME["engine"]


# --------------------------------------------------------------------- 单条操作
#
# 「删除一条」和「把一条对齐」都抽成**模块级函数** —— 单条端点和批量端点
# 各写一份实现的话，两边迟早漂移（回收站要存哪些字段、局部缓存怎么摘、
# link 表什么时候清，这些细节最容易漏一处，而漏了就会留下孤儿映射）。


def delete_one(store, engine, key: str) -> dict[str, Any]:
    """把一条记录**移入回收站**（两端同删：哪端有就删哪端）。

    为什么是两端同删：这个程序的目标就是两端同步，删一边留一边反而制造
    "单边孤儿"。为什么删之前先完整入库：恢复要还原成**删除当时的样子**
    （当时只有一端有 / 两端都有且内容不同），所以两端正文各存一份。
    先写库再执行删除，中途失败也不会丢内容。

    返回 {ok, key, label, trash_id, errors}；失败时带 error。
    """
    notes = getattr(engine, "last_notes", {}) or {}
    g_map = {n["id"]: n for n in (notes.get("graph") or [])}
    m_map = {n["id"]: n for n in (notes.get("xiaomi") or [])}
    kind, _, ident = key.partition(":")
    g = m = None
    mi_id = ""
    was_linked = False
    if kind == "link":
        lk = next((l for l in store.all_links()
                   if str(l.get("mi_id")) == ident), None)
        if lk:
            was_linked = True
            mi_id = str(lk.get("mi_id") or "")
            g = g_map.get(str(lk.get("graph_id") or ""))
            m = m_map.get(mi_id)
    elif kind == "g":
        g = g_map.get(ident)
    elif kind == "m":
        m = m_map.get(ident)
    if not g and not m:
        return {"ok": False, "key": key, "label": "",
                "error": "找不到这条记录 —— 列表可能已过期，刷新一次再试"}

    tid = store.trash_add({
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
        "was_linked": was_linked,
        "mi_id": mi_id,
    })

    errs: list[str] = []
    if g:
        try:
            RUNTIME["graph"].delete_note(str(g["id"]))
        except Exception as e:
            errs.append(f"便笺：{e}")
    if m:
        try:
            RUNTIME["xiaomi"].delete_note(str(m["id"]))
        except Exception as e:
            errs.append(f"小米：{e}")
    if mi_id:
        store.delete_link(mi_id)

    which = ("便笺+小米" if (g and m) else ("便笺" if g else "小米"))
    label = first_line((g or m or {}).get("text"))[:30] or "(空)"
    store.log(f"已移入回收站 #{tid}（{which}）：{label}", "warn")
    if errs:
        store.log("删除过程中有失败：" + "；".join(errs), "error")
    # **局部更新列表，不全量重拉** —— 微软侧拉一次要 20 秒，
    # 而这里我们明确知道删掉了哪几条，直接摘掉即可。
    if g:
        engine.cache_drop("graph", str(g["id"]))
    if m:
        engine.cache_drop("xiaomi", str(m["id"]))
    return {"ok": True, "key": key, "label": label,
            "trash_id": tid, "errors": errs}


def _align_one(store, engine, key: str, gid: str, mid: str,
               g: dict | None, m: dict | None) -> list[str]:
    """把**一条**记录对齐成"两侧一致"。返回做过的动作描述列表。

    规则（按行 key 解析出来的三种形态）：
      · 只有便笺 → 在小米侧**补建**一条（带上便笺原本的修改时间）
      · 只有小米 → 在便笺侧**补建**一条（带上小米原本的修改时间）
      · 两侧都有但内容不同 → **以"最后修改的那一侧"为准**覆盖另一侧
        （和前端指示灯"绿的那侧"是同一个判据，所见即所得）
      · 两侧都有且一致 → 不动
    """
    graph = RUNTIME["graph"]
    xiaomi = RUNTIME["xiaomi"]

    # ---- 只有便笺：补建到小米 ----
    if g and not m:
        created = xiaomi.create_note(g["text"], engine._folder_id(),
                                     when_ms=iso_to_ms(g.get("modified_iso")))
        if mid:                      # 旧映射先摘掉，避免留下孤儿
            store.delete_link(mid)
        store.upsert_link(str(created["id"]), gid, content_hash(g["text"]),
                          g.get("change_key", ""),
                          iso_to_ms(g.get("modified_iso")) or 0)
        engine.cache_add("xiaomi", engine._m_item(
            str(created["id"]), g["text"],
            engine._mi_patch_time(iso_to_ms(g.get("modified_iso")))))
        # **不要**把便笺侧那条从列表里摘掉。
        # 原来这里写了 cache_drop("graph", gid) —— 那是错的：
        # 链接表刚 upsert 成 (新 mi_id ↔ gid)，/api/notes 正是靠
        # "graph 列表里有 gid" 才能把两侧合并成一行。摘掉之后合并失败，
        # 界面上就只剩小米一侧、灯变成一红一绿（用户实测到的
        # "同步选中之后灯没更新/不对"）。让它留着，链接表会让它并进 link 行。
        return [f"便笺 → 小米（补建 {str(created['id'])[:10]}）"]

    # ---- 只有小米：补建到便笺 ----
    if m and not g:
        created = graph.create_note(m["text"],
                                    when_iso=ms_to_iso(m.get("modify_date")))
        if mid:
            store.delete_link(mid)
        store.upsert_link(mid, str(created["id"]), content_hash(m["text"]),
                          created.get("change_key", ""),
                          int(m.get("modify_date") or 0))
        engine.cache_add("graph", engine._g_item(
            str(created["id"]), m["text"], created.get("change_key", ""),
            ms_to_iso(m.get("modify_date"))))
        # 同理：**不要**摘掉小米侧那条 —— 合并成一行要靠它
        return [f"小米 → 便笺（补建 {str(created['id'])[:10]}）"]

    # ---- 两侧都在 ----
    g_text = g.get("text") or ""
    m_text = m.get("text") or ""
    if content_hash(g_text) == content_hash(m_text):
        store.upsert_link(mid, gid, content_hash(g_text),
                          g.get("change_key", ""), int(m.get("modify_date") or 0))
        return ["两侧已一致，跳过"]

    g_time = g.get("modified_iso") or ""
    m_time = ms_to_iso(m.get("modify_date")) or ""
    if g_time >= m_time:
        # 便笺较新 —— 用它覆盖小米
        xiaomi.update_note(mid, g_text,
                           when_ms=iso_to_ms(g.get("modified_iso")))
        engine.cache_add("xiaomi", engine._m_item(
            mid, g_text, engine._mi_patch_time(iso_to_ms(g.get("modified_iso")))))
        store.upsert_link(mid, gid, content_hash(g_text),
                          g.get("change_key", ""),
                          iso_to_ms(g.get("modified_iso")) or 0)
        return [f"便笺较新（{g_time[:19]} vs {m_time[:19]}）→ 覆盖小米"]
    # 小米较新 —— 用它覆盖便笺
    res = graph.update_note(gid, m_text, g.get("change_key", ""),
                            when_iso=ms_to_iso(m.get("modify_date")))
    engine.cache_add("graph", engine._g_item(
        gid, m_text, res.get("change_key", ""), ms_to_iso(m.get("modify_date"))))
    store.upsert_link(mid, gid, content_hash(m_text),
                      res.get("change_key", "") or g.get("change_key", ""),
                      int(m.get("modify_date") or 0))
    return [f"小米较新（{m_time[:19]} vs {g_time[:19]}）→ 覆盖便笺"]


def sync_selected(store, engine, keys: list[str]) -> dict[str, Any]:
    """把选中的记录**对齐到两侧一致**（逐条独立，一条失败不影响其它）。

    与「立即同步」的区别：这里是**用户明确点名**的几条，
    所以不做"冲突保留双方副本"那一套 —— 直接以较新的一侧为准求一致，
    否则会凭空多出一条 [冲突副本]，反而"不一致"了。
    """
    results: list[dict[str, Any]] = []
    done = 0
    for key in keys:
        kind, _, ident = key.partition(":")
        gid = mid = ""
        if kind == "link":
            lk = next((l for l in store.all_links()
                       if str(l.get("mi_id")) == ident), None)
            if not lk:
                results.append({"key": key, "ok": False, "error": "这条的映射已不存在"})
                continue
            gid = str(lk.get("graph_id") or "")
            mid = str(lk.get("mi_id") or "")
        elif kind == "g":
            gid = ident
        elif kind == "m":
            mid = ident
        else:
            results.append({"key": key, "ok": False, "error": f"无法识别的 key：{key}"})
            continue
        try:
            # 取**当前**内容（单条读取，很快），不信界面里那份可能过期的快照
            g = engine._fetch_one("graph", gid) if gid else None
            m = engine._fetch_one("xiaomi", mid) if mid else None
            if not g and not m:
                results.append({"key": key, "ok": False,
                                "error": "两侧都找不到（可能已被删除）"})
                continue
            acts = _align_one(store, engine, key, gid, mid, g, m)
            results.append({"key": key, "ok": True, "actions": acts})
            done += 1
        except Exception as e:
            results.append({"key": key, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
    ok_n = sum(1 for r in results if r["ok"])
    store.log(f"同步选中：共 {len(keys)} 条，成功 {ok_n} 条"
              f"（以较新的一侧为准求一致）", "info" if ok_n else "warn")
    return {"ok": True, "total": len(keys), "succeeded": ok_n, "results": results}


def build_backup(store, engine) -> dict[str, Any]:
    """导出一份**全量备份**：两侧全部记录（含正文与时间字段）+ 同步状态 + 配置。

    设计取舍（都是刻意的）：

    · **含正文全文**。备份的意义是"万一没了还能看出原来是什么"，
      只存 id 和时间戳没有意义。
    · **绝不含任何凭据**。cookie / token / passToken 一律不进备份文件 ——
      它会被下载到本地、可能被随手转发或丢在下载目录里，带凭据等于泄露登录态。
      凭据丢了重新登录即可，不需要靠备份恢复。
    · **时间字段两侧都原样导出**：便笺是 ISO 字符串、小米是毫秒时间戳，
      都保留原始精度与原始形态（不做归一化），将来做恢复时不用猜。
    · **现场全量重拉**，不拿界面那份快照凑数 —— 受限模式或刚启动时，
      界面快照可能只有一页，那样的备份是残缺的。代价是慢约 20 秒，
      但备份本来就不是高频操作。
    """
    graph = RUNTIME["graph"]
    xiaomi = RUNTIME["xiaomi"]
    g_list = [n for n in graph.list_notes() if not n.get("deleted")]
    m_list = [n for n in xiaomi.list_notes() if not n.get("deleted")]

    def g_row(n: dict) -> dict:
        return {
            "id": n.get("id"),
            "text": n.get("text") or "",
            # 原始修改时间（ISO，UTC，.NET 7 位小数那种形态）
            "modified_iso": n.get("modified_iso") or "",
            "change_key": n.get("change_key") or "",
            "chars": len(n.get("text") or ""),
        }

    def m_row(n: dict) -> dict:
        ms = int(n.get("modify_date") or 0)
        return {
            "id": n.get("id"),
            "text": n.get("text") or "",
            "subject": n.get("subject") or "",
            "folder_id": n.get("folder_id") or "",
            # 小米原样给毫秒，同时附一个 ISO 方便人看
            "modify_date": ms,
            "modified_iso": ms_to_iso(ms),
            "chars": len(n.get("text") or ""),
        }

    links = []
    for l in store.all_links():
        links.append({
            "mi_id": l.get("mi_id"),
            "graph_id": l.get("graph_id"),
            # 基线哈希 = "上次同步后双方一致的内容指纹"
            "base_hash": l.get("base_hash"),
            "graph_changekey": l.get("graph_changekey"),
            "mi_modifydate": l.get("mi_modifydate"),
            "last_synced_at": l.get("last_synced_at"),
            "tombstone": l.get("tombstone"),
            "conflict": l.get("conflict"),
        })

    trash = []
    try:
        for it in store.trash_list(limit=1000):
            trash.append(dict(it))
    except Exception:
        pass

    meta = {}
    try:
        for r in store.db.execute("select k, v from meta"):
            meta[r[0]] = r[1]
    except Exception:
        pass
    # 这两项**不进备份**（都是"看起来像凭据"的诊断信息，不是密钥，但没必要带）：
    #   · `cred_life_json` —— 凭据体检快照，逐条列了 cookie 的**名字**和有效期。
    #   · `last_fail_msg`  —— 上一轮失败的错误原文，会**字面出现**
    #     "serviceToken" 这类词（如"凭据里没有 serviceToken，无法写入"）。
    # 备份要能放心下载、随手转发、丢在下载目录里也不心慌，
    # 所以宁可让"不含凭据"这句话毫无歧义。
    # 诊断价值由 `fail_streak` / `last_fail_at` 保留（恢复时真正要看的就是这两个）。
    meta.pop("cred_life_json", None)
    meta.pop("last_fail_msg", None)

    # 配置照抄（config.json 里没有密钥：client_id 不是密钥，凭据在 cred 表里，
    # 而那张表**不导出**）
    cfg = json.loads(json.dumps(store.cfg, ensure_ascii=False))

    return {
        "format": "sticky-mi-sync-backup",
        "version": 1,
        "exported_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "exported_at_unix": int(time.time()),
        "note": "不含任何凭据（cookie/token 不进备份，丢了重新登录即可）；"
                "时间字段保留原始形态（便笺 ISO / 小米毫秒各按原样）",
        "_counts": {"graph": len(g_list), "xiaomi": len(m_list),
                    "links": len(links), "trash": len(trash)},
        "graph": {
            "channel": getattr(graph, "channel", "") or store.cfg["graph"]["mode"],
            "count": len(g_list),
            "notes": [g_row(n) for n in g_list],
        },
        "xiaomi": {
            "folder_id": store.cfg["xiaomi"].get("folder_id") or "",
            "folder_name": store.cfg["xiaomi"].get("folder_name") or "",
            "count": len(m_list),
            "notes": [m_row(n) for n in m_list],
        },
        # 同步状态：这一份才是"配对关系与基线"，恢复时最关键的东西
        "links": links,
        "trash": trash,
        "meta": meta,
        "config": cfg,
    }


def _finish_xiaomi_login(store, r: dict) -> dict:
    """账号密码登录成功后的收尾：把 passToken 换成 i.mi.com 的服务 cookie 并落库。

    直接复用 `mi_qr._finish` —— 扫码和账号密码两条路最终落在同一个状态上，
    各写一份迟早会漂移（回收站字段、局部缓存这些最容易漏）。
    """
    pass_token = r.get("pass_token") or {}
    if not pass_token:
        return {"ok": False, "error": "登录返回里没有 passToken"}
    try:
        merged = mi_qr._finish(store, pass_token)      # noqa: SLF001
    except Exception as e:
        return {"ok": False, "error": f"登录成功，但换取服务凭据失败：{e}"}
    try:
        rebuild()      # 让 xiaomi 实例立刻用上新凭据，用户不用重启容器
    except Exception:
        pass
    return {"ok": True, "account": str(merged.get("userId") or ""),
            "message": "登录成功，凭据已保存"}


def pair_by_content(store, engine) -> dict[str, Any]:
    """把两侧**内容相同**的未配对笔记关联起来（一次性整理）。

    为什么需要它
    ------------
    引擎的首次配对策略只有两种：「把便笺推过去」和「两边都收」——
    **都会在小米侧重复一遍**。它没有"两侧已经有一模一样的内容，
    那就把它们认成同一条"这个动作。

    而现实中很常见：用户两边各自已经有数据（以前手动同步过、
    或者在两边分别写过同样的东西），登录成功后列表里每条都出现两次、
    全都标着「未配对」。

    做法：两侧按**配对指纹**（`pair_hash` —— 在 content_hash 基础上
    额外忽略中文字符之间的空格）分组，同指纹的按顺序一一配对。

    为什么不是 content_hash：实测剩下的差异就是"小米侧多了词间空格"
    （`全屋智能旅游规划` ↔ `全屋智能 旅游规划`，相似度 0.98+），
    中文里那通常只是输入习惯，但严格哈希会判成两条不同内容 → 永远配不上。
    （英文里的空格仍有语义，pair_hash 只对非 ASCII 之间放宽。）

    - 完全不动已经配好的（link 表里已有的跳过）
    - 同指纹但**数量不等**的（比如便笺有 3 条一样的、小米只有 1 条），
      只配 min(个数) 条，多出来的留着不动 —— 硬凑会张冠李戴
    - 内容只在一边的，保持未配对（走正常的新建/推送流程）

    这是**纯配对**操作：不改任何一侧的正文、不新建、不删除。
    """
    from collections import defaultdict

    from notesync.textutil import content_hash, pair_hash

    g_all = engine.graph.list_notes()
    m_all = engine.xiaomi.list_notes()

    links = store.all_links()
    linked_g = {l["graph_id"] for l in links if l.get("graph_id")}
    linked_m = {l["mi_id"] for l in links if l.get("mi_id")}

    g_by_hash: dict[str, list] = defaultdict(list)
    for g in g_all:
        if g.get("id") and g["id"] not in linked_g:
            g_by_hash[pair_hash(g.get("text"))].append(g)
    m_by_hash: dict[str, list] = defaultdict(list)
    for m in m_all:
        if m.get("id") and m["id"] not in linked_m:
            m_by_hash[pair_hash(m.get("text"))].append(m)

    paired = 0
    leftover = 0
    for h, gs in g_by_hash.items():
        ms = m_by_hash.get(h) or []
        if not ms:
            continue
        n = min(len(gs), len(ms))
        for i in range(n):
            g, m = gs[i], ms[i]
            # base_hash 仍用**严格**指纹（小米侧那份）——
            # 于是第一次同步会判定"便笺侧改过"，把便笺内容写过去，
            # 也就是**以便笺为准对齐**（顺便把小米侧多出来的空格抹平）。
            store.upsert_link(
                m["id"], g["id"], content_hash(m.get("text")),
                g.get("change_key", "") or "",
                iso_to_ms(m.get("modified_iso") or "") or 0,
            )
            paired += 1
        leftover += abs(len(gs) - len(ms))

    if paired:
        store.log(f"按内容配对：新增 {paired} 条映射"
                  + (f"，另有 {leftover} 条因两侧数量不等未配" if leftover else ""))
    return {"ok": True, "paired": paired, "leftover": leftover,
            "graph_total": len(g_all), "xiaomi_total": len(m_all)}


def pair_selected(store, engine, keys: list[str]) -> dict[str, Any]:
    """把**手动勾选**的一条便笺 + 一条小米笔记关联起来。

    为什么需要它：「按内容配对」只能认**内容完全一致**的（规范化空白之后）。
    但总有一些条两侧内容**确实不同**（实测剩 42 条，差异是词间空格、
    多一个标点这种"实质差异"）—— 那种不能自动配，否则可能把两条**本来不同**
    的笔记错误关联，之后同步会互相覆盖。所以留一个手动口子。

    只接受**恰好 1 条便笺 + 1 条小米**：批量按顺序两两配对看着省事，
    但顺序一错就全错，而且配错了要一条条解，代价比多点几次大得多。
    """
    from notesync.textutil import content_hash

    gids = [k[2:] for k in keys if k.startswith("g:")]
    mids = [k[2:] for k in keys if k.startswith("m:")]
    if len(gids) != 1 or len(mids) != 1:
        return {"ok": False,
                "error": f"请恰好勾选 1 条便笺 + 1 条小米笔记"
                         f"（现在勾了便笺 {len(gids)} 条、小米 {len(mids)} 条）"}
    gid, mid = gids[0], mids[0]

    notes = getattr(engine, "last_notes", {}) or {}
    g = next((n for n in (notes.get("graph") or []) if str(n.get("id")) == gid), None)
    m = next((n for n in (notes.get("xiaomi") or []) if str(n.get("id")) == mid), None)
    if not g or not m:
        return {"ok": False, "error": "找不到这两条 —— 列表可能已过期，刷新一次再试"}

    # ★ base_hash 用**小米侧**的指纹。
    #   两侧内容本来就不同（不然"按内容配对"就配上了），第一次同步必然要挑一边。
    #   用它 ⇒ 引擎会把便笺那侧当成"改过的一方"，把便笺内容写过去 ——
    #   也就是**以便笺为准对齐**，和本项目"便笺是权威源"的设定一致。
    store.upsert_link(mid, gid, content_hash(m.get("text")),
                      g.get("change_key", "") or "",
                      iso_to_ms(m.get("modified_iso") or "") or 0)
    store.log(f"手动配对：{first_line(g.get('text'))[:30]} ↔ {first_line(m.get('text'))[:30]}")
    return {"ok": True, "graph_id": gid, "xiaomi_id": mid,
            "message": "已配对。下次同步会以便笺为准把两侧内容对齐。"}


def sync_loop():
    """后台同步循环。

    只做两件事：看配置决定要不要跑、跑了就把下次时间点往后推。
    **开关关闭时完全不动作** —— 不登录、不读接口、不写任何东西，
    这样"没打开同步"就是一个真正安静的状态。
    """
    store: Store = RUNTIME["store"]
    while not RUNTIME["stop"].is_set():
        cfg = store.cfg
        on = bool(cfg.get("auto_sync"))
        interval = max(1, int(cfg.get("sync_interval_sec") or 5))
        RUNTIME["next_sync_at"] = time.time() + interval if on else 0
        if on:
            try:
                RUNTIME["engine"].run_once(source="auto")
            except Exception as e:  # 兜底：循环绝不能死
                store.log(f"同步循环异常：{type(e).__name__}: {e}", "error")
        # 用可中断的等待，方便改配置后尽快生效
        RUNTIME["stop"].wait(interval)


# --------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "sticky-mi-sync/0.1"
    # 用 HTTP/1.1 复用连接。
    # BaseHTTPRequestHandler 默认 HTTP/1.0 —— 每个请求都要新建一条 TCP，
    # 而页面在轮询状态时反复建连，会在 TIME_WAIT 里堆出一大批（实测 117 条）。
    # 改成 1.1 后同一个连接能接着用，这才是"低占用"该有的样子。
    protocol_version = "HTTP/1.1"
    # keep-alive 的代价是连接会占着线程，所以给个空闲超时让它们自己回收。
    timeout = 10

    # 本次响应要附带的 Set-Cookie（登录/登出时设置）。
    # ★ 必须是**类属性**：_send() 在登录/登出之外的请求上也会读它，
    #   只写成实例属性的话，第一个普通请求就会 AttributeError ——
    #   而且是在发响应头**之前**抛，客户端只会看到"空回复"（curl 52），
    #   没有任何错误信息，极难定位。
    _extra_cookie: str | None = None

    # ---------- 基础工具
    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 本次响应要附带的 Set-Cookie（登录/登出时设置）
        if self._extra_cookie:
            self.send_header("Set-Cookie", self._extra_cookie)
        # 附加头（备份下载要 Content-Disposition 才会触发"另存为"）
        for _k, _v in (extra or {}).items():
            self.send_header(_k, _v)
        # CORS：**只对 loopback 来源放开**。
        # 原来的 `*` 是为了让"预览面板以静态文件方式打开页面"这种情况还能用
        # （那时它的 /api/* 会打到静态服务器上，页面会自动退回真实后端）。
        # 但现在有了 Cookie 鉴权，`*` 意味着**任何网站都能读到本服务的响应**；
        # 虽然 SameSite=Lax 会让跨站请求不带 cookie（因而是 401），
        # 也没必要留这么大的口子 —— 收紧到本机来源，预览面板照样能用。
        origin = (self.headers.get("Origin") or "").strip()
        if origin and re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
                               origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        """CORS 预检。POST 带 application/json 一定会先发 OPTIONS"""
        self._send(204, b"")

    def _json(self, obj, code: int = 200, extra: dict | None = None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   extra=extra)

    # ------------------------------------------------------------ 鉴权
    #
    # 单个 Handler 实例只处理一个请求，所以 `_extra_cookie` 用实例字段没问题。
    def _auth(self):
        return RUNTIME["auth"]

    def _read_cookie(self, name: str = COOKIE):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return urllib.parse.unquote(v)
        return None

    def _logged_in(self) -> bool:
        try:
            tok = self._read_cookie()
            a = self._auth()
            if not a.validate(tok):
                return False
            # ★ 校验通过就刷新"最后活动时间"（内部按 60 秒节流，不写爆库）。
            #   不刷新的话，用户连续操作 40 分钟也会被当成"空闲 30 分钟"踢掉。
            a.touch(tok)
            return True
        except Exception:
            return False

    def _set_cookie(self, token: str) -> None:
        # HttpOnly —— JS 读不到，防 XSS 偷会话
        # SameSite=Lax —— 跨站 POST 不带 cookie，挡住大部分 CSRF
        # **不加 Secure**：本地/内网多半是 http，加了直接登不上。
        #   套了 https 反代的话，请在反代层补 Set-Cookie 的 Secure（见文档）。
        #
        # ★ **刻意不设 Max-Age** → 这是一个「会话 cookie」：
        #   浏览器关闭后自动丢弃，下次打开页面要重新输密码
        #   （用户明确要求"关闭页面再次打开应该要求重新登录"）。
        #   刷新页面不会丢（同一标签页内 cookie 还在），符合"刷新保持登录"。
        #   服务端另有 SESSION_TTL(30 天) 与 IDLE_TIMEOUT(30 分钟) 兜底 ——
        #   cookie 是会话级的只保证"关浏览器失效"，
        #   而"长时间挂着不动"由服务端的空闲超时来管。
        self._extra_cookie = (
            "%s=%s; Path=/; HttpOnly; SameSite=Lax"
            % (COOKIE, urllib.parse.quote(token)))

    def _clear_cookie(self) -> None:
        self._extra_cookie = ("%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                              % COOKIE)

    def _guard(self, path: str) -> bool:
        """未登录就挡下。返回 True 表示**已经挡下**（调用方立刻 return）。"""
        if not path.startswith("/api/"):
            return False          # 静态页要放行，否则登录页本身都打不开
        if path in AUTH_OPEN:
            return False
        if self._logged_in():
            return False
        self._json({"ok": False, "error": "未登录", "need_login": True}, 401)
        return True

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    # ---------- 路由
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        store: Store = RUNTIME["store"]
        engine: SyncEngine = RUNTIME["engine"]
        xiaomi = RUNTIME["xiaomi"]

        if path in ("/", "/index.html"):
            f = WEB / "index.html"
            if not f.exists():
                return self._send(500, b"web/index.html missing", "text/plain")
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

        # ---- 登录状态查询（放行，前端靠它决定弹哪个界面）----
        if path == "/api/auth/status":
            a = self._auth()
            return self._json({
                "ok": True,
                "needs_setup": not a.has_password(),
                "logged_in": self._logged_in(),
                "min_len": 8,
            })

        # ★ 鉴权闸门：上面放行的路径之外，/api/* 一律要已登录
        if self._guard(path):
            return

        if path == "/api/state":
            st = engine.state()
            st["config"] = store.cfg
            st["logs"] = store.recent_logs(60)
            st["next_sync_at"] = RUNTIME.get("next_sync_at", 0)
            # 界面快照是否被**本地补丁**改过（不再等于远端真实状态）。
            # 前端据此在空闲时自己补一次全量拉取 —— 因为同步开关关着时，
            # 后台不会重拉列表，补丁造成的偏差会一直显示下去（实测反馈）。
            st["snapshot_dirty"] = bool(getattr(engine, "snapshot_dirty", False))
            st["now"] = time.time()
            return self._json(st)

        if path == "/api/trash":
            # 回收站列表。`graph_exists` / `xiaomi_exists` 表示**删除前**那一侧
            # 到底有没有这条 —— 恢复时就照这个还原，界面上也照这个显示。
            items = store.trash_list()
            for it in items:
                it["graph_exists"] = bool(it.get("graph_text")
                                          or it.get("graph_id"))
                it["xiaomi_exists"] = bool(it.get("xiaomi_text")
                                           or it.get("xiaomi_id"))
                it["restored"] = bool(it.get("restored_tid"))
            return self._json({"ok": True, "items": items,
                               "count": len(items)})

        if path == "/api/note":
            # 取单条记录的**完整正文**（只读）。
            # key 由列表接口给出，形如 link:<mi_id> / g:<gid> / m:<mi_id>。
            # 之所以单独开一个端点而不是让 /api/notes 直接带全文：
            # 167 行的正文加起来是几百 KB，每次刷新都传一遍纯属浪费 ——
            # 点开哪条才取哪条。
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (qs.get("key") or [""])[0]
            notes = getattr(engine, "last_notes", {}) or {}
            g_map = {n["id"]: n for n in (notes.get("graph") or [])}
            m_map = {n["id"]: n for n in (notes.get("xiaomi") or [])}

            def full_g(n):
                if not n:
                    return None
                return {"id": n["id"], "title": first_line(n.get("text")),
                        "text": n.get("text") or "",
                        "chars": len(n.get("text") or ""),
                        "time": n.get("modified_iso") or ""}

            def full_m(n):
                if not n:
                    return None
                return {"id": n["id"],
                        "title": first_line(n.get("subject") or n.get("text")),
                        "text": n.get("text") or "",
                        "chars": len(n.get("text") or ""),
                        "time": ms_to_iso(n.get("modify_date"))}

            kind, _, ident = key.partition(":")
            g = m = None
            if kind == "link":
                link = next((l for l in store.all_links()
                             if str(l.get("mi_id")) == ident), None)
                if link:
                    g = g_map.get(link.get("graph_id") or "")
                    m = m_map.get(str(link.get("mi_id")) or "")
            elif kind == "g":
                g = g_map.get(ident)
            elif kind == "m":
                m = m_map.get(ident)
            if not g and not m:
                return self._json({"ok": False,
                                   "error": "找不到这条记录 —— 列表可能已过期，"
                                            "刷新一次再点"}, 404)
            # `same`：两侧内容是否**一致**（用同步引擎同一个 content_hash 算）。
            # 前端据此决定面板怎么画：一致就只画一个框，不一致才两侧都列出来。
            same = bool(g and m
                        and content_hash(g.get("text") or "")
                        == content_hash(m.get("text") or ""))
            return self._json({"ok": True, "key": key, "same": same,
                               "graph": full_g(g), "xiaomi": full_m(m)})

        if path == "/api/backup":
            # **全量备份**：两端全部记录（含正文与时间字段）+ 配对/基线状态 + 配置。
            # 不导出凭据。会全量重拉两端（约 20 秒），保证备份是完整的。
            bk = build_backup(store, engine)
            fn = ("sticky-mi-sync-backup-%s.json"
                  % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
            store.log(f"已导出全量备份（下载）：便笺 {bk['_counts']['graph']} 条 / "
                      f"小米 {bk['_counts']['xiaomi']} 条 / "
                      f"映射 {bk['_counts']['links']} 条")
            return self._json(bk, extra={
                "Content-Disposition": f'attachment; filename="{fn}"'})

        if path == "/api/notes":
            notes = getattr(engine, "last_notes", {}) or {}
            g_list = notes.get("graph") or []
            m_list = notes.get("xiaomi") or []
            g_map = {n["id"]: n for n in g_list}
            m_map = {n["id"]: n for n in m_list}

            def g_side(n):
                """微软侧那一格。为 None 表示这条在便笺里不存在。"""
                if not n:
                    return None
                return {"id": n["id"],
                        "title": first_line(n.get("text")),
                        "chars": len(n.get("text") or ""),
                        # 内容指纹：前端靠它判断"两侧内容是否一致"。
                        # 只比字数不够 —— 同样字数、内容不同是常见的。
                        "hash": content_hash(n.get("text") or ""),
                        "time": n.get("modified_iso") or ""}

            def m_side(n):
                """小米侧那一格。小米的时间是毫秒时间戳，转成 ISO 好和便笺比。"""
                if not n:
                    return None
                return {"id": n["id"],
                        "title": first_line(n.get("subject") or n.get("text")),
                        "chars": len(n.get("text") or ""),
                        "hash": content_hash(n.get("text") or ""),
                        "time": ms_to_iso(n.get("modify_date"))}

            # **把两个列表合并成一个。** 关键是：已映射的两条要并成**一行**，
            # 否则同一条内容会在列表里出现两次，看起来像两条不同的记录。
            # 合并的依据就是 link 表 —— 它是唯一的权威配对信息。
            used_g: set[str] = set()
            used_m: set[str] = set()
            rows: list[dict[str, Any]] = []

            for l in store.all_links():
                gid = l.get("graph_id") or ""
                mid = l.get("mi_id") or ""
                g, m = g_map.get(gid), m_map.get(mid)
                if not g and not m:
                    # 两侧都没了 —— 映射是孤儿，等同步时清理
                    continue
                if gid:
                    used_g.add(gid)
                if mid:
                    used_m.add(mid)
                gs, ms = g_side(g), m_side(m)
                g_time = (gs or {}).get("time") or ""
                m_time = (ms or {}).get("time") or ""
                # ★ 便笺侧的时间可能不是"内容原本的时间"：
                #   从**小米侧补建过来**的便笺，云端时间必然是补建那一刻
                #   （Outlook REST 不接受客户端指定时间，实测过）。
                #   那种情况下改用**小米侧**的时间 —— 否则这条会虚假地排到最前。
                #   标记由 engine 在补建时写进 link.graph_time_synthetic。
                if l.get("graph_time_synthetic") and m_time:
                    row_time = m_time
                else:
                    row_time = max(g_time, m_time)
                rows.append({
                    "key": "link:" + str(mid),
                    "graph": gs,
                    "xiaomi": ms,
                    "linked": True,
                    "conflict": bool(l.get("conflict")),
                    # 便笺侧云端时间不可信（补建盖的）—— 前端可据此提示
                    "time_synthetic": bool(l.get("graph_time_synthetic")),
                    "time": row_time,
                })

            for n in g_list:
                if n["id"] in used_g:
                    continue
                gs = g_side(n)
                rows.append({"key": "g:" + n["id"], "graph": gs, "xiaomi": None,
                             "linked": False, "conflict": False,
                             "time": gs["time"]})

            for n in m_list:
                if n["id"] in used_m:
                    continue
                ms = m_side(n)
                rows.append({"key": "m:" + n["id"], "graph": None, "xiaomi": ms,
                             "linked": False, "conflict": False,
                             "time": ms["time"]})

            # 按时间倒序。两边的 ISO 都能直接字符串比较（同格式、同 UTC）。
            rows.sort(key=lambda r: r["time"] or "", reverse=True)
            return self._json({
                "at": getattr(engine, "last_notes_at", 0),
                "rows": rows,
                "counts": {
                    "graph": len(g_list),
                    "xiaomi": len(m_list),
                    "merged": sum(1 for r in rows if r["linked"]),
                    "total": len(rows),
                },
            })

        # 小米扫码登录：状态查询（不含二维码图，避免每 2 秒反复传 28KB）
        if path == "/api/xiaomi/qr/status":
            return self._json({"ok": True, **mi_qr.status()})

        # 小米账号密码登录的进度（GET 和 POST 都收 —— 前端用 POST 保持一致）
        if path == "/api/xiaomi/pwd/status":
            return self._json(mi_pwd.status())

        # 小米文件夹：列表 + 当前选择 + 条数对比（目标文件夹内 / 全部）
        if path == "/api/xiaomi/folders":
            out: dict = {"ok": True}
            try:
                out["folders"] = xiaomi.list_folders()
            except Exception as e:
                out["ok"] = False
                out["error"] = str(e)
                out["folders"] = []
            try:
                out["stats"] = xiaomi.folder_stats()
            except Exception as e:
                out["stats"] = {"total": 0, "scoped": 0, "error": str(e)}
            out["current"] = {
                "folder_id": store.cfg["xiaomi"].get("folder_id", ""),
                "folder_name": store.cfg["xiaomi"].get("folder_name", ""),
            }
            return self._json(out)

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        store: Store = RUNTIME["store"]
        graph = RUNTIME["graph"]
        xiaomi = RUNTIME["xiaomi"]
        engine: SyncEngine = RUNTIME["engine"]
        body = self._read_json()

        # ★ 鉴权闸门 + 登录相关端点。都放在 try 之前 ——
        # 登录失败不该走业务异常处理那套。
        if path == "/api/auth/login":
            a = self._auth()
            ip = self.client_address[0] if self.client_address else "?"
            wait = a.throttled(ip)
            if wait:
                return self._json({"ok": False,
                                   "error": f"尝试过于频繁，请 {wait} 秒后再试"}, 429)
            if not a.has_password():
                return self._json({"ok": False, "error": "还没设置访问密码",
                                   "needs_setup": True}, 400)
            pw = str(body.get("password") or "")
            if not a.verify(pw):
                a.note_fail(ip)
                store.log("访问密码错误（来自 %s）" % ip, "warn")
                return self._json({"ok": False, "error": "密码不对"}, 401)
            a.note_ok(ip)
            self._set_cookie(a.new_session())
            store.log("已登录（来自 %s）" % ip)
            return self._json({"ok": True})

        if path == "/api/auth/setup":
            # 只在**还没设过密码**时可用 —— 否则任何人不用密码就能改密码。
            a = self._auth()
            if a.has_password():
                return self._json({"ok": False,
                                   "error": "已经设置过密码了。要重置请到宿主机执行："
                                            "python server.py --set-password"}, 400)
            pw = str(body.get("password") or "")
            bad = check_strength(pw)
            if bad:
                return self._json({"ok": False, "error": bad}, 400)
            a.set_password(pw)
            self._set_cookie(a.new_session())
            store.log("已设置访问密码（首次）", "warn")
            return self._json({"ok": True})

        if path == "/api/auth/logout":
            self._auth().logout(self._read_cookie())
            self._clear_cookie()
            return self._json({"ok": True})

        # ★ 其余接口一律要已登录
        if self._guard(path):
            return

        try:
            # ---------------- 同步控制
            if path == "/api/note/delete":
                key = str(body.get("key") or "").strip()
                if not key:
                    return self._json({"ok": False, "error": "需要 key"}, 400)
                r = delete_one(store, engine, key)
                return self._json(r, 200 if r.get("ok") else 404)

            if path == "/api/notes/delete-selected":
                # 批量删除：**逐条复用 delete_one**（同一套"先入库再删"的逻辑），
                # 一条失败不影响其它 —— 批量操作最忌讳"中间挂掉、前后状态不明"。
                keys = [str(k) for k in (body.get("keys") or []) if str(k).strip()]
                if not keys:
                    return self._json({"ok": False, "error": "没有选中任何记录"}, 400)
                res = [delete_one(store, engine, k) for k in keys]
                ok_n = sum(1 for r in res if r.get("ok"))
                store.log(f"批量删除：共 {len(keys)} 条，成功 {ok_n} 条",
                          "info" if ok_n == len(keys) else "warn")
                return self._json({"ok": True, "total": len(keys),
                                   "succeeded": ok_n, "results": res})

            if path == "/api/pair-selected":
                # 手动配对：勾选的 1 条便笺 + 1 条小米 → 建立映射
                keys = [str(k) for k in (body.get("keys") or []) if str(k).strip()]
                if not keys:
                    return self._json({"ok": False, "error": "没有选中任何记录"}, 400)
                if getattr(engine, "busy", False):
                    return self._json({"ok": False,
                                       "error": "正在同步中，等这一轮跑完再试"}, 409)
                engine.busy = True
                try:
                    r = pair_selected(store, engine, keys)
                finally:
                    engine.busy = False
                if r.get("ok"):
                    try:
                        engine.fetch_only(source="manual")
                    except Exception:
                        pass
                return self._json(r)

            if path == "/api/pair-by-content":
                # 按内容给两侧已有的笔记建立映射（一次性整理）。
                # 纯配对：不改正文、不新建、不删除。
                if getattr(engine, "busy", False):
                    return self._json({"ok": False,
                                       "error": "正在同步中，等这一轮跑完再试"}, 409)
                engine.busy = True
                try:
                    r = pair_by_content(store, engine)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                finally:
                    engine.busy = False
                # 配对改了映射关系 —— 列表必须重拉一次，否则界面还是旧的（每条两份）
                try:
                    engine.fetch_only(source="manual")
                except Exception:
                    pass
                return self._json(r)

            if path == "/api/sync/selected":
                # 「同步选中」：把点名的几条对齐成两侧一致。
                # 用 engine.busy 挡住与后台同步并发 —— 两边同时对同一条记录
                # 做写入，乐观锁会互相顶掉（表现为莫名其妙的 tag 失效）。
                keys = [str(k) for k in (body.get("keys") or []) if str(k).strip()]
                if not keys:
                    return self._json({"ok": False, "error": "没有选中任何记录"}, 400)
                if getattr(engine, "busy", False):
                    return self._json({"ok": False,
                                       "error": "正在同步中，等这一轮跑完再试"}, 409)
                engine.busy = True
                try:
                    r = sync_selected(store, engine, keys)
                finally:
                    engine.busy = False
                    engine.last_notes_at = int(time.time())
                return self._json(r)


            if path == "/api/trash/restore":
                tid = int(body.get("tid") or 0)
                it = store.trash_get(tid)
                if not it:
                    return self._json({"ok": False, "error": "回收站里没有这条"}, 404)
                gid = mid = None
                errs: list[str] = []
                now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
                # **按删除前两端的实际状态还原**：当时哪一端有，就只重建哪一端。
                # 一律两边都建的话，会凭空多出用户当时并不存在的条目。
                if it.get("graph_text"):
                    try:
                        r = RUNTIME["graph"].create_note(
                            it["graph_text"],
                            # 恢复也要还原**原本的修改时间**，否则这条会
                            # 跳到列表最前面（新建时间 = 现在）
                            when_iso=it.get("graph_time") or "")
                        gid = str(r.get("id") or "")
                        if gid:
                            # 局部塞回列表，不用为这一条去翻 16 页
                            engine.cache_add("graph", {
                                "id": gid, "text": it["graph_text"],
                                "change_key": str(r.get("change_key") or ""),
                                "modified": 0,
                                "modified_iso": it.get("graph_time") or now_iso,
                                "deleted": False,
                            })
                    except Exception as e:
                        errs.append(f"便笺：{e}")
                if it.get("xiaomi_text"):
                    try:
                        from notesync.engine import iso_to_ms
                        r = RUNTIME["xiaomi"].create_note(
                            it["xiaomi_text"], engine._folder_id(),
                            when_ms=iso_to_ms(it.get("xiaomi_time")))
                        mid = str(r.get("id") or "")
                        if mid:
                            engine.cache_add("xiaomi", {
                                "id": mid, "text": it["xiaomi_text"],
                                "subject": first_line(it["xiaomi_text"]),
                                "change_key": str(r.get("change_key") or ""),
                                "modify_date": iso_to_ms(it.get("xiaomi_time")) or 0,
                                "modified_iso": it.get("xiaomi_time") or now_iso,
                                "deleted": False,
                            })
                    except Exception as e:
                        errs.append(f"小米：{e}")
                # 当时是配对的，恢复后也要重新配上 ——
                # 否则会变成两条各自独立的记录，等于把配对关系弄丢了
                if gid and mid and not errs:
                    store.upsert_link(mid, gid,
                                      content_hash(it.get("graph_text")
                                                   or it.get("xiaomi_text")),
                                      "")
                made = ("便笺+小米" if (gid and mid)
                        else ("便笺" if gid else ("小米" if mid else "无")))
                if not errs:
                    # **恢复成功就把回收站这条删掉。**
                    # 留着没有意义（内容已经重建回两端了），只会越积越多；
                    # 失败时则保留，方便用户重试。
                    store.trash_del(tid)
                    store.log(f"已从回收站恢复 #{tid}（重建了 {made}）", "info")
                else:
                    store.trash_mark_restored(tid)
                    store.log(f"恢复 #{tid} 时有失败（已保留在回收站，可重试）："
                              + "；".join(errs), "error")
                return self._json({"ok": not errs, "errors": errs,
                                   "graph_id": gid, "xiaomi_id": mid})

            if path == "/api/trash/purge":
                # 从回收站彻底移除 —— 这一步不可逆
                tid = int(body.get("tid") or 0)
                if not store.trash_get(tid):
                    return self._json({"ok": False, "error": "回收站里没有这条"}, 404)
                store.trash_del(tid)
                store.log(f"已从回收站彻底删除 #{tid}", "warn")
                return self._json({"ok": True})

            if path == "/api/sync/abort":
                # 中止当前这轮同步。
                # **不是"立即掐断"** —— 只在"当前这条处理完、下一条开始前"生效，
                # 因为写便笺写到一半被打断会留下半截内容，比不中止更糟。
                # 已完成的动作不会回滚（同步幂等，下次接着收敛）。
                eng = RUNTIME["engine"]
                if not getattr(eng, "busy", False):
                    return self._json({"ok": False,
                                       "error": "当前没有正在执行的同步"}, 409)
                eng.abort = True
                p = getattr(eng, "progress", {}) or {}
                store.log(f"收到中止请求（当前进度 {p.get('done', 0)}/"
                          f"{p.get('total', 0)}）—— 处理完当前这条就停，"
                          f"下次点同步会从断点继续", "warn")
                return self._json({"ok": True, "progress": p})

            if path == "/api/sync":
                dry = body.get("dry_run")
                # limit：只同步最新 N 条（测试用，避免一次性动几百条）。
                # 安全阀在 engine.run_once 里 —— 受限模式下跳过所有"只有一边在"
                # 的分支，否则截断的列表会被误判成大批删除。
                lim = body.get("limit")
                try:
                    lim = int(lim) if lim else None
                except (TypeError, ValueError):
                    lim = None
                # resume 默认 true：上次中止过就从断点接着跑。
                # 传 false 可以强制全量重跑（怀疑状态不一致时用）。
                rs = body.get("resume")
                rs = True if rs is None else bool(rs)
                # force：**只用于绕过"大规模删除安全阀"**。
                # 安全阀拦住的是"一次删掉一大片"（例如清空小米笔记却忘了清映射表），
                # 那种情况几乎必然是误操作。确实要删时才传 force=true。
                fc = bool(body.get("force"))
                # changed_only：「只同步改动过内容的」窄模式 ——
                # 只对齐**已配对且内容有差异**的记录，**不新建、不删除、不重建**。
                # 强制定 resume=False：断点是"上一轮处理过哪些条目"的集合，
                # 沿用旧断点会把本轮该看的记录直接跳过（这个模式本来就是
                # 手动小范围操作，重跑一遍的代价很小）。
                co = bool(body.get("changed_only"))
                RUNTIME["next_sync_at"] = time.time() + int(
                    store.cfg.get("sync_interval_sec") or 5)
                # 预演也要忽略断点 —— 否则"上次跑到第 23 条"会让预演只列出
                # 剩下那些，用户看到的计划是不完整的（实测踩到：
                # 预演说 143 个动作，实际待做 165 个）。
                return self._json(engine.run_once(
                    dry_run=dry, limit=lim,
                    resume=(False if (co or dry) else rs), force=fc,
                    source="manual", changed_only=co))

            if path == "/api/backup/save":
                # 备份存到**服务所在的本地文件夹** data/backups/ 下。
                # 和"下载"走同一个 build_backup，内容完全一致 ——
                # 两个入口只是去向不同，绝不能各写一份导出逻辑。
                bk = build_backup(store, engine)
                bdir = Path(store.data_dir) / "backups"
                bdir.mkdir(parents=True, exist_ok=True)
                fn = ("sticky-mi-sync-backup-%s.json"
                      % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
                fp = bdir / fn
                fp.write_text(json.dumps(bk, ensure_ascii=False, indent=2),
                              encoding="utf-8")
                size = fp.stat().st_size
                store.log(f"已导出全量备份：{fn}（{size // 1024} KB，"
                          f"便笺 {bk['_counts']['graph']} 条 / "
                          f"小米 {bk['_counts']['xiaomi']} 条 / "
                          f"映射 {bk['_counts']['links']} 条）→ {bdir}")
                return self._json({"ok": True, "path": str(fp.resolve()),
                                   "name": fn, "bytes": size,
                                   "counts": bk["_counts"]})

            if path == "/api/config":
                patch = body.get("patch") or {}
                # 硬校验：**没指定文件夹（以及没登录）就不许开自动同步**。
                # 前端把开关置灰只是提示，真正的把关在这里。
                if patch.get("auto_sync") is True:
                    ready = engine.readiness()
                    if not ready["ready"]:
                        store.log("拒绝开启自动同步：" + "；".join(ready["blockers"]), "warn")
                        return self._json({
                            "ok": False,
                            "error": "还不能开启自动同步 —— " + "；".join(ready["blockers"]),
                            "blockers": ready["blockers"],
                        }, 409)
                cfg = store.patch_config(patch)
                # 记一条日志 —— 用户点「保存设置」后要能在日志里看到确认。
                # 原来这里是**静默保存**，用户反馈"点了没有任何提示，
                # 不知道到底存上没有"。日志是最合适的落点：既能确认，
                # 也能事后回看"我什么时候改了什么"。
                try:
                    changed = []
                    for k, v in (patch or {}).items():
                        if isinstance(v, dict):
                            changed += [f"{k}.{kk}={vv}" for kk, vv in v.items()]
                        else:
                            changed.append(f"{k}={v}")
                    if changed:
                        store.log("设置已保存：" + "；".join(changed)[:300])
                except Exception:
                    pass
                # 刚打开自动同步时先只读拉一次，让列表立刻有内容
                if patch.get("auto_sync") is True:
                    try:
                        engine.fetch_only(source="switch")
                    except Exception as e:
                        store.log(f"开启后首次拉取失败：{e}", "warn")
                # client_id 变了必须重建连接器
                if "graph" in patch and "client_id" in (patch["graph"] or {}):
                    rebuild()
                return self._json({"ok": True, "config": cfg,
                                   "readiness": RUNTIME["engine"].readiness()})

            # ---------------- 只读拉取两侧列表
            if path == "/api/fetch":
                return self._json(engine.fetch_only(source="manual"))

            # ---------------- 切换连接模式（mock / real）
            if path == "/api/mode":
                wg = str(body.get("graph") or "").strip()
                wx = str(body.get("xiaomi") or "").strip()
                # **只允许在线模式。** mock / local 已停用（2026-09-16）——
                # 它们的数据源不碰真实账号，摆在生产界面里只会让人分不清
                # 眼前的列表是真数据还是假数据。校验放在这里而不是只靠前端：
                # 前端能改，后端才是真正的关口。
                if wg not in ("", "real", "fabric") or wx not in ("", "real"):
                    return self._json({"ok": False,
                                       "error": "微软模式只能是 fabric / real；"
                                                "小米只能是 real。"
                                                "（mock / local 是测试模式，已停用）"}, 400)
                if wg:
                    old_g = store.cfg["graph"]["mode"]
                    store.cfg["graph"]["mode"] = wg
                    # mock / real / local / fabric 的便笺 id 是**四套互不相干的体系**：
                    #   mock   → AAMkMOCK0001（假 id）
                    #   real   → 邮件 message id
                    #   local  → plum.sqlite 的 RemoteId
                    #   fabric → AAkALgAAAAAAHYQ…（MAPI EntryID）
                    # 只要切换涉及 mock，旧映射必然指向不存在的便笺 ——
                    # 界面上会显示一堆"已映射"，但另一边其实没有对应条目
                    # （用户实际遇到过：小米的 test 显示已映射，真实便笺里却没这条）。
                    # 所以直接清掉，让程序按新数据源重新配对。
                    if old_g != wg and "mock" in (old_g, wg):
                        n = store.clear_links()
                        store.log(f"切换微软模式 {old_g} → {wg}，已清空 {n} 条旧映射"
                                  f"（不同数据源的 id 体系不通用）", "warn")
                if wx:
                    old_x = store.cfg["xiaomi"]["mode"]
                    store.cfg["xiaomi"]["mode"] = wx
                    # mock 和 real 的文件夹 id 是两套体系，切换以后必须重选。
                    # 不清掉的话 real 模式会拿着 mock 的 F_MOCK 当目标文件夹 —— 范围直接是错的。
                    if old_x != wx:
                        store.cfg["xiaomi"]["folder_id"] = ""
                        store.log("已清空目标文件夹（切换连接模式后需要重新选择）", "warn")
                # 换连接器等于换了数据源，顺手关掉自动同步，避免用半配置状态开工
                store.cfg["auto_sync"] = False
                store.save_config()
                eng = rebuild()
                store.log(f"连接模式：graph={store.cfg['graph']['mode']} "
                          f"xiaomi={store.cfg['xiaomi']['mode']}（自动同步已自动关闭）")
                return self._json({"ok": True, "config": store.cfg,
                                   "readiness": eng.readiness()})

            # ---------------- 微软登录
            if path == "/api/graph/login/start":
                if isinstance(graph, NotesFabricGraph):
                    # fabric 的 usertoken 不是 OAuth 换来的，设备码对它无效。
                    # 旧文案写的是"当前是 mock 模式"，会把人误导到错误方向。
                    return self._json({
                        "status": "error",
                        "error": "当前是 NotesFabric 通道（数据最全），它不用设备码登录 —— "
                                 "设备码换出来的是 Graph 令牌，读不到 2026 年的便笺。"
                                 "请改用下面的「粘贴凭据」。"})
                if not isinstance(graph, RealGraph):
                    return self._json({"status": "error",
                                       "error": f"当前通道是 {type(graph).__name__}，不支持设备码登录"})
                try:
                    return self._json(graph.start_device_login())
                except Exception as e:
                    # 把原始错误原样带出来 —— client_id 填错、账户类型不支持等
                    # 都会在这一步暴露，吞掉错误只会让用户瞎猜
                    return self._json({"status": "error", "error": str(e)}, 400)

            if path == "/api/graph/login/poll":
                if not isinstance(graph, RealGraph):
                    return self._json({"status": "error",
                                       "error": "当前通道不支持设备码登录"})
                r = graph.poll_device_login()
                # 授权成功就**立刻做分层体检**，把"能不能用、卡在哪一层"当场给出结论，
                # 而不是等用户点了同步再对着一个 401 猜。
                # 「登录成功」和「能读到便笺」是两件事 —— 这中间的差距
                # （token 格式、权限同意、通道可用性）必须一次说清。
                if r.get("status") == "success":
                    try:
                        d = graph.diagnose()
                        r["diagnose"] = d
                        store.log("微软登录后体检：" + str(d.get("verdict", "")),
                                  "info" if d.get("ok") else "error")
                        for s in d.get("steps") or []:
                            store.log(("  ✅ " if s.get("ok") else "  ❌ ")
                                      + str(s.get("name")) + "：" + str(s.get("detail")),
                                      "info" if s.get("ok") else "warn")
                    except Exception as e:
                        r["diagnose"] = {"ok": False,
                                         "error": f"{type(e).__name__}: {e}"}
                    try:
                        r["fetch"] = engine.fetch_only(source="manual")
                    except Exception as e:
                        r["fetch"] = {"ok": False, "errors": [f"{type(e).__name__}: {e}"]}
                return self._json(r)

            # 随时可跑的分层体检（不用重新登录）
            if path == "/api/graph/diagnose":
                if not isinstance(graph, RealGraph):
                    return self._json({"ok": False, "error": "mock 模式"})
                d = graph.diagnose()
                store.log("微软体检：" + str(d.get("verdict", "")),
                          "info" if d.get("ok") else "error")
                return self._json(d)

            if path == "/api/graph/logout":
                if isinstance(graph, RealGraph):
                    graph.logout()
                # 同理：退出后关掉自动同步，避免空凭据反复请求
                if store.cfg.get("auto_sync"):
                    store.cfg["auto_sync"] = False
                    store.save_config()
                    store.log("已退出微软登录，自动同步已随之关闭", "warn")
                else:
                    store.log("已退出微软登录", "warn")
                return self._json({"ok": True})

            # 快捷通道：直接粘贴访问令牌，不需要 Azure 应用注册
            if path == "/api/graph/token":
                # 两条通道各有自己的粘贴格式，必须分开处理：
                #   fabric → MSAuth1.0 usertoken（从 DevTools 复制，或用本机导出工具）
                #   real   → Graph 的 OAuth access_token（Graph Explorer 拿，约 1 小时）
                # ★ 旧版这里写死 `if not isinstance(graph, RealGraph): 报"先切到 real"`，
                #   把 fabric 挡在门外 —— 而 fabric 的 token **只能靠粘贴**（OAuth 拿不到），
                #   等于在新环境里彻底没法登录。这是发布后才发现的致命缺口。
                payload = (body.get("payload") or body.get("access_token") or "")
                try:
                    if isinstance(graph, NotesFabricGraph):
                        r = graph.use_pasted_token(payload)
                    elif isinstance(graph, RealGraph):
                        r = graph.use_pasted_token(payload)
                    else:
                        return self._json({
                            "ok": False,
                            "error": f"当前通道是 {type(graph).__name__}，不支持粘贴登录"},
                            400)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                if r.get("expires_in_min"):
                    store.log("微软侧已用粘贴令牌登录，剩余约 "
                              f"{r['expires_in_min']} 分钟")
                return self._json(r)

            # ---------------- 小米扫码登录（在页面里完成，不需要开终端）
            if path == "/api/xiaomi/qr/start":
                try:
                    return self._json(mi_qr.start(store))
                except ImportError:
                    return self._json({
                        "ok": False,
                        "error": "没装 migate，扫码登录不可用。装法：pip install migate",
                    }, 400)

            if path == "/api/xiaomi/qr/cancel":
                mi_qr.cancel()
                return self._json({"ok": True})

            # ---------------- 小米账号密码登录（容器里唯一能完整走通的登录方式）
            #
            # 为什么单独做这一套：扫码在容器里过不了小米的**新设备安全验证**
            # （isSecondValidation —— 扫码那条链路根本没处理 notificationUrl），
            # 而「浏览器登录」要弹有头浏览器，容器里没显示器。
            # 账号密码全程都是普通 HTTPS，能把二次验证也搬到页面上：
            #   登录 → （可能）图形验证码 → （可能）选手机/邮箱 → 发码 → 输码 → 落库
            if path == "/api/xiaomi/pwd/login":
                try:
                    r = mi_pwd.login((body.get("user") or "").strip(),
                                     body.get("password") or "",
                                     (body.get("capt_code") or "").strip())
                except ImportError:
                    return self._json({"ok": False,
                                       "error": "没装 migate，这条登录方式不可用"}, 400)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                if r.get("ok"):
                    return self._json(_finish_xiaomi_login(store, r))
                return self._json(r)

            if path == "/api/xiaomi/pwd/send":
                try:
                    r = mi_pwd.send_code(body.get("type") or "PH",
                                         (body.get("capt_code") or "").strip())
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._json(r)

            if path == "/api/xiaomi/pwd/check":
                try:
                    r = mi_pwd.check_code((body.get("ticket") or "").strip())
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                if r.get("ok"):
                    return self._json(_finish_xiaomi_login(store, r))
                return self._json(r)

            if path == "/api/xiaomi/pwd/status":
                return self._json(mi_pwd.status())

            # ---------------- 小米凭据
            if path == "/api/xiaomi/cookie":
                if not isinstance(xiaomi, RealXiaomi):
                    return self._json({"ok": True, "note": "mock 模式，忽略"})
                raw = (body.get("cookie") or "").strip()
                parsed = parse_cookie(raw)
                if not parsed:
                    return self._json({"ok": False, "error": "没解析出任何 cookie"}, 400)
                if not (parsed.get("serviceToken") and parsed.get("userId")):
                    # 允许只贴 passToken/deviceId 这种"补充式"粘贴 —— 合并到已有凭据里
                    existing = store.get_cred("xiaomi_cookie", {}) or {}
                    merged_preview = {**existing, **parsed}
                    if not (merged_preview.get("serviceToken")
                            and merged_preview.get("userId")):
                        return self._json({
                            "ok": False,
                            "error": "Cookie 里至少要能凑出 serviceToken 和 userId。"
                                     "已有：" + (", ".join(sorted(existing)) or "（空）") +
                                     "；这次贴进来：" + ", ".join(sorted(parsed)),
                        }, 400)
                    # 只补字段，不动已有凭据
                    merged = merged_preview
                else:
                    # 完整 cookie：以新贴的为准，但保留已有键（可能来自另一个域）
                    existing = store.get_cred("xiaomi_cookie", {}) or {}
                    merged = {**existing, **parsed}

                # 验证用完整的合并结果去试
                try:
                    xiaomi.verify_cookie(merged)   # 先验证再落库
                except Exception as e:
                    # 验证失败也把凭据存下来 —— 用户可能是在补 passToken，
                    # 而 serviceToken 还没到手。存着没坏处，读不了会自己报错。
                    store.set_cred("xiaomi_cookie", merged)
                    if parsed.get("passToken") and parsed.get("deviceId"):
                        store.set_cred("xiaomi_pass_token", {
                            "deviceId": parsed["deviceId"],
                            "passToken": parsed["passToken"],
                            "userId": parsed.get("userId")
                                      or merged.get("userId") or "",
                        })
                    return self._json({
                        "ok": False,
                        "error": f"凭据已保存，但验证没通过：{e}",
                        "saved_keys": sorted(merged),
                    }, 200)

                store.set_cred("xiaomi_cookie", merged)
                # 记下凭据的获得时间，用来实测 serviceToken 能活多久
                store.set_meta("xiaomi_cred_at", str(int(time.time())))
                store.log(f"小米凭据已更新（userId={merged.get('userId')}），"
                          f"键={sorted(merged)}")

                # 顺手把续期凭据也抽出来 —— 浏览器里的 deviceId 是"可信设备"，
                # 用它 + passToken 换 serviceToken 才不会触发二次安全验证。
                if parsed.get("passToken") and parsed.get("deviceId"):
                    store.set_cred("xiaomi_pass_token", {
                        "deviceId": parsed["deviceId"],
                        "passToken": parsed["passToken"],
                        "userId": parsed.get("userId") or merged.get("userId") or "",
                    })
                    store.log("已提取 passToken + deviceId（可信设备），"
                              "之后 serviceToken 过期会自动续期")
                else:
                    # 关键：如果这次粘贴的凭据来自**另一台设备**（deviceId 不同），
                    # 库里旧的 passToken 就不能再用来续期了 —— 否则会拿 A 设备的 passToken
                    # 换出 token 去配 B 设备的 cookie，服务端直接 401，而且会把好凭据换坏。
                    old_pt = store.get_cred("xiaomi_pass_token") or {}
                    old_dev = old_pt.get("deviceId", "")
                    new_dev = merged.get("deviceId", "")
                    if old_dev and new_dev and old_dev != new_dev:
                        store.del_cred("xiaomi_pass_token")
                        store.log("检测到凭据来自另一台设备（deviceId 不同），"
                                  "已丢弃旧的 passToken 以避免续期时混搭出错", "warn")
                    missing = [k for k in ("passToken", "deviceId")
                               if not merged.get(k)]
                    store.log("暂时没有 " + "/".join(missing) +
                              "，无法自动续期（过期后需要重新粘贴）", "warn")

                return self._json({"ok": True, "keys": sorted(merged),
                                   "auto_refresh": bool(merged.get("passToken")
                                                        and merged.get("deviceId"))})

            # ---------------- 小米文件夹选择
            if path == "/api/xiaomi/folder":
                fid = (body.get("folder_id") or "").strip()
                fname = (body.get("folder_name") or "").strip()
                if not fid and not fname:
                    return self._json({"ok": False, "error": "要选一个文件夹"}, 400)
                store.cfg["xiaomi"]["folder_id"] = fid
                store.cfg["xiaomi"]["folder_name"] = fname
                store.save_config()
                store.log(f"小米目标文件夹改为：{fname or fid}")
                return self._json({"ok": True})

            # 弹出真实浏览器，让你**登录一次**，之后长期免登录。
            # 原理见 notesync/browser_auth.py 模块头：登录态落在持久化 profile 里，
            # 以后凭据过期时无头打开同一个 profile 就能自动换新。
            if path == "/api/xiaomi/browser-login":
                if not isinstance(xiaomi, RealXiaomi):
                    return self._json({"ok": True, "note": "mock 模式，忽略"})
                import subprocess

                from notesync.browser_auth import find_python_with_playwright
                exe = find_python_with_playwright()
                if not exe:
                    return self._json({
                        "ok": False,
                        "error": "没找到装了 playwright 的 python。先执行："
                                 "pip install playwright（在能跑 playwright 的解释器里）",
                    }, 400)
                flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                try:
                    subprocess.Popen(
                        [exe, "-u", "-m", "notesync.browser_auth",
                         "--data-dir", str(store.data_dir)],
                        cwd=str(ROOT), creationflags=flags)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 500)
                store.log("已打开浏览器登录窗口 —— 在窗口里登录 i.mi.com（可扫码），"
                          "完成后窗口会自己关闭并自动保存。只需做这一次。")
                return self._json({"ok": True, "python": exe,
                                   "note": "浏览器窗口已弹出，请在其中登录"})

            # 重新从数据库读凭据（给"另一个进程刚写完凭据"的场景用）
            if path == "/api/xiaomi/reload":
                from notesync.credlife import invalidate_profile_cache
                invalidate_profile_cache(store)
                eng = rebuild()
                store.log("已重新加载小米凭据")
                return self._json({"ok": True, "readiness": eng.readiness(),
                                   "xiaomi": RUNTIME["xiaomi"].status()})

            # 只做**只读**检查，不触发任何续期/登录
            if path == "/api/xiaomi/check":
                if not isinstance(xiaomi, RealXiaomi):
                    return self._json({"ok": True, "note": "mock 模式，忽略"})
                if not xiaomi.status().get("connected"):
                    return self._json({"ok": False,
                                       "error": "还没有可用的小米凭据"}, 400)
                try:
                    xiaomi._get("/note/full/page", {"limit": 1}, _retry=False)
                except Exception as e:
                    store.log(f"小米凭据体检：已失效（{e}）", "warn")
                    return self._json({"ok": False, "alive": False,
                                       "error": f"凭据已失效：{e}"}, 200)
                store.log("小米凭据体检：仍然有效")
                return self._json({"ok": True, "alive": True,
                                   "note": "凭据有效，能正常读取"})

            # 从浏览器 profile 静默刷新凭据（不用登录、不用粘贴）
            # 这条路**不发任何登录请求**，是"测试续期"最该走的路 —— 零风控风险。
            if path == "/api/xiaomi/browser-refresh":
                if not isinstance(xiaomi, RealXiaomi):
                    return self._json({"ok": True, "note": "mock 模式，忽略"})
                store.log("开始从浏览器 profile 静默续期（不发登录请求，无风控风险）…")
                ok = xiaomi.refresh_from_browser_safe()
                msg = ("小米凭据已从浏览器 profile 静默续期成功（续期后仍可正常读取）"
                       if ok else f"从浏览器 profile 续期失败：{xiaomi.last_error}")
                store.log(msg, "info" if ok else "warn")
                if ok:
                    # 成功就意味着凭据刚刚被验证可用 —— 顺手刷新一次列表，
                    # 让页面上的"已连续可用"和条数都动起来
                    try:
                        engine.fetch_only(source="auto")
                    except Exception as e:
                        store.log(f"续期后刷新列表失败：{e}", "warn")
                return self._json(
                    {"ok": ok, "error": "" if ok else xiaomi.last_error,
                     "profile": store.cfg and xiaomi.status().get("profile")},
                    200 if ok else 400)

            # 用保存的 passToken 静默换新的 serviceToken（无需人工介入）
            if path == "/api/xiaomi/relogin":
                if not isinstance(xiaomi, RealXiaomi):
                    return self._json({"ok": True, "note": "mock 模式，忽略"})
                if not store.get_cred("xiaomi_pass_token"):
                    msg = ("库里没有 passToken —— 当前是手工粘贴的 Cookie，无法续期。"
                           "想开启续期，先从 account.xiaomi.com 复制 passToken + deviceId 补进来")
                    store.log("续期失败：" + msg, "warn")
                    return self._json({"ok": False, "error": msg}, 400)
                store.log("开始测试小米凭据续期…")
                ok = xiaomi._refresh_token()
                if ok:
                    store.log("小米凭据续期成功：已用 passToken 换到新的 serviceToken")
                # 失败时 _refresh_token 内部已经写过日志了
                return self._json(
                    {"ok": ok, "error": "" if ok else xiaomi.last_error},
                    200 if ok else 400)

            if path == "/api/xiaomi/verify":
                if not isinstance(xiaomi, RealXiaomi):
                    return self._json({"ok": True, "note": "mock 模式"})
                return self._json(xiaomi.verify_cookie(xiaomi._cookie()))

            if path == "/api/xiaomi/logout":
                store.del_cred("xiaomi_cookie")
                store.del_cred("xiaomi_pass_token")   # 退出就一起清掉，免得残留
                # 退出后必须把自动同步关掉 —— 否则它会拿着空凭据每 5 秒去请求一次，
                # 日志被"续期失败"刷屏（这个现象已经出现过）
                if store.cfg.get("auto_sync"):
                    store.cfg["auto_sync"] = False
                    store.save_config()
                    store.log("已退出小米登录，自动同步已随之关闭", "warn")
                else:
                    store.log("已退出小米登录", "warn")
                return self._json({"ok": True})

            # ---------------- 清理后台数据
            if path == "/api/reset":
                what = str(body.get("what") or "creds").strip()
                # ★ **白名单校验放在最前面。**
                # 原来的结构是 if links / elif xiaomi / elif all / else 默认清凭据 ——
                # 于是一个拼错的 what（比如探测时随手写的 `__probe__`）
                # 会**静默落到那个破坏性的默认分支，把凭据删掉**。
                # 这不是假设：2026-09-16 调试时就因为一个错误参数
                # 把小米 cookie / passToken 清掉了（后来靠浏览器 profile 续期救回）。
                # 破坏性操作绝不能有"兜底默认"，参数不认识就必须报错。
                if what not in ("links", "xiaomi", "all", "creds"):
                    return self._json({
                        "ok": False,
                        "error": f"未知的 what={what!r}。"
                                 f"可用值：links（只清映射）/ creds（清凭据+映射）"
                                 f" / xiaomi（连小米文件夹一起清）/ all（全部重置）",
                    }, 400)
                if what == "links":
                    # **只清映射，什么都不删、什么凭据都不动。**
                    #
                    # 这是"清空小米笔记后重新配对"的正确入口 ——
                    # 必须先清映射再重同步，否则本地那上百条 link 会指向
                    # 已不存在的小米笔记，被引擎解读成"小米侧删除了"，
                    # 于是把便笺也删掉（会同步到微软云端，真正丢数据）。
                    #
                    # 清掉的五样东西：
                    #   link           映射表（不清理就会误删便笺）
                    #   trash          回收站里的旧记录（引用的是失效 id）
                    #   notes_cache    笔记列表缓存（旧的已无意义）
                    #   paired         配对标记（置空＝当作全新配对）
                    #   sync_done      断点（列表要重建，旧断点已无意义）
                    #   pairing_since  配对起点 ← **必须一起重置**：
                    #     引擎靠它判断"哪些小米笔记是配对前就有的、不该收养"。
                    #     不重置的话起点还停在很久以前，清空后新建的笔记
                    #     也会被当成"既有"，被永久跳过（用户实测踩到过）。
                    # **保留**：小米/微软凭据、目标文件夹、写入开关、同步间隔。
                    n_link = len(store.all_links())
                    store.db.execute("DELETE FROM link")
                    store.db.execute("DELETE FROM trash")
                    store.db.execute("DELETE FROM notes_cache")
                    store.db.commit()
                    store.set_meta("paired", "")
                    store.set_meta("sync_done", "")
                    store.set_meta("pairing_since", str(int(time.time() * 1000)))
                    store.cfg["auto_sync"] = False
                    store.save_config()
                    store.log(f"已清空映射（{n_link} 条）与相关缓存 —— "
                              f"凭据、文件夹、写入开关全部保留。"
                              f"配对起点已重置，此后新建的笔记会正常同步", "warn")
                    return self._json({"ok": True, "what": "links",
                                       "cleared_links": n_link})
                if what == "xiaomi":
                    # 只清小米：凭据 + 映射 + 目标文件夹。**保留微软那边的登录**，
                    # 免得用户刚授权完还要再来一次。
                    for k in ("xiaomi_cookie", "xiaomi_pass_token"):
                        store.del_cred(k)
                    store.db.execute("DELETE FROM link")
                    store.db.commit()
                    store.cfg["auto_sync"] = False
                    store.cfg["xiaomi"]["folder_id"] = ""
                    store.save_config()
                    store.log("已清空小米侧凭据、映射与目标文件夹（微软侧登录保留）", "warn")
                elif what == "all":
                    # 全清：凭据 + 映射 + 配置回默认。等同于"从头开始"
                    for k in ("xiaomi_cookie", "xiaomi_pass_token", "graph_device",
                              "graph_token"):
                        store.del_cred(k)
                    store.db.execute("DELETE FROM link")
                    store.db.execute("DELETE FROM log")
                    store.db.commit()
                    store.cfg.update({
                        "auto_sync": False, "dry_run": False,
                        "graph": {"mode": "mock", "client_id": "",
                                  "tenant": "common",
                                  "scope": "ShortNotes.ReadWrite offline_access"},
                        "xiaomi": {"mode": "mock", "base_url": "https://i.mi.com",
                                   "folder_name": "微软便笺", "folder_id": "",
                                   "write_enabled": False, "warn_interval_sec": 30},
                    })
                    store.save_config()
                    rebuild()
                    store.log("已全部重置（凭据 / 映射 / 设置都回到初始状态）", "warn")
                else:
                    # 默认只清凭据和映射，保留设置（模式、文件夹名、间隔这些）
                    for k in ("xiaomi_cookie", "xiaomi_pass_token", "graph_device",
                              "graph_token"):
                        store.del_cred(k)
                    store.db.execute("DELETE FROM link")
                    store.db.commit()
                    store.cfg["auto_sync"] = False
                    store.save_config()
                    store.log("已清空凭据与映射（设置保留）", "warn")
                return self._json({"ok": True, "what": what})

            # ---------------- mock 数据操纵（只读演示用）
            if path.startswith("/api/mock/"):
                return self._mock(path, body)

        except Exception as e:
            store.log(f"{path} 出错：{type(e).__name__}: {e}", "error")
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

        return self._json({"error": "not found"}, 404)

    # ---------- mock 操纵（**已停用**，2026-09-16）
    def _mock(self, path: str, body: dict):
        """停用的 mock 操作端点。

        这个服务现在只操作**真实的在线数据**，不再提供"往内存里塞假便笺"
        这类测试入口 —— 摆在界面上只会让人分不清看到的是真数据还是假数据。

        原实现完整保留在下面（注释形式），需要恢复 mock 测试时，
        把 return 那行删掉、取消注释即可。
        """
        return self._json({"ok": False,
                           "error": "mock 测试功能已停用。本服务现在只操作真实在线数据。"},
                          410)

        # ------------------------- 原实现（保留以便回滚）-------------------------
        # graph = RUNTIME["graph"]
        # xiaomi = RUNTIME["xiaomi"]
        # store: Store = RUNTIME["store"]
        # side = body.get("side")
        # target = graph if side == "graph" else xiaomi
        # if not hasattr(target, "create"):
        #     return self._json({"ok": False, "error": "只有 mock 模式支持这个操作"}, 400)
        #
        # act = path.rsplit("/", 1)[-1]
        # if act == "add":
        #     target.create(body.get("text") or "(空)")
        # elif act == "edit":
        #     nid = body.get("id") or ""
        #     if nid not in target.notes:
        #         return self._json({"ok": False, "error": f"没有 {nid}"}, 404)
        #     target.update(nid, body.get("text") or "")
        # elif act == "del":
        #     nid = body.get("id") or ""
        #     if nid not in target.notes:
        #         return self._json({"ok": False, "error": f"没有 {nid}"}, 404)
        #     target.delete(nid)
        # else:
        #     return self._json({"ok": False, "error": "未知动作"}, 404)
        # store.log(f"[mock] {side} 侧 {act} 完成")
        # return self._json({"ok": True})
        # ---------------------------------------------------------------------


def ms_to_iso(ms: Any) -> str:
    """小米的毫秒时间戳 → ISO 字符串（UTC）。

    统一成 ISO 是为了能和便笺的 `documentModifiedAt`（本来就是 ISO）
    **直接做字符串比较来排序** —— 两边格式一致时，字典序就是时间序。
    解不出来返回空串，排序时会落到末尾（比乱序好）。
    """
    try:
        v = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    # 小米给的是毫秒；万一给了秒级（位数明显偏小）也顺手兼容一下
    if v < 10**11:
        v *= 1000
    try:
        return datetime.datetime.utcfromtimestamp(v / 1000).strftime(
            "%Y-%m-%dT%H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def first_line(text: str | None, limit: int = 60) -> str:
    """取**首个非空行**当列表标题。

    两个坑都踩过：
      1. 不能直接用 `split("\\n")[0]` —— 微软便笺的 body 剥完 HTML 标签后
         最前面会残留一个换行，于是"首行"是空的，整列标题全变空。
      2. 便笺正文每段开头带 `\\id=<uuid>` 块标记，要先用 `strip_block_markers` 剥掉，
         否则标题显示成 `\\id=d43012d3-… 测试 2026`。
    """
    t = strip_block_markers(text or "")
    for line in t.split("\n"):
        if line.strip():
            return line.strip()[:limit]
    return ""


def parse_cookie(raw: str) -> dict:
    """支持两种粘贴格式：`k=v; k=v` 或 JSON"""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return {str(k): str(v) for k, v in json.loads(raw).items()}
        except json.JSONDecodeError:
            pass
    out = {}
    for part in re.split(r"[;\n]", raw):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _read_new_password(interactive_confirm: bool):
    """读一个新密码。

    **刻意不提供 `--password` 参数**：命令行参数会留在 shell 历史和 `ps` 里，
    自托管产品的通行做法都是"从终端或 stdin 读，绝不出现在 argv"。
    （Capstan / docker-commander 的文档都专门写了这一点。）

    有 tty → 交互输入（并要求二次确认）；
    没有 tty（管道 / CI / `docker exec -T`）→ 从 stdin 读一行，方便脚本化。
    """
    if sys.stdin is not None and sys.stdin.isatty():
        pw = getpass.getpass("新访问密码（输入时不显示）：")
        if interactive_confirm:
            again = getpass.getpass("再输一次：")
            if pw != again:
                print("两次输入不一致，未做任何修改。")
                return None
        return pw
    return (sys.stdin.readline() if sys.stdin else "").rstrip("\r\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--set-password", action="store_true",
                    help="设置/重置访问密码，然后退出（不会启动服务）")
    ap.add_argument("--clear-password", action="store_true",
                    help="清除访问密码，下次打开页面会要求重新设置")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)

    # ---------------- 密码管理子命令 ----------------
    # 只碰 data/ 目录，不需要起服务 —— 所以**容器没在跑也能用**。
    if args.set_password or args.clear_password:
        store = Store(data_dir)
        auth = Auth(store, log=lambda m, lv="info": print("  " + m))
        if args.clear_password:
            auth.clear_password()
            print("已清除访问密码。下次打开页面会要求重新设置。")
            return 0
        pw = _read_new_password(interactive_confirm=True)
        if pw is None:
            return 2
        bad = check_strength(pw)
        if bad:
            print("密码不合格：" + bad)
            return 2
        auth.set_password(pw)
        print("访问密码已更新。所有已登录的浏览器都会被踢下线，需要重新登录。")
        return 0

    store, graph, xiaomi, engine = build(data_dir)
    RUNTIME.update({
        "store": store, "graph": graph, "xiaomi": xiaomi, "engine": engine,
        "stop": threading.Event(),
    })

    port = args.port or int(store.cfg.get("port") or 8787)

    # 容器化便利：`SMS_PASSWORD` 环境变量可以直接设定/重置访问密码。
    # **只在和现有密码不一致时才写** —— 否则每次重启都会吊销所有会话，
    # 而且哈希每次带新随机盐、内容不同，会造成无意义的写库。
    _env_pw = (os.environ.get("SMS_PASSWORD") or "").strip()
    if _env_pw:
        _a = RUNTIME["auth"]
        if _a.has_password() and _a.verify(_env_pw):
            pass                                   # 已经是这个密码，不动
        else:
            _bad = check_strength(_env_pw)
            if _bad:
                store.log(f"SMS_PASSWORD 不合格，已忽略：{_bad}", "warn")
            else:
                _a.set_password(_env_pw)
                store.log("已按环境变量 SMS_PASSWORD 设置访问密码"
                          "（所有旧会话已失效）", "warn")

    store.log(f"服务启动：graph={store.cfg['graph']['mode']} "
              f"xiaomi={store.cfg['xiaomi']['mode']} "
              f"间隔={store.cfg['sync_interval_sec']}s")
    # 启动时把能力边界说清楚，省得用户猜"为什么又要我手工贴"
    try:
        import migate  # noqa: F401
        store.log("migate 可用 —— 小米凭据支持 passToken 自动续期")
    except ImportError:
        store.log("未安装 migate：小米侧只能手工 Cookie，且过期后需要重新粘贴。"
                  "想开启自动续期：pip install migate 然后跑 "
                  "python -m notesync.mi_login", "warn")

    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()

    # 监听地址：默认**只听本机**（安全，别人扫不到你电脑上的这个服务）。
    # 容器里必须让外面能连进来 —— 用环境变量 SMS_HOST=0.0.0.0 覆盖
    # （docker-compose.yml 里已经设好）。硬编码 127.0.0.1 的话端口映射不进来，
    # 这是移植到 docker 时第一个会踩的坑。
    host = (os.environ.get("SMS_HOST") or "").strip() or "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"sticky-mi-sync 已启动 -> http://{host}:{port}")
    print(f"数据目录：{data_dir}")

    # ★ 优雅退出。docker stop / k8s 删 Pod 都是先发 **SIGTERM**，
    # 而 Python 默认收到 SIGTERM 直接终止进程 —— 正在写的那一条会被切断，
    # 云端留下半截内容。这里接住它：先请求中止当前这一轮、等它收尾，再退出。
    # 注意 shutdown() 必须从**另一个线程**调用，否则会和 serve_forever() 死锁。
    def _graceful(signum, _frame):
        try:
            store.log(f"收到信号 {signum} —— 停止中（先让当前这一条写完）", "warn")
        except Exception:
            pass
        try:
            engine.abort = True
        except Exception:
            pass
        threading.Thread(target=srv.shutdown, daemon=True).start()
        # 给正在写的那一条留收尾时间：最多 8 秒
        # （docker 默认 10 秒后 SIGKILL，留 2 秒余量）
        deadline = time.time() + 8
        while time.time() < deadline:
            if not getattr(engine, "busy", False):
                break
            time.sleep(0.3)
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
    except Exception:
        pass          # 某些受限环境装不了处理器，不影响启动

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中…")
    finally:
        RUNTIME["stop"].set()
        srv.server_close()


if __name__ == "__main__":
    main()
