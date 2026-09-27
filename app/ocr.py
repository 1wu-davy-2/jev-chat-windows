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
_VOICE = re.compile(r"\d{1,3}\s*[\"”″＂'′’‘()（）?？!！|｜]{1,2}.{0,2}")

# 时长被读成光秃秃一个数字、引号整个丢掉的情况（真机上出现过「3"」→「3」）。
# 这种只靠文字认不出来——「3」也可能是真消息——所以得看像素：语音气泡里数字旁边
# 紧挨着喇叭图标，真发一个「3」旁边是空的。见 _has_icon()。
_DIGITS = re.compile(r"\d{1,3}")


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
            if _VOICE.fullmatch(text.strip()) or (_DIGITS.fullmatch(text.strip())
                                                  and _has_icon(chat, box, kind, h)):
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

    def _under_voice(self, y):
        """这一行是不是紧跟在某个语音气泡下面。

        微信的「语音转文字」是**插在那条语音气泡正下方**的，不是追加到聊天末尾——转一条老语音，
        结果落在「已知行」上面，按下面那条「只认已知行下方的」规则会被当成往上翻出来的旧消息丢掉，
        用户转完了应用却根本没看见。所以这一类单独放行。"""
        for item in self.last_voice:
            if 0 <= y - item[3] <= 2.5 * (self.lh or 17):  # item[3] = 气泡底
                return True
        return False

    def new_lines(self, lines):
        """去重（滚动不重复）→ 这一帧里真正新出现的 [(who, name, text)]。
        本帧有已知行时只要已知行下方的：往上滚翻出来的旧消息在已知行上方，不算。
        例外是语音转出来的字（见 _under_voice）：它就插在语音气泡下面，位置在已知行上方。
        本帧一行已知的都没有（大图把旧文字全顶出去了、切了聊天、滚远了）：全算，宁可多算不能漏。
        ponytail: 同一人连发两句一模一样的会吞一句——对触发分析无害。"""
        known_y = [y for w, n, t, y in lines if self._seen(w, n, t)]
        floor = max(known_y) if known_y else -1
        new = [(w, n, t) for w, n, t, y in lines
               if (y > floor or self._under_voice(y)) and not self._seen(w, n, t)]
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
    print("ocr._VOICE ok")
