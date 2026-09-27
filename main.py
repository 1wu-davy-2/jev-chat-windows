# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
对方来了消息、且安静 5+1~5 秒没再发，才调一次 engine → 悬浮窗给 3 条候选 → 人点「填入」。
发送永远手动。静默期零调用——设置里开了「冷场开场白」才多一个触发点：最后一句是自己说的、
对方一直没回，等够时间起草一批开场白（见 arm_opener / start_opener，默认关）。
上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import multiprocessing
import queue
import random
import re
import threading
import time
import traceback
from collections import deque

from app import settings, update, voice, worker
from app.capture import find_wechat_hwnd
from app.fill import fill
from app.ocr import similar
from app.overlay import Overlay
from app.version import VERSION
from core import chatlog, trace
from core.engine import analyze, analyze_opener

# {会话名: {history, result, rev, target, senders}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
# phase 是派生出来的流水线状态（宠物换姿势、候选条显隐都看它），notify_until 是「刚来新消息」
# 那一下的截止时刻——用时间戳而不是布尔量，省得还要找地方把它清掉
# chat 是**我们盯着的**那个会话（固定模式下 = pin），screen 是微信屏幕上**当前开着的**那个：
# 平时两者一样，固定之后微信切走了就分家——消息按 screen 归户、建议只给 chat 出。
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "",
         "phase": "idle", "notify_until": 0.0, "voice": None, "pending": None,
         "screen": "", "pin": ""}
_NOTIFY_HOLD = 0.9  # 新消息到了先闪这么久「提醒」，再进判断
_VOICE_WINDOW = 20.0  # 点了「转文字」之后，子进程认「语音气泡下面的新文字」的窗口（秒）
_HISTORY_DUP = 24  # 判重时往回看这么多条（一屏大概也就十来条）
# 静默窗口：对方最后一条消息之后再等 5 + rand(1~5) 秒才问模型。连着发的几条（还有表情包、
# 语音这种读不出正文的）都算「还在说」，把窗口往后推；攒够一次问完，不然每条问一次既费钱，
# 判断看到的还是半句话。从第一条消息起最多等 _QUIET_MAX，免得对方一直发就一直不问。
_QUIET_MIN = 5.0
_QUIET_JITTER = 5.0
_QUIET_MAX = 30.0
# 开机从聊天记录库往回填多少：气泡最多 300（跟界面那份 _LOG_LINES 一个量级），
# 喂模型的 history 是 deque(maxlen=60)，多喂的也会被挤掉，所以按 60 截一下就行
_RESTORE_LINES = 300
_RESTORE_HISTORY = 60
_RESTORE_CHATS = 40  # 下拉框最多接回这么多个会话，免得历史一大堆时那个框长得没法用
results = queue.Queue()
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起


def chat_of(title):
    # nudge：冷场计时（到期时刻，None = 没在计时），nudge_at 是起算时刻（界面上说「对方 N 分钟没回」
    # 用它算），nudge_len 是起算时 history 记到哪儿了（到点只查这之后有没有对方的话），
    # nudged：这个冷场已经自动出过一批开场白了吗——对方回了话才清掉（见 schedule_analyze）
    # run_id：这个会话最近一轮在 AI 记录里是哪一行（「填入」时回填「我用了哪条」）
    return chats.setdefault(title, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": [], "run_id": None,
                                    "nudge": None, "nudge_at": 0.0, "nudge_len": 0,
                                    "nudged": False})


def tracked(title):
    """这个会话现在归不归我们管。固定模式下只有固定那一个算数（见 set_pin）。

    别的会话的消息照记（聊天记录、去重、回复对象名单都留着，切回去就是现成的），
    只是不点火、不弹提醒、不出建议——固定就是要它别跟着微信跑。"""
    pin = state["pin"]
    return not pin or title == pin


def _alias(title):
    """固定模式下 OCR 抖出来的别名（「白金搬砖小分队」↔「白金搬砖小分认」）并回固定那个名字。

    子进程那份「相似就并成一个名字」认的是它自己这一轮见过的名字，跨进程重开就没了；
    而固定那个名字是用户点按钮那会儿记下来的。不并的话，标题一抖就会被当成「微信开着
    别的会话」，固定这个会话的消息还记到另一个键上，面板里一条都看不见。

    **字数得一样**，比 ocr.similar() 自己那套更严：「张三」和「张三丰」的相似度有 0.8，
    但那是两个人。并错了比不并坏——不并最多是这个会话暂时读不进来（状态栏里写着微信开着
    哪个），并错了是拿别人的话替固定这个人起草。"""
    pin = state["pin"]
    if pin and title != pin and len(title) == len(pin) and similar(pin, title):
        return pin
    return title


