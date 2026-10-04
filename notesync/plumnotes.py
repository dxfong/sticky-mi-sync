"""从 Windows 便笺的本地数据库（plum.sqlite）读便笺。

为什么需要这条路
----------------
实测发现 **Windows 便笺 App 的数据和 Outlook 的 Notes 邮件文件夹是两套东西**：

| 数据源 | 内容 |
|---|---|
| Outlook Notes 文件夹（Graph 可读） | 2020–2025 共 166 条，最后一条停在 **2025-12** |
| **便笺 App 自己的服务（ShortNotes）** | **2026 年的全部新便笺** |

ShortNotes 那个终点只存在于 **beta**（v1.0 返 400「不存在」，beta 返
**403 ErrorAccessDenied**），第三方应用拿不到它的授权 —— 微软只给自家客户端开。

好在**本地库里什么都有**：`plum.sqlite` 的 Note 表 165 行，最新几条正是
App 里看到的 2026 便笺，而且**每条都带 `RemoteId`**（说明确实同步到了服务端）。
所以本地库是一个可靠的**只读**数据源，代价是只在本机有效。

字段速查（schema 实测）
-----------------------
    Text            便笺正文，开头带 `\\id=<uuid>` 块标记（要用 strip_block_markers 剥掉）
    Id              本地 GUID
    RemoteId        **服务端 ID**（MAPI EntryID），跨设备一致 —— 拿它做同步映射最稳
    ChangeKey       乐观并发用的版本号
    ParentId        所属分组（便笺 App 的"分组"）
    CreatedAt       注意：**这是占位值**（很多条都是同一个值），**不能用来排序**
    UpdatedAt       FILETIME（1601 起，100ns），**排序要用它**
    DeletedAt       软删除时间戳
    LastServerVersion / RemoteSchemaVersion / IsRemoteDataInvalid   同步状态

**纯只读**：拷贝副本再读（App 运行时原库被锁住），绝不写回。
写回 plum.sqlite 风险太高（App 有自己的缓存和 delta 状态，我们改了它不知道），
所以写入仍然走服务端。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 便笺 UWP 包的 LocalState 位置
PKG_DIRS = ("Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe",)


def find_db() -> Path | None:
    """找到 plum.sqlite。优先 LocalState，其次 RoamingState。"""
    local = os.environ.get("LOCALAPPDATA") or ""
    if not local:
        return None
    base = Path(local) / "Packages"
    for pkg in PKG_DIRS:
        for sub in ("LocalState", "RoamingState"):
            p = base / pkg / sub / "plum.sqlite"
            if p.exists():
                return p
    return None


def filetime_to_iso(x: Any) -> str:
    """plum.sqlite 的时间戳 → ISO 字符串。

    **基准是 0001-01-01，不是 1601-01-01。** 这踩过一次：
    值 `639248310940000000` 按 FILETIME（1601 起）换算会得到 **3626 年**，
    按 .NET Ticks（0001 起，单位同样是 100ns）换算才是 **2026 年**。
    便笺 App 是 .NET 写的，所以存的是 Ticks。
    """
    try:
        us = int(x) // 10
        dt = datetime(1, 1, 1) + timedelta(microseconds=us)
        if dt.year < 1990 or dt.year > 2200:      # 占位值/脏数据一律当无效
            return ""
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return ""


def read_notes(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """读全部未删除便笺。

    返回结构和 `RealGraph.list_notes()` 对齐：
        {"id", "text", "change_key", "modified", "modified_iso", "deleted", "folder_id"}
    """
    db = Path(db_path) if db_path else find_db()
    if not db or not Path(db).exists():
        raise FileNotFoundError("没找到 Windows 便笺的 plum.sqlite")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "plum.sqlite"
        shutil.copy2(db, tmp)
        # 便笺 App 正在运行时数据可能在 WAL 里，一并拷过来
        for suf in ("-wal", "-shm"):
            side = Path(str(db) + suf)
            if side.exists():
                shutil.copy2(side, Path(str(tmp) + suf))
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT Id, RemoteId, ChangeKey, ParentId, Text, "
                "       CreatedAt, UpdatedAt, DeletedAt "
                "FROM Note"
            ).fetchall()
        finally:
            con.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        # 软删除的跳过
        if r["DeletedAt"] not in (None, "", 0, "0"):
            continue
        text = r["Text"] or ""
        if not text.strip():
            continue
        # 优先用 RemoteId（跨设备稳定），本地新建还没同步的才回退到本地 Id
        nid = str(r["RemoteId"] or "").strip() or str(r["Id"] or "")
        upd = int(r["UpdatedAt"] or 0)
        out.append({
            "id": nid,
            "text": text,
            "change_key": str(r["ChangeKey"] or ""),
            "modified": upd,
            "modified_iso": filetime_to_iso(upd),
            "folder_id": str(r["ParentId"] or ""),
            "deleted": False,
        })
    # **按 UpdatedAt 倒序** —— CreatedAt 是占位值，用它排会乱（这个坑记在文档里）
    out.sort(key=lambda n: n["modified"], reverse=True)
    return out


def stats(db_path: str | Path | None = None) -> dict[str, Any]:
    """给前端显示用的概况"""
    notes = read_notes(db_path)
    years: dict[str, int] = {}
    for n in notes:
        y = (n.get("modified_iso") or "")[:4] or "?"
        years[y] = years.get(y, 0) + 1
    return {
        "count": len(notes),
        "newest": notes[0]["modified_iso"] if notes else "",
        "oldest": notes[-1]["modified_iso"] if notes else "",
        "by_year": dict(sorted(years.items())),
    }
