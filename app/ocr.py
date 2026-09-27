# -*- coding: utf-8 -*-
"""消息区截图 → 谁说了什么。RapidOCR 吃 numpy，不落盘。"""
import difflib
import re
import time

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
    det_limit_type 默认 'min' 会把小图放大到短边 736，裁小反而更慢；必须 'max'。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOCR(intra_op_num_threads=4, det_limit_type="max", det_limit_side_len=4000)
    return _ENGINE


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
    """按 OCR 框里的颜色分类，不看 x 坐标。返回 (谁, 底色, 墨高)：
    先看底色平不平：框里众数颜色占比 <45% 就是图片（头像/照片/表情包）里的字 → None 丢掉。
    绿底 → me；非绿且文字对底色对比度 ≥150 → her；其余（引用块、群里的发言人名、时间戳、系统提示、
    链接卡片描述——都是灰字，对比度 80~95）→ "gray"。
    实测：气泡正文对比度 178~208，me 绿泡 142~150，灰字 ≤ 93。深浅主题都靠这套。
    墨高 = 框里最长一段连续有字的行数（OCR 框对小字有固定 padding、还会蹭到上下行，不能拿框高比大小）。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    reg = chat[int(min(ys)):int(max(ys)), int(min(xs)):int(max(xs))].astype(int)
    if reg.size == 0:
        return None, None, 0
    vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    if cnt.max() / reg.shape[0] / reg.shape[1] < 0.45:
        # 文字必须落在平底色上：WGC 帧是精确像素，气泡/面板里众数颜色占 0.56~0.82，
        # 头像/照片/表情包里只有 0.1~0.3——那是图片里的字（头像上的「借仲夏夜之梦」之类），不是消息。
        # ponytail: 只对精确像素的帧成立；缩放/压缩过的截图（比如拿预览窗再截一次的图）底色会糊成几百种颜色，全会被当图片。
        return None, bg, 0
    diff = np.abs(reg @ [0.299, 0.587, 0.114] - bg @ [0.299, 0.587, 0.114])
    ink_h = best = 0
    for r in (diff > 60).any(axis=1):
        best = best + 1 if r else 0
        ink_h = max(ink_h, best)
    if bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40:
        return "me", bg, ink_h
    return ("her" if diff.max() >= 150 else "gray"), bg, ink_h


def bottom_speaker(lines, voices):
    """画面最底下那条是谁说的，`"me"` / `"her"`；一条都没认出来就是空串。

    文字和语音**一起算**——微信新消息在最下面，所以「最底下那条」就是这条会话里最后说的一句话。
    父进程拿它判「最后说话的是不是我」：自己发的语音也是「我已经回了」，对方那句早就答过了，
    不该再触发一次判断（以前只拿文字判，所以对方来一句、自己回两条语音，照样会问一次）。
    lines 是 read() 的返回（`[(who, name, text, y)]`），voices 是 last_voice（`[(x0,y0,x1,y1,时长,谁)]`）。"""
    items = [(y, who) for who, _, _, y in lines] + [(v[1], v[5]) for v in voices]
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
        self.last_voice = []  # 这一帧的语音气泡 [(x0,y0,x1,y1,时长,谁)]，_under_voice 要用全部
        self.last_voice_open = []  # 上面那些里**还没转过文字**的，给候选条决定要不要提示「转文字」
        self.seen_voice = []  # 已经报给父进程的语音 [(谁, 时长)]，按出现顺序攒着，new_voices 要用
        self.last_ms = 0  # 上一帧 OCR 耗时

    def read(self, chat, pane_bg):
        """→ [(who, name, text, y)]，同一气泡的多行已合并。who ∈ me/her；name 群聊里是发言人，单聊 None。
        顺带把每个框的分类记进 self.last_boxes（调试视图画框用，几十个 tuple，不开也不亏）。"""
        t0 = time.perf_counter()
        res, _ = self.ocr(chat, use_cls=False)
        self.last_ms = int((time.perf_counter() - t0) * 1000)
        self.last_boxes = []
        self.last_voice = []
        self.last_voice_open = []
        W = chat.shape[1]
        # 群聊：每条 her 气泡上方一行灰色发言人名（靠左、短、不带冒号、印在面板底色上），从上往下扫，名字带给后面的气泡。
        # 引用块/时间戳/公告带冒号，链接卡片灰字印在气泡底色上，都不会被当成名字。
        # ponytail: 名字行被 OCR 漏掉时会挂到上一个人头上。
        name, raw = None, []
        for box, text, _ in sorted(res or [], key=lambda r: r[0][0][1]):
            kind, bg, h = who_said(chat, box)
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            rect = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
            if (_VOICE.fullmatch(text.strip()) or _VOICE_ICON.fullmatch(text.strip())
                    or (_DIGITS.fullmatch(text.strip()) and _has_icon(chat, box, kind, h))):
                # 不进 raw（它没有正文，触发判断没意义），但位置记下来：
                # 用户点「转文字」时要悬停到它上面。存的是**整个气泡**的范围，
                # 不是这个 OCR 框——按钮挂在气泡外侧，得按气泡边缘算。见 app/voice.py
                self.last_boxes.append(rect + ("voice", text))
                self.last_voice.append(_bubble_extent(chat, box, bg) + (text, kind))
                continue
            if kind == "gray":
                on_pane = np.abs(bg - pane_bg).sum() <= 6
                taken = bool(on_pane and box[0][0] < 0.25 * W and len(text) <= 16
                             and not re.search("[:：]", text))
                if taken:
                    name = text
                self.last_boxes.append(rect + ("name" if taken else "gray", text))
                continue
            if kind is None or (self.lh and h < 0.6 * self.lh):
                # 字比正常气泡小得多 = 图片消息（截图/表情包）里的字，不是气泡
                self.last_boxes.append(rect + ("image" if kind is None else "tiny", text))
                continue
            self.last_boxes.append(rect + (kind, text))
            raw.append((kind, name if kind == "her" else None, text, box[0][1], box[2][1], h))
        if not self.lh and len(raw) >= 3:
            self.lh = float(np.median([r[5] for r in raw]))
        # 同一气泡的多行合并：同人、上一行底到这一行顶的间距不到半个字高（不同气泡之间至少隔一个字高）
        lines = []
        for who, nm, text, top, bottom, h in raw:
            if lines and lines[-1][0] == who and lines[-1][1] == nm and top - lines[-1][4] < 0.6 * (self.lh or h):
                lines[-1][2] += text
                lines[-1][4] = bottom
            else:
                lines.append([who, nm, text, top, bottom])
        out = [(w, n, t, y) for w, n, t, y, _ in lines]
        # 已经转过文字的语音：转出来的字就紧贴在气泡正下方（实测隔 17px，而下一条普通消息隔 69px，
        # 字高 lh=13）。转过之后别再提示「转文字」了——不然用户转完、转写气泡一出现、布局一移，
        # 子进程重报一次位置，候选条上的「转文字」就又冒回来了。
        self.last_voice_open = [v for v in self.last_voice
                                if not any(0 <= y - v[3] <= 2.5 * (self.lh or 17) for _, _, _, y in out)]
        return out

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

    def _under_voice(self, y):
        """这一行紧跟在哪个语音气泡下面？是的话返回 `(谁, 时长)`（时长是 OCR 原样，可能是 `3"`、
        也可能是 `)3`），不是就返回空元组——空元组在布尔位置上就是假，调用方直接当条件用。

        微信的「语音转文字」是**插在那条语音气泡正下方**的，不是追加到聊天末尾——转一条老语音，
        结果落在「已知行」上面，按下面那条「只认已知行下方的」规则会被当成往上翻出来的旧消息丢掉，
        用户转完了应用却根本没看见。所以这一类单独放行。

        返回时长是为了让父进程把转写并回它那条「🔊 语音消息 N"」上——同一个时长才并。

        返回**谁**是因为微信把转出来的字画在一个**灰白气泡**里，自己那条语音转出来也是这个
        颜色，按底色分类会被认成对方说的（真机上报过：自己的语音转完，界面上写着「对方刚说
        ……」，还白问了一次模型）。谁说的以那条语音为准。"""
        for item in self.last_voice:
            if 0 <= y - item[3] <= 2.5 * (self.lh or 17):  # item[3] = 气泡底
                return item[5], item[4]  # (谁, 时长)
        return ()

    def new_lines(self, lines, under_voice=False):
        """去重（滚动不重复）→ 这一帧里真正新出现的 [(who, name, text, 语音时长或 "")]。
        本帧有已知行时只要已知行下方的：往上滚翻出来的旧消息在已知行上方，不算。
        例外是语音转出来的字（见 _under_voice）：它就插在语音气泡下面，位置在已知行上方。

        under_voice 才开那个例外，而且只在「用户刚点过转文字」之后开一小会儿——见
        worker.run()。常开的话，往上翻/把窗口拉高时露出来的旧语音，底下那条老转写
        也会被当成新消息报上去，白触发一次判断（判断不便宜，还打扰人）。

        第四个字段只给界面用（聊天记录里标/并「语音」那条）：判据就是「紧贴在某个语音
        气泡下面」，跟放不放它过 floor 无关——最新那条语音转出来的字是走正常规则进来的，
        同样得标上。实测转写贴 17px、下一条普通消息隔 69px，2.5×lh 分得开。
        转写那行的 **who 也以那条语音为准**，不看气泡底色（微信把转写画成灰白气泡，
        自己的语音转出来也长这样）。

        本帧一行已知的都没有（大图把旧文字全顶出去了、切了聊天、滚远了）：全算，宁可多算不能漏。
        ponytail: 同一人连发两句一模一样的会吞一句——对触发分析无害。"""
        known_y = [y for w, n, t, y in lines if self._seen(w, n, t)]
        floor = max(known_y) if known_y else -1
        new = []
        for w, n, t, y in lines:
            under = self._under_voice(y)
            if not (y > floor or (under_voice and under)) or self._seen(w, n, t):
                continue
            # 紧跟语音气泡的那行是转写：谁说的、时长多少都以**那条语音**为准，别看气泡底色
            new.append((under[0] if under else w, n, t, under[1] if under else ""))
        self.seen.extend((w, n, t) for w, n, t, _ in lines if not self._seen(w, n, t))
        del self.seen[:-500]
        return new

    def _seen(self, who, name, text):
        # 名字不参与判重：名字行滚出画面后同一条消息会从 her(LO) 变成 her，不能算新消息
        return any(w == who and similar(t, text) for w, _, t in self.seen)


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
    reader.lh, reader.last_voice = 13.0, [(0, 0, 100, 50, '4"', "her")]
    lines = [("her", None, "在吗", 130), ("her", None, "干什么呢？快下来", 67)]
    reader.seen = [("her", None, "在吗")]
    assert reader.new_lines(lines, under_voice=True) == [("her", None, "干什么呢？快下来", '4"')]
    reader.seen = [("her", None, "在吗")]  # new_lines 会把这一帧的行记进 seen，比第二次前先还原
    assert reader.new_lines(lines) == [], "没点过转文字就不认这个口子（往上翻出来的旧转写）"
    # 第四位：贴着气泡的带回那条语音的时长（父进程拿它并成一条），下面那条普通消息是空串
    reader.seen = []
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

    print("ocr._VOICE ok")