def _already_read(chat, item):
    """这条是不是已经在会话记录里了。

    去重本来是子进程 Reader 的活儿，但那份状态会丢：子进程重开（微信关了又开）、
    标题 OCR 抖出一个没见过的名字，都会新起一个 Reader，把整屏当新消息重报一遍——
    白触发一次判断，记录里还多一份。父进程这边留着 history，再兜一道。"""
    who, _, text = item[:3]
    return any(w == who and similar(t, text) for w, t, _ in list(chat["history"])[-_HISTORY_DUP:])


def _voice_label(dur):
    """语音时长统一成 `N"` 的样子：OCR 读出来的原样可能是 `3"`、`3″`、`)3`、`3 (`，
    界面上别照搬，并语音那条也要靠这个字串对上号。认不出数字就原样返回。"""
    digits = re.sub(r"\D", "", dur or "")
    return f'{digits}"' if digits else (dur or "")


def schedule_analyze(title):
    """对方来了消息：先别问，把静默窗口往后推，等他不说了再问。

    等多久是随机的（5 + rand(1~5) 秒）：固定间隔会让「对方发完」和「我们开口」卡在同一个
    节拍上，看着像抢话。窗口到期由 tick() 去点火——那是唯一会动界面的地方。
    notify_until 一起推到那个时刻，这样等着的这几秒宠物保持「有新消息」的样子（角标在），
    而不是先闪一下再回到静息态。"""
    now = time.monotonic()
    chat = chat_of(title)
    chat["nudged"] = False  # 对方开口了 = 这个冷场结束，下一次冷场重新有一次机会
    chat["nudge"] = None
    pending = state["pending"]
    first = pending[2] if pending and pending[0] == title else now
    deadline = min(now + _QUIET_MIN + random.uniform(1.0, _QUIET_JITTER), first + _QUIET_MAX)
    state["pending"] = (title, deadline, first)
    state["notify_until"] = deadline


def arm_opener(title):
    """冷场计时：最后一句是我说的，等 opener_minutes() 分钟对方还没回，就起草一批开场白。

    每个冷场只自动出一次（chat["nudged"]），对方回了话才算下一个冷场——不然每来一条新消息
    都会重新点着一轮，等于没完没了地替他找话题。开关关着、采集停着、这个冷场已经出过，
    都什么都不做，返回 False（界面据此换一句状态栏文案）。"""
    if not title or not settings.opener() or not capture_on.is_set():
        return False
    chat = chat_of(title)
    if chat["nudged"]:
        return False
    now = time.monotonic()
    chat["nudge"] = now + settings.opener_minutes() * 60
    chat["nudge_at"] = now
    chat["nudge_len"] = len(chat["history"])  # 起算时记到哪儿了，到点只查这之后新记进来的
    return True


def mark_replied(title=None):
    """最新那条是自己说的（文字或语音）：等着的那次判断取消，提醒也一起收掉。

    对方那句已经答过了，再问一次模型既费钱又打扰人，结论还必然是「你已经回过了」。
    **自己发的语音也算**——语音读不出正文，但「我发过东西」这件事屏幕上看得见。
    顺手把冷场计时起上（设置里开了开场白的话），到期由 tick() 点火。"""
    state["pending"] = None
    state["notify_until"] = 0.0
    state["rerun"] = None
    ov.set_busy(False)
    if arm_opener(title):
        ov.set_status(f"你已回复，对方 {settings.opener_minutes()} 分钟没动静就给你起个头")
        return
    ov.set_status("你已回复，等待对方的新消息")


def set_pin(name):
    """固定盯着某个会话（name 为空 = 回到跟随）。界面上「当前会话」右边那个按钮、以及启动时
    读上次存的那个，都走这儿。

    固定：把盯着的会话换成它，在等着的那次判断要是别的会话的就收掉——那个结果回头贴到固定
    这个会话上就串了。回到跟随：盯着微信现在开着的那个（跟默认行为一样），要是它最后一句
    正好是对方说的，就照常走一遍静默窗口给建议（固定期间攒下的消息没点过火，这会儿补上）。

    设置存盘走 save_chat_pin（只改这一个键），跟宠物位置一个道理：拨一下按钮不该把两把 key
    重写一遍注册表。"""
    name = str(name or "")
    before, state["pin"] = state["pin"], name
    ov.set_pin(name)  # 先让界面知道现在的模式：下面 unpin 那支要靠它才会真的切过去
    if name:
        state["chat"] = name
        if state["pending"] and state["pending"][0] != name:
            state["pending"] = None
            state["notify_until"] = 0.0
    else:
        # 屏幕上还没认出来是谁就是空，等「chat」那一帧补上（别留着固定那个名字：
        # 那会儿已经没人盯着它了）
        state["chat"] = state["screen"]
        if state["pending"] and state["pending"][0] != state["screen"]:
            state["pending"] = None
            state["notify_until"] = 0.0
        if state["screen"]:
            ov.set_chat(state["screen"])  # 面板跟过去；这会儿已经是跟随模式了，所以它会真的切
            msgs = list(chat_of(state["screen"])["history"])
            if capture_on.is_set() and msgs and msgs[-1][0] == "her":
                schedule_analyze(state["screen"])
                ov.set_waiting()
                ov.set_status("已回到跟随，等他把话说完就给建议", "busy")
    if name != before:
        try:
            settings.save_chat_pin(name)
        except Exception:  # 存不下也不该把按钮点崩（config.json 只读之类）
            pass


