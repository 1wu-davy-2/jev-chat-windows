# -*- coding: utf-8 -*-
"""消息区截图 → 谁说了什么。RapidOCR 吃 numpy，不落盘。"""
import difflib
import re
import time

import cv2  # rapidocr 本来就带着它；「重新识别」放大消息区用（见 read 的 thorough）
import numpy as np
from rapidocr_onnxruntime import RapidOCR


_ENGINE = None

# 语音消息：气泡里只有「时长 + 喇叭图标」，没有正文。OCR 出来就是「8"」这种碎片，
# 不认的话它会被当成对方说的一句「8"」，白白触发一整套 Jev 判断（还花钱）。
#
# 拿时长后面那个引号当锚点：它在 13 次实测里每次都读得出来，只是会被读成 ( ) ? 等，
# 所以符号集要放宽。引号之后再放最多两个字符——喇叭图标偶尔会被读成一个字母
# （2.5 倍缩放下读成过「G」），不放过它就漏了。
#
# 必须有那个引号，纯数字的消息才不会被误伤：「6」「666」「88」「5G」「8点见」「8-9」
# 都不带引号，照常放行。
_VOICE = re.compile(r"\d{1,3}\s*[\"”“″＂'′’‘()（）?？!！|｜]{1,2}.{0,2}")

# 时长被读成光秃秃一个数字、引号整个丢掉的情况（真机上出现过「3"」→「3」）。
# 这种只靠文字认不出来——「3」也可能是真消息——所以得看像素：语音气泡里数字旁边
# 紧挨着喇叭图标，真发一个「3」旁边是空的。见 _has_icon()。
_DIGITS = re.compile(r"\d{1,3}")

# 喇叭图标本身被读成一个**后**括号/竖线、跑到时长前面：「3"」→「)3」（真机上出现过，
# 界面上就多出一条「)3」的假消息，还拿它去触发判断）。前面那个符号是图标的一部分，
# 所以不走 _has_icon——图标已经被并进这个框里了，框左边是空的，量不出来。
#
# 要求**整条**就是这个形状，且只认后括号：真消息里「)3」这种写法几乎没有，而「（3）」
# 「(3」这类前括号开头的很常见，别误伤（引号那一段可选，因为引号常常也一起丢）。
_VOICE_ICON = re.compile(r"[)）\]】|｜]{1,2}\s*\d{1,3}\s*[\"”“″＂'′’‘()（）?？!！|｜]{0,2}")

# 「撤回」提示：微信把「谁撤回了消息」画成一条居中的提示，OCR 照样读得出字，于是被当成一条
# 真消息。真机上报的是自己撤回那条——「你撤回了一条消息重新编辑」按底色被分成了 **her**
# （它那个白底提示跟对方的白气泡一个颜色），于是顶着「对方刚说」的名义白触发一次判断，
# 候选条上还写着「对方刚说 你撤回了一条消息重新编辑」，张冠李戴。
#
# 两种分开处理（`_is_recall` 返回 "me" / "her" / ""）：
#   ① 「你撤回了一条消息[重新编辑]」= 我自己撤的，等于屏幕上什么都没发生 → 整条丢掉；
#   ② 「"A 阿坤" 撤回了一条消息」= 对方撤的，那是「他本来要说、又收回去了」，算他说了话。
#
# **必须 fullmatch**：不然对方真发一句「你撤回了一条消息干嘛」就会被当成系统提示整条吃掉——
# 少一条真消息比多一条假消息难查得多。宁可漏拦，不能错拦。
_RECALL_ME = re.compile(r"^你\s*(?:撤回|收回)了一条消息\s*(?:重新编辑)?$")
# 名字段最多 24 个字符（单聊是「对方」，群里是昵称或「"昵称"」），且整条要以它收尾
_RECALL_HER = re.compile(r"^.{0,24}?(?:撤回|收回)了一条消息$")

# 「这一块底色就是面板底色」的容差（三通道差的绝对值之和）。真机上对方气泡是 (238,238,240)、
# 面板底是 (250,250,250)，差 34，分得开；同色系打光的那点抖动在 6 以内。见 read() 里那道关。
_PANE_TOL = 6

# 「这条语音上一帧也在同一个位置」的容差（像素）。画面没动时同一帧的坐标是逐像素稳定的，
# 留几像素给 chat_area() 每帧重新定位的抖动；真滚了一下（一次几十上百像素）就越过去了。
_STILL_PX = 4


def _is_recall(text):
    """这条是不是「撤回」提示：`"me"` = 我自己撤的（丢掉）、`"her"` = 对方撤的（算他说了话）、
    `""` = 不是。纯函数，不碰像素，自测直接打表验。

    为什么不看底色：提示是居中画的，可它的白底跟对方的白气泡几乎同色，`who_said` 分不出来
    （真机上就被判成了 her）。只能按文字认，而且认死了要 fullmatch——见上面两个正则的注释。"""
    t = (text or "").strip()
    if not t:
        return ""
    if _RECALL_ME.match(t):
        return "me"
    if _RECALL_HER.match(t):
        return "her"
    return ""


