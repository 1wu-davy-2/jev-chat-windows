# -*- coding: utf-8 -*-
"""AI 调用记录（本地 SQLite）：每一轮问了什么、模型回了什么、花了多少、最后用了哪条。

**为什么是 SQLite**：参考 cc-switch（`~/.cc-switch/cc-switch.db`）——它记的是
`proxy_request_logs`（每次请求的模型 / token / 成本 / 耗时 / 状态码 / 错误原文）加一张
`usage_daily_rollups` 按天聚合，**但请求和响应的正文一个字都不存**。我们要的是流程审计：
出了问题能翻回「那一轮它到底看到了什么、回了什么」，所以除了元数据，system / user 提示原文、
模型原始返回、被出口过滤扔掉的候选、Jev 的七道题答案也一并记。stdlib 自带 sqlite3，不加依赖。

**存哪儿**：跟 config.json 并排（路径由调用方 `configure()` 传进来——core/ 不认识 app/，
也不该认识）。没 configure 过就是空操作，tools/ 和自测里不会凭空建库。

**隐私**：库里是聊天原文的明文。设置里「记录 AI 调用」默认开，关掉就一次都不写；
「AI 记录」窗口里能看、能清空。所有字符串进库前一律过 `redact_secrets()`，绝不落 key。

**一张宽表**：每条记录就是一轮，不拆表不关联——审计要的是「一眼看完这一轮」，
join 出来的碎片反而难读。列名写死在 `_COLUMNS` 里，加字段就往那儿加一条。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .jev_client import redact_secrets
except ImportError:
    from jev_client import redact_secrets

# 库路径。空 = 没配（自测 / tools 里就是这样），所有写操作变成空操作、读操作返回空
_DB = ""
_LOCK = threading.Lock()  # 写来自后台线程、读来自 Qt 主线程，串一下最省心（一分钟也就几笔）

# 每一轮一行，列按「一轮里发生的顺序」排：谁触发的 → 发了什么上下文 → 起草 → 判断 → 排序 → 结果 → 人干了什么
_COLUMNS = (
    "created_at", "finished_at", "ms", "chat", "kind", "trigger",  # 什么时候、多久、哪个会话、哪种轮次
    "relationship", "scene", "context_n", "messages",        # 这一轮发出去的上下文
    # 起草（语言模型）
    "draft_provider", "draft_model", "draft_base_url", "draft_thinking", "draft_ms",
    "draft_system", "draft_prompt", "draft_reply", "draft_candidates", "draft_dropped",
    "draft_retry_prompt", "draft_retry_reply", "draft_in", "draft_out", "draft_error",
    # 判断（Jev 七道题）
    "judge_provider", "judge_model", "judge_path", "judge_ms",
    "judge_state", "judge_answers", "judge_reply", "judge_in", "judge_out", "judge_error",
    # 排序（Jev 第二次调用：哪条候选最合适）
    "rank_ms", "rank_answers", "rank_reply", "rank_in", "rank_out", "rank_error",
    # 最终结果 + 人最后用了哪条
    "candidates", "best_index", "scores", "judged", "ranked", "trouble",
    "used_index", "used_action", "used_text", "used_at",
)

# 存文本的那几列；其余一律 INTEGER（时间戳、耗时、token、下标、布尔）
_TEXT_COLUMNS = frozenset((
    "chat", "kind", "trigger", "relationship", "scene", "messages", "draft_provider",
    "draft_model", "draft_base_url", "draft_system", "draft_prompt", "draft_reply",
    "draft_candidates", "draft_dropped", "draft_retry_prompt", "draft_retry_reply",
    "draft_error", "judge_provider", "judge_model", "judge_path", "judge_state",
    "judge_answers", "judge_reply", "judge_error", "rank_answers", "rank_reply",
    "rank_error", "candidates", "scores", "trouble", "used_action", "used_text"))


def _col_type(name: str) -> str:
    return "TEXT" if name in _TEXT_COLUMNS else "INTEGER"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    %s
);
CREATE INDEX IF NOT EXISTS runs_time ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS runs_chat ON runs(chat, created_at DESC);
""" % ",\n    ".join(f"{c} {_col_type(c)}" for c in _COLUMNS)