def set_relation(title, key):
    """用户给某个会话挑了关系（面板上「当前会话」右边那个下拉）。

    **只写盘，不重跑**：手上那三条候选是上一个关系写出来的，改这个不该让它们作废——人家可能
    就是看着候选顺眼才顺手改的，一改就重跑既费钱又把手上的东西弄没了。下次生成（新消息、
    「换一批」、开场白）读到的就是新关系（analyze_bg / opener_bg 都是现读设置）。

    跟 set_pin 一个道理：拨一下就写盘，走 save_chat_relation（只改这一格），不用整份 save()。"""
    try:
        settings.save_chat_relation(title, key)
    except Exception:  # 存不下也不该把下拉点崩（config.json 只读之类）
        return
    ov.set_status(f"「{title}」按「{settings.relation_name(key)}」来写，下次生成生效。", "idle")


def set_scene(title, key):
    """用户给某个会话挑了场景模板（用另一型关系的正文说话，空串 = 跟随关系）。同上：只写盘。"""
    try:
        settings.save_chat_scene(title, key)
    except Exception:
        return
    note = f"「{settings.relation_name(key)}」那套口气" if key else "跟着「关系」走"
    ov.set_status(f"「{title}」的口吻改成{note}，下次生成生效。", "idle")


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def fill_reply(text):
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError("未找到聊天窗口，请确认已经打开")
    if state["area"] is None:
        raise RuntimeError("输入区域尚不可用，请确认聊天窗口可见（不要最小化）")
    if ov.current_chat() != ov.screen_chat():
        # 界面那边已经拦过一道（固定模式下按钮灰着、点候选条会给提示），这儿是最后一道：
        # 粘贴是直接发给微信当前会话的，「按会话隔离」在我们这儿成立、在它那儿不成立。
        raise RuntimeError(f"微信现在开着的不是「{ov.current_chat()}」，切回去再填")
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target:
            text = f"@{target} " + text  # 纯文本，微信不认成真正的 @，只是让群里看得出在跟谁说
    fill(state["hwnd"], state["area"], text)


