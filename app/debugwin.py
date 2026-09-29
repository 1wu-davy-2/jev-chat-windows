# -*- coding: utf-8 -*-
"""识别调试窗：把子进程送来的帧和每个 OCR 框按分类画出来，看识别到底哪儿错了。
帧只在内存里画（QImage 拿 bytes 建），不存图、不进日志。"""
from datetime import datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import PlainTextEdit, PushButton

# (kind, 画框的色, 色的中文, 这类是什么)：跟设置页那条提示一个口径
_KINDS = (("me", "#18794e", "绿", "我"), ("her", "#1f6fd0", "蓝", "对方"),
          ("gray", "#8a8a8a", "灰", "过滤掉的灰字"), ("name", "#e08b18", "橙", "当成发言人名"),
          ("image", "#d0342c", "红", "当成图片丢掉"), ("tiny", "#d4b106", "黄", "小字丢掉"),
          ("voice", "#c026d3", "紫", "语音消息丢掉"),
          # 撤回提示：自己撤的整条丢，对方撤的照对方说的话收（见 app/ocr.py 的 _is_recall）
          ("recall_me", "#9a3412", "褐", "自己撤回，丢掉"),
          ("recall_her", "#0d9488", "青", "对方撤回，算他说了话"))
_COLOR = {k: c for k, c, _, _ in _KINDS}
_NAME = {k: n for k, _, _, n in _KINDS}
_AREA = "#1f6fd0"  # 消息区
_HEAD = "#8b5cf6"  # 头部（会话名那条）


def _box_dump(pkt):
    """把每个识别框的判定依据列成人能读的几行。

    为什么要它：OCR 会把短消息整条吃掉（真机上「?」「嗯」这种单字），而三道关——**当图片**
    （众数底色占比 < 0.45）、**当小字**（墨高 < 0.6×lh）、**当灰字**（对比度 < 150）——到底哪一道
    吃的、离阈值差多少，光看「被丢掉了」这个结论是猜不出来的，得把数字摊开。

    **只出数字和 OCR 读出来的字，不出图、不落盘**：按钮把这段文字塞进剪贴板，用户自己粘出去。"""
    lh = pkt.get("lh") or 0
    eng = pkt.get("engine") or {}
    stamp = datetime.fromtimestamp(pkt.get("ts") or 0).strftime("%H:%M:%S")
    out = [f"帧时间 {stamp} · 会话 {pkt.get('title') or '（未识别）'}"
           f" · 消息区底色 {pkt.get('pane_bg')} · 参考字高 lh = {lh}",
           f"引擎 box_thresh {eng.get('box_thresh')} / text_score {eng.get('text_score')}"
           f" · 这一帧是放大重读：{'是' if pkt.get('thorough') else '否'}",
           f"阈值：众数占比 < 0.45 当图片 · 墨高 < {0.6 * lh:.1f}（0.6×lh）当小字 · 对比度 < 150 当灰字",
           ""]
    for m in pkt.get("metrics", ()):
        x0, y0, x1, y1 = m["rect"]
        out += [f"框 ({x0},{y0})-({x1},{y1})  {x1 - x0}×{y1 - y0}px",
                f"    底色 {m['bg']}   众数占比 {m['flat']:.2f}   墨高 {m['ink']}",
                f"    颜色判成 {m['kind'] or '（当图片）'} → 最后判成 {m['final']}"
                f"   OCR 文字「{m['text']}」"]
    if not pkt.get("metrics"):
        out.append("（这一帧没有识别框）")
    return "\n".join(out)