def _bubble_extent(chat, box, bg):
    """把整个气泡的范围扫出来——OCR 框只框住里面的字，气泡本身比它宽得多。

    语音气泡是个纯色圆角矩形，颜色就是 OCR 框里的众数色；从框中心往四个方向扩，
    扩到颜色不是气泡色为止。量这个是为了「转文字」按钮：它挂在气泡**外**侧，
    离气泡边缘的距离是固定的（实测 her：药丸中心在气泡右边 +48px），
    而离 OCR 框的距离会随语音长短变——气泡宽度是随时长变的。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    x0, x1 = int(min(xs)), int(max(xs))
    y0, y1 = int(min(ys)), int(max(ys))
    H, W = chat.shape[:2]
    pad = 3 * max(x1 - x0, y1 - y0) + 24
    lx, ty = max(0, x0 - pad), max(0, y0 - pad)
    rx, by = min(W, x1 + pad), min(H, y1 + pad)
    same = np.abs(chat[ty:by, lx:rx].astype(int) - bg).sum(axis=2) <= 12
    cy, cx = (y0 + y1) // 2 - ty, (x0 + x1) // 2 - lx

    def run(has, i):
        """从 i 往两边扩，返回连续为真的那段 [lo, hi]"""
        lo = hi = i
        while lo - 1 >= 0 and has[lo - 1]:
            lo -= 1
        while hi + 1 < len(has) and has[hi + 1]:
            hi += 1
        return lo, hi

    # 横向：拿文字框**上方**那一行去扫。不能扫文字框正中那几行——字本身就是气泡色以外的
    # 颜色，会把连续段从中间打断（踩过：量出来只有 11px 宽，正好是数字那一小截）。
    # 文字框顶再往上几像素那条横线，整条都还是气泡底色。
    row_i = max(0, min(y0 - 4, y1) - ty)
    lo, hi = run(same[row_i], cx)
    # 纵向：横向范围内只要还有气泡色就算在气泡里，这样文字挡不住
    t, b = run(same[:, max(0, lo):hi + 1].any(axis=1), cy)
    return lx + lo, ty + t, lx + hi, ty + b


def _has_icon(chat, box, kind, h):
    """数字旁边有没有那个喇叭图标。her 的图标在时长左边、me 的在右边（跟微信的排法一致）。

    比例是按真机截图量的（her：图标 11x16px，在数字左边 9px 处，数字本身 6x11px），
    所以下面这些系数都乘字高 h，换 DPI 时跟着缩放。"""
    if kind not in ("her", "me") or h <= 0:
        return False
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    x0, x1 = int(min(xs)), int(max(xs))
    y0, y1 = int(min(ys)), int(max(ys))
    pad = int(0.5 * h)
    if kind == "her":
        lo, hi = x0 - int(1.9 * h), x0 - int(0.3 * h)
    else:
        lo, hi = x1 + int(0.3 * h), x1 + int(2.4 * h)
    lo, hi = max(0, lo), min(chat.shape[1], hi)
    if hi - lo < 2:
        return False
    reg = chat[max(0, y0 - pad):y1 + pad, lo:hi].astype(int)
    if reg.size == 0:
        return False
    vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[cnt.argmax()]  # 这一条的底色（气泡底），不是整帧的
    ink = int((np.abs(reg @ [0.299, 0.587, 0.114] - bg @ [0.299, 0.587, 0.114]) > 60).sum())
    return ink >= max(10, int(0.9 * h))  # 图标实测有 60px 墨；真消息旁边那点气泡留白是 0


def _engine():
    """OCR 引擎全进程共用：一个实例 ~40MB，每个会话一个 Reader，不能各带一个。
    det_limit_type 默认 'min' 会把小图放大到短边 736，裁小反而更慢；必须 'max'。

    **intra_op_num_threads 必须留 1**（真机实测，同一块 726×967 的消息区）：
    4 线程 954ms wall / 3656ms cpu，2 线程 1765ms / 2938ms，1 线程 1903ms / **1688ms**。
    多线程不但没赚，CPU 还翻一倍多——识别那一趟是十几个小框挨个跑，onnxruntime 的线程池
    在两次推理之间**自旋等活**，等的那部分全白烧。而这块 CPU 是应用空闲时的主要开销：
    一次 OCR 就是 1.7~6.6 秒 CPU，来一条消息跑一次，机器上看着就是「CPU 一直二十几」。
    换 1 线程多花的那一秒（相对 4 线程）没人看得出来：起草前本来就要等 5~10 秒静默窗口。
    **别再调回多线程**，除非哪天量出来 ORT 不转自旋了。

    **别再随手调检测阈值**：`det_box_thresh` 试过 0.5 → 0.3，真机上一帧的框集合**一个没变**
    （10 个框，还是漏那两个）。限住短消息的是更上游的 `Det.thresh`（像素级二值化），
    要动它得先想清楚怎么验——详见 CLAUDE.md「技术坑」里那条未解决的。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOCR(intra_op_num_threads=1, det_limit_type="max", det_limit_side_len=4000)
    return _ENGINE


# TODO（未解决）：窗口字高小的时候（真机量到 lh = 12px），短消息会被整条漏掉——
# 「?」检测器一个框都没有、「嗯」只检出一个 13×13、墨高 0 的歪框（框里一个深色像素都没有，
# 说明框没落在字上）。两个都不是被 read() 那三道关过滤的，是**检测阶段就没框住**。
# read(thorough=True) 的放大重读是照这条路试的补丁，但**还没在真机上验出效果**（调试窗每来一帧
# 就刷新，用户很难抓到放大那一帧；现在的做法是让「重新识别」自己把框数报进聊天记录）。
# 证据、试过什么、下一步试什么，全在 CLAUDE.md 的「技术坑 → 未解决」那一条里。
_THOROUGH_SCALE = 2  # 「重新识别」把消息区放大这么多倍再 OCR，见 read(thorough=True)


def engine_params() -> dict:
    """引擎当前实际生效的几个参数。调试窗把它一起导出去——「改了参数没生效」和「改了没用」
    从结果上看一模一样，把参数写在数据里才分得清（这个坑踩过：同一串数字贴了两遍）。"""
    if _ENGINE is None:
        return {}
    return {"box_thresh": float(_ENGINE.text_det.postprocess_op.box_thresh),
            "text_score": float(_ENGINE.text_score),
            "thorough_scale": _THOROUGH_SCALE}


def _upscale(img, k):
    """把消息区放大 k 倍（双三次）。放大之后小字的笔画才够粗，检测器和识别器都稳得多。"""
    if k <= 1:
        return img
    return cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)


def read_title(header):
    """面板头部那一条截图 → 会话名（numpy RGB）。取最靠上的一行，同一行里取最左的
    （右边是图标按钮，OCR 不出字；下面那行是公告）。群聊的成员数「(422)」去掉，只留名字当 key。
    认不出返回 ""。一次约 60ms，所以调用方只在头部像素变了时才问。"""
    res, _ = _engine()(header, use_cls=False)
    if not res:
        return ""
    first = min(res, key=lambda r: r[0][0][1])
    row = first[0][0][1] + (first[0][2][1] - first[0][0][1])  # 框底：顶在这之上的算同一行
    text = min((r for r in res if r[0][0][1] < row), key=lambda r: r[0][0][0])[1]
    return re.sub(r"\s*[（(]\d+[)）]\s*$", "", text.strip())