def convert_voice():
    """用户点了候选条上的「转文字」：右键那条语音 → 点菜单第一项「语音转文字」。

    转出来的文字会作为一条新气泡出现在聊天区，被采集链路照常读到、照常触发判断——
    所以这里点完就完事，不用自己再去 OCR 一遍。返回 "" 表示成功，否则是给用户看的原因。"""
    if state["hwnd"] is None:
        return "未找到聊天窗口，请确认已经打开"
    if not capture_on.is_set():
        return "采集已暂停，先开启采集再转文字"
    if not state["voice"] or not state["voice"][1]:
        return "还没定位到那条语音，稍等一下再试"
    # 坐标是相对那一帧的，只有微信现在开着的就是这条语音所在的会话，点下去才落得准
    title = state["voice"][0]
    # 认的是**屏幕上开着**的那个（screen_chat），不是面板里摆着的那个：固定模式下面板钉在 A，
    # 微信可能早开到 B 了，照面板去点等于在 B 的聊天区乱点
    if title != ov.screen_chat() or title != ov.current_chat():
        return "微信现在开着的不是这条语音所在的会话，切回去再试"
    last = state["voice"][1][-1]  # 取最近的那条（气泡自上而下排）
    reason = voice.convert(state["hwnd"], last[:4])
    if not reason:
        # 点成了：给子进程开个口子。转出来的字是**插在那条语音气泡正下方**的，位置在
        # 「已知行」上面，不开口子会被当成往上翻出来的旧消息丢掉。只在这个窗口里认，
        # 不然往上翻、把窗口拉高时露出来的旧语音，底下那条老转写会被当成新消息报上去。
        voice_until.value = time.monotonic() + _VOICE_WINDOW
    return reason


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。"""
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on, voice_until),
                                daemon=True)
    p.start()
    return p


def set_debug(on):
    """调试视图开关：开 → 开窗 + 置位（子进程这才开始送帧，一帧 2~3MB）；关 → 清掉 + 收窗。"""
    global dbg
    if not on:
        debug_on.clear()
        if dbg is not None:
            dbg.hide()
        return
    if dbg is None:
        from app.debugwin import DebugWindow

        dbg = DebugWindow(on_close=on_debug_closed)
    dbg.show()
    debug_on.set()


def on_debug_closed():
    """用户直接关了调试窗 = 把开关也关了，否则设置页显示开着但没窗。"""
    debug_on.clear()
    ov.set_debug_switch(False)
    settings.save(debug_view_on=False)


def open_history():
    """标题栏的「AI 记录」：开过就复用同一个窗（列表、选中位置都还在），没有就现建。

    库按需初始化：设置里关着、也从没开过记录窗的话，硬盘上连 history.db 都不建。"""
    global hist
    trace.configure(settings.history_db())
    if hist is None:
        from app.historywin import HistoryWindow

        hist = HistoryWindow(db_path=settings.history_db())
    hist.show()
    hist.raise_()
    hist.activateWindow()


def restore_log():
    """开机把上次存的聊天记录接回来：界面的气泡 + 喂模型的 history 一起。

    **得在子进程起来之前、也在 set_pin 之前调**——set_pin 那一步会 _switch_to → 重画记录，
    这会儿 feeds 还是空的就白画了。接回来之后屏幕上还留着的那几屏会被 `_already_read` 当成
    旧消息挡掉：本来就读过，不该再记一遍（这正是以前没有库时的一个毛病）。

    **只回填内存、不写库**：库里本来就有，走 log_message 那条路会再插一遍，重启几次就翻几倍。"""
    if not settings.chatlog():
        return
    chatlog.configure(settings.chatlog_db())
    for title, _n in chatlog.chats(_RESTORE_CHATS):
        rows = chatlog.recent(title, _RESTORE_LINES)
        if not rows:
            continue
        chat = chat_of(title)
        for who, text, name, _stamp, _voice in rows[-_RESTORE_HISTORY:]:
            chat["history"].append((who, text, name or None))
        ov.restore(title, rows)


def set_chatlog(on):
    """「聊天会话存储」开关。拨一下**立刻生效**（跟调试视图、桌面宠物一个路子），顺手落盘。

    关掉只是不再往库里写，**已经存下的一条都不动**——要删去设置页点「清空聊天记录」。
    两件事分开：拨一下开关就把人家攒的记录抹了，那才叫坑。"""
    if on:
        chatlog.configure(settings.chatlog_db())
    else:
        chatlog.close()
    try:
        settings.save(chatlog_on=bool(on))
    except Exception:  # 存不下也不该把开关拨不动
        pass


def record_run(title, kind, trigger, result=None, exc=None):
    """把一轮写进 AI 记录。设置里关着就不写——记录是个旁路，一次都不该影响生成。

    result 是 engine 给的（里面带 trace），起草就挂了的话是异常，材料挂在 `exc.trace` 上
    （那种轮次恰恰最该查，所以照样落库）。写库失败返回的 None 也就是没有 id 可回填而已。

    **整个函数永不抛**：它是在 analyze_bg 的 except 分支里也被调用的，那儿一抛就没人往队列里
    放结果了，界面的 busy 会永远卡在「正在生成」，比丢一条记录严重得多。"""
    try:
        return _record_run(title, kind, trigger, result, exc)
    except Exception:
        return None


def _record_run(title, kind, trigger, result, exc):
    if not settings.history():
        return None
    # 库按需初始化：设置里一直关着、也从没开过记录窗的话，硬盘上连 history.db 都不建
    trace.configure(settings.history_db())
    info = dict((getattr(exc, "trace", None) or {}) if exc is not None else result.get("trace") or {})
    draft = info.get("draft") or {}
    judge_usage = info.get("judge_usage") or {}
    rank_usage = info.get("rank_usage") or {}
    draft_usage = draft.get("usage") or {}
    row = {
        "finished_at": int(time.time() * 1000),
        "chat": title, "kind": kind, "trigger": trigger,
        "relationship": info.get("relationship"), "scene": info.get("scene"),
        "context_n": info.get("context_n"), "messages": info.get("messages"),
        "ms": info.get("ms"),
        # 起草
        "draft_provider": draft.get("provider"),
        "draft_model": draft.get("model"), "draft_base_url": draft.get("base_url") or info.get("draft_base_url"),
        "draft_thinking": draft.get("thinking"), "draft_ms": draft.get("ms"),
        "draft_system": draft.get("system"), "draft_prompt": draft.get("prompt"),
        "draft_reply": draft.get("reply"), "draft_candidates": draft.get("candidates"),
        "draft_dropped": draft.get("dropped"),
        "draft_retry_prompt": draft.get("retry_prompt"), "draft_retry_reply": draft.get("retry_reply"),
        "draft_in": draft_usage.get("input_tokens"), "draft_out": draft_usage.get("output_tokens"),
        # 只会在起草那一步炸（判断和排序挂了都不抛，走 trouble），异常本身就是原因
        "draft_error": str(exc) if exc is not None else "",
        # 判断
        "judge_provider": info.get("judge_provider"), "judge_model": info.get("judge_model"),
        "judge_path": info.get("judge_path"), "judge_ms": info.get("judge_ms"),
        "judge_state": info.get("judge_state"), "judge_answers": info.get("judge_answers"),
        "judge_reply": info.get("judge_reply"), "judge_error": info.get("judge_error"),
        "judge_in": judge_usage.get("input_tokens"), "judge_out": judge_usage.get("output_tokens"),
        # 排序
        "rank_ms": info.get("rank_ms"), "rank_answers": info.get("rank_answers"),
        "rank_reply": info.get("rank_reply"), "rank_error": info.get("rank_error"),
        "rank_in": rank_usage.get("input_tokens"), "rank_out": rank_usage.get("output_tokens"),
        # 结果
        "candidates": (result or {}).get("candidates"), "best_index": (result or {}).get("best_index"),
        "scores": (result or {}).get("scores"), "judged": (result or {}).get("judged"),
        "ranked": (result or {}).get("ranked"),
        "trouble": (result or {}).get("trouble") or (str(exc) if exc is not None else ""),
    }
    run_id = trace.record(row)
    if run_id and result is not None:
        result["run_id"] = run_id  # tick 里存进 chats[title]，回头「填入」要拿它回填
    return run_id


def mark_used(index, action):
    """用户填了/复制了第几条候选：回填到那一轮上。只认「正在看着的会话 + 最近那轮」。"""
    title = ov.current_chat() or state["chat"]
    chat = chats.get(title) or {}
    text = ""
    result = chat.get("result") or {}
    cands = result.get("candidates") or []
    if 0 <= index < len(cands):
        text = cands[index]
    trace.mark_used(chat.get("run_id"), index, action, text)


def on_toggle_capture(on):
    """标题栏开关。启动时没找到微信就没有子进程，这会儿再找一次，找到了才真开得起来。"""
    global child
    if not on:
        capture_on.clear()
        for c in chats.values():  # 暂停期间不替你起头（暂停就是个「先别管我」的信号）
            c["nudge"] = None
        return
    if child is None:
        try:
            state["hwnd"] = find_wechat_hwnd()
        except RuntimeError:
            ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
            return
        child = spawn_worker()
    capture_on.set()


def analyze_bg(msgs, title, revision, reply_to=None, trigger="对方来新消息"):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。

    读设置整体挪进 try：在外面抛的话下面一条结果都不入队，busy 永远卡在 True。
    trigger 只进 AI 记录，说明这轮是谁点着的（新消息 / 用户换了回复对象）。"""
    try:
        # 起草和判断都可能是中转，那边地址只有一个（设置里共用），谁选中转就把它传给它
        provider, jev_provider = settings.draft_provider(), settings.jev_provider()
        relay_base = settings.relay_base_url()
        # 关系和口吻都**按会话现读**：面板上拨一下只写盘，下一轮生成（就是这儿）才用上。
        # 喂给模型的是关系的中文名（「朋友」「前女友」），判断那一步也看它。
        r = analyze(msgs, settings.relation_name(settings.relation_of(title)),
                    context=settings.context(),
                    model=settings.draft_model() or None,
                    provider=provider,
                    base_url=(relay_base if "relay" in (provider, jev_provider)
                              else settings.draft_base_url()) or None,
                    reply_to=reply_to, scene=settings.scene_text(title),
                    thinking=settings.thinking(),
                    thinking_style=settings.relay_thinking_style(),
                    judge_path=settings.relay_judge_path(),
                    jev_provider=jev_provider,
                    jev_model=settings.jev_model() or None)
        record_run(title, "reply", trigger, result=r)
        results.put(("ok", r, title, revision))
    except Exception as e:
        record_run(title, "reply", trigger, exc=e)  # 挂了的轮次也留痕，材料挂在 e.trace 上
        # 原文照发，别在这儿包一层套话：界面那条状态栏会截短显示，点「查看详情」看全文
        results.put(("err", str(e), title, revision))


