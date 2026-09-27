# -*- coding: utf-8 -*-
"""浅色置顶回复助手：回复建议和独立设置页。发送始终由用户确认。"""
import os
import sys
import threading
from datetime import datetime
from math import isfinite
from types import SimpleNamespace

from PySide6.QtCore import QLocale, QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QSizeGrip, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    Action, BodyLabel, CardWidget, CheckBox, ComboBox, EditableComboBox, FluentIcon as FIF,
    HyperlinkButton, IndeterminateProgressBar, LineEdit, MessageBoxBase, PasswordLineEdit,
    PrimaryPushButton, PushButton, RoundMenu, ScrollArea, SpinBox, SubtitleLabel, SwitchButton,
    TextEdit, Theme, TransparentToolButton, setCustomStyleSheet, setFont, setTheme, setThemeColor,
)

from app import settings, theme
from app.pet import HotkeyHost, PetWindow
from app.theme import (
    FONT_2XL, FONT_DISPLAY, FONT_H1, FONT_LG, FONT_MD, FONT_SM, FONT_XL, FONT_XS,
    GAP_LG, GAP_MD, GAP_SM, GAP_XL, GAP_XS, RADIUS_LG, RADIUS_MD, RADIUS_SM,
    RADIUS_XL, SHADOW_PAD,
)
from app.version import VERSION
from core import jev_client, llm, providers, relay, styles
from core.questions import CHOICE_LABELS

_LOG_LINES = 300
_BAR_HOLD_MS = 25000  # 候选条自己待多久没人理就收起来（鼠标搭上来会重新计时）
_BAR_LEAVE_MS = 700   # 鼠标离开候选条后宽限这么久再收，够从宠物挪到条上
_MUTED = theme.MUTED   # 次要文字色（原来是偏绿的 #68776f，现在统一走 token）
_ACCENT = theme.SAGE   # 强调色（原来是 #18794e）
_RELATIONSHIPS = [
    ("恋人", "romantic partners"), ("朋友", "friends"), ("同事", "colleagues"),
    ("家人", "family"), ("自定义", None),
]
# 「场景模板」下拉的选项 → core/styles 里的键。空串 = 不用，什么都不追加
_PRESET_CHOICES = ((("", "不用"),) + tuple((k, styles.NAMES[k]) for k in styles.KEYS)
                   + ((styles.CUSTOM, styles.CUSTOM_NAME),))
# 「思考开关的传法」下拉框的索引 → core/relay.py 里的键。顺序得跟下面 addItems 一致
_THINK_STYLE_ORDER = ("thinking", "reasoning", "none")


def _choice(answers, name):
    return CHOICE_LABELS[name].get((answers.get(name) or {}).get("choice"), "暂未判断")