class _Canvas(QWidget):
    """左边那块画布：整帧等比缩放铺满，再按同一个倍率把各种框套上去。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.img = None
        self.pkt = None
        self.setMinimumSize(320, 240)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1b1f1d"))
        if self.img is None:
            p.setPen(QColor("#9aa6a0"))
            p.drawText(self.rect(), Qt.AlignCenter, "等待画面…\n开着采集，聊天窗口有动静就会有帧")
            return
        # 等比铺满 + 居中；s 是「缩小后的帧 → 控件」的倍率，k 是子进程缩了多少
        s = min(self.width() / self.img.width(), self.height() / self.img.height())
        w, h = self.img.width() * s, self.img.height() * s
        ox, oy = (self.width() - w) / 2, (self.height() - h) / 2
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(QRectF(ox, oy, w, h), self.img)
        k = self.pkt.get("scale", 1) or 1
        f = lambda x, y: (ox + x * s / k, oy + y * s / k)  # 原帧坐标 → 控件坐标
        area = self.pkt.get("area")
        if not area:
            p.setPen(QColor("#d0342c"))
            p.drawText(QRectF(ox, oy, w, 30), Qt.AlignCenter, "认不出消息区")
            return
        x0, y0, x1, y1 = area
        p.setPen(QPen(QColor(_HEAD), 1))
        p.drawRect(QRectF(*f(x0, self.pkt.get("pane_top", 0)),
                          (x1 - x0) * s / k, (y0 - self.pkt.get("pane_top", 0)) * s / k))
        p.setPen(QPen(QColor(_AREA), 2))
        p.drawRect(QRectF(*f(x0, y0), (x1 - x0) * s / k, (y1 - y0) * s / k))
        tag = QFont(self.font())
        tag.setPointSizeF(7.5)
        p.setFont(tag)
        fm = QFontMetricsF(tag)
        for bx0, by0, bx1, by1, kind, _text in self.pkt.get("boxes", ()):
            color = QColor(_COLOR.get(kind, "#ffffff"))
            p.setPen(QPen(color, 2))
            left, top = f(x0 + bx0, y0 + by0)
            p.drawRect(QRectF(left, top, (bx1 - bx0) * s / k, (by1 - by0) * s / k))
            # 小标签贴在框左上角外侧；宽度按文字实际宽度来，别糊住旁边的框
            label = QRectF(left, top - 12, fm.horizontalAdvance(kind) + 6, 12)
            p.fillRect(label, color)
            p.setPen(QColor("#ffffff"))
            p.drawText(label, Qt.AlignCenter, kind)


class DebugWindow(QWidget):
    """独立小窗，Qt.Tool 不占任务栏。show_packet() 喂一帧就重画一次；关窗回调把设置里的开关拨回去。"""

    def __init__(self, on_close=None):
        super().__init__()
        self.on_close = on_close
        self.setWindowTitle("识别调试")
        self.setWindowFlags(Qt.Tool)
        self.resize(900, 650)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 8)
        outer.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.canvas = _Canvas(self)
        row.addWidget(self.canvas, 1)
        right = QVBoxLayout()
        right.setSpacing(6)
        # 复制这一帧每个框的判定依据：识别漏了东西时把这串数字发出去，比截图和猜都快。
        # 只进剪贴板，不写文件（见 _box_dump）
        self.copyButton = PushButton("复制框数据", self)
        self.copyButton.setToolTip("把这一帧每个识别框的底色、众数占比、墨高、分类和 OCR 文字"
                                   "复制到剪贴板。只出文字和数字，不出图、不写文件。")
        self.copyButton.clicked.connect(self._copy_boxes)
        right.addWidget(self.copyButton)
        self.info = PlainTextEdit(self)
        self.info.setReadOnly(True)
        self.info.setFixedWidth(300)
        self.info.setPlainText(_legend())
        right.addWidget(self.info)
        row.addLayout(right)
        outer.addLayout(row)
        self.status = QLabel("最近一帧 —— · 等待中…", self)
        self.status.setStyleSheet("color: #68776f;")
        outer.addWidget(self.status)

    def _copy_boxes(self):
        """把当前这一帧的框数据塞进剪贴板。没有帧就什么都不做，别塞一段空的进去把人原来的剪贴板顶掉。"""
        if not self.canvas.pkt:
            self.status.setText("还没有帧——先把聊天窗口弄出点动静")
            return
        QApplication.clipboard().setText(_box_dump(self.canvas.pkt))
        self.status.setText(f"已复制 {len(self.canvas.pkt.get('metrics', ()))} 个框的数据，"
                            "直接粘出去就行")

    def show_packet(self, pkt):
        """子进程送来的一帧：RGB 裸字节 → QImage（copy 一份，原 bytes 之后就回收了）。"""
        self.canvas.img = QImage(pkt["rgb"], pkt["w"], pkt["h"], pkt["w"] * 3,
                                 QImage.Format_RGB888).copy()
        self.canvas.pkt = pkt
        self.canvas.update()
        area = pkt.get("area")
        counts = {}
        for b in pkt.get("boxes", ()):
            counts[b[4]] = counts.get(b[4], 0) + 1
        text = [
            f"会话：{pkt.get('title') or '（未识别）'}",
            "消息区：" + (f"x {area[0]}–{area[2]} · y {area[1]}–{area[3]}" if area else "认不出消息区"),
            f"头部顶：y {pkt.get('pane_top', 0)}",
            f"OCR 耗时：{pkt.get('ocr_ms', 0)} ms",
            f"帧：{pkt['w']}×{pkt['h']}（原帧缩了 1/{pkt.get('scale', 1)} 再过队列）",
            "框：" + ("、".join(f"{_NAME.get(k, k)} {v}" for k, v in counts.items()) or "无"),
            "",
            f"本帧 {len(pkt.get('lines', ()))} 行",
            "",
            "（要看每个框的判定依据，点上面的「复制框数据」）",
        ]
        for who, name, line in pkt.get("lines", ()):
            text.append(f"{who}({name})：{line}" if name else f"{who}：{line}")
        text += ["", _legend()]
        self.info.setPlainText("\n".join(text))
        stamp = datetime.fromtimestamp(pkt.get("ts") or 0).strftime("%H:%M:%S")
        self.status.setText(f"最近一帧 {stamp} · 共 {len(pkt.get('boxes', ()))} 个框")

    def closeEvent(self, event):
        if self.on_close:
            self.on_close()
        super().closeEvent(event)


def _legend():
    return ("图例（蓝粗框 = 消息区，紫细框 = 头部会话名）\n"
            + "\n".join(f"  {word} = {what}（{k}）" for k, _, word, what in _KINDS))