def who_said(chat, box):
    """按 OCR 框里的颜色分类，不看 x 坐标。返回 (谁, 底色, 墨高, 众数占比)：
    先看底色平不平：框里众数颜色占比 <45% 就是图片（头像/照片/表情包）里的字 → None 丢掉。
    绿底 → me；非绿且文字对底色对比度 ≥150 → her；其余（引用块、群里的发言人名、时间戳、系统提示、
    链接卡片描述——都是灰字，对比度 80~95）→ "gray"。
    实测：气泡正文对比度 178~208，me 绿泡 142~150，灰字 ≤ 93。深浅主题都靠这套。
    墨高 = 框里最长一段连续有字的行数（OCR 框对小字有固定 padding、还会蹭到上下行，不能拿框高比大小）。

    第 4 个返回值（众数占比）**只有调试窗要**——见 read() 里的 last_metrics。判定的三条线全是照真机
    量出来的，出了「短消息被整条吃掉」这种问题，光看结果猜不出是哪条线卡的，得把数字摊开。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    reg = chat[int(min(ys)):int(max(ys)), int(min(xs)):int(max(xs))].astype(int)
    if reg.size == 0:
        return None, None, 0, 0.0
    vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    flat = float(cnt.max()) / reg.shape[0] / reg.shape[1]  # 众数颜色占这块框的比例
    if flat < 0.45:
        # 文字必须落在平底色上：WGC 帧是精确像素，气泡/面板里众数颜色占 0.56~0.82，
        # 头像/照片/表情包里只有 0.1~0.3——那是图片里的字（头像上的「借仲夏夜之梦」之类），不是消息。
        # ponytail: 只对精确像素的帧成立；缩放/压缩过的截图（比如拿预览窗再截一次的图）底色会糊成几百种颜色，全会被当图片。
        return None, bg, 0, flat
    diff = np.abs(reg @ [0.299, 0.587, 0.114] - bg @ [0.299, 0.587, 0.114])
    ink_h = best = 0
    for r in (diff > 60).any(axis=1):
        best = best + 1 if r else 0
        ink_h = max(ink_h, best)
    if bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40:
        return "me", bg, ink_h, flat
    return ("her" if diff.max() >= 150 else "gray"), bg, ink_h, flat


def bottom_speaker(lines, voices):
    """画面最底下那条是谁说的，`"me"` / `"her"`；一条都没认出来就是空串。

    文字和语音**一起算**——微信新消息在最下面，所以「最底下那条」就是这条会话里最后说的一句话。
    父进程拿它判「最后说话的是不是我」：自己发的语音也是「我已经回了」，对方那句早就答过了，
    不该再触发一次判断（以前只拿文字判，所以对方来一句、自己回两条语音，照样会问一次）。
    lines 是 read() 的返回（`[(who, name, text, y, 横向中心)]`），voices 是 last_voice（`[(x0,y0,x1,y1,时长,谁)]`）。"""
    items = [(y, who) for who, _, _, y, *_ in lines] + [(v[1], v[5]) for v in voices]
    return max(items)[1] if items else ""


def _index_of(hay, needle):
    """needle 作为**连续一段**在 hay 里第一次出现的下标，没有就 -1（给 new_voices 对齐用）。"""
    n = len(needle)
    return next((i for i in range(len(hay) - n + 1) if hay[i:i + n] == needle), -1)


def similar(a, b):
    """同一段像素挪个位置 OCR 会抖（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」），按相似度判同一条。"""
    if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1  # 短句错一个字


class Reader:
    """一个会话一个 Reader：lh/seen 各自算各自的，切走再切回来不会把旧消息当新的重报一遍。"""

    def __init__(self):
        self.ocr = _engine()
        self.lh = None  # 正常气泡字高，头一帧定
        self.seen = []  # [(who, name, text)]，累计，封顶 500
        self.last_boxes = []  # 调试视图用：[(x0,y0,x1,y1,kind,text)]，消息区裁剪坐标
        self.last_voice = []  # 这一帧的语音气泡 [(x0,y0,x1,y1,时长,谁)]，_voice_above 要用全部
        self.prev_voice = []  # 上一帧的同一样东西，_still 拿它判「这条语音是不是一直挂在屏幕上」
        self.last_voice_open = []  # 上面那些里**还没转过文字、也还没被更新的消息压过去**的，见 _open_voices
        self._w = 0  # 消息区宽度，read() 每帧更新：判「这一行跟那条语音同不同侧」要用
        self.seen_voice = []  # 已经报给父进程的语音 [(谁, 时长)]，按出现顺序攒着，new_voices 要用
        self.last_ms = 0  # 上一帧 OCR 耗时
        self.last_metrics = []  # 调试窗「列出每个框的数据」用，见 read()

    def read(self, chat, pane_bg, thorough=False):
        """→ [(who, name, text, y)]，同一气泡的多行已合并。who ∈ me/her；name 群聊里是发言人，单聊 None。
        顺带把每个框的分类记进 self.last_boxes（调试视图画框用，几十个 tuple，不开也不亏），
        以及每个框的判定依据 last_metrics（弹出问题时要看数字，光看结论猜不出是哪条线卡的）。

        thorough=True 时先把整块放大 `_THOROUGH_SCALE` 倍再 OCR：这条只给用户手动点的
        「重新识别」走。字高只有 12px 上下时，检测器对小字号、孤零零一个字的短消息很不稳
        （真机上「?」一个框都检不出、「嗯」检出一个墨高 0 的歪框），放大是最直接的补救。
        **代价是慢三四倍**，所以正常的实时采集绝不能用它。框回来之后坐标折回原尺度，
        下游（气泡范围、图标位置、调试窗）一律还按原图算。"""
        t0 = time.perf_counter()
        k = _THOROUGH_SCALE if thorough else 1
        # 只传 use_cls：RapidOCR.__call__ 收到**任何** kwargs 就会把 self.text_score 重置成它的
        # 默认 0.5（`if kwargs:` 那一支），构造时配的 text_score 会被悄悄抹掉
        res, _ = self.ocr(_upscale(chat, k), use_cls=False)
        self.last_ms = int((time.perf_counter() - t0) * 1000)
        self.last_boxes = []
        self.last_metrics = []
        # 上一帧那串转成「再上一帧」，这一帧从空开始收：_still 判的是**上一帧**在不在，
        # 拿新的当旧的比就永远成立，等于没这道闸
        self.prev_voice, self.last_voice = self.last_voice, []
        self.last_voice_open = []
        W = chat.shape[1]
        self._w = W  # 给 _same_side 用（判转写和语音同不同侧）
        # 群聊：每条 her 气泡上方一行灰色发言人名（靠左、短、不带冒号、印在面板底色上），从上往下扫，名字带给后面的气泡。
        # 引用块/时间戳/公告带冒号，链接卡片灰字印在气泡底色上，都不会被当成名字。
        # ponytail: 名字行被 OCR 漏掉时会挂到上一个人头上。
        name, raw = None, []
        for box, text, _ in sorted(res or [], key=lambda r: r[0][0][1]):
            if k > 1:  # 放大过就把框折回原尺度，后面一律按原图算
                box = [(x / k, y / k) for x, y in box]
            kind, bg, h, flat = who_said(chat, box)
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            rect = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
            # 判定依据：出问题时把这一串摊给用户复制（调试窗那个按钮），数字比结论有用
            m = {"rect": rect, "kind": kind or "", "text": text, "flat": flat, "ink": h,
                 "bg": None if bg is None else tuple(int(v) for v in bg), "final": kind or ""}
            self.last_metrics.append(m)
            rec = _is_recall(text)
            if rec == "me":
                # 自己撤回的：屏幕上等于什么都没发生。不进 raw（不点火、不进上下文），也不进
                # 聊天记录——记了界面上就多一条「你撤回了一条消息」的假气泡，看着像对方说了话
                m["final"] = "recall_me"
                self.last_boxes.append(rect + ("recall_me", text))
                continue
            if rec == "her":
                # 对方撤回的：算他说了句话。**两道颜色关都绕过去**——提示的白底跟对方的白气泡
                # 同色，who_said 可能判成 her 也可能判成 gray；字号又比气泡小，后面「小字」
                # 那道也会吃它。所以这两关都不走，直接照 her 落进 raw。
                m["final"] = "recall_her"
                self.last_boxes.append(rect + ("recall_her", text))
                raw.append(("her", None, text, box[0][1], box[2][1], h, min(xs), max(xs)))
                continue
            if (_VOICE.fullmatch(text.strip()) or _VOICE_ICON.fullmatch(text.strip())
                    or (_DIGITS.fullmatch(text.strip()) and _has_icon(chat, box, kind, h))):
                # 不进 raw（它没有正文，触发判断没意义），但位置记下来：
                # 用户点「转文字」时要悬停到它上面。存的是**整个气泡**的范围，
                # 不是这个 OCR 框——按钮挂在气泡外侧，得按气泡边缘算。见 app/voice.py
                m["final"] = "voice"
                self.last_boxes.append(rect + ("voice", text))
                self.last_voice.append(_bubble_extent(chat, box, bg) + (text, kind))
                continue
            if kind == "gray":
                on_pane = np.abs(bg - pane_bg).sum() <= _PANE_TOL
                taken = bool(on_pane and box[0][0] < 0.25 * W and len(text) <= 16
                             and not re.search("[:：]", text))
                if taken:
                    name = text
                m["final"] = "name" if taken else "gray"
                self.last_boxes.append(rect + (m["final"], text))
                continue
            if (kind == "her" and bg is not None
                    and np.abs(bg - pane_bg).sum() <= _PANE_TOL):
                # 印在**面板底色**上的字不是消息——真消息都落在气泡里（上面 gray 那一支认群里
                # 发言人名用的就是这条，判据取一样）。真机上报过：自己发的一张表情包（图里
                # 写着「想你」，画在面板底色上，字号还不小）被读成对方说的一句「想你」，
                # 还照它起草了一次——底色不绿、也不是对方气泡色，对比度又够，一路判成 her。
                m["final"] = "pane"
                self.last_boxes.append(rect + ("pane", text))
                continue
            if kind is None or (self.lh and h < 0.6 * self.lh):
                # 字比正常气泡小得多 = 图片消息（截图/表情包）里的字，不是气泡
                m["final"] = "image" if kind is None else "tiny"
                self.last_boxes.append(rect + (m["final"], text))
                continue
            self.last_boxes.append(rect + (kind, text))
            raw.append((kind, name if kind == "her" else None, text, box[0][1], box[2][1], h,
                        min(xs), max(xs)))
        if not self.lh and len(raw) >= 3:
            self.lh = float(np.median([r[5] for r in raw]))
        # 同一气泡的多行合并：同人、上一行底到这一行顶的间距不到半个字高（不同气泡之间至少隔一个字高）
        # 顺带把这几行的横向范围攒成一条（left/right），第 5 个返回值要用它判这一行在左半边还是右半边
        lines = []
        for who, nm, text, top, bottom, h, left, right in raw:
            if lines and lines[-1][0] == who and lines[-1][1] == nm and top - lines[-1][4] < 0.6 * (self.lh or h):
                lines[-1][2] += text
                lines[-1][4] = bottom
                lines[-1][5] = min(lines[-1][5], left)
                lines[-1][6] = max(lines[-1][6], right)
            else:
                lines.append([who, nm, text, top, bottom, left, right])
        out = [(w, n, t, y, (lo + hi) / 2) for w, n, t, y, _, lo, hi in lines]
        self.last_voice_open = self._open_voices(out)
        return out

    def _open_voices(self, out):
        """这一帧里还算数的语音气泡：**下面没有更新的文字消息**的那些。

        文字那一路本来就有「只认已知行下方的」这道闸（见 new_lines 的 floor），语音这一路一直
        缺——补在这儿。少了它，往上翻、切会话时露出来的老语音会被当成刚发来的：真机上报过，
        切过去没认出最底下那两条新消息，反而把顶上翻出来的老语音记成了「对方最近说」，
        候选条上还摆着它的「转文字」。`new_voices` 报哪几条也是按它筛的（`fresh` 只在
        last_voice_open 里挑），所以记录里也不会平白多出两行旧的。

        已经转过文字的语音一并被这条挡住：转出来的字就贴在气泡正下方，它比气泡新，所以
        转完不会再提示一次「转文字」（以前是靠「紧贴的那一行」单独判的，现在同一条规则管了）。

        代价：一帧里「先一条语音、紧跟一条文字」时那条语音不记了（宁可漏记一行占位，
        也不能把压在上面的老语音当成刚发来的）。整屏一条文字都没有（只发语音的会话）就都留着。"""
        tail = max((t[3] for t in out), default=None)
        return [v for v in self.last_voice if tail is None or v[1] > tail]

    def new_voices(self):
        """这一帧里**新出现、还没转过文字**的语音气泡——父进程拿它往聊天记录里记一行
        「🔊 语音消息 N"」。可能不止一条，所以返回列表。

        以前是父进程按「这一帧比上一帧条数多」判，而且只记 `items[-1]` 那一条。两个毛病：
        一帧里冒出两条（刚启动那帧整屏都是新的、或者对方连发两条）就少记一条；往上翻多出来
        一条时，又会把翻出来的旧语音当新消息记一遍。真机上报上来的「我回了两条语音，记录里
        只有一条」就是前者。

        判据改成看**顺序**：语音只会从底下加进来、从顶上滚出去，顺序永远不变。拿报过的那串
        在眼前这串里找**最长的一段**（取第一次出现的位置），它后面剩下的才是新的。往上翻时
        报过的那段落在后面，后面就没剩什么，天然不算新消息。

        在 last_voice（全部）里找位置、但只报 last_voice_open 里的：转过文字的那条会从 open
        里消失，拿 open 找位置会中间缺一格对不齐；而已经转过文字的那条也不该再记一行占位
        ——它自己的转写会带着时长进记录。"""
        keys = [(v[5], v[4]) for v in self.last_voice]
        at = 0
        for length in range(min(len(self.seen_voice), len(keys)), 0, -1):
            found = _index_of(keys, self.seen_voice[-length:])
            if found >= 0:
                at = found + length
                break
        if not at and self.seen_voice:
            # 报过的一条都没在眼前（翻远了、窗口换了一块）：认不出哪条是新的，宁可漏记一行，
            # 也不能把翻出来的旧语音当新消息记上去。seen_voice 不动，下一帧重新对。
            return []
        self.seen_voice.extend(keys[at:])
        del self.seen_voice[:-500]
        left = list(self.last_voice_open)  # 同一时长有好几条时按顺序一条条销
        fresh = []
        for v in self.last_voice[at:]:
            if v in left:
                left.remove(v)
                fresh.append(v)
        return fresh

    def _same_side(self, voice, x):
        """这一行跟那条语音在不在同一侧（左半边 / 右半边）。

        转写是画在语音气泡正下方、跟它同侧的；普通消息则可能跟语音分属两个人（一左一右）。
        拿不到横向位置就不拦（自测里手搓的行没有 x）——这道闸是给误判打的补丁，
        宁可退回老行为，也别把真转写挡在外面。"""
        if x is None or not self._w:
            return True
        return (x > self._w / 2) == ((voice[0] + voice[2]) / 2 > self._w / 2)

    def _voice_above(self, y, x=None):
        """这一行紧跟在哪个语音气泡下面？返回那条语音（`(x0,y0,x1,y1,时长,谁)`），没有就是 None。

        微信的「语音转文字」是**插在那条语音气泡正下方**的，不是追加到聊天末尾——转一条老语音，
        结果落在「已知行」上面，按下面那条「只认已知行下方的」规则会被当成往上翻出来的旧消息丢掉，
        用户转完了应用却根本没看见。所以这一类单独放行。

        `x` 是这一行的横向中心（read() 算出来的）：还得跟那条语音**同一侧**才算数。只按「紧贴」
        判的话，跟在语音下面的**普通消息**会被当成它的转写——真机上报过：对方一条「是的」正好
        压在我那条 3" 语音下面，被记成了我语音转出来的字（谁说的、时长全错，进上下文的那份也错）。"""
        for item in self.last_voice:
            if 0 <= y - item[3] <= 2.5 * (self.lh or 17) and self._same_side(item, x):
                return item
        return None

    def _still(self, item):
        """这条语音上一帧是不是也挂在屏幕上的**同一个位置**。

        用来分开两件几何上一模一样、只有时间不同的事：**用户刚在微信里右键转了一条老语音**
        （气泡一直在那儿，转出来的字刚插到它下面 —— 要认），和**往上翻/把窗口拉高时连带滚出来
        的一条老语音 + 它底下早就转过的字**（两个都是刚露出来的 —— 不能认）。差别就在气泡
        上一帧在不在：前者在，后者不在。

        容差按像素给：画面没动时同一帧里坐标是逐像素稳定的，留几像素给 chat_area() 定位抖动。
        真的滚了一下（一次滚轮几十上百像素）就越过容差，那道闸照样拦得住。"""
        x0, y0, x1, y1, dur, who = item
        return any(abs(p[0] - x0) <= _STILL_PX and abs(p[1] - y0) <= _STILL_PX
                   and abs(p[2] - x1) <= _STILL_PX and abs(p[3] - y1) <= _STILL_PX
                   and p[4] == dur and p[5] == who for p in self.prev_voice)

    def new_lines(self, lines, under_voice=False):
        """去重（滚动不重复）→ 这一帧里真正新出现的 [(who, name, text, 语音时长或 "")]。
        本帧有已知行时只要已知行下方的：往上滚翻出来的旧消息在已知行上方，不算。
        例外是语音转出来的字（见 _voice_above）：它就插在语音气泡下面，位置在已知行上方。

        那个例外只在两种情形下开（其余一律还按 floor 拦）：
        ① **这条语音上一帧就在屏幕同一个位置**（`_still`）——用户在微信里自己右键转的老语音
           走这条。真机上报过：手动转的那条 3" 一直没进记录，因为那道口子当时只认「刚点过
           我们那个按钮」，而用户是在微信里自己转的。
        ② **刚点过我们那个「转文字」**（`under_voice`）——见 worker.run()。留着它是为了兜住
           「转完微信顺手把画面往上推了一格」：那会儿气泡位置变了，① 判不出来。
        两个都关着还开的话，往上翻/把窗口拉高时露出来的旧语音，底下那条老转写也会被当成
        新消息报上去，白触发一次判断（判断不便宜，还打扰人）。

        第四个字段只给界面用（聊天记录里标/并「语音」那条）：判据就是「紧贴在某个语音
        气泡下面」，跟放不放它过 floor 无关——最新那条语音转出来的字是走正常规则进来的，
        同样得标上。实测转写贴 17px、下一条普通消息隔 69px，2.5×lh 分得开。
        转写那行的 **who 也以那条语音为准**，不看气泡底色（微信把转写画成灰白气泡，
        自己的语音转出来也长这样），而且还得跟那条语音**同一侧**——不然跟在语音下面的
        普通消息会被当成它的转写，见 _voice_above。

        本帧一行已知的都没有（大图把旧文字全顶出去了、切了聊天、滚远了）：全算，宁可多算不能漏。
        ponytail: 同一人连发两句一模一样的会吞一句——对触发分析无害。
        lines 是 read() 的返回（`[(who, name, text, y, 横向中心)]`；自测里手搓的行只给前四个）。"""
        known_y = [y for w, n, t, y, *_ in lines if self._seen(w, n, t)]
        floor = max(known_y) if known_y else -1
        new = []
        for w, n, t, y, *rest in lines:
            v = self._voice_above(y, rest[0] if rest else None)
            under = (v[5], v[4]) if v is not None else ()
            allow = v is not None and (self._still(v) or under_voice)
            if not (y > floor or allow) or self._seen(w, n, t):
                continue
            # 紧跟语音气泡的那行是转写：谁说的、时长多少都以**那条语音**为准，别看气泡底色
            new.append((under[0] if under else w, n, t, under[1] if under else ""))
        self.seen.extend((w, n, t) for w, n, t, *_ in lines if not self._seen(w, n, t))
        del self.seen[:-500]
        return new

    def _seen(self, who, name, text):
        # 名字不参与判重：名字行滚出画面后同一条消息会从 her(LO) 变成 her，不能算新消息
        return any(w == who and similar(t, text) for w, _, t in self.seen)

    def reset(self):
        """把「见过哪些行」清掉：下一帧整屏都当新的报上去。

        给界面上那个「重新识别」用（见 worker.run 的 reread）——平时绝不能调，一调就等于把
        屏幕上所有消息重报一遍。只清 seen：floor 跟着没了（known_y 为空 → floor = -1），
        每行都放行。**语音那份状态不动**（seen_voice / last_voice），不然屏幕上那几条语音会被
        当成新出现的再记一遍「🔊 语音消息 N"」。"""
        self.seen = []