def opener_bg(msgs, title, revision, waited, reply_to=None, trigger="冷场到点"):
    """起草一批开场白。跟 analyze_bg 一个套路：网络调用在线程里，结果丢队列，UI 只在 tick 里动。

    只打起草那个口——开场白不问 Jev（七道题问的都是「对方最新那条什么意思」，这儿对方没说话），
    所以只要起草那把 key，jev_* 那几项一概不传。waited 是「对方多少分钟没回」，界面拿它说人话；
    trigger 只进 AI 记录（冷场到点 / 用户点了「换一批」）。"""
    try:
        provider, jev_provider = settings.draft_provider(), settings.jev_provider()
        relay_base = settings.relay_base_url()
        r = analyze_opener(msgs, settings.relation_name(settings.relation_of(title)),
                           context=settings.context(),
                           model=settings.draft_model() or None, provider=provider,
                           base_url=(relay_base if "relay" in (provider, jev_provider)
                                     else settings.draft_base_url()) or None,
                           reply_to=reply_to, scene=settings.scene_text(title),
                           thinking=settings.thinking(),
                           thinking_style=settings.relay_thinking_style())
        r["waited"] = waited
        record_run(title, "opener", trigger, result=r)
        # 队列形状跟 analyze_bg 一样（kind, 结果, 会话名, 版本号）——是不是开场白看 r["opener"]，
        # tick 照它交给界面，不用再多一个槽位
        results.put(("ok", r, title, revision))
    except Exception as e:
        record_run(title, "opener", trigger, exc=e)
        # 原文照发，别在这儿包一层套话：界面那条状态栏会截短显示，点「查看详情」看全文
        results.put(("err", str(e), title, revision))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs, trigger="对方来新消息"):
    """trigger 只进 AI 记录：这一轮是谁点着的（新消息 / 用户换了回复对象）。"""
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg,
                     args=(msgs, title, chat_of(title)["rev"], reply_to, trigger),
                     daemon=True).start()


