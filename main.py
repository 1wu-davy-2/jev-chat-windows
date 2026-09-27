# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
对方来了消息、且安静 5+1~5 秒没再发，才调一次 engine → 悬浮窗给 3 条候选 → 人点「填入」。
发送永远手动。静默期零调用。
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
from core.engine import analyze

# {会话名: {history, result, rev, target, senders}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
# phase 是派生出来的流水线状态（宠物换姿势、候选条显隐都看它），notify_until 是「刚来新消息」
# 那一下的截止时刻——用时间戳而不是布尔量，省得还要找地方把它清掉
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "",
         "phase": "idle", "notify_until": 0.0, "voice": None, "pending": None}
_NOTIFY_HOLD = 0.9  # 新消息到了先闪这么久「提醒」，再进判断
_VOICE_WINDOW = 20.0  # 点了「转文字」之后，子进程认「语音气泡下面的新文字」的窗口（秒）
_HISTORY_DUP = 24  # 判重时往回看这么多条（一屏大概也就十来条）
# 静默窗口：对方最后一条消息之后再等 5 + rand(1~5) 秒才问模型。连着发的几条（还有表情包、
# 语音这种读不出正文的）都算「还在说」，把窗口往后推；攒够一次问完，不然每条问一次既费钱，
# 判断看到的还是半句话。从第一条消息起最多等 _QUIET_MAX，免得对方一直发就一直不问。
_QUIET_MIN = 5.0
_QUIET_JITTER = 5.0
_QUIET_MAX = 30.0
results = queue.Queue()
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": []})


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
    pending = state["pending"]
    first = pending[2] if pending and pending[0] == title else now
    deadline = min(now + _QUIET_MIN + random.uniform(1.0, _QUIET_JITTER), first + _QUIET_MAX)
    state["pending"] = (title, deadline, first)
    state["notify_until"] = deadline


def mark_replied():
    """最新那条是自己说的（文字或语音）：等着的那次判断取消，提醒也一起收掉。

    对方那句已经答过了，再问一次模型既费钱又打扰人，结论还必然是「你已经回过了」。
    **自己发的语音也算**——语音读不出正文，但「我发过东西」这件事屏幕上看得见。"""
    state["pending"] = None
    state["notify_until"] = 0.0
    state["rerun"] = None
    ov.set_busy(False)
    ov.set_status("你已回复，等待对方的新消息")


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
    if title != state["chat"] or title != ov.current_chat():
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


def on_toggle_capture(on):
    """标题栏开关。启动时没找到微信就没有子进程，这会儿再找一次，找到了才真开得起来。"""
    global child
    if not on:
        capture_on.clear()
        return
    if child is None:
        try:
            state["hwnd"] = find_wechat_hwnd()
        except RuntimeError:
            ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
            return
        child = spawn_worker()
    capture_on.set()


def analyze_bg(msgs, title, revision, reply_to=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。

    读设置整体挪进 try：在外面抛的话下面一条结果都不入队，busy 永远卡在 True。"""
    try:
        # 起草和判断都可能是中转，那边地址只有一个（设置里共用），谁选中转就把它传给它
        provider, jev_provider = settings.draft_provider(), settings.jev_provider()
        relay_base = settings.relay_base_url()
        results.put(("ok", analyze(msgs, settings.relationship(), context=settings.context(),
                                   model=settings.draft_model() or None,
                                   provider=provider,
                                   base_url=(relay_base if "relay" in (provider, jev_provider)
                                             else settings.draft_base_url()) or None,
                                   reply_to=reply_to, scene=settings.scene_text(),
                                   thinking=settings.thinking(),
                                   thinking_style=settings.relay_thinking_style(),
                                   judge_path=settings.relay_judge_path(),
                                   jev_provider=jev_provider,
                                   jev_model=settings.jev_model() or None),
                     title, revision))
    except Exception as e:
        # 原文照发，别在这儿包一层套话：界面那条状态栏会截短显示，点「查看详情」看全文
        results.put(("err", str(e), title, revision))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs):
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, chat_of(title)["rev"], reply_to),
                     daemon=True).start()


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
        start_analyze(title, msgs)


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            state["chat"] = msg[1]
            ov.set_chat(msg[1])
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
            if fresh and latest == "me":
                mark_replied()  # 最新那条是我发的语音 = 我已经回了，跟回了句文字一样
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
            mark_replied()


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
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
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
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_voice_convert=convert_voice,
                 result_of=lambda t: chats.get(t, {}).get("result"))
    child = dbg = None
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