class _FitCombo(ComboBox):
    """长名字不撑开窄布局。按钮上按当前宽度省略；条目仍是全文，findText 靠它。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def setText(self, text):
        self._full = text or ""
        QPushButton.setText(self, self._elide(self._full))
        if self._full and self.text() != self._full:
            self.setToolTip(self._full)

    def minimumSizeHint(self):
        hint = QPushButton.minimumSizeHint(self)
        return QSize(48, hint.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        shown = self._elide(self._full)
        if shown != self.text():
            QPushButton.setText(self, shown)
        if self._full and shown != self._full:
            self.setToolTip(self._full)

    def _elide(self, text):
        # 右侧箭头大约 28px。还没排上版时先按一个窄宽度省略，避免最小宽度被整句名字撑开。
        avail = self.width() - 36 if self.width() > 64 else 120
        return self.fontMetrics().elidedText(text, Qt.ElideRight, max(24, avail))


def _short_error(text, limit=90):
    """状态栏只有一行，服务器那串原文得截断。全文留给「查看详情」。"""
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


class _ErrorBox(MessageBoxBase):
    """报错原文。用只读多行框，不用 MessageBox 那个标签：原文要能选中、能复制——
    多半得拿去问中转站或者搜索。挂面板上（不是独立窗口），所以面板收起时它也跟着看不见，
    那种时候报错只在面板的状态栏里，展开面板才看得到。"""

    def __init__(self, text, parent):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("出错了，原文在这儿", self)
        self.viewLayout.addWidget(self.titleLabel)
        self.textEdit = TextEdit(self)
        self.textEdit.setPlainText(text)
        self.textEdit.setReadOnly(True)
        self.textEdit.setMinimumHeight(160)
        self.viewLayout.addWidget(self.textEdit)
        self.yesButton.setText("知道了")
        self.cancelButton.hide()


def _label(text="", size=14, color=None, bold=False, parent=None):
    label = BodyLabel(text, parent)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    setFont(label, size, QFont.DemiBold if bold else QFont.Normal)
    if color:
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(label, qss, qss)
    return label


def _tool(icon, title, callback, parent=None):
    button = TransparentToolButton(icon, parent)
    button.setFixedSize(28, 28)  # 设计稿里图标按钮是 size-7 = 28px
    button.setToolTip(title)
    button.setAccessibleName(title)
    button.clicked.connect(callback)
    return button


def _app_icon():
    """窗口/任务栏用的图标：源码跑从仓库的 docs/ 读，打包后从 _MEIPASS/docs 读。

    不设的话任务栏按钮用的是 python.exe 的图标（源码跑时），设了才是那个绿底白「J」。
    exe 文件本身的图标是另一回事，那个由 jev.spec 的 icon= 在打包时嵌进去。"""
    base = (getattr(sys, "_MEIPASS", None)
            or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return QIcon(os.path.join(base, "docs", "icon.ico"))


def _tab_qss(active):
    """设置页签的胶囊样式：选中 = 墨底纸字，未选中 = 奶油底墨字。

    跟设计稿里「你们的关系」「桌面外观」那排胶囊一个口径（SettingsPanel.tsx 里
    选中是 bg-ink text-paper，未选中是 bg-cream text-ink）。"""
    bg = theme.INK if active else theme.CREAM
    fg = theme.PAPER if active else theme.INK
    return (f"QPushButton {{ background: {bg}; color: {fg}; border: none; "
            f"border-radius: 15px; padding: 0 16px; font-weight: 600; }}")


class _Surface(CardWidget):
    """卡片。底色/圆角走 token，柔和投影靠 theme.apply_shadow（Qt 没有 box-shadow）。

    挂了阴影之后，**放它的容器必须留出 SHADOW_PAD 的边距**，卡片之间的间距也要够，
    否则阴影会被裁掉、或者被下一张卡盖住。"""
    def __init__(self, parent=None, accent=False):
        self.accent = accent
        super().__init__(parent)
        self.setBorderRadius(RADIUS_LG)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        theme.apply_shadow(self)

    def _normalBackgroundColor(self):
        return QColor(theme.SAGE_SOFT if self.accent else theme.PAPER)

    def _hoverBackgroundColor(self):
        return self._normalBackgroundColor()

    def _pressedBackgroundColor(self):
        return self._normalBackgroundColor()


class _Fetched(QObject):
    """取模型列表的后台线程 → 主线程：哪一组（SimpleNamespace）、取回来的模型 id、失败原因（成功是空串）。
    Qt 不让跨线程碰控件，信号是跨线程唯一干净的路。"""
    done = Signal(object, list, str)


class _TitleBar(QWidget):
    """只有标题栏可拖动，选择正文或按按钮不会意外移动窗口。"""
    def __init__(self, parent):
        super().__init__(parent)
        self._drag = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.window().pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)


class _MainWindow(QWidget):
    """窗口大小变了就叫 Overlay 重新排布；断点没跨过时 _relayout 自己不做事，这里不用防抖。

    底色和圆角是手绘的：开了 WA_TranslucentBackground 之后 Qt 不再自动填窗口底，
    而普通 QWidget 的 QSS `background` 在这种窗口上压根不会被绘制（实测连
    WA_StyledBackground 也救不回来），所以只能自己画圆角矩形，顺带把描边一起画了。"""
    def __init__(self, relayout):
        super().__init__()
        self._relayout = relayout

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 内缩半像素，1px 的描边才落在像素格上，不然会糊成两像素的灰边
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, RADIUS_XL, RADIUS_XL)
        p.fillPath(path, QColor(theme.CREAM))
        p.setPen(QPen(QColor(theme.LINE), 1))
        p.drawPath(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout(event.size().width(), event.size().height())


class _ReplyCard(_Surface):
    def __init__(self, owner, index, recommended=False, number=1, score=None):
        super().__init__(accent=recommended)
        box = QVBoxLayout(self)
        self.box = box
        box.setSpacing(GAP_SM)
        top = QHBoxLayout()
        label = "推荐回复" if recommended else f"备选 {number}"
        if score is not None:
            label += f" · {round(score * 100)}%"
        top.addWidget(_label(label, FONT_XS, _ACCENT if recommended else _MUTED, True))
        self.copyButton = _tool(FIF.COPY, "复制这条回复", lambda: owner._copy(index), self)
        top.addWidget(self.copyButton)
        box.addLayout(top)
        self.text = _label(owner.cands[index], FONT_LG)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.text)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.fillButton = (PrimaryPushButton if recommended else PushButton)("填入", self)
        self.fillButton.setAccessibleName(f"填入{'推荐回复' if recommended else f'备选 {number}'}")
        self.fillButton.clicked.connect(lambda: owner._fill(index))
        bottom.addWidget(self.fillButton)
        box.addLayout(bottom)
        self.set_compact(owner._compact)

    def set_available(self, enabled):
        self.fillButton.setEnabled(enabled)
        self.copyButton.setEnabled(enabled)

    def set_compact(self, compact):
        self.box.setContentsMargins(*(GAP_MD, GAP_SM, GAP_MD, GAP_SM) if compact
                                    else (GAP_LG, GAP_MD, GAP_LG, GAP_MD))
        self.fillButton.setMinimumWidth(80 if compact else 100)


class _BarRow(QWidget):
    """候选条里的一行：序号方块 + 正文/百分比 + 复制按钮。整行可点，等于「填入」。

    index 是这条候选在 `cands` 里的**原始下标**，不是行号——候选条按推荐顺序重排过，
    界面上看到的「1/2/3」是排完的位置，两者只有推荐那条本来就排第一时才重合。
    每次 set_content() 都会把它刷成对的那条，别在这儿存行号。"""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.index = 0  # 还没喂内容；行是 hidden 的，点不到
        self.recommended = False
        self.setObjectName("barRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(GAP_SM, GAP_SM, GAP_SM, GAP_SM)
        row.setSpacing(GAP_SM)
        self.number = QLabel("1")
        self.number.setAlignment(Qt.AlignCenter)
        self.number.setFixedSize(24, 24)
        row.addWidget(self.number, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.text = _label("", FONT_MD, theme.INK, True)
        self.text.setWordWrap(False)
        col.addWidget(self.text)
        self.meta = _label("", FONT_XS, _MUTED)
        col.addWidget(self.meta)
        row.addLayout(col, 1)
        self.copyButton = _tool(FIF.COPY, "复制这条回复", self._copy, self)
        self.copyButton.setFixedSize(24, 24)
        row.addWidget(self.copyButton, 0, Qt.AlignTop)
        self._restyle()

    def _copy(self):
        self.owner._copy(self.index)

    def set_content(self, index, text, meta, number, recommended):
        self.index = index
        self.text.setText(text)
        self.meta.setText(meta)
        self.number.setText(str(number))
        self.recommended = recommended
        self._restyle()

    def _restyle(self):
        """推荐那条用 sage 底、其余透明；悬停给一层 cream。序号方块跟着一起变。"""
        bg = theme.SAGE_SOFT if self.recommended else "transparent"
        hover = theme.SAGE_SOFT if self.recommended else theme.CREAM
        self.setStyleSheet(
            f"QWidget#barRow {{ background: {bg}; border-radius: {RADIUS_MD}px; }}"
            f"QWidget#barRow:hover {{ background: {hover}; }}"
        )
        num_bg = theme.SAGE if self.recommended else theme.CREAM
        num_fg = theme.PAPER if self.recommended else theme.INK
        self.number.setStyleSheet(
            f"QLabel {{ background: {num_bg}; color: {num_fg}; "
            f"border-radius: {RADIUS_SM}px; font-weight: 600; }}"
        )

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.owner._fill(self.index)
        super().mouseReleaseEvent(event)


class _CandidateBar(QWidget):
    """宠物旁边的紧凑候选条（280px），对应设计稿的 CandidateIme。

    平时不出现，有新消息才弹出来；点某一行 = 填入那条，右下角可展开成完整面板。
    和面板共用同一份 cands，不新增数据流。

    结构上分两层：外层留出 SHADOW_PAD 给投影，内层 frame 才画底色和圆角——
    顶层窗口开了透明之后 QSS 背景不会被绘制（跟 _MainWindow 是同一个坑），
    所以背景画在 frame 上、阴影也挂在 frame 上。"""
    _PILL = {"scanning": "OCR", "notify": "提醒", "thinking": "判断", "ready": "候选"}
    _BUSY = {"scanning": "正在截取聊天窗口并 OCR…",
             "notify": "读到新消息，等他发完再判断",
             "thinking": "Jev 判断中，正在起草三条回复"}
    _ROWS = 3

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        # 不设 flags 的话默认是 Qt.Window：会套一个 Windows 标题栏、还占任务栏一格
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedWidth(280 + theme.SHADOW_PAD_FLOAT[0] + theme.SHADOW_PAD_FLOAT[2])
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        outer = QVBoxLayout(self)
        # 投影余量必须够，不然脏区顶到窗口外、透明窗上直接画不出来
        outer.setContentsMargins(*theme.SHADOW_PAD_FLOAT)
        outer.setSpacing(0)
        self.frame = QWidget()
        self.frame.setObjectName("candidateBar")
        self.frame.setAttribute(Qt.WA_StyledBackground, True)
        self.frame.setStyleSheet(
            f"QWidget#candidateBar {{ background: {theme.PAPER}; "
            f"border-radius: {RADIUS_XL}px; }}"
        )
        theme.apply_shadow(self.frame, kind="float")
        outer.addWidget(self.frame)
        box = QVBoxLayout(self.frame)
        box.setContentsMargins(GAP_SM, GAP_SM, GAP_SM, GAP_SM)
        box.setSpacing(GAP_XS)

        self.head = QWidget()
        self.head.setObjectName("barHead")
        self.head.setAttribute(Qt.WA_StyledBackground, True)
        self.head.setStyleSheet(
            f"QWidget#barHead {{ background: {theme.CREAM}; border-radius: {RADIUS_MD}px; }}"
        )
        head_row = QHBoxLayout(self.head)
        head_row.setContentsMargins(10, GAP_SM, 10, GAP_SM)
        head_row.setSpacing(GAP_SM)
        head_col = QVBoxLayout()
        head_col.setContentsMargins(0, 0, 0, 0)
        head_col.setSpacing(1)
        head_col.addWidget(_label("对方刚说", FONT_XS, _MUTED))
        self.heard = _label("…", FONT_MD, theme.INK, True)
        self.heard.setWordWrap(False)
        head_col.addWidget(self.heard)
        head_row.addLayout(head_col, 1)
        self.pill = QLabel("")
        self.pill.setAlignment(Qt.AlignCenter)
        head_row.addWidget(self.pill, 0, Qt.AlignTop)
        box.addWidget(self.head)

        # 语音消息那一块：没有正文可判断，给个「转文字」的入口顶上去
        self.voice = QWidget()
        self.voice.setObjectName("barVoice")
        self.voice.setAttribute(Qt.WA_StyledBackground, True)
        self.voice.setStyleSheet(
            f"QWidget#barVoice {{ background: {theme.SAGE_SOFT}; "
            f"border-radius: {RADIUS_MD}px; }}"
        )
        voice_col = QVBoxLayout(self.voice)
        voice_col.setContentsMargins(10, GAP_SM, 10, GAP_SM)
        voice_col.setSpacing(GAP_XS)
        self.voiceLabel = _label("", FONT_MD, theme.INK, True)
        voice_col.addWidget(self.voiceLabel)
        voice_col.addWidget(_label("在微信里转成文字后，我会自动接着判断。", FONT_XS, _MUTED))
        self.convertButton = PrimaryPushButton("转文字")
        self.convertButton.setAccessibleName("把这条语音转成文字")
        self.convertButton.setToolTip("右键那条语音并选「语音转文字」，转完自动判断")
        self.convertButton.clicked.connect(owner._convert_voice)
        voice_col.addWidget(self.convertButton)
        self.voice.hide()
        box.addWidget(self.voice)

        self.hint = _label("", FONT_MD, _MUTED)
        self.hint.hide()
        box.addWidget(self.hint)

        self.rows = [_BarRow(owner, self.frame) for _ in range(self._ROWS)]
        for r in self.rows:
            r.hide()
            box.addWidget(r)

        self.suggest = _label("", FONT_XS, _MUTED)
        self.suggest.hide()
        box.addWidget(self.suggest)

        self.foot = QWidget()
        foot_row = QHBoxLayout(self.foot)
        foot_row.setContentsMargins(GAP_XS, GAP_XS, GAP_XS, 0)
        foot_row.addWidget(_label("按 Ctrl+1/2/3 填入", FONT_XS, _MUTED), 1)
        expand = QPushButton("展开面板")
        expand.setFlat(True)
        expand.setCursor(Qt.PointingHandCursor)
        expand.setStyleSheet(
            f"QPushButton {{ color: {theme.SAGE}; background: transparent; border: none; }}"
        )
        expand.clicked.connect(owner.show_panel)
        foot_row.addWidget(expand, 0, Qt.AlignRight)
        box.addWidget(self.foot)

    def enterEvent(self, event):
        """鼠标停在候选条上就别自动收了，不然正看着它自己跑掉。"""
        self.owner._hold_bar()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.owner._schedule_bar_hide()
        super().leaveEvent(event)

    def _set_pill(self, phase):
        text = self._PILL.get(phase, "")
        self.pill.setText(text)
        self.pill.setVisible(bool(text))
        if text:
            self.pill.setStyleSheet(
                f"QLabel {{ background: {theme.INK}; color: {theme.PAPER}; "
                f"border-radius: {RADIUS_SM}px; padding: 1px 6px; }}"
            )

    def set_items(self, items, suggest, phase, heard="", voice=""):
        """items: [(候选原始下标, 序号, 正文, 百分比或 None, 是否推荐)]；voice 非空 = 这条会话
        有语音消息，这时把「转文字」那一块顶上来，候选行让位。

        下标要带着走：行是按显示位置摆的，但点下去填的必须是那条候选本身。

        候选行、忙碌文案、建议行三者按 phase 互斥（跟设计稿的 CandidateIme 一个口径）：
        只有 ready 才摆候选，其余阶段只显示一句进度说明。这样即便上层忘了清候选，
        也不会出现「一边说正在判断、一边把旧候选摆在那儿」的错乱。"""
        self.heard.setText(heard or "…")
        self._set_pill(phase)
        ready = phase == "ready" and bool(items)
        self.voiceLabel.setText(voice)
        self.voice.setVisible(bool(voice))
        # 有语音时连「对方刚说」那块一起收掉：那里挂的是上一条**文字**消息，
        # 最新一条明明是语音，摆着它就成了张冠李戴。
        self.head.setVisible(not voice)
        if voice:
            self.hint.hide()
            self.suggest.hide()
            self.foot.hide()
            for row in self.rows:
                row.hide()
            self.adjustSize()
            return
        busy = self._BUSY.get(phase)
        self.hint.setText(busy or "")
        self.hint.setVisible(bool(busy) and not ready)
        self.suggest.setText(suggest or "")
        self.suggest.setVisible(ready and bool(suggest))
        self.foot.setVisible(ready)
        for i, row in enumerate(self.rows):
            if ready and i < len(items):
                index, number, text, score, recommended = items[i]
                # 用 number（1 起）不用行号：行号从 0 数，跟左边那个序号方块对不上
                meta = ("推荐回复" if recommended else f"备选 {number}")
                if score is not None:
                    meta += f" · {round(score * 100)}%"
                row.set_content(index, text, meta, number, recommended)
                row.show()
            else:
                row.hide()
        self.adjustSize()


def _fit_width(label, limit, pad=0):
    """折行 QLabel 的宽度得自己定：Qt 对开了 wordWrap 的标签会挑一个「看着合适」的窄宽度，
    结果气泡被挤成细细一条（实测 16 个字折成三行）。这里按字体量单行要多宽，取 min(上限, 单行宽)。

    量之前得先 setFont，QSS 里的 font-size 不保证这会儿已经生效。"""
    fm = label.fontMetrics()
    want = max(fm.horizontalAdvance(line) for line in label.text().split("\n"))
    return max(48, min(limit, want + pad))


class _Bubble(QWidget):
    """聊天记录里的一条消息：头像 + 气泡。对方在左、我在右，跟微信一个排法。

    气泡宽度由 _ChatLog 按记录区宽度封顶（设计稿是 max-w-[70%]），超了自动折行——
    QLabel 自己算折行后的高度，所以宽度一变（拉窗口、过紧凑断点）得重新设一遍上限。
    头像里那个字：单聊用会话名首字，群里用发言人首字。

    voice 非空的是语音转出来的字，值是那条语音的时长：气泡里多一行「🔊 语音消息 3"」的
    小字。转写气泡本身长得跟普通文字消息一模一样，不标一下根本分不出来（而且微信那边
    那条语音气泡我们是不显示的）。"""

    AVATAR = 26

    def __init__(self, who, text, name="", peer="", voice="", parent=None):
        super().__init__(parent)
        mine = who == "me"
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP_SM)
        avatar = QLabel("我" if mine else (name or peer or "对方")[:1], self)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(self.AVATAR, self.AVATAR)
        avatar.setStyleSheet(
            f"QLabel {{ background: {theme.mix(theme.CARAMEL, 0.4)}; color: {theme.INK}; "
            f"border-radius: {RADIUS_SM}px; font-size: {FONT_XS}px; }}")
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        if name and not mine:  # 群里才在气泡上方标发言人；单聊不标
            speaker = QLabel(name, self)
            setFont(speaker, FONT_XS)
            speaker.setStyleSheet(f"QLabel {{ color: {_MUTED}; background: transparent; }}")
            col.addWidget(speaker)
        # 气泡是个容器（不是单个 QLabel）：里面可能还压着一行「语音转文字」的标记
        self.bubble = QWidget(self)
        self.bubble.setObjectName("bubble")
        self.bubble.setAttribute(Qt.WA_StyledBackground, True)
        ring = "" if mine else f" border: 1px solid {theme.LINE};"
        self.bubble.setStyleSheet(
            f"QWidget#bubble {{ background: {theme.IM_MINE if mine else theme.PAPER};"
            f"{ring} border-radius: {RADIUS_MD}px; }}")
        box = QVBoxLayout(self.bubble)
        box.setContentsMargins(9, 5, 9, 5)
        box.setSpacing(1)
        self.tag = None
        if voice:
            self.tag = QLabel(f"🔊 语音消息 {voice}", self.bubble)
            setFont(self.tag, FONT_XS)
            self.tag.setStyleSheet(f"QLabel {{ color: {_MUTED}; background: transparent; }}")
            box.addWidget(self.tag)
        self.text = QLabel(text, self.bubble)
        self.text.setWordWrap(True)
        setFont(self.text, FONT_MD)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text.setStyleSheet(f"QLabel {{ color: {theme.INK}; background: transparent; }}")
        box.addWidget(self.text)
        col.addWidget(self.bubble)
        if mine:
            row.addStretch(1)
            row.addLayout(col)
            row.addWidget(avatar, 0, Qt.AlignTop)
        else:
            row.addWidget(avatar, 0, Qt.AlignTop)
            row.addLayout(col)
            row.addStretch(1)

    def set_limit(self, width):
        # 22 = 气泡左右 padding + 富余。标记那行比正文宽时（短语音）按它算，别把标记折了
        want = _fit_width(self.text, width, 22)
        if self.tag is not None:
            want = max(want, _fit_width(self.tag, width, 22))
        self.bubble.setFixedWidth(want)


class _Note(QWidget):
    """居中的一行小字：时间条（chip=True，灰底胶囊）和采集状态行（纯灰字，跟微信
    「你撤回了一条消息」那种系统提示一个位置）。"""

    def __init__(self, text, chip=False, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignCenter)
        setFont(self.label, FONT_XS)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bg = theme.mix(theme.INK, 0.06) if chip else "transparent"
        self.label.setStyleSheet(
            f"QLabel {{ background: {bg}; color: {_MUTED}; border-radius: {RADIUS_SM}px; "
            f"padding: 1px 8px; }}")
        row.addWidget(self.label)
        row.addStretch(1)

    def set_limit(self, width):
        self.label.setFixedWidth(_fit_width(self.label, width, 20))  # 20 = 左右 padding + 富余


class _ChatLog(QWidget):
    """聊天记录：像微信那样一条条气泡，不是原来那种纯文本流水账。

    内容是 Overlay.feeds[会话名] 里存的那份，切会话时整体重建；采集状态行只落在
    当前视图上、不按会话存（跟原来那个 PlainTextEdit 一个口径）。行数封顶 _LOG_LINES，
    超了从头上删——聊天记录是给你回看的，不是日志。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatLog")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#chatLog {{ background: {theme.CREAM}; border-radius: {RADIUS_MD}px; }}")
        box = QVBoxLayout(self)
        box.setContentsMargins(GAP_SM, GAP_SM, GAP_SM, GAP_SM)
        self.scroll = ScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.viewport().setAutoFillBackground(False)
        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.col = QVBoxLayout(self.content)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(GAP_SM)
        self.placeholder = QLabel("识别到的聊天内容会显示在这里")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(
            f"QLabel {{ color: {_MUTED}; background: transparent; font-size: {FONT_SM}px; }}")
        self.col.addWidget(self.placeholder)
        self.col.addStretch(1)  # 内容不满一屏时贴着顶排，跟微信一样
        self.scroll.setWidget(self.content)
        box.addWidget(self.scroll)
        self.rows = []  # 已经排上去的行，删最老的那条时要拿它
        self._time = ""  # 上一条的时间，变了才插一根时间条

    def _add(self, widget):
        follow = self.at_bottom()  # 先问再看：加完就已经滚下去了，判断会永远为真
        self.col.insertWidget(self.col.count() - 1, widget)  # 插在末尾那根 stretch 前面
        self.rows.append(widget)
        widget.set_limit(self._limit())
        self.placeholder.hide()
        while len(self.rows) > _LOG_LINES:
            old = self.rows.pop(0)
            self.col.removeWidget(old)
            old.deleteLater()
        if follow:
            # 布局要等这一轮事件循环走完才重算，这会儿读 scrollbar 的 maximum 还是旧的
            QTimer.singleShot(0, self._to_bottom)

    def at_bottom(self):
        """是不是正贴着底看。是才跟着新消息走，用户往上翻着看就别把他拽回来。"""
        bar = self.scroll.verticalScrollBar()
        return self.isHidden() or bar.value() >= bar.maximum() - 4

    def _to_bottom(self):
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _limit(self):
        """气泡宽度上限：记录区的 72%（设计稿 max-w-[70%]），再扣掉头像和间距。"""
        return max(96, int(self.width() * 0.72) - _Bubble.AVATAR - GAP_SM)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for row in self.rows:
            row.set_limit(self._limit())

    def message(self, who, text, name="", timestamp="", peer="", voice=False):
        if timestamp and timestamp != self._time:
            self._add(_Note(timestamp, chip=True))  # 时间变了才插一根，跟微信一样
        self._time = timestamp or self._time
        self._add(_Bubble(who, text, name, peer, voice))

    def system(self, text):
        self._add(_Note(text))

    def clear(self):
        for row in self.rows:
            self.col.removeWidget(row)
            row.deleteLater()
        self.rows = []
        self._time = ""
        self.placeholder.show()