def reconcile(have, screen):
    """把刚重读出来的整屏跟记录里已有的对一遍：哪些是漏掉的、哪些是同一条被读花了。

    have / screen 都是 [(who, text)]，两边都按时间正序。have 传**记录的尾巴**（屏幕上那几屏
    多半落在这几条里），screen 是刚读出来的整屏。

    返回 (adds, fixes)：
      adds  = [(插在 have 的第几条**之后**, who, text)]，屏幕上认得出、记录里没有的
      fixes = [(have 里的下标, 新正文)]，同一条消息（同一个人 + similar 判同）但这次字不一样

    **只加不删**：屏幕只显示最后那几屏，记录里比它多的那些是往上翻出去的老消息——不在这屏上
    不等于不存在，删了才是真丢。所以对齐出来的 delete 段一律跳过。

    配对从**后往前**：OCR 抖出来的差异是 1:1 的，从后往前配才不会因为中间多出一条就整段错位
    （往前配的话第一条对不上，后面全跟着错）。"""
    adds, fixes = [], []
    a = [(str(w), str(t)) for w, t in have]
    b = [(str(w), str(t)) for w, t in screen]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("equal", "delete"):
            continue  # delete = 翻出去了，不动
        block = []
        for n, j in enumerate(range(j2 - 1, j1 - 1, -1)):
            i = i2 - 1 - n
            if i >= i1 and a[i][0] == b[j][0] and similar(a[i][1], b[j][1]):
                block.append(("fix", i, b[j]))
            else:
                block.append(("add", max(i1, i + 1), b[j]))
        for kind, at, item in reversed(block):  # 上面是倒着扫的，正过来
            if kind == "fix":
                fixes.append((at, item[1]))
            else:
                adds.append((at, item[0], item[1]))
    adds.sort(key=lambda x: x[0])  # 调用方从后往前插，得按位置排好
    fixes.sort()
    return adds, fixes