def start_opener(title, manual=False):
    """起草一批开场白（tick 到点自动点火，或者用户点了「换一批」/宠物菜单里的「生成开场白」）。

    跟 start_analyze 分开是因为它会问的东西不一样：只起草、不问 Jev，所以只要起草那把 key。

    上下文为空也照起草（刚加上的好友、对方只发过表情/图片，屏幕上一条文字都没有）：那会儿
    没有「上次」可接，要的就是一句先开口的招呼。waited 给 0，界面据此不摆「对方 N 分钟没回」。"""
    if not manual and not settings.opener():
        return
    if state["busy"]:
        ov.set_status("正在生成，稍等一下再换", "busy")
        return
    if not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    chat = chat_of(title)
    msgs = list(chat["history"])
    # 「对方 N 分钟没回」按计时起算那会儿算；没计过时（手动换一批）就用设置里那个数。
    # 空会话给 0：这个会话压根没聊过，说「对方几分钟没回」是编的
    waited = 0 if not msgs else (
        max(1, int(round((time.monotonic() - chat["nudge_at"]) / 60))) if chat["nudge_at"]
        else settings.opener_minutes())
    chat["nudged"] = True  # 这个冷场的一次机会用掉了
    chat["nudge"] = None
    state["busy"] = True
    ov.set_busy(True, opener=True, waited=waited)
    ov.set_status("正在给你想一句开场白…", "busy")  # set_busy 里那句是给正常流程写的
    reply_to = target_of(title) if settings.reply_target() else None
    threading.Thread(target=opener_bg,
                     args=(msgs, title, chat["rev"], waited, reply_to,
                           "用户换一批" if manual else "冷场到点"),
                     daemon=True).start()


def on_settings_saved():
    """设置保存后补一次冷场计时。

    用户可能就是**为了**这个开关才进的设置页，而屏幕上最后一句早就是他说过的了——那之后
    没有任何新消息，mark_replied 不会再被调一次，计时就永远起不来，看着像没生效。
    这里现看一眼记录：最后一条是我的话就把计时补上（存完设置才关掉开关的，arm_opener 会拦）。"""
    title = state["chat"]
    chat = chats.get(title)
    if not title or not chat or chat["nudge"] or chat["nudged"]:
        return
    if chat["history"] and chat["history"][-1][0] == "me":
        arm_opener(title)