class Overlay:
    def __init__(self, on_fill, on_toggle_capture=None, on_target_change=None, result_of=None,
                 on_toggle_debug=None, on_voice_convert=None):
        """result_of(会话名) → 那个会话上次的结果或 None；切着看别的会话时用它把旧结果放回来。
        on_target_change(会话名, 人名) → 用户在群里挑了回复对象。
        on_toggle_debug(开不开) → 开关调试视图那个独立窗口。
        on_voice_convert() → 用户点了「转文字」，返回 "" 或一句给用户看的失败原因。"""
        self.app = QApplication.instance() or QApplication([])
        self.app.setWindowIcon(_app_icon())
        setTheme(Theme.LIGHT)
        setThemeColor(_ACCENT, save=False)
        self.on_fill = on_fill
        self.on_toggle_capture = on_toggle_capture
        self.on_target_change = on_target_change
        self.on_toggle_debug = on_toggle_debug
        self.on_voice_convert = on_voice_convert
        self.result_of = result_of
        self._voices = {}  # {会话名: [(x0,y0,x1,y1,时长)]}，语音气泡的位置，转文字要右键它
        self.cands = []
        self.cards = []
        self._busy = False
        self._current = False
        self._compact = None  # 断点模式：None 保证 _relayout 第一次调用必定生效
        self._pageLayouts = []
        self._hintLabels = []
        self.feeds = {}  # {会话名: [(who, text, name, 时间, 是不是语音转出来的)]}，切会话时重建
        self.counts = {}  # {会话名: 消息条数}
        self.hers = {}  # {会话名: 对方最近一句}
        self.targets = {}  # {会话名: ([发言人], 当前回复对象)}
        self._chat = ""  # 微信当前开着的会话
        self._shown = ""  # 界面上正在看的会话（浏览时和上面不一样）
        self._ordered = []  # [(candidates 里的原始索引, 百分比, 是否推荐)]，按推荐顺序排好
        self._answers = {}  # 上一次判断的 7 道题答案，候选条和面板共用
        self._errorDetail = ""  # 最近一次失败的原文，「查看详情」里显示它
        self._phase = "idle"  # 流水线状态，由 main.py 派生后经 set_phase() 推进来
        self.win = _MainWindow(self._relayout)
        self.win.setObjectName("assistantWindow")
        self.win.setWindowTitle("jev-chat")
        self.win.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        # 开透明是为了让 _MainWindow 手绘的那个圆角真的透出去；不开的话窗口是矩形，
        # 圆角外那几像素在桌面上没东西画。底色和描边都在 _MainWindow.paintEvent 里。
        self.win.setAttribute(Qt.WA_TranslucentBackground, True)
        self.win.setMinimumWidth(320)
        self.win.setMaximumWidth(640)
        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        header = _TitleBar(self.win)
        title = QHBoxLayout(header)
        title.setContentsMargins(GAP_LG, GAP_MD, GAP_SM, GAP_SM)
        title.setSpacing(GAP_SM)
        name = _label("Jev", FONT_2XL, theme.INK, True)
        name.setFixedWidth(40)
        name.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(name)
        self.subtitle = _label("jev-chat", FONT_SM, _MUTED)
        self.subtitle.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(self.subtitle, 1)
        self.captureSwitch = SwitchButton(header)
        self.captureSwitch.setOnText("采集中")
        self.captureSwitch.setOffText("已暂停")
        self.captureSwitch.setToolTip("开启或暂停采集")
        self.captureSwitch.setAccessibleName("开启或暂停采集")
        self.captureSwitch.setChecked(True)
        self.captureSwitch.checkedChanged.connect(self._capture_toggled)
        title.addWidget(self.captureSwitch)
        self.settingsButton = _tool(FIF.SETTING, "设置", self.open_settings, header)
        title.addWidget(self.settingsButton)
        title.addWidget(_tool(FIF.MINIMIZE, "收起（宠物还在）", self.collapse, header))
        title.addWidget(_tool(FIF.CLOSE, "退出助手", self._quit, header))
        outer.addWidget(header)
        self.updateBar = QWidget(self.win)
        update_row = QHBoxLayout(self.updateBar)
        update_row.setContentsMargins(GAP_LG, GAP_XS, GAP_SM, GAP_XS)
        update_row.setSpacing(GAP_SM)
        self.updateLabel = _label("", FONT_SM, _ACCENT, True)
        update_row.addWidget(self.updateLabel, 1)
        self.updateLink = HyperlinkButton("", "去下载", self.updateBar)
        self.updateLink.setFixedHeight(24)
        update_row.addWidget(self.updateLink)
        closeUpdate = TransparentToolButton(FIF.CLOSE, self.updateBar)
        closeUpdate.setFixedSize(20, 20)
        closeUpdate.setToolTip("关闭更新提示")
        closeUpdate.setAccessibleName("关闭更新提示")
        closeUpdate.clicked.connect(lambda: self.updateBar.hide())
        update_row.addWidget(closeUpdate)
        self.updateBar.setFixedHeight(32)
        self.updateBar.hide()
        outer.addWidget(self.updateBar)
        self.pages = QStackedWidget(self.win)
        outer.addWidget(self.pages, 1)
        self._build_home()
        self._build_settings()
        self.bar = _CandidateBar(self)
        # 宠物窗和候选条都是独立顶层窗，默认都不显示；由 set_phase / pet_enabled 决定
        self.pet_enabled = settings.pet_enabled()
        self.pet = PetWindow()
        if self.pet.mascot.pix.isNull():
            # 素材读不到（打包漏了 PNG 之类）就退回「只有面板」的老形态，别启动即炸
            self.pet_enabled = False
            self.log("[宠物] 吉祥物素材读不到，已退回只有面板的形态")
        # 悬停/单击宠物 → 弹紧凑候选条；双击 → 展开完整面板；右键 → 弹菜单
        self.pet.hovered.connect(self._show_bar)
        self.pet.clicked.connect(self._show_bar)
        self.pet.expand.connect(self.show_panel)
        self.pet.context_menu.connect(self._pet_menu)
        self.pet.dropped.connect(lambda x, y: settings.save_pet_pos(x, y))
        pos = settings.pet_pos()
        if pos:
            self.pet.move(*pos)
        else:
            self.pet.move_to_default()
        self._barTimer = QTimer(self.win)
        self._barTimer.setSingleShot(True)
        self._barTimer.timeout.connect(self.bar.hide)
        self.hotkeys = HotkeyHost()
        # Ctrl+1/2/3 按的是界面上看到的位置，得折成候选原始下标——见 _cand_at()
        self.hotkeys.hotkey.connect(lambda i: self._fill(self._cand_at(i - 1)))
        self._hotkeyFailed = [i for i in (1, 2, 3) if not self.hotkeys.register(i, 0x30 + i)]
        self.app.aboutToQuit.connect(self.hotkeys.unregister_all)
        footer = QHBoxLayout()
        footer.setContentsMargins(GAP_LG, GAP_SM, GAP_SM, GAP_SM)
        footer.addWidget(_label(f"仅填入输入框 · 发送由你确认 · v{VERSION}", FONT_XS, _MUTED), 1)
        grip = QSizeGrip(self.win)
        grip.setFixedSize(16, 16)
        footer.addWidget(grip, 0, Qt.AlignBottom)
        outer.addLayout(footer)
        screen = self.app.primaryScreen().availableGeometry()
        self.win.setMinimumHeight(min(360, screen.height() - 32))
        self.win.resize(min(440, screen.width() - 32), min(820, screen.height() - 48))
        self.win.move(screen.right() - self.win.width() - 20, screen.top() + 24)
        self._relayout(self.win.width(), self.win.height())  # resizeEvent 补不到构造时这一次
        self.set_status("等待新消息" if settings.has_key() else "需要配置模型",
                        "idle" if settings.has_key() else "warning")
        if self.pet_enabled:
            self.pet.clamp_to_screen()  # 上次存的坐标可能已经不在屏幕里了（换显示器）
            self.pet.show()
        else:
            self.win.show()

    def _scroll_page(self):
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("pageContent")
        content.setStyleSheet("QWidget#pageContent { background: transparent; }")
        layout = QVBoxLayout(content)
        # 左右和下方要留够：卡片挂了投影，阴影画在卡片矩形之外，容器不留位置就被裁
        layout.setContentsMargins(GAP_LG, GAP_LG, GAP_LG, SHADOW_PAD)
        layout.setSpacing(GAP_LG)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        self._pageLayouts.append(layout)
        return scroll, layout

    def _relayout(self, w, h):
        """宽度跨过断点才重新摆布局（省事）；高度每次都重算，反正只是设个下限。

        聊天记录只设**下限**不设定高：上面的卡片占满一屏时它就是下限那么高、页面自己滚，
        窗口拉高了剩下的空间全归它（拉伸因子在 _build_home 里给的）。"""
        compact = w < 400
        if compact != self._compact:
            self._compact = compact
            self._apply_compact(compact)
        self.feed.setMinimumHeight(max(140, min(240, int(h * 0.3))))

    def _apply_compact(self, compact):
        """紧凑/常规两套间距和可见性；断点没变时不会被调用。"""
        self.subtitle.setVisible(not compact)
        self.captureSwitch.setOnText("" if compact else "采集中")
        self.captureSwitch.setOffText("" if compact else "已暂停")
        for label in self._hintLabels:
            label.setVisible(not compact)
        self.referenceNote.setVisible(bool(self.cands) and not compact)
        self._sync_model_fields()
        margins = ((GAP_MD, GAP_MD, GAP_MD, SHADOW_PAD) if compact
                   else (GAP_LG, GAP_LG, GAP_LG, SHADOW_PAD))
        for layout in self._pageLayouts:
            layout.setContentsMargins(*margins)
        for card in self.cards:
            card.set_compact(compact)

    def _build_home(self):
        self.home, body = self._scroll_page()
        heading = QHBoxLayout()
        heading.addWidget(_label("回复建议", FONT_H1, theme.INK, True), 1)
        self.updated = _label("", FONT_XS, _MUTED)
        self.updated.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(self.updated)
        body.addLayout(heading)
        chat_row = QHBoxLayout()
        chat_row.setSpacing(GAP_SM)
        prefix = _label("当前会话", FONT_SM, _MUTED)
        prefix.setFixedWidth(56)
        chat_row.addWidget(prefix)
        self.chatBox = _FitCombo()
        self.chatBox.setPlaceholderText("尚未识别到会话")
        self.chatBox.setAccessibleName("当前会话")
        self.chatBox.setToolTip("聊天窗口切到哪个会话这里就跟到哪个；也可以自己选一个，只看它的记录和建议")
        self.chatBox.currentIndexChanged.connect(self._on_chat_selected)
        chat_row.addWidget(self.chatBox, 1)
        self.chatFollow = _label("", FONT_XS, _MUTED)
        self.chatFollow.setFixedWidth(52)
        self.chatFollow.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        chat_row.addWidget(self.chatFollow)
        body.addLayout(chat_row)
        self.targetRow = QWidget()  # 只有开了「群聊指定回复对象」且这个会话是群聊才露出来
        target_row = QHBoxLayout(self.targetRow)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(GAP_SM)
        target_prefix = _label("回复对象", FONT_SM, _MUTED)
        target_prefix.setFixedWidth(56)
        target_row.addWidget(target_prefix)
        self.targetBox = _FitCombo()
        self.targetBox.setAccessibleName("回复对象")
        self.targetBox.setToolTip("三条候选都按这个人来写；不选就跟着最近说话的那位")
        self.targetBox.currentIndexChanged.connect(self._on_target_selected)
        target_row.addWidget(self.targetBox, 1)
        self.atCheck = CheckBox("填入时带 @")
        self.atCheck.setChecked(True)
        self.atCheck.setToolTip("填入时在开头加「@名字 」。只是普通文字，不会变成真正的 @")
        target_row.addWidget(self.atCheck)
        self.targetRow.hide()
        body.addWidget(self.targetRow)
        status_row = QHBoxLayout()
        status_row.setSpacing(GAP_SM)
        self.status = _label("", FONT_SM, _MUTED)
        status_row.addWidget(self.status, 1)
        # 服务器回的原文（一串 JSON）塞不进状态栏，留个口子点开看
        self.detailButton = PushButton("查看详情")
        self.detailButton.setFixedHeight(24)
        self.detailButton.clicked.connect(self._show_error_detail)
        self.detailButton.hide()
        status_row.addWidget(self.detailButton, 0, Qt.AlignTop)
        body.addLayout(status_row)
        self.progress = IndeterminateProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.hide()
        body.addWidget(self.progress)
        self.context = QWidget()
        context_box = QVBoxLayout(self.context)
        context_box.setContentsMargins(0, 0, 0, 0)
        context_box.setSpacing(GAP_XS)
        context_box.addWidget(_label("对方最近说", FONT_XS, _MUTED))
        self.latest = _label("", FONT_MD, theme.INK)
        self.latest.setTextInteractionFlags(Qt.TextSelectableByMouse)
        context_box.addWidget(self.latest)
        self.context.hide()
        body.addWidget(self.context)

        self.insight = _Surface()
        insight_box = QVBoxLayout(self.insight)
        insight_box.setContentsMargins(GAP_LG, GAP_MD, GAP_LG, GAP_MD)
        insight_box.setSpacing(GAP_SM)
        row = QHBoxLayout()
        self.insightTitle = _label("对话参考", FONT_XS, _MUTED)
        row.addWidget(self.insightTitle, 1)
        self.tension = _label("", FONT_XS)
        self.tension.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.tension)
        insight_box.addLayout(row)
        self.summary = _label("", FONT_MD, theme.INK, True)
        insight_box.addWidget(self.summary)
        self.intent = _label("", FONT_XS, _MUTED)
        insight_box.addWidget(self.intent)
        self.insight.setToolTip("根据当前聊天片段推测，可能理解有偏差。紧张度为 0–9 的参考评分。")
        self.insight.hide()
        body.addWidget(self.insight)

        self.empty = _Surface()
        empty_box = QVBoxLayout(self.empty)
        empty_box.setContentsMargins(GAP_XL, 36, GAP_XL, 36)
        empty_box.setSpacing(GAP_LG)
        symbol = _label("…", FONT_DISPLAY, _ACCENT, True)
        symbol.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(symbol)
        self.emptyTitle = _label("等待对方的新消息", FONT_XL, theme.INK, True)
        self.emptyTitle.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyTitle)
        self.emptyHint = _label("保持聊天窗口打开。\n收到新消息后，回复建议会出现在这里。", FONT_SM, _MUTED)
        self.emptyHint.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyHint)
        self.setupButton = PrimaryPushButton("前往设置")
        self.setupButton.clicked.connect(self.open_settings)
        self.setupButton.setVisible(not settings.has_key())
        empty_box.addWidget(self.setupButton, 0, Qt.AlignHCenter)
        if not settings.has_key():
            self.emptyTitle.setText("先设置，再开始")
            self.emptyHint.setText("配置模型和关系背景，\n让建议更贴近你们的对话。")
        body.addWidget(self.empty)
        self.replyBox = QVBoxLayout()
        # 卡片之间要留得比设计稿宽：Qt 里后画的兄弟控件会盖住前一张卡片的投影，
        # 间距太小的话下面那张卡会把上面的阴影切掉一条
        self.replyBox.setSpacing(GAP_XL)
        body.addLayout(self.replyBox)
        self.referenceNote = _label("AI 建议仅供参考，按你的语气调整后再发送。", FONT_XS, _MUTED)
        self.referenceNote.hide()
        body.addWidget(self.referenceNote)

        self.historyButton = PushButton(FIF.HISTORY, "聊天记录")
        self.historyButton.clicked.connect(self._toggle_history)
        self.historyButton.setAccessibleName("展开或收起聊天记录")
        body.addWidget(self.historyButton)
        self.feed = _ChatLog()
        self.feed.setMinimumHeight(160)
        self.feed.hide()
        # 记录区拉伸因子 1、末尾那根弹簧也 1：展开时 _toggle_history() 把弹簧松成 0，
        # 剩下的竖向空间就全归记录区；收起时弹簧挂着，页面照旧顶部对齐。
        # **不能**干脆不挂弹簧——那样余量会被页面里一堆 Preferred 的控件分掉，排版散开。
        body.addWidget(self.feed, 1)
        self._history_title()
        body.addStretch(1)
        self.homeBody = body

    def _build_settings(self):
        self.settingsPage, body = self._scroll_page()
        heading = QHBoxLayout()
        heading.addWidget(_tool(FIF.RETURN, "返回回复建议", self._back_home))
        heading.addWidget(_label("设置", FONT_H1, theme.INK, True), 1)
        body.addLayout(heading)
        body.addWidget(_label("调整关系背景和说话风格，配置判断和起草用的两个模型。", FONT_MD, _MUTED))
        # 两块内容分页签摆，别堆成一长条滚动。标题交给页签，卡片里就不再重复写一遍
        tabs = QHBoxLayout()
        tabs.setSpacing(GAP_SM)
        self.tabButtons = {}
        for key, text in (("preference", "回复偏好"), ("models", "模型设置"),
                          ("style", "个人风格")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedHeight(30)
            button.clicked.connect(lambda _=False, k=key: self._switch_tab(k))
            self.tabButtons[key] = button
            tabs.addWidget(button)
        tabs.addStretch(1)
        body.addLayout(tabs)
        preference = _Surface()
        box = QVBoxLayout(preference)
        box.setContentsMargins(GAP_LG, GAP_LG, GAP_LG, GAP_LG)
        box.setSpacing(GAP_MD)
        relation_label = _label("你们的关系", FONT_MD)
        box.addWidget(relation_label)
        self.relationshipBox = ComboBox()
        self.relationshipBox.setMinimumWidth(0)
        self.relationshipBox.addItems([name for name, value in _RELATIONSHIPS])
        self.relationshipBox.setAccessibleName("你们的关系")
        relation_label.setBuddy(self.relationshipBox)
        box.addWidget(self.relationshipBox)
        self.relEdit = LineEdit()
        self.relEdit.setPlaceholderText("例如：刚认识的朋友，正在慢慢熟悉")
        self.relEdit.setAccessibleName("自定义关系背景")
        box.addWidget(self.relEdit)
        self.relationshipBox.currentIndexChanged.connect(
            lambda index: self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        )
        box.addWidget(self._hint("帮助助手把握称呼、语气和回应分寸。"))
        context_label = _label("参考上下文", FONT_MD)
        box.addWidget(context_label)
        self.contextBox = SpinBox()
        self.contextBox.setRange(3, 30)
        # QSpinBox 用 locale() 格式化数字，而 Qt 的 zh_CN locale 会把它变成杭州码子
        # （1→〡 U+3021、0→〇 U+3007、9→〩），在界面上就是"10"显示成"〡〇"、"9"显示成"〩"。
        # 钉成 C locale 才显示成 10。QLocale.c() 只影响这个控件，不动全局。
        self.contextBox.setLocale(QLocale.c())
        self.contextBox.setAccessibleName("参考的最近消息条数")
        context_label.setBuddy(self.contextBox)
        box.addWidget(self.contextBox)
        box.addWidget(self._hint(
            "生成和判断时看最近这么多条消息。太少会丢上下文，太多会稀释重点，建议 6–12。"
        ))
        target_row = QHBoxLayout()
        target_row.addWidget(_label("群聊指定回复对象", FONT_MD), 1)
        self.targetSwitch = SwitchButton()
        self.targetSwitch.setOnText("开")
        self.targetSwitch.setOffText("关")
        self.targetSwitch.setAccessibleName("群聊指定回复对象")
        target_row.addWidget(self.targetSwitch)
        box.addLayout(target_row)
        box.addWidget(self._hint(
            "开了以后群聊里可以选回复给谁，候选会针对 TA 写，填入时可带 @。关了就正常回复。"
        ))
        update_row = QHBoxLayout()
        update_row.addWidget(_label("启动时检查更新", FONT_MD), 1)
        self.updateSwitch = SwitchButton()
        self.updateSwitch.setOnText("开")
        self.updateSwitch.setOffText("关")
        self.updateSwitch.setAccessibleName("启动时检查更新")
        update_row.addWidget(self.updateSwitch)
        box.addLayout(update_row)
        box.addWidget(self._hint(
            "只向 GitHub 查最新版本号，不发送任何数据。国内访问 GitHub 慢的话关掉也行。"
        ))
        debug_row = QHBoxLayout()
        debug_row.addWidget(_label("调试视图", FONT_MD), 1)
        self.debugSwitch = SwitchButton()
        self.debugSwitch.setOnText("开")
        self.debugSwitch.setOffText("关")
        self.debugSwitch.setAccessibleName("调试视图")
        self.debugSwitch.checkedChanged.connect(self._debug_toggled)  # 这个开关立刻生效，不等「保存设置」
        debug_row.addWidget(self.debugSwitch)
        box.addLayout(debug_row)
        box.addWidget(self._hint(
            "另开一个窗口实时显示截到的画面和识别框：绿 = 我、蓝 = 对方、灰 = 过滤掉的灰字、"
            "红 = 当成图片丢掉、黄 = 小字丢掉、紫 = 语音消息丢掉。只在内存里画，不存图。"
        ))
        pet_row = QHBoxLayout()
        pet_row.addWidget(_label("桌面宠物", FONT_MD), 1)
        self.petSwitch = SwitchButton()
        self.petSwitch.setOnText("开")
        self.petSwitch.setOffText("关")
        self.petSwitch.setAccessibleName("桌面宠物")
        self.petSwitch.checkedChanged.connect(self._pet_toggled)  # 跟调试视图一样，立刻生效
        pet_row.addWidget(self.petSwitch)
        box.addLayout(pet_row)
        box.addWidget(self._hint(
            "平时桌面上只有宠物，有消息才在它旁边弹候选条，点宠物展开完整面板。"
            "关掉就是原来那样：面板一直开着。"
        ))
        self.preferenceCard = preference
        body.addWidget(preference)

        # 第三页签「个人风格」：选一个场景模板，正文摊在大框里，可以直接改。
        # 内置原文只有一份（core/styles.py），改过的版本按类型分开存在 config.json 的 style_texts 里
        style = _Surface()
        box = QVBoxLayout(style)
        box.setContentsMargins(GAP_LG, GAP_LG, GAP_LG, GAP_LG)
        box.setSpacing(GAP_MD)
        self._presetTexts = {}  # 内存里正在改的各型正文，点保存才落盘
        self._presetShown = ""  # 框里现在装的是哪一型：换型前得按它先把字收回来
        top = QHBoxLayout()
        top.addWidget(_label("场景模板", FONT_MD), 1)
        top.addWidget(_label("类型", FONT_MD))
        self.presetBox = ComboBox()
        self.presetBox.setMinimumWidth(0)
        self.presetBox.setAccessibleName("场景模板类型")
        self.presetBox.addItems([name for _, name in _PRESET_CHOICES])
        top.addWidget(self.presetBox)
        box.addLayout(top)
        box.addWidget(_label("追加到 AI 上下文的内容", FONT_MD))
        self.presetEdit = TextEdit()
        self.presetEdit.setPlaceholderText("选一个类型，这里会填上它的正文；也可以自己写。留空 = 不追加。")
        self.presetEdit.setFixedHeight(180)
        self.presetEdit.setAccessibleName("追加到 AI 上下文的内容")
        box.addWidget(self.presetEdit)
        preset_row = QHBoxLayout()
        self.presetReset = PushButton("重置为内置原文")
        self.presetReset.clicked.connect(self._preset_reset)
        preset_row.addWidget(self.presetReset)
        preset_row.addStretch(1)
        self.presetSave = PrimaryPushButton("保存")  # 跟页面底部那个「保存设置」是同一个动作
        self.presetSave.clicked.connect(self._save)
        preset_row.addWidget(self.presetSave)
        box.addLayout(preset_row)
        self.presetCount = _label("", FONT_XS, _MUTED)
        box.addWidget(self.presetCount)
        box.addWidget(self._hint(
            "选中的这段正文会拼进每次起草的上下文，管称呼、语气和分寸；判断那一步不喂，它只看意图和"
            "紧张度。改过的版本按类型分开存着，点「重置为内置原文」就回到出厂那份。"))
        # 信号接在控件都建好之后：addItems 自己会发一次 currentIndexChanged，那会儿框还没建出来
        self.presetBox.currentIndexChanged.connect(self._preset_type_changed)
        self.presetEdit.textChanged.connect(self._preset_count)
        self.styleCard = style
        body.addWidget(style)

        models = _Surface()
        box = QVBoxLayout(models)
        box.setContentsMargins(GAP_LG, GAP_LG, GAP_LG, GAP_LG)
        box.setSpacing(GAP_MD)
        self._fetched = _Fetched()
        self._fetched.done.connect(self._models_fetched)
        self.jev = self._model_group(box, "判断 · Jev", "jev", providers.JEV_PROVIDERS)
        box.addWidget(self._hint(
            "判断意图、紧张度，并给三条候选排序。两家给的是同一个 Jev，必填。"
        ))
        self.draft = self._model_group(box, "起草 · 语言模型", "draft", providers.DRAFT_PROVIDERS)
        box.addWidget(self._hint(
            "写那三条候选。OpenAI / Anthropic / Gemini 三种接口都走各自官方 SDK。"
            "默认 DeepSeek 官网直连，国内最快。"
        ))
        think_row = QHBoxLayout()
        think_row.addWidget(_label("起草时开启思考模式", FONT_MD), 1)
        self.thinkingSwitch = SwitchButton()
        self.thinkingSwitch.setOnText("开")
        self.thinkingSwitch.setOffText("关")
        self.thinkingSwitch.setAccessibleName("起草时开启思考模式")
        think_row.addWidget(self.thinkingSwitch)
        box.addLayout(think_row)
        box.addWidget(self._hint(
            "关：秒回，够用。开：模型先想再写，更斟酌但慢好几倍、贵一些。"
            "只有 " + " / ".join(providers.THINKING) + " 认这个开关。"
        ))
        # 中转那几项：来源选了「第三方中转」才露出来，起草和判断共用同一个地址
        self.relayLabel = _label("中转地址", FONT_MD)
        box.addWidget(self.relayLabel)
        self.relayEdit = LineEdit()
        self.relayEdit.setPlaceholderText("https://你的中转站（带不带 /v1 都认）")
        self.relayEdit.setAccessibleName("第三方中转地址")
        self.relayLabel.setBuddy(self.relayEdit)
        box.addWidget(self.relayEdit)
        self.judgePathLabel = _label("判断接口路径", FONT_MD)
        box.addWidget(self.judgePathLabel)
        self.judgePathEdit = LineEdit()
        self.judgePathEdit.setPlaceholderText(relay.DEFAULT_JUDGE_PATH)
        self.judgePathEdit.setAccessibleName("中转上的判断接口路径")
        self.judgePathLabel.setBuddy(self.judgePathEdit)
        box.addWidget(self.judgePathEdit)
        style_row = QHBoxLayout()
        style_row.addWidget(_label("思考开关的传法", FONT_MD), 1)
        self.thinkStyleBox = ComboBox()
        self.thinkStyleBox.setMinimumWidth(0)
        self.thinkStyleBox.setAccessibleName("中转认哪种思考开关")
        self.thinkStyleBox.addItems(["thinking（DeepSeek 那套）", "reasoning（OpenRouter 那套）",
                                     "不传（中转自己会关）"])
        style_row.addWidget(self.thinkStyleBox)
        box.addLayout(style_row)
        self.relayHint = self._hint(
            "中转的地址写法不挑：带不带 /v1、连 /chat/completions 一起粘进来都认。"
            "判断口各家叫法不同（OpenRouter 是 " + relay.DEFAULT_JUDGE_PATH +
            "，PackyCode 的 typesafe 通道是 /v1/systemone），填错会回 404 或"
            "「only supports ... protocol」，报错里会写它认哪个口。"
            "思考开关传错派系不报错、只被无视——思考照开、max_tokens 全被推理吃掉，"
            "表现为「起草结果解析不出候选」。probe/probe_relay.py 能把这两样挨个试出来。"
        )
        box.addWidget(self.relayHint)
        self.modelsCard = models
        body.addWidget(models)
        self._switch_tab("preference")  # 默认停在第一页
        self.settingsFeedback = _label("", FONT_MD, _ACCENT)
        self.settingsFeedback.hide()
        body.addWidget(self.settingsFeedback)
        actions = QHBoxLayout()
        back = PushButton("返回")
        back.clicked.connect(self._back_home)
        actions.addWidget(back)
        actions.addStretch(1)
        self.saveButton = PrimaryPushButton("保存设置")
        self.saveButton.clicked.connect(self._save)
        actions.addWidget(self.saveButton)
        body.addLayout(actions)
        body.addWidget(self._hint("保存后用于下一次生成的回复。"))
        body.addStretch(1)
        self._load_settings()

    def _hint(self, text):
        """设置页字段下面的灰字说明：记下来，紧凑模式一起隐藏。"""
        label = _label(text, FONT_XS, _MUTED)
        self._hintLabels.append(label)
        return label

    def _model_group(self, box, title, kind, table):
        """一组「来源 / 密钥 / 模型」控件，判断和起草各一份。table 是 core/providers.py 里那张表。"""
        group = SimpleNamespace(kind=kind, table=table, ids=list(table),
                                keyTitle="判断" if kind == "jev" else "起草",
                                stored_key=lambda k=kind: (settings.jev_key() if k == "jev"
                                                           else settings.llm_key()))
        heading = QHBoxLayout()
        heading.addWidget(_label(title, FONT_MD, theme.INK, True), 1)
        group.keyState = _label("", FONT_XS, _ACCENT)
        group.keyState.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(group.keyState)
        box.addLayout(heading)
        source_label = _label("来源", FONT_MD)
        box.addWidget(source_label)
        group.providerBox = ComboBox()
        group.providerBox.setMinimumWidth(0)  # 选项文字长短不一，别让它撑开设置页
        group.providerBox.addItems([table[i].name for i in group.ids])
        group.providerBox.setAccessibleName(f"{title} 来源")
        source_label.setBuddy(group.providerBox)
        box.addWidget(group.providerBox)
        if kind == "draft":  # 只有两个「自定义」来源要自己填地址，别的来源这一行藏着
            self.baseLabel = _label("Base URL", FONT_MD)
            box.addWidget(self.baseLabel)
            self.baseEdit = LineEdit()
            self.baseEdit.setPlaceholderText("https://你的服务/v1")
            self.baseEdit.setAccessibleName("自定义来源 Base URL")
            self.baseLabel.setBuddy(self.baseEdit)
            box.addWidget(self.baseEdit)
        key_label = _label("密钥", FONT_MD)
        box.addWidget(key_label)
        group.keyEdit = PasswordLineEdit()
        group.keyEdit.setAccessibleName(f"{title} API 密钥")
        key_label.setBuddy(group.keyEdit)
        group.keyEdit.returnPressed.connect(self._save)
        box.addWidget(group.keyEdit)
        box.addWidget(self._hint(
            "OpenRouter 的 key 或 TypeSafe 的 key，看上面选的来源。" if kind == "jev"
            else "上面选哪家就填哪家的 key；换来源重填一次，只存这一把。"))
        model_label = _label("模型", FONT_MD)
        box.addWidget(model_label)
        row = QHBoxLayout()
        row.setSpacing(GAP_SM)
        group.modelBox = EditableComboBox()  # 能选也能手打，接口新出的模型不用等我改代码
        group.modelBox.setMinimumWidth(0)
        group.modelBox.setAccessibleName(f"{title} 模型")
        model_label.setBuddy(group.modelBox)
        row.addWidget(group.modelBox, 1)
        group.fetchButton = PushButton("获取模型")
        group.fetchButton.setAccessibleName(f"获取{title}的可用模型列表")
        group.fetchButton.clicked.connect(lambda: self._fetch_models(group))
        row.addWidget(group.fetchButton)
        box.addLayout(row)
        group.status = _label("", FONT_XS, _MUTED)
        box.addWidget(group.status)
        group.providerBox.currentIndexChanged.connect(lambda _: self._provider_changed(group))
        return group

    @staticmethod
    def _provider_of(group):
        return group.ids[max(0, group.providerBox.currentIndex())]

    def _provider_changed(self, group):
        """换来源：模型框回到这家该有的值（存的就是这家才用存的，否则用它的默认），状态清掉。"""
        provider = self._provider_of(group)
        saved = settings.jev_provider() if group.kind == "jev" else settings.draft_provider()
        stored = settings.jev_model() if group.kind == "jev" else settings.draft_model()
        group.modelBox.clear()
        group.modelBox.setText(stored if provider == saved else group.table[provider].default)
        group.status.setText("")
        self._sync_model_fields()

    def _sync_model_fields(self):
        """两组共用：密钥已配置/未配置、占位文案、自定义 Base URL 行的显隐，
        外加紧凑模式下把来源按钮上的文字省略——ComboBox 是 QPushButton，
        minimumSizeHint 按整段文字算，不会自动换行/省略，长名字会把设置页撑宽。"""
        for group in (self.jev, self.draft):
            provider = self._provider_of(group)
            name = group.table[provider].name
            configured = bool(group.stored_key())
            group.keyState.setText("已配置" if configured else "未配置")
            group.keyEdit.setPlaceholderText(
                "已配置，留空保留" if configured else f"输入 {name} API 密钥")
            if self._compact:
                name = group.providerBox.fontMetrics().elidedText(name, Qt.ElideRight, 180)
            group.providerBox.setText(name)
        # 中转的地址走下面那一组共用字段，起草这行的 Base URL 就不重复露了
        custom = (self._provider_of(self.draft) in providers.CUSTOM
                  and self._provider_of(self.draft) != "relay")
        self.baseLabel.setVisible(custom)
        self.baseEdit.setVisible(custom)
        on_relay = "relay" in (self._provider_of(self.jev), self._provider_of(self.draft))
        for widget in (self.relayLabel, self.relayEdit, self.judgePathLabel, self.judgePathEdit,
                       self.thinkStyleBox):
            widget.setVisible(on_relay)
        self.relayHint.setVisible(on_relay and not self._compact)
        # 判断口只有判断也走中转时才用得上，起草走中转时藏起来少一行
        judge_relay = self._provider_of(self.jev) == "relay"
        self.judgePathLabel.setVisible(judge_relay)
        self.judgePathEdit.setVisible(judge_relay)

    def _fetch_models(self, group):
        """「获取模型」：拿填的 key（没填就拿存的）去问接口，网络调用丢后台线程。"""
        provider = self._provider_of(group)
        on_relay = provider == "relay"  # 中转的地址在那个共用字段里，不在起草那行的 Base URL
        custom = group.kind == "draft" and provider in providers.CUSTOM and not on_relay
        base = (self.baseEdit.text().strip() if custom
                else self.relayEdit.text().strip() if on_relay else None)
        key = group.keyEdit.text().strip() or group.stored_key()
        if not key:
            group.status.setText("先填密钥")
            return
        if (custom or on_relay) and not base:
            group.status.setText("先填中转地址" if on_relay else "先填 Base URL")
            return
        group.status.setText("获取中…")
        group.fetchButton.setEnabled(False)
        threading.Thread(target=lambda: self._list_models(group, provider, key, base),
                         daemon=True).start()

    def _list_models(self, group, provider, key, base):
        """后台线程：判断走 jev_client，起草按协议走 llm；失败把原因一起送回主线程。"""
        try:
            if group.kind == "jev":
                models = jev_client.list_models(provider, key, base_url=base or "")
            else:
                spec = providers.DRAFT_PROVIDERS[provider]
                # 中转的地址要归一（带不带 /v1、带没带 /chat/completions 都得认）
                base = relay.openai_base(base) if provider == "relay" and base else base
                models = llm.list_models(spec.protocol, base or spec.base, key, headers=spec.headers)
                if spec.keep:  # 目录里混了别的协议时，只留这条路打得通的
                    models = [m for m in models if spec.keep(m)]
            reason = "" if models else "这个来源没返回任何模型"
        except Exception as exc:  # 线程里漏异常会静默吞掉，按钮就永远停在禁用态
            models, reason = [], str(exc)[:120]
        self._fetched.done.emit(group, models, reason)

    def _models_fetched(self, group, models, reason):
        """回到主线程：填进下拉框，原来选中的还在列表里就留着。"""
        group.fetchButton.setEnabled(True)
        if not models:
            group.status.setText(reason or "获取失败，检查密钥、网络或 Base URL")
            return
        current = group.modelBox.text().strip()
        group.modelBox.clear()
        group.modelBox.addItems(models)
        if current in models:
            group.modelBox.setCurrentIndex(models.index(current))
        else:
            group.modelBox.setText(current)  # 手打的没在列表里也不清掉
        group.status.setText(f"共 {len(models)} 个")

    def _set_group(self, group, provider, model):
        """把存下来的来源和模型放回一组控件里；填充不算用户操作，别触发换来源的重置。"""
        group.providerBox.blockSignals(True)
        group.providerBox.setCurrentIndex(group.ids.index(provider))
        group.providerBox.blockSignals(False)
        group.keyEdit.clear()
        group.modelBox.clear()
        group.modelBox.setText(model)
        group.status.setText("")

    def _load_settings(self):
        relationship = settings.relationship()
        index = next((i for i, (_, value) in enumerate(_RELATIONSHIPS) if value == relationship),
                     len(_RELATIONSHIPS) - 1)
        self.relationshipBox.setCurrentIndex(index)
        self.relEdit.setText(relationship if _RELATIONSHIPS[index][1] is None else "")
        self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        self._presetTexts = dict(settings.style_texts())
        self._presetShown = ""  # 下面 setCurrentIndex 会触发换型，别拿上一轮的键去收框里的字
        self.presetBox.setCurrentIndex(
            next((i for i, (k, _) in enumerate(_PRESET_CHOICES) if k == settings.style_preset()), 0))
        self._preset_type_changed()  # 索引没变时上面不发信号，框得自己刷一次
        self.contextBox.setValue(settings.context())
        self.targetSwitch.setChecked(settings.reply_target())
        self._set_group(self.jev, settings.jev_provider(), settings.jev_model())
        self._set_group(self.draft, settings.draft_provider(), settings.draft_model())
        self.baseEdit.setText(settings.draft_base_url())
        self.relayEdit.setText(settings.relay_base_url())
        self.judgePathEdit.setText(settings.relay_judge_path())
        self.thinkStyleBox.setCurrentIndex(
            _THINK_STYLE_ORDER.index(settings.relay_thinking_style()))
        self.thinkingSwitch.setChecked(settings.thinking())
        self.updateSwitch.setChecked(settings.check_update())
        self.set_debug_switch(settings.debug_view())  # 屏蔽信号地拨，别在加载时开关一遍窗口
        self.petSwitch.blockSignals(True)  # 同上：加载时别真去开关宠物
        self.petSwitch.setChecked(settings.pet_enabled())
        self.petSwitch.blockSignals(False)
        self._sync_model_fields()  # 上面屏蔽了信号，这里补一次
        self.settingsFeedback.hide()

    def _save(self):
        relationship = _RELATIONSHIPS[self.relationshipBox.currentIndex()][1]
        relationship = relationship or self.relEdit.text().strip()
        jev_provider = self._provider_of(self.jev)
        draft_provider = self._provider_of(self.draft)
        base = self.baseEdit.text().strip()
        if not relationship:
            self._settings_feedback("请填写关系背景，或选择一个已有选项。", error=True)
            self.relEdit.setFocus()
            return
        if draft_provider in providers.CUSTOM and draft_provider != "relay" and not base:
            self._settings_feedback("自定义来源要填 Base URL。", error=True)
            self.baseEdit.setFocus()
            return
        relay_base = self.relayEdit.text().strip()
        if "relay" in (jev_provider, draft_provider):
            if not relay_base:
                self._settings_feedback("来源选了第三方中转，要填中转地址。", error=True)
                self.relayEdit.setFocus()
                return
            if not relay_base.startswith(("http://", "https://")):
                self._settings_feedback("中转地址要以 http:// 或 https:// 开头。", error=True)
                self.relayEdit.setFocus()
                return
        for group, provider in ((self.jev, jev_provider), (self.draft, draft_provider)):
            name = group.table[provider].name
            if not group.keyEdit.text().strip() and not group.stored_key():
                self._settings_feedback(f"请先填写 {group.keyTitle} 的 API 密钥。", error=True)
                group.keyEdit.setFocus()
                return
            if not group.modelBox.text().strip():
                self._settings_feedback(f"{name} 请先获取并选择一个模型。", error=True)
                group.modelBox.setFocus()
                return
        try:
            settings.save(relationship, self.contextBox.value(),
                          jev_provider_text=jev_provider,
                          jev_key_text=self.jev.keyEdit.text().strip() or None,
                          jev_model_text=self.jev.modelBox.text().strip(),
                          draft_provider_text=draft_provider,
                          llm_key_text=self.draft.keyEdit.text().strip() or None,
                          draft_model_text=self.draft.modelBox.text().strip(),
                          draft_base_url_text=base,
                          relay_base_url_text=relay_base,
                          relay_judge_path_text=self.judgePathEdit.text().strip(),
                          relay_thinking_style_text=_THINK_STYLE_ORDER[
                              max(0, self.thinkStyleBox.currentIndex())],
                          reply_target_on=self.targetSwitch.isChecked(),
                          style_preset_text=self._preset_key(),
                          style_texts_dict=self._preset_dirty(),
                          thinking_on=self.thinkingSwitch.isChecked(),
                          check_update_on=self.updateSwitch.isChecked(),
                          pet_enabled_on=self.petSwitch.isChecked())
        except Exception:
            self._settings_feedback("保存失败，请检查配置文件是否可写后重试。", error=True)
            return
        self._load_settings()
        self._render_targets()  # 开关刚改过，回到首页时这一行该显该藏得重算一次
        self._settings_feedback("设置已保存，将用于下一次回复。")
        self.setupButton.hide()
        if not self.cands and not self._busy:
            self._empty_text()
            self.set_status("设置已就绪，等待新消息", "idle")

    def _preset_key(self):
        """下拉里选中的类型键；空串 = 不用。"""
        return _PRESET_CHOICES[max(0, self.presetBox.currentIndex())][0]

    def _preset_stash(self):
        """把框里正在改的正文收回内存——换类型、保存之前都得先收一次。"""
        if self._presetShown:
            self._presetTexts[self._presetShown] = self.presetEdit.toPlainText().strip()

    def _preset_dirty(self):
        """收回内存之后，挑出跟内置原文不一样的那些——只有这些值得写进 config.json。"""
        self._preset_stash()
        return {k: t for k, t in self._presetTexts.items() if t != styles.default_text(k)}

    def _preset_type_changed(self, _=None):
        """换了类型：先把框里那份收好，再换上这一型的（改过的优先，没改过就用内置原文）。
        「不用」把框灰掉——那会儿框里写什么都进不了 prompt，别让人白写。"""
        self._preset_stash()
        key = self._preset_key()
        self._presetShown = key
        self.presetEdit.setPlainText(self._presetTexts.get(key, styles.default_text(key)))
        self.presetEdit.setEnabled(bool(key))
        self.presetReset.setEnabled(bool(key))
        self.presetReset.setText("清空" if key == styles.CUSTOM else "重置为内置原文")

    def _preset_reset(self):
        """回到内置原文（自定义就是清空）。只动框里的字，落盘还是走保存。"""
        key = self._preset_key()
        self._presetTexts.pop(key, None)
        self.presetEdit.setPlainText(styles.default_text(key))

    def _preset_count(self):
        """框里多少字。内置的都压在 200 以内，自己加写的超过这个数就该掂量一下了。"""
        n = len(self.presetEdit.toPlainText().strip())
        self.presetCount.setText(f"{n} 字" + (f"，超过 {styles.LIMIT} 了，会盖过对话本身"
                                              if n > styles.LIMIT else ""))

    def _debug_toggled(self, on):
        """调试视图独立于「保存设置」：拨一下就开窗/收窗，顺手落盘，重启还在。"""
        settings.save(debug_view_on=on)
        if self.on_toggle_debug:
            self.on_toggle_debug(on)

    def _pet_toggled(self, on):
        """宠物形态独立于「保存设置」：拨一下就换形态，顺手落盘，重启还在。"""
        settings.save(pet_enabled_on=on)
        self.pet_enabled = bool(on) and not self.pet.mascot.pix.isNull()
        if self.pet_enabled:
            self.pet.clamp_to_screen()
            self.pet.show()
            self.collapse()
        else:
            self.pet.hide()
            self.bar.hide()
            self.win.show()

    def set_debug_switch(self, on):
        """调试窗被用户直接关掉时把开关拨回去；屏蔽信号，免得又回调一圈。"""
        self.debugSwitch.blockSignals(True)
        self.debugSwitch.setChecked(on)
        self.debugSwitch.blockSignals(False)

    def _settings_feedback(self, text, error=False):
        color = theme.DANGER if error else _ACCENT
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.settingsFeedback, qss, qss)
        self.settingsFeedback.setText(text)
        self.settingsFeedback.show()

    def open_settings(self):
        # 先确保面板露出来：宠物形态下面板默认是收着的，没这行会把设置页开在一个看不见的窗里
        self.show_panel()
        if self.pages.currentWidget() != self.settingsPage:
            self._load_settings()
        self.pages.setCurrentWidget(self.settingsPage)
        self.settingsButton.setEnabled(False)
        # 还没配 key 就直接落在「模型设置」页，省得用户自己找那一页
        configured = settings.has_key()
        self._switch_tab("preference" if configured else "models")
        (self.relationshipBox if configured else self.jev.keyEdit).setFocus()

    def _switch_tab(self, key):
        """设置页的页签：只切显隐，控件不重建——重建会把用户填了一半的内容弄丢。"""
        for name, button in self.tabButtons.items():
            active = name == key
            button.setChecked(active)
            button.setStyleSheet(_tab_qss(active))
        self.preferenceCard.setVisible(key == "preference")
        self.modelsCard.setVisible(key == "models")
        self.styleCard.setVisible(key == "style")

    def _back_home(self):
        self.jev.keyEdit.clear()
        self.draft.keyEdit.clear()
        self.pages.setCurrentWidget(self.home)
        self.settingsButton.setEnabled(True)

    def _cand_at(self, position):
        """界面上第 position 个位置（从 0 数）摆的是哪条候选（`cands` 里的原始下标）。

        候选条和面板都按推荐顺序重排过：看到的「1/2/3」是排完的位置，跟原始下标只有在
        推荐那条本来就排第一时才重合。没有候选时退回 position，反正 _fill 会拦掉。"""
        if 0 <= position < len(self._ordered):
            return self._ordered[position][0]
        return position

    def _fill(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        try:
            self.on_fill(self.cands[index])
        except Exception as e:
            # 状态栏保持友好文案；真实原因和压缩堆栈进聊天记录，认得出是哪一步炸的
            import traceback
            self.set_status("未能填入，请确认聊天窗口可用后重试，或复制回复。", "error")
            self.log(f"[填入失败] {type(e).__name__}: {e}")
            self.log(f"[填入失败堆栈] {' '.join(traceback.format_exc().split())[:300]}")
            return
        self.set_status("已尝试填入，请确认内容后发送。", "success")

    def _copy(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        self.app.clipboard().setText(self.cands[index])
        self.set_status("回复已复制，可粘贴并修改。", "success")

    def _capture_toggled(self, on):
        """用户自己拨的开关：界面先改，再通知父进程去开/停采集。"""
        self._capture_text(on)
        if self.on_toggle_capture:
            self.on_toggle_capture(on)

    def set_update(self, latest, url):
        """main.py 后台线程查到比当前新的版本才会调这个。只显示版本号和 Release 链接，别的什么都没有。"""
        self.updateLabel.setText(f"有新版本 v{latest}")
        self.updateLink.setUrl(url)
        self.updateBar.show()

    def set_capture(self, on, reason=""):
        """父进程回报的状态：只改界面，不回调（不然和父进程来回打架）。reason 为空用默认说明。"""
        self.captureSwitch.blockSignals(True)
        self.captureSwitch.setChecked(on)
        self.captureSwitch.blockSignals(False)
        self._capture_text(on, reason)
        self.pet.set_paused(not on)  # 宠物淡下去、去个色，一眼看出现在没在读屏

    def _capture_text(self, on, reason=""):
        """开关状态对应的状态行和空态文案。已有的候选不受影响，暂停了照样能填入/复制。"""
        configured = settings.has_key()
        if not on:
            self.set_status(reason or "采集已暂停，聊天内容不再读取", "warning")
        elif configured:
            self.set_status("等待新消息", "idle")
        else:
            self.set_status("请先在设置中配置模型", "warning")
        if self._busy or self.cands:  # 正在生成或已有候选时，空态卡片本来就看不见
            return
        if not on:
            self.emptyTitle.setText("采集已暂停")
            self.emptyHint.setText("聊天内容暂时不再读取。\n打开标题栏的开关，继续接收新消息。")
            self.setupButton.setVisible(not configured)
        else:
            self._empty_text()

    def set_busy(self, busy):
        self._busy = busy
        self.progress.setVisible(busy)
        if busy:
            self.invalidate_replies()
            self.progress.start()
            self.set_status("正在根据新消息整理回复…", "busy")
            if not self.cands:
                self.emptyTitle.setText("正在想一句合适的回复")
                self.emptyHint.setText("正在结合上下文生成建议，稍等一下。")
                self.setupButton.hide()
        else:
            self.progress.stop()
            if not self.cands:
                self._empty_text()
        for card in self.cards:
            card.set_available(self._current and not busy)
        self._sync_bar()

    def set_waiting(self):
        """对方还在发消息（静默窗口里等）：把空态说清楚。

        这会儿既没候选也没在跑分析，空态默认是「等待对方的新消息」——刚收到消息还这么说，
        看着像没反应。等到真开始判断（set_busy）或者他发完了，文案自然会被换掉。"""
        if self.cands or self._busy:
            return
        self.emptyTitle.setText("对方还在发消息")
        self.emptyHint.setText("等他不说了再给建议，免得你回到半句上。")
        self.setupButton.hide()

    def _empty_text(self):
        """空态卡片的默认文案，配好没配好两套说法。"""
        configured = settings.has_key()
        self.emptyTitle.setText("等待对方的新消息" if configured else "先设置，再开始")
        self.emptyHint.setText("保持聊天窗口打开。\n收到新消息后，回复建议会出现在这里。"
                               if configured else "配置模型和关系背景，\n让建议更贴近你们的对话。")
        self.setupButton.setVisible(not configured)

    def invalidate_replies(self):
        self._current = False
        if self.cands:
            self.updated.setText("上次建议")
        for card in self.cards:
            card.set_available(False)
        # 候选条跟着清空：新消息一来，旧候选就不该再摆在宠物旁边了
        self._ordered = []
        self._sync_bar()

    def has_reply(self):
        """现在有没有可填的候选。main.py 拿它派生 phase，别去读 _current。"""
        return self._current

    def _suggest_text(self):
        """候选条底部那行「建议：xxx · 紧张度 N/9」，口径跟面板的洞察卡一致。"""
        action = _choice(self._answers, "best_action")
        score = (self._answers.get("danger_level") or {}).get("score")
        valid = isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9
        return f"建议：{action} · 紧张度 {score:.0f}/9" if valid else f"建议：{action}"

    def set_voice(self, chat, items):
        """子进程报来的语音气泡（位置+时长）。只有正看着的那个会话才反映到候选条上。"""
        if items:
            self._voices[chat] = list(items)
        else:
            self._voices.pop(chat, None)
        if chat == self._shown:
            self._sync_bar()

    def _voice_text(self):
        """候选条上那句「语音消息 8"」。已经有候选可看时让位给候选——那时用户要的是挑一句回，
        不是转文字。没语音就返回空串。"""
        if self._phase in ("thinking", "ready"):
            return ""
        items = self._voices.get(self._shown) or []
        return f"语音消息 {items[-1][4]}" if items else ""

    def _convert_voice(self):
        """候选条上的「转文字」：交给父进程去右键那条语音、点菜单第一项。

        转出来的文字会作为新气泡被采集链路照常读到并触发判断，这里点完就不管了。"""
        if not self.on_voice_convert:
            return
        self.bar.hide()  # 先把自己的条收掉：它置顶，可能正好压在语音气泡上挡住右键
        self._barTimer.stop()
        reason = self.on_voice_convert()
        if reason:
            self.set_status(reason, "warning")
            self._show_bar()  # 没成，把条摆回来让用户重试或自己手动转
            return
        self._voices.pop(self._shown, None)
        self.set_status("已点了「语音转文字」，转出来我会自动接着判断。", "busy")

    def _sync_bar(self):
        """把紧凑候选条刷成和面板一致。两边共用 self.cands / self._ordered，不新增数据流。"""
        items = [(index, position + 1, self.cands[index], score, recommended)
                 for position, (index, score, recommended) in enumerate(self._ordered)]
        heard = self.hers.get(self._shown) or self.hers.get(self._chat) or ""
        self.bar.set_items(items, self._suggest_text() if items else "", self._phase, heard,
                           self._voice_text())

    def set_phase(self, phase):
        """main.py 派生出来的流水线状态。同态重复调用是空操作，否则每 50ms 重放一次会闪。"""
        if phase == self._phase:
            return
        self._phase = phase
        self._sync_bar()
        if not self.pet_enabled:
            return
        self.pet.set_phase(phase)
        self._barTimer.stop()
        if phase in ("notify", "thinking", "ready"):
            self._place_bar()
            self.bar.show()
            self._barTimer.start(_BAR_HOLD_MS)  # 没人理就自己收回去，宠物上的角标留着
        else:
            self.bar.hide()

    def _show_bar(self):
        """悬停 / 单击宠物：把紧凑候选条摆出来。没有可看的东西就不弹，免得弹个空的。"""
        if not self.pet_enabled:
            return
        if self._phase not in ("notify", "thinking", "ready") and not self._voice_text():
            return  # 静息态又没语音可转，弹出来是空的
        self._place_bar()
        self.bar.show()
        self._barTimer.start(_BAR_HOLD_MS)

    def _hold_bar(self):
        """鼠标搭在候选条上就续命，别正看着它自己跑掉。"""
        if self.bar.isVisible():
            self._barTimer.start(_BAR_HOLD_MS)

    def _schedule_bar_hide(self):
        """鼠标离开候选条：宽限一下再收，够用户从宠物挪到条上。"""
        if self.bar.isVisible():
            self._barTimer.start(_BAR_LEAVE_MS)

    def show_panel(self):
        """展开完整面板：贴到宠物旁边（没开宠物就还用原来那个右上角位置）。"""
        if self.pet.isVisible():
            self._place_panel()
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()

    def collapse(self):
        """收起面板。宠物留着——它是常驻入口，也是拖拽的锚点。"""
        self.win.hide()

    def _build_pet_menu(self):
        """宠物右键菜单的内容：暂停采集 / 全屏（主页）/ 设置。

        三项都只是既有入口的快捷方式，不新增数据流。单独拆成一个方法是为了
        tools/preview_ui.py 能直接摆出来截图——exec() 是嵌套事件循环，截图回调进不去。"""
        menu = RoundMenu(parent=self.pet)
        capturing = self.captureSwitch.isChecked()
        toggle = Action(FIF.PAUSE if capturing else FIF.PLAY,
                        "暂停采集" if capturing else "继续采集", menu)
        # 拨的就是标题栏那个开关：checkedChanged → _capture_toggled → 父进程，
        # 跟手动点一下完全同一条路。不直接调 on_toggle_capture，否则开关自己还停在旧状态。
        toggle.triggered.connect(lambda: self.captureSwitch.setChecked(not capturing))
        menu.addAction(toggle)
        home = Action(FIF.HOME, "全屏（主页）", menu)
        home.triggered.connect(self._show_home)
        menu.addAction(home)
        prefs = Action(FIF.SETTING, "设置", menu)
        prefs.triggered.connect(self.open_settings)
        menu.addAction(prefs)
        return menu

    def _pet_menu(self, pos):
        """右键宠物：在鼠标处弹菜单。"""
        self._build_pet_menu().exec(pos)

    def _show_home(self):
        """「全屏（主页）」：面板露出来，并回到回复建议那一页（跟在设置页点「返回」一样）。"""
        self.show_panel()
        if self.pages.currentWidget() is not self.home:
            self._back_home()

    def _quit(self):
        """真退出：三个窗口一起收掉。「收起」和「退出」是两回事，别混。"""
        self.hotkeys.unregister_all()
        self.pet.hide()
        self.bar.hide()
        self.win.close()
        self.app.quit()

    def _place_panel(self):
        """把面板摆在宠物旁边：优先左边，放不下就右边，最后夹到屏幕内。"""
        area = self.win.screen().availableGeometry()
        gap = 12
        x = self.pet.x() - self.win.width() - gap
        if x < area.left() + gap:
            x = self.pet.x() + self.pet.width() + gap
        x = min(max(x, area.left() + gap), area.right() - self.win.width() - gap)
        y = min(max(self.pet.y(), area.top() + gap), area.bottom() - self.win.height() - gap)
        self.win.move(x, y)

    def _place_bar(self):
        """候选条贴宠物上方；上面放不下就翻到下面，再横向夹到屏幕内。"""
        area = self.bar.screen().availableGeometry()
        gap = 8
        x = self.pet.x() + self.pet.width() - self.bar.width()
        y = self.pet.y() - self.bar.height() - gap
        if y < area.top() + gap:
            y = self.pet.y() + self.pet.height() + gap
        x = min(max(x, area.left() + gap), area.right() - self.bar.width() - gap)
        self.bar.move(x, y)

    def _note_trouble(self, text):
        """把一段故障原文挂在状态栏旁边；空串 = 把上一次的收掉。"""
        self._errorDetail = " ".join(str(text or "").split())
        self.detailButton.setVisible(bool(self._errorDetail))

    def _show_error_detail(self):
        if self._errorDetail:
            _ErrorBox(self._errorDetail, self.win).exec()

    def set_error(self, text):
        """生成失败。状态栏给一句短的，**原文一个字都不改地留着**等「查看详情」——
        以前这儿永远是一句「请检查网络和服务设置」，真正的原因（中转回的 401 之类）
        埋在聊天记录里，等于没提示。"""
        self._note_trouble(text)
        self.set_status("生成失败：" + _short_error(self._errorDetail), "error")
        self.log("分析失败：" + self._errorDetail)

    def set_status(self, text, kind="idle"):
        colors = {"idle": _MUTED, "busy": _ACCENT, "success": _ACCENT,
                  "warning": theme.WARN, "error": theme.DANGER}
        markers = {"idle": "●", "busy": "●", "success": "✓", "warning": "!", "error": "!"}
        qss = f"BodyLabel {{ color: {colors.get(kind, _MUTED)}; background: transparent; }}"
        setCustomStyleSheet(self.status, qss, qss)
        self.status.setText(f"{markers.get(kind, '●')}  {text}")
        if kind == "error" and self._busy:
            self.set_busy(False)
        if kind == "error" and not self.cands:
            self.emptyTitle.setText("暂时没有可用的回复")
            self.emptyHint.setText("请按上方提示处理。收到新的对方消息后会再次尝试。")
            self.setupButton.setVisible(not settings.has_key())

    def _toggle_history(self):
        """展开/收起聊天记录。展开时把末尾那根弹簧松掉，剩下的竖向空间才轮到记录区吃。"""
        self.feed.setVisible(self.feed.isHidden())
        self.homeBody.setStretch(self.homeBody.count() - 1, 0 if self.feed.isVisible() else 1)
        self._history_title()

    def _history_title(self):
        action = "展开" if self.feed.isHidden() else "收起"
        count = self.counts.get(self._shown, 0)
        self.historyButton.setText(f"{action}聊天记录" + (f" · {count}" if count else ""))

    def log(self, line):
        """采集状态行：只进正在看的那个会话，不按会话存。"""
        self.feed.system(line)

    def log_message(self, who, text, name="", timestamp=None, chat=None, voice=""):
        """按会话存一份；只有正在看的那个会往显示区里写。

        voice 非空 = 这条是语音转出来的字，值是那条语音的时长（`3"`）：能对上前面那条
        「🔊 语音消息 3"」就并成一条（微信那边本来就是一条：气泡 + 转写），对不上就单独
        一条、气泡里标一行「🔊 语音转文字」。"""
        chat = chat or self._shown
        timestamp = timestamp or datetime.now().strftime("%H:%M")
        if voice and self._merge_voice(chat, who, text, name, voice):
            if who == "her":
                self.hers[chat] = text
                if chat == self._shown:
                    self._show_latest(text)  # 并了要重画，最新的那句也换掉
            return
        self.counts[chat] = self.counts.get(chat, 0) + 1
        lines = self.feeds.setdefault(chat, [])
        lines.append((who, text, name, timestamp, voice))
        del lines[:-_LOG_LINES]
        if who == "her":
            self.hers[chat] = text
        self._add_chat(chat)
        if chat != self._shown:
            return
        self.feed.message(who, text, name, timestamp, chat, voice)
        if who == "her":
            self._show_latest(text)
        self._history_title()

    def _merge_voice(self, chat, who, text, name, dur):
        """语音转出来的字并回它那条语音：把「🔊 语音消息 N"」那条占位换成带时长的转写。

        微信那边本来就是一条（语音气泡 + 底下的转写），分成两条看着像对方说了两句话。
        并上了返回 True。从后往前找**还没并过**的那条——并过之后 text 就不是占位那句了，
        不会再撞上同名占位。时间沿用语音那条的（消息本来的时间，不是转文字的时间）。"""
        mark = f"🔊 语音消息 {dur}"
        lines = self.feeds.get(chat, [])
        for i in range(len(lines) - 1, -1, -1):
            _, old, _, stamp, _ = lines[i]
            if old == mark:
                lines[i] = (who, text, name, stamp, dur)
                if chat == self._shown:
                    self._render_feed()
                return True
        return False

    def _render_feed(self, keep_scroll=True):
        """按 feeds[正在看的会话] 整屏重画。切会话和「并语音」都要走它。

        重画会把滚动条打回顶部，所以先记住用户是不是正贴着底看：贴着就跟着走，
        翻着旧记录看的时候别把他拽回底部。切会话（keep_scroll=False）则一律落到底。"""
        bar = self.feed.scroll.verticalScrollBar()
        keep = bar.value() if keep_scroll and not self.feed.at_bottom() else None
        self.feed.clear()
        for who, text, name, stamp, voice in self.feeds.get(self._shown, []):
            self.feed.message(who, text, name, stamp, self._shown, voice)
        if keep is not None:
            QTimer.singleShot(0, lambda: bar.setValue(min(keep, bar.maximum())))

    def _show_latest(self, text):
        self.latest.setText(text if len(text) <= 120 else text[:120] + "…")
        self.latest.setToolTip(text)
        self.context.show()

    def current_chat(self):
        """界面上正在看的会话（不一定是微信当前开着的那个）。"""
        return self._shown

    def set_chat(self, title):
        """微信切到了哪个会话：登记进下拉框并自动跟过去，不触发用户选择的回调。"""
        if not title:
            return
        browsing = self._shown != self._chat  # 正看着的就是它、但之前是「浏览中」：也得重画，把填入放开
        self._chat = title
        self._add_chat(title)
        if title != self._shown or browsing:
            self.chatBox.blockSignals(True)
            self.chatBox.setCurrentIndex(self.chatBox.findText(title))
            self.chatBox.blockSignals(False)
            self._switch_to(title)
        self._follow_text()

    def _add_chat(self, title):
        """新会话自动进下拉框；addItem 添第一条时会自己选中，别让它触发切换。"""
        if not title or self.chatBox.findText(title) >= 0:
            return
        self.chatBox.blockSignals(True)
        self.chatBox.addItem(title)
        self.chatBox.blockSignals(False)

    def _on_chat_selected(self, index):
        """用户自己挑了一个会话：只换看的内容，微信那边不动。"""
        title = self.chatBox.itemText(index)
        if title and title != self._shown:
            self._switch_to(title)

    def _switch_to(self, title):
        """换正在看的会话：记录、对方最近说、条数、上次的建议一起换过去。"""
        self._shown = title
        self._render_feed(keep_scroll=False)
        her = self.hers.get(title)
        if her:
            self._show_latest(her)
        else:
            self.context.hide()
        self._history_title()
        self._follow_text()
        self._render_targets()
        self.show_cached(self.result_of(title) if self.result_of else None)

    def set_targets(self, chat, senders, current):
        """某个会话的发言人名单（最近的在前）和当前回复对象；正看着它才重画。"""
        self.targets[chat] = (list(senders), current)
        if chat == self._shown:
            self._render_targets()

    def _render_targets(self):
        """开关关着、或这个会话没有发言人（单聊），这一行就不出现。
        重填下拉框时屏蔽信号，别把自己的填充当成用户挑的。"""
        senders, current = self.targets.get(self._shown, ([], None))
        visible = bool(senders) and settings.reply_target()
        self.targetRow.setVisible(visible)
        if not visible:
            return
        self.targetBox.blockSignals(True)
        self.targetBox.clear()
        self.targetBox.addItems(senders)
        self.targetBox.setCurrentIndex(senders.index(current) if current in senders else 0)
        self.targetBox.blockSignals(False)

    def _on_target_selected(self, index):
        """用户挑了回复对象。浏览别的会话时改的就是那个会话的对象——记录、候选也都按会话走，口径一致。"""
        name = self.targetBox.itemText(index)
        if not name:
            return
        senders, _ = self.targets.get(self._shown, ([], None))
        self.targets[self._shown] = (senders, name)
        self.set_status(f"按「{name}」重新生成…", "busy")
        if self.on_target_change:
            self.on_target_change(self._shown, name)

    def at_prefix_enabled(self):
        """填入时要不要带「@名字 」前缀（只记在界面上，不落盘）。"""
        return self.atCheck.isChecked()

    def _follow_text(self):
        self.chatFollow.setText(("跟随" if self._shown == self._chat else "浏览中") if self._chat else "")

    def show_cached(self, result):
        """把某个会话上次的结果放回界面；没有就回到空态。浏览别的会话时只给看不给填——
        微信当前开着的不是它，填进去就串会话了。"""
        if result:
            self.show(result)
        else:
            self.cands = []
            self._clear_cards()
            self.insight.hide()
            self.referenceNote.hide()
            self.empty.show()
            self.updated.setText("")
            self._empty_text()
        if self._shown != self._chat:
            self.invalidate_replies()
            self.set_status(f"正在浏览「{self._shown}」，只看不填；切回这个会话才能用。")

    def show(self, result):
        """按推荐顺序展示，按钮始终绑定 candidates 的原始索引。"""
        self.cands = result["candidates"]
        self.set_busy(False)
        self._current = bool(self.cands)
        self._clear_cards()
        self._note_trouble(result.get("trouble"))  # 这次成了/没成，都把上一次的报错收掉
        best = result.get("best_index", 0)
        if not result.get("ranked", True):  # 老结果没这个键，按「排过序」处理
            best = None  # 排序没跑成：三条按起草顺序摆，谁也不标「推荐」——那不是模型选的
        elif best not in range(len(self.cands)):
            best = 0
        raw_scores = result.get("scores") or []
        scores = [raw_scores[i] if i < len(raw_scores) else None for i in range(len(self.cands))]
        if not any(scores):  # 全 0/None（旧结果或接口未返回）就不展示百分比
            scores = [None] * len(self.cands)
        # 按概率降序排，推荐位（API 给的 choice）强制第一，同分按原索引
        order = sorted(range(len(self.cands)), key=lambda i: (i != best, -(scores[i] or 0), i))
        for position, index in enumerate(order):
            # 编号从 1 起，跟候选条的序号方块和 Ctrl+1/2/3 对齐；推荐那条改写「推荐回复」，
            # 所以排过序时看到的是「推荐回复 / 备选 2 / 备选 3」
            card = _ReplyCard(self, index, recommended=index == best, number=position + 1,
                              score=scores[index])
            self.replyBox.addWidget(card)
            self.cards.append(card)
        reply_to = result.get("reply_to")
        self.insightTitle.setText(f"对话参考 · 回复给 {reply_to}" if reply_to else "对话参考")
        answers = result.get("answers") or {}
        self._answers = answers
        self._ordered = [(index, scores[index], index == best) for index in order]
        self.summary.setText("建议：" + _choice(answers, "best_action"))
        self.intent.setText("可能意图 · " + _choice(answers, "true_intent") +
                            "\n可能需要 · " + _choice(answers, "she_needs"))
        score = (answers.get("danger_level") or {}).get("score")
        valid_score = isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9
        self.tension.setText(f"紧张度 {score:.0f}/9" if valid_score else "紧张度待判断")
        color = theme.WARN if valid_score and score >= 3 else _MUTED
        if valid_score and score >= 6:
            color = theme.DANGER
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.tension, qss, qss)
        self.empty.setVisible(not self.cands)
        self.insight.setVisible(bool(self.cands))
        self.referenceNote.setVisible(bool(self.cands) and not self._compact)
        self.updated.setText(datetime.now().strftime("%H:%M") + " 更新")
        if self.cands and self._errorDetail:
            # 候选是好的、只是判断/排序那一步挂了：照样能用，但要说清楚为什么没有概率和推荐
            self.set_status("判断服务没应答，这三条是按对话直接起草的；点「查看详情」看原因。",
                            "warning")
        elif self.cands:
            self.set_status("建议已更新，选一句适合你的回复", "success")
        else:
            self.set_status("未生成可用回复，请等待下一条新消息。", "error")
        self._sync_bar()

    def _clear_cards(self):
        for card in self.cards:
            self.replyBox.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []

    def after(self, ms, fn):
        QTimer.singleShot(ms, fn)

    def run(self):
        self.app.exec()