if __name__ == "__main__":
    def voice(t):
        return bool(_VOICE.fullmatch(t.strip()))

    # 实测读出来的样子（前三个是这台机器上跑出来的），加同族推断
    for t in ('8"', '8"(', '8 (', '8" G', '8" ', '8 "', '8?', '8”', "8'", '12"', '60"',
              '100"', '8!', '8（', '8)', '8｜', '8"( '):
        assert voice(t), t
    # 真消息不能误伤：纯数字、单独的问号、带数字的正常短句
    for t in ("8", "666", "88", "2024", "?", "？", "在吗", "好", "8点见", "8 点", "8-9",
              "5G", "8G", '8" 屏幕', "3D", "8楼"):
        assert not voice(t), t

    # 喇叭图标被读成后括号、跑到时长前面（真机上出现过「3"」→「)3」）：界面上会多出一条
    # 假消息，还拿它去触发判断
    for t in (')3', ')3"', '）3', '｜3', ') 3', ')3“', '))3', ']12'):
        assert _VOICE_ICON.fullmatch(t.strip()), t
    # 前括号开头的不认（「（3）」「(3」这种真消息比「)3」常见），带正文的更不认
    for t in ("（3）", "(3", "3)", "好)3", ")3点见", "）3楼", "3", "8"):
        assert not _VOICE_ICON.fullmatch(t.strip()), t

    # 撤回提示：自己撤的整条丢掉（真机上报的就是这条，被当成「对方刚说」白触发一次判断），
    # 对方撤的算他说了句话
    for t in ("你撤回了一条消息", "你撤回了一条消息重新编辑", "你撤回了一条消息 重新编辑",
              " 你撤回了一条消息 ", "你收回了一条消息"):
        assert _is_recall(t) == "me", t
    for t in ("对方撤回了一条消息", '"A 阿坤" 撤回了一条消息', "阿坤撤回了一条消息",
              "A 阿坤 撤回了一条消息", '"张三"撤回了一条消息', "对方收回了一条消息"):
        assert _is_recall(t) == "her", t
    # **必须 fullmatch**：对方真发一句带「撤回了一条消息」的话，不能被当成系统提示整条吃掉
    # （少一条真消息比多一条假消息难查得多），也不能把「撤回了别的什么」当提示
    for t in ("你撤回了一条消息干嘛", "你撤回了一条消息吗？", "我撤回了一个想法",
              "撤回", "你撤回", "他撤回了一条消息然后呢", "", "  ",
              "你撤回了一条消息" + "啊" * 40, "撒回了一条消息"):
        assert _is_recall(t) == "", repr(t)

    # read() 里的接线（光验正则不够）：撤回提示得真的被放过去/拦下来，而且要绕过那两道颜色关。
    # 拿 object.__new__ 绕过 __init__，免得为这几条断言把 40MB 的 OCR 引擎加载起来
    def _reader(res, lh=13.0):
        r = object.__new__(Reader)
        r.ocr = lambda img, **kw: (res, None)
        r.lh = lh
        r.seen = []
        r.last_boxes, r.last_voice, r.last_voice_open, r.seen_voice = [], [], [], []
        r.prev_voice = []
        r.last_ms, r.last_metrics = 0, []
        return r

    def _frame(ink=13, w=400, h=300, bg=238):
        """气泡底 + 框中间一条**细**深色横带当字：众数色 = 气泡底（flat 过半）、墨高 = ink。
        带子不能填满框——填满了众数色就变成深色，墨高只剩上下两道白边那两行，
        于是被「小字」那道当成 tiny 吃掉（第一版就这么写错了）。

        底色**必须跟面板底色不一样**（下面 `_pane`）：真机上她的气泡是 (238,238,240)、
        面板底是 (250,250,250)。两者一样的话会被「印在面板上的字」那道拦下——那道是给
        表情包里的字用的，见 read()。"""
        f = np.full((h, w, 3), bg, np.uint8)
        f[20:20 + ink, 10:200] = 40  # 框是 y 10~40，带子只占中间 13 行
        return f

    _box = [(10, 10), (200, 10), (200, 40), (10, 40)]
    _pane = (250, 250, 250)  # 面板底色（跟气泡底 238,238,240 差 34，见 read() 里那道关）

    # 自己撤回的：整条不进结果——不点火、不进上下文、也不记聊天记录
    r = _reader([(_box, "你撤回了一条消息重新编辑", 0.99)])
    assert r.read(_frame(), _pane) == [], "自己撤回的不能被当成一条消息"
    assert r.last_boxes[-1][4] == "recall_me", r.last_boxes
    assert r.last_metrics[-1]["final"] == "recall_me", r.last_metrics

    # 对方撤回的：算 her 说了句话。下面这两条都得成立——
    # ① 走 readonly 的 her 分支（不是被当灰字/小字丢掉）
    r = _reader([(_box, '"A 阿坤" 撤回了一条消息', 0.99)])
    assert r.read(_frame(), _pane) == [("her", None, '"A 阿坤" 撤回了一条消息', 10, 105.0)], \
        "对方撤回的要留下"
    assert r.last_boxes[-1][4] == "recall_her", r.last_boxes
    # ② 系统提示本来就比气泡字小（lh 抬到 40，墨高 13 够不着 0.6*lh），「小字」那关也不许吃它
    r = _reader([(_box, "对方撤回了一条消息", 0.99)], lh=40.0)
    assert r.read(_frame(), _pane) == [("her", None, "对方撤回了一条消息", 10, 105.0)], \
        "不能被「小字」那道吃掉"

    # 真消息照旧：普通气泡还是一条 her（别把新加的那道拦宽了）。
    # 第 5 位是这一行的横向中心（框 10~200），_same_side 拿它判转写跟语音同不同侧
    r = _reader([(_box, "在吗", 0.99)])
    assert r.read(_frame(), _pane) == [("her", None, "在吗", 10, 105.0)]

    # 印在**面板底色**上的字不是消息（真消息都落在气泡里）。真机上报过：自己发的一张
    # 表情包图里写着「想你」，画在面板底色上、字号还不小，被读成对方说的一句「想你」，
    # 还照它起草了一次。底色不绿、也不是对方气泡色，对比度又够，一路判成 her
    r = _reader([(_box, "想你", 0.99)])
    assert r.read(_frame(bg=250), _pane) == [], "印在面板底上的字（表情包里的字）不能当消息"
    assert r.last_boxes[-1][4] == "pane" and r.last_metrics[-1]["final"] == "pane"

    # 「重新识别」的放大重读：坐标要能折回原尺度（框是放大后给的，下游一律按原图算）
    assert _upscale(np.zeros((10, 20, 3), np.uint8), 1).shape == (10, 20, 3)
    assert _upscale(np.zeros((10, 20, 3), np.uint8), 2).shape == (20, 40, 3)
    assert _THOROUGH_SCALE >= 2, "只放一倍对 12px 的小字不够，检测器照样不稳"

    # 重读之后跟记录对账：漏掉的补进来、读花了的就地改、翻出去的老消息一条都不许删
    _have = [("her", "在吗"), ("me", "在"), ("her", "周末爬山去不去")]
    assert reconcile(_have, [("her", "在吗"), ("me", "在")]) == ([], []), "一模一样就不动"
    # 屏幕上多出一条（就是漏掉的那种，真机上「?」「嗯」这种单字被吃掉过）
    assert reconcile(_have, [("her", "在吗"), ("me", "在"), ("her", "?"),
                             ("her", "周末爬山去不去")]) == ([(2, "her", "?")], [])
    # 同一条被读花了 → 改，不新增；两边都按「最后一条」对，中间多一条也不会整段错位
    assert reconcile(_have, [("her", "在吗"), ("me", "在"), ("her", "周末爬山去不去啊")]) \
        == ([], [(2, "周末爬山去不去啊")])
    # 记录里比屏幕多 = 往上翻出去了 → 一条都不删
    assert reconcile(_have, [("her", "周末爬山去不去")]) == ([], [])
    # 屏幕上最新那条记录里没有 → 补在最后
    assert reconcile(_have, _have + [("her", "嗯")]) == ([(3, "her", "嗯")], [])
    # 换人了就不算同一条：同样一句「在吗」，她说和我说是两条
    assert reconcile([("her", "在吗")], [("me", "在吗")]) == ([(1, "me", "在吗")], [])
    # 一大段全对不上（切了会话/滚远了）也不会崩，全当新的补
    adds, _ = reconcile([("her", "甲")], [("her", "乙"), ("me", "丙")])
    assert adds == [(0, "her", "乙"), (1, "me", "丙")], adds

    # 引号被整个读丢时只剩一个裸数字（真机上出现过「3"」→「3」），这时只能靠像素认：
    # 造一块气泡底，数字左边有喇叭图标 = 语音时长，空着 = 真消息
    assert _DIGITS.fullmatch("3") and not _VOICE.fullmatch("3")
    frame = np.full((40, 80, 3), 238, np.uint8)
    frame[12:23, 45:51] = 30                                    # 数字 3
    digit = np.array([[44.0, 11.0], [52.0, 11.0], [52.0, 24.0], [44.0, 24.0]])
    assert not _has_icon(frame, digit, "her", 11), "旁边空着，是真消息，不能当语音"
    frame[9:25, 26:37] = 30                                     # 左边放上喇叭图标
    assert _has_icon(frame, digit, "her", 11), "有图标，是语音时长"
    assert not _has_icon(frame, digit, "gray", 11), "灰字不参与"

    # 转写那个口子：语音气泡底 y=50，转出来的字贴在上面 y=67（实测隔 17px），
    # 下面还有一条已知的新消息 y=130 —— 转写落在「已知行上方」，只有开了口子才认
    reader = Reader.__new__(Reader)  # 不走 __init__：那会去建 OCR 引擎，自测不该那么重
    voice = (0, 0, 100, 50, '4"', "her")
    reader.lh, reader.last_voice, reader.prev_voice = 13.0, [voice], []
    lines = [("her", None, "在吗", 130), ("her", None, "干什么呢？快下来", 67)]
    reader.seen = [("her", None, "在吗")]
    assert reader.new_lines(lines, under_voice=True) == [("her", None, "干什么呢？快下来", '4"')], \
        "刚点过我们那个「转文字」：口子开着（这时微信可能顺手把画面推了一格，气泡位置变了）"
    # 用户在微信里**自己**右键转的老语音：口子没开，但那颗气泡上一帧就挂在同一个位置
    # —— 转出来的字要认。真机上报过：手动转的那条 3" 一直进不了记录
    reader.seen = [("her", None, "在吗")]
    reader.prev_voice = [voice]
    assert reader.new_lines(lines) == [("her", None, "干什么呢？快下来", '4"')], "手动转的也要认"
    # 往上翻 / 把窗口拉高，连带滚出来的一条老语音 + 它底下早就转过的字：
    # 两个都是刚露出来的（上一帧这条气泡不在屏幕上），不能当新消息报上去
    reader.seen = [("her", None, "在吗")]
    reader.prev_voice = []  # 上一帧屏幕上没有这条气泡
    assert reader.new_lines(lines) == [], "刚滚出来的老语音底下那条老转写，不算新消息"
    # 气泡位置动过（滚动/改窗口）也不算「一直挂着」：容差是几个像素，不是几十个
    reader.seen = [("her", None, "在吗")]
    reader.prev_voice = [(0, 200, 100, 250, '4"', "her")]
    assert reader.new_lines(lines) == [], "上一帧在别的位置 = 刚滚出来的"
    # 第四位：贴着气泡的带回那条语音的时长（父进程拿它并成一条），下面那条普通消息是空串
    reader.seen, reader.prev_voice = [], []
    assert reader.new_lines([("her", None, "在吗", 130), ("her", None, "刚转出来的", 67)]) == [
        ("her", None, "在吗", ""), ("her", None, "刚转出来的", '4"')]
    # 语音去重：哪些算「新出现的」——父进程拿它往聊天记录里记「🔊 语音消息 N"」。
    # 以前按「这一帧比上一帧条数多」判、只记最底下那条，真机上就出过「我回了两条语音，
    # 记录里只有一条」。现在按顺序对：报过的那串在眼前这串里找最长的一段，后面剩的才是新的
    def band(y, dur):
        return (10, y, 120, y + 35, dur, "me")

    def row(*durs):
        return [band(100 + i * 60, d) for i, d in enumerate(durs)]

    def fresh(seen, voices, closed=()):
        """seen = 已经报过的时长；closed = 已经转过文字、不在 last_voice_open 里的那些。"""
        reader.seen_voice = [("me", d) for d in seen]
        reader.last_voice = voices
        reader.last_voice_open = [v for v in voices if v[4] not in closed]
        return [v[4] for v in reader.new_voices()]

    # 真机那一帧：刚启动，屏幕上三条语音都是新的 —— 一条不落（以前只剩最底下那条）
    assert fresh([], row('2"', '4"', '2"')) == ['2"', '4"', '2"']
    # 一帧里冒出两条
    assert fresh(['2"'], row('2"', '4"', '6"')) == ['4"', '6"']
    # 底下来一条
    assert fresh(['2"', '4"'], row('2"', '4"', '6"')) == ['6"']
    # 原样再来一帧：一条都不重报（拖动、滚动只是坐标变）
    assert fresh(['2"', '4"', '6"'], row('2"', '4"', '6"')) == []
    # 往上翻：顶上多出一条旧语音，底下那两条早报过了 —— 不能当新消息重报一遍
    assert fresh(['2"', '4"'], [band(50, '9"')] + row('2"', '4"')) == []
    # 往上翻的同时底下真来了新的：只报新的那条
    assert fresh(['2"', '4"'], [band(50, '9"')] + row('2"', '4"') + [band(400, '6"')]) == ['6"']
    # 顶上滚掉一条、底下来一条
    assert fresh(['2"', '4"'], [band(200, '4"'), band(300, '6"')]) == ['6"']
    # 转过文字的那条从 last_voice_open 里没了、但还在 last_voice 里：不能再记一行占位
    assert fresh(['2"', '4"'], row('2"', '4"'), closed=('4"',)) == []
    assert fresh(['2"', '4"'], row('2"', '4"', '6"'), closed=('4"',)) == ['6"']
    # 翻到没报过的一段（报过的一条都不在眼前）：宁可漏记，也不能把旧语音当新消息
    assert fresh(['2"', '4"'], [band(50, '9"'), band(110, '8"')]) == []
    # 两条一模一样的 2"：靠顺序分开，都得记
    assert fresh([], row('2"', '2"')) == ['2"', '2"']
    assert fresh(['2"', '2"'], row('2"', '2"', '2"')) == ['2"']

    # 画面最底下那条是谁说的：文字和语音一起算。自己发的语音也是「我已经回了」——
    # 真机上出过：对方来一句、底下自己回了两条语音，应用照样问了一次模型
    says_her = [("her", None, "在呢，你说的是哪个口", 300)]
    assert bottom_speaker(says_her, []) == "her"
    assert bottom_speaker(says_her, [band(400, '4"'), band(460, '2"')]) == "me", "语音在文字下面"
    assert bottom_speaker([("her", None, "在呢", 500)], [band(400, '4"')]) == "her", "老语音在文字上面"
    assert bottom_speaker([], []) == ""
    assert bottom_speaker([], [band(100, '2"')]) == "me"
    assert bottom_speaker([("me", None, "我回过了", 300)], []) == "me"

    # 转写那行的 who 以**那条语音**为准：微信把转文字画在灰白气泡里，自己那条语音转出来
    # 也是这个颜色，按底色分会被认成对方说的（真机：自己的语音转完，界面上写「对方刚说」）
    reader.lh, reader.last_voice = 13.0, [(0, 0, 100, 50, '2"', "me")]
    reader.last_voice_open, reader.seen = [], []
    assert reader.new_lines([("her", None, "对啊，为什么为什么。", 67)]) == [
        ("me", None, "对啊，为什么为什么。", '2"')], "转写是谁说的，看那条语音，不看气泡底色"
    reader.lh, reader.last_voice = 13.0, [(0, 0, 100, 50, '4"', "her")]
    reader.last_voice_open, reader.seen = [], []
    assert reader.new_lines([("her", None, "刚转出来的", 67)]) == [
        ("her", None, "刚转出来的", '4"')]

    # 转写还得跟那条语音**同一侧**：光按「紧贴」判的话，跟在语音下面的普通消息会被当成它的转写。
    # 真机：对方一条「是的」正好压在我那条 3" 语音下面，被记成了我语音转出来的字（who/时长全错）
    reader.lh, reader._w = 13.0, 400
    reader.last_voice = [(300, 0, 400, 50, '3"', "me")]  # 我发的语音，靠右半边
    reader.last_voice_open, reader.seen = [], []
    assert reader.new_lines([("her", None, "是的", 67, 60)]) == [("her", None, "是的", "")], \
        "她靠左那句不是转写，别跟着语音的 who 走"
    reader.seen = []
    assert reader.new_lines([("her", None, "转出来的字", 67, 380)]) == [
        ("me", None, "转出来的字", '3"')], "靠右 = 我那条语音的转写"

    # 还算数的语音：**下面没有更新的文字消息**的那些。真机：切过去没认出最底下那两条新消息，
    # 反而把顶上翻出来的老语音记成了「对方最近说」，候选条上还摆着它的「转文字」
    reader.last_voice = [band(100, '2"'), band(160, '5"')]
    assert reader._open_voices([("her", None, "不啊", 300, 60),
                                ("her", None, "不会饿", 330, 60)]) == [], "两条都压在最新那两句上面"
    # 最底下就是那条语音（对方刚发来的）：还算数，候选条照旧提示「转文字」
    assert reader._open_voices([("her", None, "在吗", 60, 60)]) == reader.last_voice
    # 转过文字的那条也被同一条规则挡住：转写贴在它下面，比它新
    reader.last_voice = [band(100, '3"')]
    assert reader._open_voices([("me", None, "转出来的字", 150, 65)]) == []
    # 一条文字都没认出来（只发语音的会话）：都留着
    assert reader._open_voices([]) == reader.last_voice

    print("ocr._VOICE ok")