def opener_again():
    """界面上的「换一批」。人在看别的会话时不给换：候选是那个会话的，填进去会串会话。"""
    title = state["chat"]
    if not title:
        ov.set_status("还没识别到会话，稍等一下再试", "warning")
        return
    if title != ov.current_chat():
        ov.set_status(f"现在看的是「{ov.current_chat()}」，切回「{title}」再换一批", "warning")
        return
    start_opener(title, manual=True)


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    state["pending"] = None  # 用户自己点的重跑，立刻跑，不等静默窗口
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        state["rerun"] = (title, msgs)
        ov.set_busy(True)
    else:
        start_analyze(title, msgs, "用户换了回复对象")


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind in ("chat", "lines", "voice", "noise"):  # 都带着会话名，先按固定那个名字归一
            msg = (kind, _alias(msg[1])) + tuple(msg[2:])
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话：界面按固定状态决定跟不跟；盯着的那个只有跟随模式才换
            state["screen"] = msg[1]
            ov.set_chat(msg[1])
            if not state["pin"]:
                state["chat"] = msg[1]
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "voice":  # 语音气泡：前一份给「转文字」用，后一份是新出现的，末位是「最底下那条谁说的」
            title, items, fresh, latest = msg[1], msg[2], msg[3], msg[4]
            state["voice"] = (title, items)
            ov.set_voice(title, items)
            # 新出现的可能不止一条，一条一行全记下来。是不是新的由子进程判（它有每会话的去重
            # 状态），这里只负责写；拖动/滚动只是坐标变，那边不会当新消息报上来
            for v in fresh:
                # 聊天记录里也留一行，不然这条语音在界面上等于不存在（只有显示用，
                # 不进喂模型的那份 history——「🔊 语音消息 3"」对模型是噪音）。
                # 谁发的按气泡底色走：自己发的语音别挂到对方头上
                ov.log_message(v[5], f"🔊 语音消息 {_voice_label(v[4])}", chat=title)
            if not tracked(title):
                continue  # 固定盯着别的会话：语音照记进那个会话的记录，但不提醒、不点火
            if fresh and latest == "me":
                mark_replied(title)  # 最新那条是我发的语音 = 我已经回了，跟回了句文字一样
            elif fresh:
                # 语音也是「对方还在说」：有等着的那次就把静默窗口往后推。没有就不新开一次
                # 判断——光一条语音没正文，问了也没得判断，等他自己转文字或者接着说
                if state["pending"] and state["pending"][0] == title:
                    schedule_analyze(title)
                else:
                    state["notify_until"] = time.monotonic() + _NOTIFY_HOLD
            continue
        if kind == "noise":  # 画面变了、却没认出新文字：多半是对方发了表情包/图片
            # 图片读不出正文，进不了上下文（对模型是噪音，也不该喂），但它说明对方还在发。
            # 有等着的那次就把窗口往后推，别在他还在发图的时候插嘴
            if state["pending"] and state["pending"][0] == msg[1]:
                schedule_analyze(msg[1])
            continue
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "dead":  # 采集彻底停了（微信关了之类），这才是真的要清状态
            state["area"] = None
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
                c["nudge"] = None  # 窗口都没了，冷场计时也就没意义（nudged 留着，别重开采集又出一次）
            state["rerun"] = state["pending"] = None
            state["notify_until"] = 0.0
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, area, latest = msg
        state["area"] = area
        chat = chat_of(title)
        fresh = [m for m in new if not _already_read(chat, m)]  # 屏幕上翻出来的旧消息不算数
        if not fresh:
            continue  # 整批都是旧的（子进程重开、往上翻、把窗口拉高）：不记不触发
        # 转文字出来的那几行要分两种：**对方那条**语音转出来的字算「他说了句话」（他确实说了，
        # 只是我们读不出声），照常点火；**自己那条**转出来的只是把我早说过的话补进记录，不是
        # 新消息——拿它点火会白问一次模型（真机上报过），拿它走 mark_replied 又会把对方刚来、
        # 正等着的那次判断给取消掉。所以这种一律不算数，只补 history 和聊天记录
        said = [m for m in fresh if not m[3] or m[0] == "her"]
        if said:
            chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
            if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
                ov.invalidate_replies()
        for who, name, text, dur in fresh:
            chat["history"].append((who, text, name))  # 喂模型的那份不带语音标记
            # dur 非空 = 这条是语音转出来的字，值是那条语音的时长：界面拿它并回「🔊 语音消息 N"」
            ov.log_message(who, text, name, chat=title, voice=_voice_label(dur) if dur else "")
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if not tracked(title):
            # 固定模式盯着别的会话：这些照记（history、去重、聊天记录、发言人名单都留着，
            # 切回去就是现成的），但不点火、不弹角标、不出建议——固定就是要它别跟着微信跑
            continue
        # fresh 里就是 new 里那几个元组本身，所以能按身份比：屏幕上最新那条是不是真新的
        if not said or said[-1] is not new[-1]:
            continue  # 只有转写/补记的是中间那条：流水线按屏幕最底下那条走，别动它
        # 只有「对方最新说话」才值得分析：最后一句是我说的（文字或语音）就不用问——
        # 自己发的语音也是我已经回了，见 mark_replied
        if said[-1][0] == "her" and latest != "me":
            schedule_analyze(title)  # 不立刻问：等他不说了再问（见 schedule_analyze）
            ov.set_status("对方刚发来消息，等他发完再给建议", "busy")
            ov.set_waiting()
        else:
            mark_replied(title)


