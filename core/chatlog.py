# -*- coding: utf-8 -*-
"""聊天记录（本地 SQLite）：界面上那串气泡，重启后还在。

不存这份的话，一次重启就把攒下的几十轮全丢了——界面上那串气泡是内存里的，喂模型的
`chats[会话名]["history"]`（最近 60 条）也是内存里的，两个一起没。存下来之后**一份存储两处用**：
气泡直接按它重画，history 从它重建（所以重启后模型还接得上上次聊到哪儿）。

**跟 core/trace.py 是两码事，别混**：trace 记的是「AI 调用」——每一轮发了什么提示、模型回了
什么，给流程审计用；这边记的是**聊天本身**，给界面和上下文用。两张表的开关、清空入口、默认值
都各管各的，关掉 AI 记录不影响聊天记录还在。

**但字节是同一个文件**（`jev.db`，路径由调用方传进来）：以前是两个库（`chatlog.db` /
`history.db`），两个模块各带一套 configure/clear/size 管道，纯属重复；合起来只是「放哪儿」，
上面那些「各管各的」一条都没变。各建各的表（这边 `messages`、那边 `runs`），
**谁开谁建**——所以关着的时候硬盘上连这张表都没有，不是建个空壳子。

**隐私**：跟 trace 一样，这是把聊天原文写进磁盘的地方——设置里「聊天会话存储」默认
**源码跑开着、打包版关着**（`app/settings.chatlog()`），关掉就一次都不写，设置页里能看占用、
能一键清空。写库前每个字符串一律过 `redact_secrets()`，绝不落 key。

**留存**：只留最近 `RETENTION_DAYS` 天，开机 configure 的时候顺手清一次——不清理的话库会一直长。
按时间清而不是按条数：一个人很久没聊再回来，前面接得上比库小重要；但太老的对上下文也没用了。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .jev_client import redact_secrets
except ImportError:
    from jev_client import redact_secrets

RETENTION_DAYS = 90  # 只留最近这么多天；改这儿就改了开机那一次清理的口径

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat TEXT NOT NULL,      -- 会话名（OCR 读到的头部标题），就是 chats / feeds 的那个键
    who TEXT NOT NULL,       -- her / me
    text TEXT NOT NULL,
    name TEXT,               -- 群里的发言人，单聊为空
    stamp TEXT,              -- 界面上那个 "HH:MM"
    voice TEXT,              -- 非空 = 这条是语音转出来的字，值是那条语音的时长（3"）
    ts INTEGER NOT NULL      -- 落库时刻（epoch 秒），留存天数按它算
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat, id DESC);
-- 库自己的小账本（谁开谁建，trace 也要用）。现在只记「老库搬过来了没有」
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# 老版本那个库：合并之前聊天记录单独一个文件。升级上来的用户手上还有它，
# 首次开库把数据搬进 messages（见 _import_legacy），搬完老文件一个字节都不动
_LEGACY_DB = "chatlog.db"
_IMPORT_TAG = "imported_chatlog"

# 库路径 + 现在写不写。关掉之后路径**留着**：设置页要拿它算占用（关着也告诉你硬盘上还占多少），
# size() 也才有得看。空路径 = 从没配过（自测 / tools 里就是这样），写操作空转、读操作返回空
_DB = ""
_ON = False
_LOCK = threading.Lock()  # 写来自 Qt 主线程、读也在主线程，串一下最省心


def configure(path: str) -> None:
    """设库路径、建表、把老库搬进来、顺手清掉过期的。路径空 = 关掉（tools / 自测里跑不会凭空建库）。
    建表失败也不抛——记录是个旁路，磁盘满、文件被占都不该把主流程拖下水。

    同一个路径重复调用直接返回（调用方按需调它）。"""
    global _DB, _ON
    path = str(path or "")
    if not path:
        _ON = False  # 关掉：库文件留着，路径也留着，下次拨回来还是它
        return
    if path == _DB and _ON:
        return
    _DB, _ON = path, False
    try:
        with _LOCK, closing(_connect()) as c:
            c.executescript(_SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")  # 边写边读不打架；设一次就记在库里了
            c.commit()
        _ON = True
    except Exception:
        _DB = ""  # 建不出来就当没配过，别每次写都再失败一遍
        return
    _import_legacy()
    try:  # 留存：搬进来的老数据也一起过一遍，别让它们绕过清理
        with _LOCK, closing(_connect()) as c:
            c.execute("delete from messages where ts < ?", (_cutoff(),))
            c.commit()
    except Exception:
        pass


def _import_legacy() -> int:
    """把老版本那个 chatlog.db 里的记录搬进 messages，只搬一次（搬完在 meta 里记一笔）。

    **不能拿「messages 表空不空」当判据**：用户清空一次记录，下次启动又会把老数据搬回来。
    老库可能比新库少几列，所以只搬两边都有的列、缺的留空。整个过程一个事务，中途失败整体
    回滚、标记也不写，下次启动重来；老文件一个字节都不动（删数据该由用户自己确认过再做），
    所以重试是安全的。"""
    if not _DB or _imported():
        return 0
    old = os.path.join(os.path.dirname(_DB), _LEGACY_DB)
    if os.path.abspath(old) == os.path.abspath(_DB) or not os.path.exists(old):
        return 0
    try:
        with _LOCK, closing(_connect()) as c:
            c.execute("attach database ? as old", (old,))
            have = {r[1] for r in c.execute("pragma old.table_info(messages)")}
            cols = [x for x in ("id", "chat", "who", "text", "name", "stamp", "voice", "ts")
                    if x in have]
            if "id" not in cols:  # 没有 messages 表（或者根本不是我们的库）
                raise sqlite3.OperationalError("老库里没有 messages 表")
            names = ", ".join(cols)
            c.execute(f"insert into messages ({names}) select {names} from old.messages")
            n = int(c.execute("select changes()").fetchone()[0])
            c.execute("insert or replace into meta (key, value) values (?, ?)",
                      (_IMPORT_TAG, str(int(time.time()))))
            c.commit()
        return n
    except Exception:
        return 0


def _imported() -> bool:
    """这次搬迁做过了吗。读不出来一律当「做过了」——宁可漏搬一次，也别每次启动重搬一遍。"""
    try:
        with closing(_connect()) as c:
            return c.execute("select 1 from meta where key=?", (_IMPORT_TAG,)).fetchone() is not None
    except Exception:
        return True


def close() -> None:
    """关掉（设置里把开关拨回去时用）。库文件留着——关开关不等于删数据，删要走 clear()。"""
    global _ON
    _ON = False


def _cutoff() -> int:
    return int(time.time()) - RETENTION_DAYS * 86400


def _connect():
    c = sqlite3.connect(_DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _s(value) -> str:
    """入列的字符串：一律脱敏，None 当空串。"""
    return redact_secrets(str(value)) if value is not None else ""


def append(chat: str, who: str, text: str, name: str = "", stamp: str = "",
           voice: str = "") -> bool:
    """记一条。没配库返回 False。任何异常都吞掉——存记录不该影响采集和生成。"""
    if not _ON or not chat:
        return False
    try:
        with _LOCK, closing(_connect()) as c:
            c.execute("insert into messages (chat, who, text, name, stamp, voice, ts)"
                      " values (?, ?, ?, ?, ?, ?, ?)",
                      (_s(chat), _s(who), _s(text), _s(name), _s(stamp), _s(voice),
                       int(time.time())))
            c.commit()
            return True
    except Exception:
        return False


def merge_voice(chat: str, mark: str, who: str, text: str, dur: str) -> bool:
    """语音转出来的字并回它那条占位（界面那边 `Overlay._merge_voice()` 是同一件事）。

    按「会话 + 占位那句原文」从后往前找最近的一条，就地换成带时长的转写——**不改 id、
    不改时间**，所以重画出来的顺序跟原来一样，占位那条也不会剩下。"""
    if not _ON or not chat or not mark:
        return False
    try:
        with _LOCK, closing(_connect()) as c:
            cur = c.execute(
                "update messages set who=?, text=?, voice=? where id ="
                " (select id from messages where chat=? and text=? order by id desc limit 1)",
                (_s(who), _s(text), _s(dur), _s(chat), _s(mark)))
            c.commit()
            return cur.rowcount > 0
    except Exception:
        return False


def rewrite_tail(chat: str, rows, drop: int) -> bool:
    """把某个会话**最后 drop 条**换成 rows（rows 是界面那个五元组，时间正序，比 drop 多的
    就是新补进来的那几条）。

    给「重新识别」补漏用（见 main.apply_reread）。为什么不能像平时那样 append 一条：漏掉的
    消息往往要插在**中间**（真机上被吃掉的「?」「嗯」就是夹在别的消息之间的），而这张表是按
    自增 id 排的——append 只能往末尾加，重启之后顺序就乱了，喂给模型的上下文也跟着乱。
    所以把这一段尾巴整段删掉、按界面那份重插。

    drop 由调用方给而不是取 len(rows)：界面那份里还有「🔊 语音消息 N"」这种不进 history 的
    占位行，两边的条数对不上，自己猜会删多或删少。

    代价是这一段的 id 会换成新的（`ts` 也刷新成这会儿）。顺序、正文、时间、语音标记都以 rows
    为准，跟界面一模一样；id 变了对谁都没影响——它只是个排序用的自增号。"""
    rows = [tuple(r) for r in (rows or ())]
    drop = max(0, int(drop))
    if not _ON or not chat or not rows or not drop:
        return False
    try:
        with _LOCK, closing(_connect()) as c:
            ids = [r[0] for r in c.execute(
                "select id from messages where chat=? order by id desc limit ?",
                (_s(chat), drop)).fetchall()]
            if not ids:
                return False
            c.execute("delete from messages where id in (%s)" % ",".join("?" * len(ids)), ids)
            c.executemany("insert into messages (chat, who, text, name, stamp, voice, ts)"
                          " values (?, ?, ?, ?, ?, ?, ?)",
                          [(_s(chat), _s(w), _s(t), _s(n), _s(st), _s(v), int(time.time()))
                           for w, t, n, st, v in rows])
            c.commit()
            return True
    except Exception:
        return False


def replace_chat(chat: str, rows) -> bool:
    """把某个会话的记录**整段换成** rows（界面那个五元组，时间正序）。导入专用。

    跟 `rewrite_tail` 的差别只有一个：那个只换尾巴，这个从头换。导入的旧记录要按时间戳插到
    **现有记录中间甚至前面**，尾巴那一招够不着——所以整段删了重插。

    调用方（`main.import_history`）负责把新旧两批合并好、排好序再递进来；这里只管换。

    `rows` 为空不删——**「清空」是另一条路**（`clear_chat`）。传空就当调用方算错了，
    别把人家攒的记录顺手抹了。"""
    rows = [tuple(r) for r in (rows or ())]
    if not _ON or not chat or not rows:
        return False
    try:
        with _LOCK, closing(_connect()) as c:
            c.execute("delete from messages where chat=?", (_s(chat),))
            c.executemany("insert into messages (chat, who, text, name, stamp, voice, ts)"
                          " values (?, ?, ?, ?, ?, ?, ?)",
                          [(_s(chat), _s(w), _s(t), _s(n), _s(st), _s(v), int(time.time()))
                           for w, t, n, st, v in rows])
            c.commit()
            return True
    except Exception:
        return False


def recent(chat: str, limit: int = 300) -> list[tuple]:
    """某个会话最近的若干条，**按时间正序**（最老的在前，最新的在最后）——跟 feeds 一个方向。
    读不出来返回空表，界面照常开。"""
    if not _ON or not chat:
        return []
    try:
        with closing(_connect()) as c:
            rows = c.execute("select who, text, name, stamp, voice from messages"
                             " where chat=? order by id desc limit ?",
                             (_s(chat), int(limit))).fetchall()
        return [(r["who"], r["text"], r["name"] or "", r["stamp"] or "", r["voice"] or "")
                for r in reversed(rows)]
    except Exception:
        return []


def chats(limit: int = 40) -> list[tuple]:
    """有记录的会话名 + 各多少条，最近说过话的排最前。开机拿它把下拉框和记录填回来。"""
    if not _ON:
        return []
    try:
        with closing(_connect()) as c:
            rows = c.execute("select chat, count(*) as n, max(id) as last from messages"
                             " group by chat order by last desc limit ?", (int(limit),)).fetchall()
        return [(r["chat"], int(r["n"])) for r in rows]
    except Exception:
        return []


def count() -> int:
    if not _ON:
        return 0
    try:
        with closing(_connect()) as c:
            return int(c.execute("select count(*) from messages").fetchone()[0])
    except Exception:
        return 0


def size() -> int:
    """库文件多少字节（含 -wal / -shm，一起算才是真实占用）。文件不在返回 0。"""
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


def clear() -> bool:
    """清空全部聊天记录（库文件留着，省得跟 WAL 那两个附属文件打架）。"""
    if not _ON:
        return False
    try:
        with _LOCK, closing(_connect()) as c:
            c.execute("delete from messages")
            c.commit()
        return True
    except Exception:
        return False


def clear_chat(chat: str) -> bool:
    """只清一个会话（导入时勾了「替换现有记录」走这儿）。

    跟 clear() 分开是**故意的**：那个是设置页的「清空聊天记录」，一按全没；
    这个是导入时顺手清一个会话，不该顺手把别的会话也带走。"""
    if not _ON or not chat:
        return False
    try:
        with _LOCK, closing(_connect()) as c:
            c.execute("delete from messages where chat = ?", (_s(chat),))
            c.commit()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    # 自测（不联网、不碰真库）：临时目录里建一个，写读、并语音、留存清理、清空。
    import tempfile

    assert append("没配库", "her", "在吗") is False, "没 configure 就该是空操作"
    assert recent("x") == [] and chats() == [] and count() == 0 and size() == 0
    assert merge_voice("x", "占位", "her", "转写", '3"') is False and clear() is False

    path = os.path.join(tempfile.mkdtemp(prefix="jev-chatlog-"), "jev.db")
    configure(path)
    assert append("白金搬砖小分队", "her", "在吗", "阿杰", "18:40")
    append("白金搬砖小分队", "me", "在")
    append("白金搬砖小分队", "her", '🔊 语音消息 3"', "阿杰", "18:41")
    append("另一个会话", "her", "晚上吃啥")
    # 读回来是正序（最老的在前），五元组跟 feeds 一个形状；语音那条要能把时长带回来
    rows = recent("白金搬砖小分队")
    assert [r[1] for r in rows] == ["在吗", "在", '🔊 语音消息 3"'], rows
    assert rows[0] == ("her", "在吗", "阿杰", "18:40", ""), rows[0]
    # 占位那条的时长还写在正文里（「🔊 语音消息 3"」），voice 是空的——转出文字来才会填上
    assert rows[2][4] == "", rows[2]
    # 只取最近 N 条也是正序
    assert [r[1] for r in recent("白金搬砖小分队", 2)] == ["在", '🔊 语音消息 3"']
    # 会话列表：最近说过话的在前
    assert [c for c, _ in chats()] == ["另一个会话", "白金搬砖小分队"], chats()
    assert dict(chats())["白金搬砖小分队"] == 3

    # 并语音：占位那条就地换成转写、带上时长，条数不变、顺序不变
    assert merge_voice("白金搬砖小分队", '🔊 语音消息 3"', "her", "我三分钟后到", '3"')
    rows = recent("白金搬砖小分队")
    assert [r[1] for r in rows] == ["在吗", "在", "我三分钟后到"], rows
    assert rows[2][3] == "18:41", "并了要沿用语音那条的时间，不是转文字那会儿的时间"
    assert rows[2][4] == '3"' and count() == 4
    assert merge_voice("白金搬砖小分队", "根本没有这句", "her", "x", '1"') is False

    # 整段尾巴重写（重新识别补漏：漏掉的那条要插在中间，append 只能往末尾加）。
    # drop=2 = 换掉最后两条，rows 给三条 = 多出来的那条就是补进来的
    tail = recent("白金搬砖小分队")[-2:]
    assert rewrite_tail("白金搬砖小分队", [("her", "补在中间的", "", "18:41", "")] + tail, 2)
    rows = recent("白金搬砖小分队")
    assert [r[1] for r in rows] == ["在吗", "补在中间的", "在", "我三分钟后到"], rows
    assert rows[1][3] == "18:41" and len(rows) == 4, "重写不该多出或少掉这个会话的条数"
    # 同条数替换（只把某条读花的字改回来）也照做；空 rows / drop=0 / 没这个会话都不动手
    assert rewrite_tail("白金搬砖小分队",
                        [("her", "在吗？", "", "18:40", "")] + rows[1:], len(rows))
    assert [r[1] for r in recent("白金搬砖小分队")] == ["在吗？", "补在中间的", "在", "我三分钟后到"]
    assert rewrite_tail("白金搬砖小分队", [], 2) is False
    assert rewrite_tail("白金搬砖小分队", tail, 0) is False
    assert rewrite_tail("没这个会话", tail, 2) is False

    # 字符串一律脱敏：环境里那把 key 绝不许落到库里
    os.environ["JEV_API_KEY"] = "sk-secret-123456"
    append("x", "her", "401 with key sk-secret-123456 rejected")
    assert "sk-secret-123456" not in recent("x")[0][1], recent("x")[0][1]
    assert "[REDACTED]" in recent("x")[0][1]
    del os.environ["JEV_API_KEY"]

    # 形状兜底：**本机没配过的** key 也要抹掉。导入聊天记录是最常见的情形——
    # 粘进来那段对话里带的可能是别人的 key，按值那一步（上面）查不到，会一声不吭放行
    append("x", "her", "我的 sk-4cz3A03TGwVyMI0fyfNxe3PFCdy4bLItbC5CPoH2SAnpBTQI 你拿去用")
    got = recent("x")[-1][1]
    assert "sk-4cz3A0" not in got, got
    assert "[REDACTED]" in got, got
    # 正常中文和太短的 sk- 不能误伤
    append("x", "her", "晚上吃啥 sk-abc")
    assert recent("x")[-1][1] == "晚上吃啥 sk-abc", recent("x")[-1][1]

    # 按会话清：只动那一个，别的会话不碰（导入时勾「替换现有记录」走这条）
    assert recent("白金搬砖小分队"), "前提：这个会话里有东西"
    assert clear_chat("白金搬砖小分队") and recent("白金搬砖小分队") == []
    assert recent("另一个会话"), "别的会话不该被顺手带走"
    assert clear_chat("") is False and clear_chat("根本没这个会话") is True

    # 留存：超期的开机时清掉，没过期的留着
    old = os.path.join(tempfile.mkdtemp(prefix="jev-chatlog-old-"), "jev.db")
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT NOT NULL,"
                  " who TEXT NOT NULL, text TEXT NOT NULL, name TEXT, stamp TEXT, voice TEXT,"
                  " ts INTEGER NOT NULL)")
        c.execute("INSERT INTO messages (chat, who, text, ts) VALUES ('旧会话', 'her', '上辈子的', ?)",
                  (int(time.time()) - (RETENTION_DAYS + 1) * 86400,))
        c.execute("INSERT INTO messages (chat, who, text, ts) VALUES ('旧会话', 'her', '昨天的', ?)",
                  (int(time.time()) - 86400,))
    configure(old)
    assert [r[1] for r in recent("旧会话")] == ["昨天的"], "过期的该清掉，没过期的不能动"

    assert size() > 0 and human_size(size()).endswith(("KB", "MB"))
    assert clear() and count() == 0 and recent("旧会话") == []
    # 关掉之后写读都变成空操作，但库文件还在（关开关 != 删数据）
    close()
    assert append("x", "her", "y") is False and count() == 0 and size() > 0

    # 老库导入：合并之前聊天记录是单独一个 chatlog.db，升级上来的用户手上还有它。
    # 同 trace：只搬一次，判据不能是「messages 空不空」——清空一次记录再重启不能又搬回来
    legacy_dir = tempfile.mkdtemp(prefix="jev-chatlog-legacy-")
    old_db = os.path.join(legacy_dir, "chatlog.db")
    with sqlite3.connect(old_db) as c:
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                  " chat TEXT NOT NULL, who TEXT NOT NULL, text TEXT NOT NULL, ts INTEGER NOT NULL)")
        c.execute("INSERT INTO messages (chat, who, text, ts) VALUES ('老会话', 'her', '搬过来的', ?)",
                  (int(time.time()),))
    merged = os.path.join(legacy_dir, "jev.db")
    configure(merged)
    assert [r[1] for r in recent("老会话")] == ["搬过来的"], "老库的数据要搬进来"
    assert os.path.exists(old_db), "老文件不许动——删数据得用户自己确认过"
    clear()
    close()
    configure(merged)
    assert recent("老会话") == [], "清空之后重启，不能又把老数据搬回来一遍"
    print("chatlog ok")