def configure(path: str) -> None:
    """设库路径并建表。路径空 = 关掉记录（tools / 自测里跑不会凭空建库）。建表失败也不抛——
    记录是个旁路，磁盘满、文件被占都不该把主流程拖下水。

    同一个路径重复调用直接返回：调用方按需调它（设置里关了、也从没开过记录窗的话，
    硬盘上连库文件都不建），不该每次都重跑一遍建表。"""
    global _DB
    path = str(path or "")
    if path and path == _DB:
        return
    _DB = path
    if not _DB:
        return
    try:
        with _LOCK, closing(_connect()) as c:
            c.executescript(_SCHEMA)
            _migrate(c)
            c.execute("PRAGMA journal_mode=WAL")  # 边写边读不打架；设一次就记在库里了
            c.commit()
    except Exception:
        _DB = ""  # 建不出来就当没开，别每次写都再失败一遍


def _migrate(c) -> None:
    """补齐表里缺的列。

    这是一张「加字段就往 _COLUMNS 里加一条」的宽表，而用户手上的库是**上一个版本**建的；
    `CREATE TABLE IF NOT EXISTS` 不会动已有的表，缺列时 insert 会报 no such column，
    异常又被吞掉，表现成「一条记录都没有」（真踩过：加了 ms 列之后老库再也写不进去）。
    只补不删不改类型——加列是无损的，删列得重建整张表，不值当。"""
    have = {row[1] for row in c.execute("PRAGMA table_info(runs)")}
    for name in _COLUMNS:
        if name not in have:
            c.execute(f"ALTER TABLE runs ADD COLUMN {name} {_col_type(name)}")


