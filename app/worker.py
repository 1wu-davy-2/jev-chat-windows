# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR 头部会话名和消息 → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。"""
import ctypes
import time
import traceback

import numpy as np

from app.capture import Capture, chat_area, unminimize
from app.ocr import (_THOROUGH_SCALE, Reader, bottom_speaker, engine_params, read_title,
                     similar)

_TAIL_H = 150  # 「画面变了却没认出新文字」看的是消息区最底下这么高一条
_TAIL_DIFF = 0.08  # 变了这么多才算对方发了东西，鼠标划过高亮那点变化不算


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


def _packet(full, area, title, reader, lines, thorough=False):
    """调试视图的一帧：整帧缩到长边 ≤1100 再走队列（原帧 2560 宽裸传要 20MB），只在内存里传，不落盘。
    整数步长切片够用，不引新依赖；框和坐标照发原始值，画的那边按 scale 折算。"""
    k = max(1, -(-max(full.shape[:2]) // 1100))
    small = np.ascontiguousarray(full[::k, ::k])
    return {"w": small.shape[1], "h": small.shape[0], "rgb": small.tobytes(), "scale": k,
            "area": tuple(int(v) for v in area[:4]) if area else None,
            "pane_top": int(area[5]) if area else 0, "title": title,
            "boxes": reader.last_boxes if reader else [],
            # 每个框的判定依据（底色、众数占比、墨高、被分到哪一类）——调试窗那个「复制框数据」
            # 按钮拿它导出。只出数字和 OCR 读到的字，不出图也不写文件
            "metrics": reader.last_metrics if reader else [],
            # 参考字高和面板底色：判「当小字丢掉」要拿墨高跟 0.6×lh 比，没这两个数看不出差多少
            "lh": float(reader.lh or 0) if reader else 0,
            "pane_bg": tuple(int(v) for v in area[4]) if area else None,
            "lines": [(w, n, t) for w, n, t, *_ in lines],
            # 这一帧是不是「重新识别」那一遍（放大重读）。导出数据时要带上：不然分不清
            # 「改了参数没生效」和「改了没用」——同一串数字能贴两遍（真踩过）
            "thorough": bool(thorough),
            "engine": engine_params(),
            "ocr_ms": reader.last_ms if reader else 0, "ts": time.time()}


def run(q, hwnd, enabled, debug_on, voice_until=None, reread=None):
    """enabled 置位=采集，清掉=暂停。暂停时停掉 WGC 会话（Windows 那圈黄色采集边框也跟着没了），
    恢复时重开一个；readers 一直留着，去重状态不丢，恢复后不会把屏幕上的旧消息再报一遍。
    debug_on 置位才往队列里送整帧（一帧 2~3MB），关着一点额外活都不干。

    voice_until 是父进程在点了「转文字」之后盖的一个时间戳（time.monotonic()，两个进程
    同一台机器同一个基准）。只有在这个窗口里，才认「插在语音气泡下面的新文字」——
    不然往上翻、把窗口拉高时露出来的旧语音，底下那条老转写会被当成新消息报上去。

    reread 是个计数器：父进程点一次「重新识别」就 +1。见一个没见过的值就把这个会话的去重状态
    清掉、把手上那一帧**整屏**当新的报回去（kind 是 reread，不是 lines），父进程拿它跟记录对账。
    用计数不用时间窗口：点的时候画面多半是静止的，`settled()` 不会给帧，得走 `cap.snapshot()`。"""
    ctypes.windll.user32.SetProcessDPIAware()
    cap = None
    readers = {}  # {会话名: Reader}，一个会话一套去重状态
    title, head = "", None  # 当前会话名 / 上一帧的头部像素
    last_area = None  # 上次发给父进程的 4 元组，变了才再发一次
    last_voice = None  # 上次发过去的语音气泡，变了才再发（拖动/滚动时位置一直在变，别每帧刷）
    warned = False  # 消息区识别失败是否已经报过，拖窗口时别每帧刷一条
    tail = None  # 上一帧消息区最底下那条，用来认「画面变了却没认出新文字」
    done_reread = reread.value if reread is not None else 0  # 已经处理到哪一次了
    while True:
        if not enabled.is_set():
            if cap is not None:
                cap.stop()
                cap = None
                q.put(("paused",))
            enabled.wait()
            continue
        if cap is None:
            try:
                cap = Capture(hwnd)
            except Exception as e:
                q.put(("dead", "无法开始采集：" + (" ".join(str(e).split())[:120] or type(e).__name__)))
                enabled.clear()  # 自己清掉，下一圈就去等着，别一秒重试几十次
                continue
            q.put(("resumed",))
        if not cap.alive():
            break
        try:
            unminimize(hwnd)
            full = cap.settled()
            # 父进程点了「重新识别」：画面没变就没有新帧可交，拿手上最近那一帧来重读
            forced = reread is not None and reread.value != done_reread
            if forced:
                done_reread = reread.value
                if full is None:
                    full = cap.snapshot()
            if full is not None:
                reader, lines = None, []  # 调试视图要用，消息区没认出来时就是空的
                area = chat_area(full)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死
                if area is None:
                    if not warned:
                        q.put(("status", "消息区认不出来（窗口太小？）"))
                        warned = True
                else:
                    warned = False
                    cap.area = area  # 采集线程拿它做 diff
                    x0, y0, x1, y1, bg, y_pane = area
                    rect = (x0, y0, x1, y1)
                    if rect != last_area:
                        q.put(("area", rect))
                        last_area = rect
                    crop = full[y_pane:y0, x0:x1]  # 头部：会话名在这里
                    if head is None or not np.array_equal(crop, head):  # 名字没动就别白跑一次 OCR
                        head = crop
                        name = read_title(crop)
                        # OCR 抖一下（「小分队」↔「小分认」）不能分裂出一个新会话
                        name = next((k for k in readers if similar(k, name)), name) if name else ""
                        # ponytail: 认不出就沿用上次；开头就认不出给个占位名，总比把消息全丢了强
                        name = name or title or "当前会话"
                        if name != title:
                            title = name
                            tail = None  # 换了会话，底下那条没有可比性了
                            q.put(("chat", title))
                    reader = readers.setdefault(title, Reader())
                    # 用户点的那一次「重新识别」走放大重读（慢三四倍，但短消息认得准得多）
                    lines = reader.read(full[y0:y1, x0:x1], bg, thorough=forced)
                    # 帧是「画面变了」才交上来的，可变完却没认出新文字——多半是对方发了
                    # 表情包/图片（OCR 读不出正文），也可能是鼠标划过高亮。父进程拿它把静默
                    # 窗口往后推：对方还在发，这会儿问出来的是半句话。只看最底下那一条、
                    # 还要变化够大，鼠标悬停那点小高亮推不动它。比完就更新，跟认没认出新文字无关
                    bottom = full[max(y0, y1 - _TAIL_H):y1, x0:x1]
                    moved = (tail is not None and tail.shape == bottom.shape
                             and (bottom != tail).mean() > _TAIL_DIFF)
                    tail = bottom.copy()
                    # 只有刚点过「转文字」那一小会儿，才认插在语音气泡下面的新文字
                    converting = voice_until is not None and time.monotonic() < voice_until.value
                    if forced:
                        reader.reset()  # 整屏都当新的报上去，父进程那边跟记录对账
                    new = reader.new_lines(lines, under_voice=converting)
                    # 重读那一帧不算「新出现的语音」——这一帧整屏都是「新」的，报上去父进程会
                    # 把屏幕上每条语音再记一遍「🔊 语音消息 N"」
                    fresh_voice = [] if forced else reader.new_voices()
                    # 画面最底下那条是谁说的（文字和语音一起算）：父进程拿它判「最后说话的是不是我」，
                    # 自己发的语音也是「我已经回了」。放在 lines 里是因为「我方回复」能把 lines
                    # 刚点着的那次判断取消掉，它得跟 lines 同一批到
                    latest = bottom_speaker(lines, reader.last_voice)
                    if forced:
                        # 整屏（不是增量）。就算 `new` 是空的也要发：父进程得知道「重读过了，
                        # 一条都没多」，不然界面上那句「正在重新识别…」会一直挂着。
                        # 顺带把**被丢掉的**那些框报回去，**带上判定数字**：OCR 把「?」「嗯」这种
                        # 单字消息整条吃掉过，得知道是被哪一道关吃的、离阈值差多少。让用户去调试窗
                        # 抢那一帧不现实（每来一帧就刷新，点复制时多半已经被顶掉了，真踩过），
                        # 所以重读的结果自己带全，一次点击就能看
                        dropped = [(m["final"], str(m["text"]).strip(), m["rect"], m["flat"], m["ink"])
                                   for m in reader.last_metrics
                                   if m["final"] in ("image", "tiny", "gray")
                                   and str(m["text"]).strip()]
                        stats = {"boxes": len(reader.last_boxes), "lines": len(lines),
                                 "ms": reader.last_ms, "scale": _THOROUGH_SCALE}
                        q.put(("reread", title, new, rect, latest, dropped, stats))
                    elif new:
                        q.put(("lines", title, new, rect, latest))
                    elif moved:
                        q.put(("noise", title))
                    # 语音气泡：Reader 那边按坐标裁剪过，这里加回裁剪原点，父进程才好换算成屏幕坐标。
                    # 只发**还没转过文字**的（last_voice_open）——转过的就别再提示「转文字」了。
                    # 后面那份是本帧新出现的（可能不止一条），新出现必然让前一份也变，所以
                    # 下面这个「变了才发」不会把新语音漏掉
                    voice = tuple((x0 + a, y0 + b, x0 + c, y0 + d, t, k)
                                  for a, b, c, d, t, k in reader.last_voice_open)
                    if voice != last_voice or fresh_voice:
                        q.put(("voice", title, voice,
                               tuple((x0 + a, y0 + b, x0 + c, y0 + d, t, k)
                                     for a, b, c, d, t, k in fresh_voice), latest))
                        last_voice = voice
                if debug_on.is_set():
                    q.put(("debug", _packet(full, area, title, reader, lines, thorough=forced)))
        except Exception:
            _err(q)  # 一帧出错不退出
        time.sleep(0.05)
    q.put(("dead", "采集停了（聊天窗口关了？）"))
    try:
        cap.wait()  # 采集线程若是报错死的，这里把错抛出来
    except Exception:
        _err(q)