def phase_now():
    """现在该是哪个形态。纯函数，只读既有事实，不记流水账——所以不会因为中间态而闪。

    scanning 是静息态（采集开着、在等对方说话），不是「此刻正在跑 OCR」：OCR 占了一帧
    85% 以上的时间，做成状态就是个常亮灯，没有信息量。"""
    if time.monotonic() < state["notify_until"]:
        return "notify"
    if state["busy"]:
        return "thinking"
    if not capture_on.is_set():
        return "idle"
    if ov.has_reply():
        return "ready"
    return "scanning"


def refresh_phase():
    """phase 的唯一写者，唯一调用点是 tick() 的出口。

    只在出口采样一次，drain() 中途一次都不调——这样 tick 里
    「busy=False 紧跟 start_analyze 又置 True」那一瞬间的中间态永远不会被界面看到。"""
    p = phase_now()
    if p != state["phase"]:
        state["phase"] = p
        ov.set_phase(p)


def tick():
    try:
        drain()
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not results.empty():
            kind, r, title, revision = results.get()
            state["busy"] = False
            if state["rerun"]:  # 分析期间用户又挑了回复对象，接着跑他挑的那份
                (t, msgs), state["rerun"] = state["rerun"], None
                start_analyze(t, msgs)
                continue
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
                continue
            if kind == "ok":
                chat = chat_of(title)
                chat["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                chat["run_id"] = r.get("run_id")  # 「填入」时回填到 AI 记录里的就是这一轮
                if title == ov.current_chat():
                    ov.show(r)
                else:
                    ov.set_busy(False)
            else:
                ov.set_busy(False)
                ov.set_error(r)  # 状态栏给一句短的，「查看详情」里是服务器返回的原文
        # 静默窗口到了才真去问。这期间攒下的消息早就在 history 里了（drain 一收到就记），
        # 所以这里现取一次，连着发的几条一起喂进去
        pending = state["pending"]
        if pending and not state["busy"] and time.monotonic() >= pending[1]:
            state["pending"] = None
            start_analyze(pending[0], list(chat_of(pending[0])["history"]))
        # 冷场开场白到点了（见 arm_opener）。这会儿得三条都成立才开口：开关还开着、人正开着
        # 这个会话、采集没停；而且**起算之后对方一句话都没说**——说了（哪怕因为去重没接住）
        # 就不是冷场了，硬起头会张冠李戴。只看起算之后新记进来的那几行，不回头翻历史：
        # 我自己用语音回的最后一句不进 history，回头翻会把它当成「对方后来发了话」而白作废。
        # 不成立就把这次作废，别留着下个 tick 再问一遍。
        for title, chat in list(chats.items()):
            if not chat["nudge"] or state["busy"] or time.monotonic() < chat["nudge"]:
                continue
            since = list(chat["history"])[chat["nudge_len"]:]
            if (settings.opener() and title == state["chat"] and capture_on.is_set()
                    and not any(who == "her" for who, _, _ in since)):
                start_opener(title)
            else:
                chat["nudge"] = None
            break
        refresh_phase()
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    debug_on = multiprocessing.Event()  # 同上，置位=子进程往队列里送整帧给调试窗
    # 点了「转文字」之后盖的时间戳（time.monotonic()，同机同基准），子进程拿它决定
    # 认不认「语音气泡下面的新文字」。见 convert_voice() 和 app/worker.py
    voice_until = multiprocessing.Value("d", 0.0)
    state["pin"] = settings.chat_pin()  # 上次固定在哪个会话（"" = 跟随微信切），下面照样摆到界面上
    state["chat"] = state["pin"]
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_voice_convert=convert_voice, on_opener_again=opener_again,
                 on_settings_saved=on_settings_saved, on_open_history=open_history,
                 on_use=mark_used, on_pin_change=set_pin,
                 on_relation_change=set_relation, on_scene_change=set_scene,
                 on_toggle_chatlog=set_chatlog,
                 result_of=lambda t: chats.get(t, {}).get("result"))
    restore_log()  # 先把上次的记录接回来，再摆固定会话、再开采集（顺序有讲究，见函数里）
    if state["pin"]:
        ov.set_pin(state["pin"])  # 面板先摆到固定那个会话上，等子进程读到微信开着谁再各归各位
    child = dbg = hist = None
    try:
        state["hwnd"] = find_wechat_hwnd()
    except RuntimeError:
            ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
    else:
        capture_on.set()
        child = spawn_worker()
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            child.terminate()