def _connect():
    c = sqlite3.connect(_DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _cell(value):
    """入列的原始值 → SQLite 认的类型：dict/list 转 JSON、字符串一律脱敏。
    布尔不用单独管——bool 是 int 的子类，第一行就接住了，sqlite3 存成 1/0。"""
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return redact_secrets(str(value))


def record(row: dict) -> int | None:
    """写一轮，返回这行的 id（界面拿它回填「我填了哪条」）。没配库返回 None。

    接收的 dict 只认 `_COLUMNS` 里的键，多的忽略、少的留空——调用方不用凑齐所有字段，
    起草挂了的轮次就只有 error 那几列有值。任何异常都吞掉：记录不该影响生成。"""
    if not _DB or not row:
        return None
    data = {k: _cell(row.get(k)) for k in _COLUMNS}
    data["created_at"] = data["created_at"] or int(time.time() * 1000)
    cols = ", ".join(_COLUMNS)
    marks = ", ".join("?" * len(_COLUMNS))
    try:
        with _LOCK, closing(_connect()) as c:
            cur = c.execute(f"insert into runs ({cols}) values ({marks})",
                            [data[k] for k in _COLUMNS])
            c.commit()
            return int(cur.lastrowid)
    except Exception:
        return None


def mark_used(run_id: int | None, index: int, action: str, text: str = "") -> None:
    """人在界面上填了/复制了第几条候选。action ∈ {fill, copy}，index 是 candidates 里的原始下标。"""
    if not _DB or not run_id:
        return
    try:
        with _LOCK, closing(_connect()) as c:
            c.execute("update runs set used_index=?, used_action=?, used_text=?, used_at=? where id=?",
                      (int(index), action, _cell(text), int(time.time() * 1000), int(run_id)))
            c.commit()
    except Exception:
        pass


def recent(limit: int = 200, chat: str = "") -> list[dict]:
    """最近的记录，新的在前。chat 非空就只看那个会话。读不出来返回空表，界面照常开。"""
    if not _DB:
        return []
    sql = "select * from runs" + (" where chat=?" if chat else "") + " order by id desc limit ?"
    args = ([chat] if chat else []) + [int(limit)]
    try:
        with closing(_connect()) as c:
            return [dict(r) for r in c.execute(sql, args)]
    except Exception:
        return []


def latest_id() -> int:
    """最大 id，界面拿它判断有没有新记录（比每次重画整个列表便宜）。"""
    if not _DB:
        return 0
    try:
        with closing(_connect()) as c:
            return int(c.execute("select coalesce(max(id), 0) from runs").fetchone()[0])
    except Exception:
        return 0


def count() -> int:
    if not _DB:
        return 0
    try:
        with closing(_connect()) as c:
            return int(c.execute("select count(*) from runs").fetchone()[0])
    except Exception:
        return 0


def clear() -> bool:
    """清空全部记录（库文件留着，省得跟 WAL 的两个附属文件打架）。"""
    if not _DB:
        return False
    try:
        with _LOCK, closing(_connect()) as c:
            c.execute("delete from runs")
            c.commit()
        return True
    except Exception:
        return False


def size() -> int:
    """库文件多少字节（含 -wal，一起算才是真实占用）。文件不在返回 0。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(_DB + suffix)
        except OSError:
            pass
    return total


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(0, n) / 1024:.0f} KB"


if __name__ == "__main__":
    # 自测（不联网、不碰真库）：临时目录里建一个，写一轮、读回来、回填、清空。
    import tempfile

    assert record({"chat": "没配库"}) is None, "没 configure 就该是空操作"
    assert recent() == [] and count() == 0 and size() == 0 and latest_id() == 0

    path = os.path.join(tempfile.mkdtemp(prefix="jev-trace-"), "history.db")
    configure(path)
    rid = record({"chat": "白金搬砖小分队", "kind": "reply", "trigger": "对方来新消息",
                  "context_n": 10, "messages": [("her", "在吗"), ("me", "在")],
                  "draft_model": "deepseek-flash", "draft_candidates": ["甲", "乙", "丙"],
                  "draft_dropped": ["丙"], "draft_thinking": False,
                  "judge_answers": {"true_intent": {"choice": "casual_chat"}},
                  "ranked": True, "created_at": 1700000000000})
    assert rid, "写了应当拿到 id"
    rows = recent()
    assert len(rows) == 1 and rows[0]["id"] == rid and rows[0]["chat"] == "白金搬砖小分队"
    # dict / list 自动转 JSON，bool 转 0/1，元组也能存
    assert json.loads(rows[0]["messages"]) == [["her", "在吗"], ["me", "在"]], rows[0]["messages"]
    assert json.loads(rows[0]["draft_candidates"]) == ["甲", "乙", "丙"]
    assert rows[0]["draft_thinking"] == 0 and rows[0]["ranked"] == 1
    assert rows[0]["created_at"] == 1700000000000, "给了时间就用给的"
    # 没传的列留空，不报错
    assert rows[0]["draft_error"] is None and rows[0]["used_index"] is None

    mark_used(rid, 1, "fill", "乙")
    row = recent()[0]
    assert (row["used_index"], row["used_action"], row["used_text"]) == (1, "fill", "乙"), dict(row)
    assert row["used_at"] and row["used_at"] >= row["created_at"]

    # 会话过滤 + 新记录 latest_id 变化
    record({"chat": "另一个会话", "kind": "opener", "draft_error": "Jev HTTP 401"})
    assert latest_id() == recent()[0]["id"] and count() == 2
    assert len(recent(chat="白金搬砖小分队")) == 1

    # 字符串一律脱敏：环境里那把 key 绝不许落到库里
    os.environ["JEV_API_KEY"] = "sk-secret-123456"
    record({"chat": "x", "draft_error": "401 with key sk-secret-123456 rejected"})
    assert "sk-secret-123456" not in recent()[0]["draft_error"], recent()[0]["draft_error"]
    assert "[REDACTED]" in recent()[0]["draft_error"]
    del os.environ["JEV_API_KEY"]

    assert size() > 0 and human_size(size()).endswith(("KB", "MB"))
    assert clear() and count() == 0

    # 老库补列：拿「上一个版本建的表」再配一次，缺的列要自动补上、写读照常。
    # 少了这一步，加了新列之后老库会一条都写不进去（insert 报 no such column，还被吞掉）
    old = os.path.join(tempfile.mkdtemp(prefix="jev-old-"), "history.db")
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER,"
                  " chat TEXT, kind TEXT)")  # 只有最早那几列
        c.execute("INSERT INTO runs (created_at, chat, kind) VALUES (1, '老记录', 'reply')")
    configure(old)
    assert count() == 1, "老记录不能因为补列而丢"
    rid = record({"chat": "新记录", "kind": "opener", "ms": 123,
                  "draft_candidates": ["甲"], "ranked": True})
    assert rid and count() == 2
    row = recent()[0]
    assert row["chat"] == "新记录" and row["ms"] == 123 and row["ranked"] == 1
    assert json.loads(row["draft_candidates"]) == ["甲"]
    assert recent()[1]["chat"] == "老记录" and recent()[1]["ms"] is None

    # 库路径坏掉（目录不存在）不该抛：configure 之后 record 当没开
    configure(os.path.join(tempfile.gettempdir(), "没有这个目录-jev", "x.db"))
    assert record({"chat": "y"}) is None
    print("trace ok")
