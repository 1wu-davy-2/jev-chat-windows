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

from app import settings, shortcut, theme
from app.pet import HotkeyHost, PetWindow
from app.theme import (
    FONT_2XL, FONT_DISPLAY, FONT_H1, FONT_LG, FONT_MD, FONT_SM, FONT_XL, FONT_XS,
    GAP_LG, GAP_MD, GAP_SM, GAP_XL, GAP_XS, RADIUS_LG, RADIUS_MD, RADIUS_SM,
    RADIUS_XL, SHADOW_PAD,
)
from app.version import VERSION
from core import chatlog, draft, jev_client, llm, paste, providers, relations, relay, styles
from core.questions import CHOICE_LABELS

_LOG_LINES = 300
_BAR_HOLD_MS = 25000  # 候选条自己待多久没人理就收起来（鼠标搭上来会重新计时）
_BAR_LEAVE_MS = 700   # 鼠标离开候选条后宽限这么久再收，够从宠物挪到条上
_MUTED = theme.MUTED   # 次要文字色（原来是偏绿的 #68776f，现在统一走 token）
_ACCENT = theme.SAGE   # 强调色（原来是 #18794e）
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


class _ConfirmBox(MessageBoxBase):
    """删除类操作问一句再动手。删了没有撤销，别做成点一下就没了。"""

    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(title, self)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(_label(text, FONT_MD))
        self.yesButton.setText("确定")
        self.cancelButton.setText("取消")


class _ImportBox(MessageBoxBase):
    """导入聊天记录：把微信「复制」出来的那几段文本粘进来，解析成消息行。

    粘进来的是**别人的**对话，里面可能带着 key（真机案例：导入的对话里夹着一把中转的
    `sk-…`）。这儿只管解析和让用户挑「哪个是我」——脱敏不在这一层做，在 `chatlog` 那个
    唯一的写库口子上（见 CLAUDE.md 硬约束 6）。

    解析是**边打边做**的：`textChanged` 每动一下就重解析一次，好让「解析出 N 条」和那个
    下拉框实时跟上。纯字符串操作，几百条也就毫秒级，不用防抖。"""

    def __init__(self, parent, chat):
        super().__init__(parent)
        self._rows = []
        self._names = []
        self._skipped = 0
        self._media = 0
        self._chat = chat
        # 上次给这个会话起过的名字，这次直接套上，不用重填
        self._saved = settings.chat_remarks(chat)
        self._remarkEdits = {}  # {原昵称: LineEdit}，只装「认不出来」的那些
        self.titleLabel = SubtitleLabel(f"导入到「{chat}」", self)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(_label(
            "在微信里选中几条消息 → 复制 → 粘到下面。格式是「昵称 / 时间 / 正文」，段间空一行。",
            FONT_XS, _MUTED))
        self.textEdit = TextEdit(self)
        self.textEdit.setPlaceholderText(
            "昵称\n2026年09月29日 13:58\n晚上吃啥\n\n另一个昵称\n2026年09月29日 13:58\n火锅鸡吧")
        self.textEdit.setMinimumHeight(170)
        self.textEdit.setMinimumWidth(430)
        self.viewLayout.addWidget(self.textEdit)

        # 「哪个是我」是**多选**：同一个人在不同场合昵称会变（真机上一个人既是那串隐形
        # 昵称、又是转发记录里的 ZBK），单选只能认一个，另一个会被当成对方——语气就学反了。
        # **默认一个都不勾**：以前是个默认选中第一项的下拉，用户没留意就选错了，整批
        # 记录的主客关系全反过来（真机上踩过，全库 176 条里同一昵称横跨 me 和 her 两边）。
        # 逼着主动勾一下，比事后查半天便宜。
        self.viewLayout.addWidget(_label("哪个是我（可多选）", FONT_SM))
        self.meHolder = QWidget()
        self.meBox = QHBoxLayout(self.meHolder)
        self.meBox.setContentsMargins(0, 0, 0, 0)
        self.meBox.setSpacing(GAP_MD)
        self.viewLayout.addWidget(self.meHolder)
        self._meBoxes = {}  # {原昵称: CheckBox}

        # 认不出的昵称（全隐形字符）给一个改名的入口，摆在「哪个是我」**下面**——
        # 得先知道谁是谁，上面那个下拉才挑得出来。没有这种昵称时这一片是空的，不占地方。
        self.remarkBox = QVBoxLayout()
        self.remarkBox.setSpacing(GAP_XS)
        self.viewLayout.addLayout(self.remarkBox)

        # 默认勾上：`[语音] 5"` 这种进上下文对模型就是噪音（它会被当成对方说的一句话）。
        # 想留个「当时发过语音」的痕迹就取消勾选，那会儿会换成跟实时采集一样的
        # 「🔊 语音消息 N"」占位——不滤的语音**不能**原样当正文存，见 main.import_history。
        self.mediaBox = CheckBox("跳过非文本消息（语音 / 图片 / 表情 / 转账 / 文件）")
        self.mediaBox.setChecked(True)
        self.viewLayout.addWidget(self.mediaBox)

        self.replaceBox = CheckBox("替换这个会话现有的聊天记录（不勾就是接在后面）")
        self.viewLayout.addWidget(self.replaceBox)

        self.summary = _label("把聊天记录粘进来就会自动解析。", FONT_XS, _MUTED)
        self.summary.setWordWrap(True)
        self.viewLayout.addWidget(self.summary)

        self.yesButton.setText("导入")
        self.cancelButton.setText("取消")
        self.yesButton.setEnabled(False)
        self.textEdit.textChanged.connect(self._reparse)
        # 勾选框变了要重解析一遍：滤掉的那些条数得跟着变，不然摘要里那句一直不动
        self.mediaBox.stateChanged.connect(self._reparse)

    def _label_of(self, raw):
        """这个昵称显示成什么：用户起的名字优先；没起名就按 `visible()` 把隐形字符
        画成 `·`——**不能显示原文**，那是一串空白，下拉框里几个选项会长得一模一样。"""
        edit = self._remarkEdits.get(raw)
        typed = edit.text().strip() if edit is not None else ""
        return typed or self._saved.get(raw) or paste.visible(raw)

    def _sync_names(self):
        """改名之后只刷勾选框的文字，**不重建**——重建会把正在打字的那个输入框弄失焦
        （每敲一个字符都会走到这儿）。"""
        for raw, box in self._meBoxes.items():
            box.setText(self._label_of(raw))
        self._update_summary()

    def _rebuild_me(self, names):
        """给「哪个是我」摆一排勾选框。只在昵称集合变了的时候重建，已勾的保持勾着。"""
        for i in reversed(range(self.meBox.count())):
            item = self.meBox.takeAt(i)
            holder = item.widget()
            if holder is not None:
                holder.deleteLater()  # 光 removeWidget 不会真删，留着等 GC 会漏一片
        checked = {raw for raw, box in self._meBoxes.items() if box.isChecked()}
        self._meBoxes = {}
        for raw in names:
            box = CheckBox(self._label_of(raw))
            box.setChecked(raw in checked)  # setChecked 在 connect 之前，不会提前触发一次
            box.stateChanged.connect(self._me_changed)
            self.meBox.addWidget(box)
            self._meBoxes[raw] = box

    def _me_changed(self, *_):
        self._update_summary()

    def _picked_me(self):
        """勾上的那些昵称（原样的，没改名之前的）。"""
        return [raw for raw, box in self._meBoxes.items() if box.isChecked()]

    def _update_summary(self):
        bits = []
        if self._rows:
            bits.append(f"解析出 {len(self._rows)} 条")
        if self._names:
            bits.append("发言者：" + "、".join(self._label_of(n) for n in self._names))
        if self._skipped:
            bits.append(f"{self._skipped} 段认不出来，跳过")
        if self._media:
            bits.append(f"{self._media} 条非文本消息已跳过")
        picked = self._picked_me()
        if len(picked) > 1:
            bits.append(f"「我」认了 {len(picked)} 个昵称")
        if self._names and not picked:
            # 一个都没勾时按钮也是灰的，得说清为什么，不然只看到「导入」点不动
            bits.append("还没勾哪个是你")
        self.summary.setText(" · ".join(bits)
                             or "还没解析出内容——检查一下是不是「昵称 / 时间 / 正文」的格式。")

    def _rebuild_remarks(self, names):
        """给「认不出来」的昵称摆一排改名输入框。只在集合变了的时候重建。"""
        for i in reversed(range(self.remarkBox.count())):
            item = self.remarkBox.takeAt(i)
            holder = item.widget()
            if holder is not None:
                holder.deleteLater()  # 光 removeWidget 不会真删，留在那儿等 GC 会漏一片
        self._remarkEdits = {}
        for raw in names:
            holder = QWidget()
            line = QHBoxLayout(holder)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(GAP_SM)
            line.addWidget(_label(f"「{paste.visible(raw)}」认不出是谁，起个名", FONT_XS, _MUTED))
            edit = LineEdit()
            edit.setPlaceholderText("比如：我 / 阿杰")
            edit.setText(self._saved.get(raw, ""))
            # 先填字再接信号：setText 也会发 textChanged，那会儿下拉框还没建好
            edit.textChanged.connect(self._sync_names)
            line.addWidget(edit, 1)
            self._remarkEdits[raw] = edit
            self.remarkBox.addWidget(holder)

    def _reparse(self, *_):
        # `*_` 是给 stateChanged 那种带参数信号用的（textChanged 不带，两边都得吃得下）
        rows, names, skipped, media = paste.parse(
            self.textEdit.toPlainText(), skip_media=self.mediaBox.isChecked())
        self._rows, self._skipped, self._media = rows, skipped, media
        # 只在发言者变了的时候重建勾选框。每敲一个字都重建的话，用户刚勾好的「哪个是我」
        # 会被顶掉——正文里随便加个字都会触发 textChanged。
        if names != self._names:
            self._names = names
            self._rebuild_remarks([n for n in names if paste.needs_label(n)])
            self._rebuild_me(names)
        self._update_summary()
        self.yesButton.setEnabled(bool(rows) and bool(self._picked_me()))

    def result_rows(self):
        """`(rows, 哪些是我, 要不要替换, 起过的名字)`。

        rows 的昵称已经换成用户起的名字了——调用方直接喂 `paste.to_messages` 就行。
        第二个是**一组**昵称：本人可能有好几个名字（隐形昵称 + 转发记录里的 ZBK），
        `to_messages` 收得下。
        第四个是 `{原昵称: 名字}`，调用方拿它落盘，下次导入不用重填。"""
        labels = {raw: e.text().strip() for raw, e in self._remarkEdits.items()}
        # 跟 rows 用**同一条**改名链，否则勾出来的名字在 rows 里找不到（隐形昵称那条尤其
        # 明显：rows 兜底留原文，_label_of 兜底换成 `·`，两边对不上）
        def _name_of(raw):
            return labels.get(raw) or self._saved.get(raw) or raw
        rows = [(_name_of(n), t, s) for n, t, s in self._rows]
        me = [_name_of(raw) for raw in self._picked_me()]
        return rows, me, self.replaceBox.isChecked(), {k: v for k, v in labels.items() if v}


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


class _RoundTabs(QWidget):
    """面板顶部的轮次切换：多轮候选才出现，点数字翻看第几条。

    **三张卡片一起翻**——切换按钮在面板顶部（「对方最近说」那一行右边），不是每张卡片各管各的
    （2026-10-01 定的）。轮数不够的候选摆它最后一条：不空着，也不重复摆一遍。
    没有多轮候选时整块藏起来，单条候选的面板跟以前一模一样——**除非**设置里开着多轮而这一轮
    AI 全给的是一句，那时摆一句 hint（「AI 建议回复一轮」）：不摆的话那块空着，用户分不清是
    AI 判断不用多轮、还是这个功能没生效。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_pick = None  # 点了第几条（从 0 起）
        self._count = 0
        self._current = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP_XS)
        self.buttons = []
        for i in range(settings.ROUNDS_MAX):  # 候选最多几条，跟设置里那个控件同一个上限
            button = QPushButton(str(i + 1), self)
            button.setFixedSize(20, 20)
            button.setCursor(Qt.PointingHandCursor)
            button.setAccessibleName(f"看第 {i + 1} 条")
            button.clicked.connect(lambda _=False, n=i: self.pick(n))
            row.addWidget(button)
            self.buttons.append(button)
        self.hint = _label("", FONT_XS, _MUTED)
        row.addWidget(self.hint)
        self.hide()

    def pick(self, n):
        if n != self._current and self.on_pick:
            self.on_pick(n)

    def set_rounds(self, count, current, hint=""):
        """count = 最长的那个候选有几条。<= 1 时：hint 非空就摆 hint，空就整块藏起来。"""
        self._count, self._current = count, current
        if count <= 1:
            for button in self.buttons:
                button.hide()
            self.hint.setText(hint)
            self.hint.setVisible(bool(hint))
            self.setVisible(bool(hint))
            return
        self.hint.hide()
        for i, button in enumerate(self.buttons):
            button.setVisible(i < count)
            on = i == current
            bg = theme.SAGE if on else "transparent"
            button.setStyleSheet(
                f"QPushButton {{ background: {bg}; border: none; border-radius: {RADIUS_SM}px; "
                f"color: {theme.PAPER if on else _MUTED}; font-weight: 600; }}"
                f"QPushButton:hover {{ background: {theme.SAGE if on else theme.CREAM}; }}"
            )
        self.show()


class _ReplyCard(_Surface):
    def __init__(self, owner, index, recommended=False, number=1, score=None):
        super().__init__(accent=recommended)
        self.owner = owner
        self.index = index
        box = QVBoxLayout(self)
        self.box = box
        box.setSpacing(GAP_SM)
        # 「回复轮数」> 1 时一个候选可能是几句**连着发**的消息（之间用 \n 分隔，见 core/draft.lines）：
        # 标题里标一句「连发 N 条」，正文只摆当前这一轮（翻页在面板顶部的 _RoundTabs），
        # 按钮写「填入 1/N」
        self.parts = draft.lines(owner.cands[index]) or [owner.cands[index]]
        top = QHBoxLayout()
        label = "推荐回复" if recommended else f"备选 {number}"
        if len(self.parts) > 1:
            label += f" · 连发 {len(self.parts)} 条"
        if score is not None:
            label += f" · {round(score * 100)}%"
        top.addWidget(_label(label, FONT_XS, _ACCENT if recommended else _MUTED, True))
        self.copyButton = _tool(FIF.COPY, "复制这条回复", lambda: owner._copy(index), self)
        top.addWidget(self.copyButton)
        box.addLayout(top)
        self.text = _label("", FONT_LG)  # 内容由 set_round 填：只摆当前那一轮
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
        self.set_round(owner._round_view)

    def set_available(self, enabled):
        self.fillButton.setEnabled(enabled)
        self.copyButton.setEnabled(enabled)

    def set_round(self, n):
        """摆第 n 条（从 0 起；超出就摆最后一条），按钮文字跟着走。

        几条**不再堆在一页上**：多轮候选只显示当前这一轮，翻看在面板顶部（三个候选一起翻）。
        单条候选（绝大多数）永远是「填入」，跟以前一模一样。"""
        n = min(n, len(self.parts) - 1)
        self.text.setText(self.parts[n])
        self.fillButton.setText(f"填入 {n + 1}/{len(self.parts)}" if len(self.parts) > 1 else "填入")

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
    # 开场白那一套：没有「对方刚说」，也没有 Jev 判断那一步，说辞得跟着换
    _OPENER_PILL = {"thinking": "开场白", "ready": "开场白"}
    _BUSY = {"scanning": "正在截取聊天窗口并 OCR…",
             "notify": "读到新消息，等他发完再判断",
             "thinking": "Jev 判断中，正在起草三条回复"}
    _OPENER_BUSY = "正在给你想一句开场白…"
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
        # 开场白时这句得改成「对方还没回」，见 set_items——摆「对方刚说」就张冠李戴了
        self.headTitle = _label("对方刚说", FONT_XS, _MUTED)
        head_col.addWidget(self.headTitle)
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
        self.footHint = _label("按 Ctrl+1/2/3 填入", FONT_XS, _MUTED)
        foot_row.addWidget(self.footHint, 1)
        self.againButton = QPushButton("换一批")
        self.againButton.setFlat(True)
        self.againButton.setCursor(Qt.PointingHandCursor)
        self.againButton.setToolTip("对这次冷场重新起草三条开场白")
        self.againButton.setStyleSheet(
            f"QPushButton {{ color: {theme.SAGE}; background: transparent; border: none; }}"
        )
        self.againButton.clicked.connect(owner._opener_again)
        self.againButton.hide()
        foot_row.addWidget(self.againButton, 0, Qt.AlignRight)
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

    def _set_pill(self, phase, opener=False):
        text = (self._OPENER_PILL if opener else self._PILL).get(phase) or self._PILL.get(phase, "")
        self.pill.setText(text)
        self.pill.setVisible(bool(text))
        if text:
            self.pill.setStyleSheet(
                f"QLabel {{ background: {theme.INK}; color: {theme.PAPER}; "
                f"border-radius: {RADIUS_SM}px; padding: 1px 6px; }}"
            )

    def set_items(self, items, suggest, phase, heard="", voice="", opener=False, blocked=False,
                  blank=False):
        """items: [(候选原始下标, 序号, 正文, 百分比或 None, 是否推荐, meta 后缀)]；voice 非空 =
        这条会话有语音消息，这时把「转文字」那一块顶上来，候选行让位。
        末项是给多轮候选标「连发 N 条」用的（条子只有一行宽，摆不下第二句），空串就是不标。

        下标要带着走：行是按显示位置摆的，但点下去填的必须是那条候选本身。

        opener 非空 = 现在摆的是冷场开场白（正在起草或已经摆出来）：标题那句从「对方刚说」改成
        「对方还没回」、角标改成「开场白」、生成中那句话也不用「Jev 判断中」（根本没那一步）、
        底下多一个「换一批」。

        blocked 非空 = 候选摆着但填不了（固定着、微信开在别的会话）：底下那句提示不能再说
        「按 Ctrl+1/2/3 填入」，那是句反话。

        blank 非空 = 这批开场白是**空会话**那种（刚加的好友、只有表情）：头上那句不能写
        「对方还没回」——一句话都没说过，没人被晾着。

        候选行、忙碌文案、建议行三者按 phase 互斥（跟设计稿的 CandidateIme 一个口径）：
        只有 ready 才摆候选，其余阶段只显示一句进度说明。这样即便上层忘了清候选，
        也不会出现「一边说正在判断、一边把旧候选摆在那儿」的错乱。"""
        self.headTitle.setText("还没聊过" if blank else ("对方还没回" if opener else "对方刚说"))
        self.heard.setText(heard or "…")
        self._set_pill(phase, opener)
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
        busy = self._OPENER_BUSY if opener else self._BUSY.get(phase)
        self.hint.setText(busy or "")
        self.hint.setVisible(bool(busy) and not ready)
        self.suggest.setText(suggest or "")
        self.suggest.setVisible(ready and bool(suggest))
        # 填不了的时候（固定着、微信开在别人那儿）提示别说「按 Ctrl+1/2/3 填入」，那是句反话。
        # 复制不受影响，所以说这个——每行右边就有复制按钮
        self.footHint.setText("可以先复制" if blocked else "按 Ctrl+1/2/3 填入")
        self.foot.setVisible(ready)
        self.againButton.setVisible(ready and opener)
        for i, row in enumerate(self.rows):
            if ready and i < len(items):
                index, number, text, score, recommended, extra = items[i]
                # 用 number（1 起）不用行号：行号从 0 数，跟左边那个序号方块对不上
                meta = ("推荐回复" if recommended else f"备选 {number}")
                if score is not None:
                    meta += f" · {round(score * 100)}%"
                row.set_content(index, text, meta + extra, number, recommended)
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
                 on_toggle_debug=None, on_voice_convert=None, on_opener_again=None,
                 on_settings_saved=None, on_open_history=None, on_use=None, on_pin_change=None,
                 on_relation_change=None, on_scene_change=None, on_toggle_chatlog=None,
                 on_reread=None, on_make_shortcut=None, on_import=None, on_clear=None):
        """result_of(会话名) → 那个会话上次的结果或 None；切着看别的会话时用它把旧结果放回来。
        on_target_change(会话名, 人名) → 用户在群里挑了回复对象。
        on_toggle_debug(开不开) → 开关调试视图那个独立窗口。
        on_voice_convert() → 用户点了「转文字」，返回 "" 或一句给用户看的失败原因。
        on_opener_again() → 用户点了「换一批」，要重新起草一批开场白。
        on_settings_saved() → 设置存盘了，父进程有些「按新设置现算一次」的事要做。
        on_open_history() → 用户点了标题栏那个「AI 记录」，开（或收起）记录窗。
        on_use(原始下标, "fill"|"copy") → 用户用了第几条候选，父进程记进 AI 记录里。
        on_pin_change(会话名或 "") → 用户点了「跟随/固定」，要不要固定、固定哪个由父进程定
        （它才是准的那一份，界面这边只是照着显示，见 set_pin）。
        on_relation_change(会话名, 关系键) → 用户给这个会话挑了关系；on_scene_change 同理，
        挑的是场景模板（第二项为空串 = 跟随关系）。两个都是拨一下立刻写盘、下次生成才用。
        on_toggle_chatlog(开不开) → 「聊天会话存储」那个开关，拨一下立刻生效（父进程配库）。
        on_reread() → 用户点了「重新识别」，要叫醒子进程把屏幕整个重读一遍（父进程那边发信号）。
        on_make_shortcut() → 用户点了「创建桌面快捷方式」（写 .lnk 要 COM，界面这边不碰）。
        on_clear(会话名) → 用户点了「清除」，要把这个会话的记录清掉（库和上下文在父进程那边）。"""
        self.app = QApplication.instance() or QApplication([])
        self.app.setWindowIcon(_app_icon())
        setTheme(Theme.LIGHT)
        setThemeColor(_ACCENT, save=False)
        self.on_fill = on_fill
        self.on_toggle_capture = on_toggle_capture
        self.on_target_change = on_target_change
        self.on_toggle_debug = on_toggle_debug
        self.on_voice_convert = on_voice_convert
        self.on_opener_again = on_opener_again
        self.on_settings_saved = on_settings_saved
        self.on_open_history = on_open_history
        self.on_use = on_use
        self.on_pin_change = on_pin_change
        self.on_relation_change = on_relation_change
        self.on_scene_change = on_scene_change
        self.on_toggle_chatlog = on_toggle_chatlog
        self.on_reread = on_reread
        self.on_make_shortcut = on_make_shortcut
        self.on_import = on_import
        self.on_clear = on_clear
        self.result_of = result_of
        self._voices = {}  # {会话名: [(x0,y0,x1,y1,时长)]}，语音气泡的位置，转文字要右键它
        self._opener = {}  # {会话名: 对方多少分钟没回}，现在摆的是开场白（不是回复）的会话
        self.cands = []
        self.cards = []
        self._busy = False
        self._busy_opener = False  # 正在跑的那次是开场白：生成中那句话别照抄「Jev 判断中」
        self._opener_wait = 0  # 上面那次等了多久（生成期间头上的时间靠它）
        self._current = False
        self._compact = None  # 断点模式：None 保证 _relayout 第一次调用必定生效
        self._pageLayouts = []
        self._hintLabels = []
        self.feeds = {}  # {会话名: [(who, text, name, 时间, 是不是语音转出来的)]}，切会话时重建
        self.counts = {}  # {会话名: 消息条数}
        self.hers = {}  # {会话名: 对方最近一句}
        self.targets = {}  # {会话名: ([发言人], 当前回复对象)}
        self._relKeys = []  # 「关系」下拉的选项 [(键, 名字)]，跟控件里的行号一一对应
        self._sceneKeys = []  # 「场景模板」下拉的选项，同上
        self._chat = ""  # 微信当前开着的会话。**能不能填只看它**（粘贴是发给微信的，跟面板看谁无关）
        self._shown = ""  # 界面上正在看的会话（浏览、固定时和上面不一样）
        self._pinned = ""  # 固定盯着哪个会话（"" = 跟随微信切）。父进程那份是准的，这里只是镜像
        self._ordered = []  # [(candidates 里的原始索引, 百分比, 是否推荐)]，按推荐顺序排好
        # 现在翻到第几条（从 0 数）：「回复轮数」> 1 时三张卡片一起翻，见 _RoundTabs
        self._round_view = 0
        # {候选原始下标: 这条已经填过几条}（0/缺 = 没填过）。只服务一件事：候选作废之后
        # 判断「这条多轮还没填完」——自己刚填进去那条被 OCR 读回来会让候选作废，
        # 不认这个的话「点一次填一条」到第二条就点不动了。**作废时不清**，新一批候选才清
        self._fill_round = {}
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
        # 名字别叫 historyButton：面板底部那个「聊天记录」已经占了，两边会打架
        self.traceButton = _tool(FIF.DOCUMENT, "AI 记录", self._open_history, header)
        self.traceButton.setToolTip("每一轮 AI 调用问了什么、回了什么、你最后用了哪条（只在本机）")
        title.addWidget(self.traceButton)
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
        # 两个下拉先按「还没有会话」填上（灰着）；等 set_chat 认到会话再拨到它那一型
        self._render_relations()
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
        self.footerNote = _label("", FONT_XS, _MUTED)  # 文案跟着「自动发送」开关走，见 _sync_footer
        footer.addWidget(self.footerNote, 1)
        self._sync_footer()
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
        # 这个会话是哪一型关系（朋友/恋人/同事……）。**按会话存**，不是全局一个：
        # 原来设置页那个「你们的关系」是一份配置管所有会话，等于拿对家人那套口气去回客户。
        # 拨一下立刻写盘（走 on_relation_change → save_chat_relation），下次生成就用新的。
        self.relationBox = _FitCombo()
        self.relationBox.setFixedWidth(86)
        self.relationBox.setAccessibleName("关系")
        self.relationBox.setToolTip("这个会话里的人跟你是什么关系，决定称呼、语气和分寸。"
                                    "每种关系的正文在「设置 → 个人风格」里改。")
        self.relationBox.currentIndexChanged.connect(self._on_relation_selected)
        chat_row.addWidget(self.relationBox, 0, Qt.AlignVCenter)
        # 跟随 / 固定：固定 = 面板钉在这一个会话上，微信切到别的会话也不跟过去（见 set_pin）。
        # 做成按钮而不是原来那个纯文字标签，是因为它现在点得动——文字本身就写着现在是什么模式
        self.chatFollow = QPushButton("")
        self.chatFollow.setFlat(True)
        self.chatFollow.setFixedWidth(52)
        self.chatFollow.setCursor(Qt.PointingHandCursor)
        setFont(self.chatFollow, FONT_XS)
        self.chatFollow.clicked.connect(self._toggle_pin)
        chat_row.addWidget(self.chatFollow, 0, Qt.AlignVCenter)
        self._follow_text()  # 还没认到会话：空着 + 点不动，别摆个能点却没反应的按钮
        body.addLayout(chat_row)
        # 场景模板：给这个会话**临时换个口吻**，用另一型关系的正文说话。默认「跟随关系」，
        # 也就是用上面那个「关系」自己的正文——绝大多数会话根本不用碰这一行。
        scene_row = QHBoxLayout()
        scene_row.setSpacing(GAP_SM)
        scene_prefix = _label("场景模板", FONT_SM, _MUTED)
        scene_prefix.setFixedWidth(56)  # 跟「当前会话」对齐
        scene_row.addWidget(scene_prefix)
        self.sceneBox = _FitCombo()
        self.sceneBox.setAccessibleName("场景模板")
        self.sceneBox.setToolTip("挑一型就用那一型的正文说话（比如对一个客户用「同事」那套口气）；"
                                 "不挑就跟着上面那个「关系」走。")
        self.sceneBox.currentIndexChanged.connect(self._on_scene_selected)
        scene_row.addWidget(self.sceneBox, 1)
        body.addLayout(scene_row)
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
        # 开场白下这里摆的是「上次聊到哪儿」而不是刚收到的话，见 show()
        self.contextTitle = _label("对方最近说", FONT_XS, _MUTED)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.contextTitle, 1)
        # 多轮候选的翻页按钮就摆在这一行右边（没有多轮候选时整块藏起来）
        self.roundTabs = _RoundTabs(self.context)
        self.roundTabs.on_pick = self._set_round
        title_row.addWidget(self.roundTabs, 0, Qt.AlignRight)
        context_box.addLayout(title_row)
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
        # 开场白才有的「换一批」（三条都不满意时重新起草）。跟面板底部那个「保存设置」不是一回事，
        # 所以只在摆开场白时露出来——那时候洞察卡里既没有意图也没有紧张度可看。
        again_row = QHBoxLayout()
        again_row.addStretch(1)
        self.againButton = PushButton("换一批")
        self.againButton.setToolTip("对这次冷场重新起草三条开场白")
        self.againButton.clicked.connect(self._opener_again)
        self.againButton.hide()
        again_row.addWidget(self.againButton)
        insight_box.addLayout(again_row)
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

        log_row = QHBoxLayout()
        log_row.setSpacing(GAP_SM)
        self.historyButton = PushButton(FIF.HISTORY, "聊天记录")
        self.historyButton.clicked.connect(self._toggle_history)
        self.historyButton.setAccessibleName("展开或收起聊天记录")
        log_row.addWidget(self.historyButton, 1)
        # 导入：把微信里「复制」出来的聊天记录粘进来。用户要的是「仿照说话的语气」——
        # 语气只能从聊天记录本身来，所以导入的会**同时**进 feeds、history 和库
        # （三处一起写，见 main.import_history）。
        self.importButton = PushButton("导入")
        self.importButton.clicked.connect(self._import)
        self.importButton.setAccessibleName("导入聊天记录")
        self.importButton.setToolTip(
            "在微信里选中几条消息复制，粘进来当聊天记录。\n"
            "导入的对话会进喂给模型的上下文，后面起草会参考这段的语气。")
        log_row.addWidget(self.importButton, 0)
        # 清除：把**这一个会话**的记录清干净，好从头重导。别跟设置页那个「清空聊天记录」
        # 搞混——那个一次清全库，这个只清当前会话（见 _clear）。
        self.clearButton = PushButton("清除")
        self.clearButton.clicked.connect(self._clear)
        self.clearButton.setAccessibleName("清除这个会话的聊天记录")
        self.clearButton.setToolTip(
            "把这个会话的聊天记录清空（只清这一个会话，别的会话不受影响）。\n"
            "导入搞脏了、想从头来过的时候用。删了找不回来。")
        log_row.addWidget(self.clearButton, 0)
        # 重新识别：把微信里现在这几屏**整个重读一遍**，跟下面的记录对账——漏掉的补进来、
        # 之前读花了的就地改掉（真机上出现过「?」「嗯」这种单字被 OCR 吃掉、当没说过）。
        # 采集只在画面变过才交帧，点它的时候画面多半早静止了，所以走 Capture 留的那份最近帧
        self.rereadButton = PushButton("重新识别")
        self.rereadButton.clicked.connect(self._reread)
        self.rereadButton.setAccessibleName("重新识别屏幕上的消息")
        self.rereadButton.setToolTip(
            "把聊天窗口里现在这几屏重新读一遍，跟下面的记录对一下：漏掉的补进来，"
            "之前读花了的就地改掉。只补记录，不会去问 AI。")
        log_row.addWidget(self.rereadButton, 0)
        body.addLayout(log_row)
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
        body.addWidget(_label("维护每种关系的说话方式，配置判断和起草用的两个模型。", FONT_MD, _MUTED))
        # 两块内容分页签摆，别堆成一长条滚动。标题交给页签，卡片里就不再重复写一遍
        tabs = QHBoxLayout()
        tabs.setSpacing(GAP_SM)
        self.tabButtons = {}
        for key, text in (("preference", "回复偏好"), ("models", "模型设置"),
                          ("style", "个人风格"), ("system", "系统设置")):
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
        # 「你们的关系」原来就在这儿，是**一份配置管所有会话**——等于拿对家人那套口气去回客户。
        # 现在关系按会话走、正文按关系走，整块都搬到「个人风格」页了（面板上那个下拉改的就是它）。
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
            "生成和判断时看最近这么多条消息。太少会丢上下文，太多会稀释重点。"
        ))
        rounds_label = _label("回复轮数", FONT_MD)
        box.addWidget(rounds_label)
        self.roundsBox = SpinBox()
        self.roundsBox.setRange(1, 3)
        self.roundsBox.setLocale(QLocale.c())  # 跟上面同理：Qt 的 zh_CN 会把数字显示成杭州码子
        self.roundsBox.setAccessibleName("一个候选最多连着发几条消息")
        rounds_label.setBuddy(self.roundsBox)
        box.addWidget(self.roundsBox)
        box.addWidget(self._hint(
            "一个候选里最多连着发几条消息。1 = 一句一回；大于 1 时模型会把「接着想说的那句」"
            "也写进同一个候选，填入时点一次填一条，发出去再点下一条。"
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
        opener_row = QHBoxLayout()
        opener_row.addWidget(_label("冷场时起草开场白", FONT_MD), 1)
        self.openerSwitch = SwitchButton()
        self.openerSwitch.setOnText("开")
        self.openerSwitch.setOffText("关")
        self.openerSwitch.setAccessibleName("冷场时起草开场白")
        self.openerSwitch.checkedChanged.connect(self._opener_toggled)
        opener_row.addWidget(self.openerSwitch)
        box.addLayout(opener_row)
        wait_row = QHBoxLayout()
        wait_row.addWidget(_label("等多久算冷场", FONT_MD), 1)
        self.openerBox = SpinBox()
        self.openerBox.setRange(1, 720)
        self.openerBox.setSuffix(" 分钟")
        self.openerBox.setLocale(QLocale.c())  # 同「参考上下文」：不钉 C locale 会显示成杭州码子
        self.openerBox.setAccessibleName("等多少分钟算冷场")
        wait_row.addWidget(self.openerBox)
        box.addLayout(wait_row)
        box.addWidget(self._hint(
            "最后一句是你说的、对方一直没回：等这么久就起草 3 条开场白接上话，"
            "没什么可接的就现编一个由头。每个冷场只自动出一次，对方回了话才算下一个冷场；"
            "不满意可以点「换一批」。关着就完全不管，一次都不调模型。"
        ))
        self.preferenceCard = preference
        body.addWidget(preference)

        # 第四页签「系统设置」：存储和本机行为那几项。原来散在「回复偏好」「模型设置」里，
        # 跟回复本身没关系，归到一堆更好找（见 _switch_tab 的 tabButtons）
        system = _Surface()
        box = QVBoxLayout(system)
        box.setContentsMargins(GAP_LG, GAP_LG, GAP_LG, GAP_LG)
        box.setSpacing(GAP_MD)
        chatlog_row = QHBoxLayout()
        chatlog_row.addWidget(_label("聊天会话存储", FONT_MD), 1)
        self.chatlogSwitch = SwitchButton()
        self.chatlogSwitch.setOnText("开")
        self.chatlogSwitch.setOffText("关")
        self.chatlogSwitch.setAccessibleName("聊天会话存储")
        self.chatlogSwitch.checkedChanged.connect(self._chatlog_toggled)  # 拨一下立刻生效
        chatlog_row.addWidget(self.chatlogSwitch)
        box.addLayout(chatlog_row)
        box.addWidget(self._hint(
            "界面上的聊天记录和 AI 看的上下文都存到本机的 jev.db 里，重启之后还在、模型还接得上"
            "上次聊到哪儿。写库前统一脱敏（不存密钥），只留最近 90 天。关掉就不再写，"
            "已经存下的一条都不动——要删点下面那个按钮。"
        ))
        stat_row = QHBoxLayout()
        self.chatlogStats = _label("", FONT_SM, _MUTED)
        stat_row.addWidget(self.chatlogStats, 1)
        self.chatlogClear = PushButton("清空聊天记录")
        self.chatlogClear.clicked.connect(self._clear_chatlog)
        stat_row.addWidget(self.chatlogClear)
        box.addLayout(stat_row)
        history_row = QHBoxLayout()
        history_row.addWidget(_label("记录 AI 调用", FONT_MD), 1)
        self.historySwitch = SwitchButton()
        self.historySwitch.setOnText("开")
        self.historySwitch.setOffText("关")
        self.historySwitch.setAccessibleName("记录 AI 调用")
        history_row.addWidget(self.historySwitch)
        box.addLayout(history_row)
        box.addWidget(self._hint(
            "每一轮问了什么、模型回了什么、你最后用了哪条，都记在本机的 jev.db 里（runs 表），"
            "点标题栏那个「AI 记录」能翻。存的是聊天原文（不存密钥，写库前统一脱敏）；"
            "关掉就一次都不写，已有的记录还在，去记录窗里清空。跟上面那个共用同一个文件，"
            "但是两张表、两个开关，互不影响。"
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
        # 「绝不自动发送」那条硬约束唯一的例外（见 CLAUDE.md 硬约束 4）。摆在这儿是故意的：
        # 跟调试视图挨着，用户知道自己在拨一个开发用的东西。**要点「保存设置」才生效**——
        # 这个开关会真发消息，多一步确认不算麻烦；而且它没有需要实时联动的界面状态，
        # 不像「桌面宠物」「调试视图」那样必须拨一下立刻生效。
        autosend_row = QHBoxLayout()
        autosend_row.addWidget(_label("自动发送", FONT_MD), 1)
        self.autoSendSwitch = SwitchButton()
        self.autoSendSwitch.setOnText("开")
        self.autoSendSwitch.setOffText("关")
        self.autoSendSwitch.setAccessibleName("自动发送")
        autosend_row.addWidget(self.autoSendSwitch)
        box.addLayout(autosend_row)
        # 这条不走 _hint()：那一行是灰的，而这条得让人一眼看见——它讲的是「会真发消息」。
        # 用 WARN 色加粗，但照样登记进 _hintLabels，紧凑模式跟别的说明一起藏。
        warn = _label(
            "调试用，开了会真的替你发消息。模型确实排过序时，把匹配度最高的那条候选填进"
            "输入框、再敲回车发出去——发之前不问你，撤回只有 2 分钟。只在「微信开着的正是"
            "这个会话」「采集没暂停」「模型确实排了序」三条都成立时才动"
            "（没排序就没有「最匹配」，那种一律不发）。默认关，用完记得关掉；"
            "拨完要点下面的「保存设置」才生效。", FONT_XS, theme.WARN, bold=True)
        self._hintLabels.append(warn)
        box.addWidget(warn)
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
        # 桌面快捷方式：打包版第一次跑会自动建一个（main.auto_shortcut），这儿是**手动重建**的
        # 口子——挪了文件夹、或者手滑把桌面图标删了（删了不会再自动冒出来），点一下就行。
        # 源码跑时按钮灰着：sys.executable 是 python.exe，指过去等于放一个打不开的图标。
        link_row = QHBoxLayout()
        link_row.addWidget(_label("桌面快捷方式", FONT_MD), 1)
        self.shortcutButton = PushButton("创建")
        self.shortcutButton.setAccessibleName("创建桌面快捷方式")
        self.shortcutButton.clicked.connect(self._make_shortcut)
        link_row.addWidget(self.shortcutButton)
        box.addLayout(link_row)
        _why = shortcut.can_create()
        self.shortcutButton.setEnabled(not _why)
        box.addWidget(self._hint(
            "在桌面放一个指向本程序的图标，省得每次都翻进文件夹双击 exe。"
            "打包版第一次跑会自动建一次；你在桌面把它删了不会再冒出来，想重建就点这个。"
            + (f"（{_why}）" if _why else "")
        ))
        self.systemCard = system
        body.addWidget(system)

        # 第三页签「个人风格」：**关系**在这儿维护——默认用哪一型、每一型的正文是什么。
        # 正文只有这一份（core/relations.py 是内置原文，改过的按型存在 config.json 的 relations.texts
        # 里）；面板上那个「关系」下拉只是挑某个会话用哪一型，不在这儿。
        style = _Surface()
        box = QVBoxLayout(style)
        box.setContentsMargins(GAP_LG, GAP_LG, GAP_LG, GAP_LG)
        box.setSpacing(GAP_MD)
        self._relTexts = {}  # 内存里正在改的各型正文，点保存才落盘
        self._relCustoms = []  # 用户自己加的关系 [{"key","name","text"}]，键自动编（r1、r2……）
        self._relShown = ""  # 文本框里现在装的是哪一型：换型前得按它先把字收回来
        default_row = QHBoxLayout()
        default_row.addWidget(_label("默认关系", FONT_MD), 1)
        self.defaultRelBox = ComboBox()
        self.defaultRelBox.setMinimumWidth(0)
        self.defaultRelBox.setAccessibleName("默认关系")
        default_row.addWidget(self.defaultRelBox)
        box.addLayout(default_row)
        box.addWidget(self._hint(
            "还没单独指定过关系的会话（刚加的好友、刚打开的群、群里换了回复对象）一律按这个来。"
            "具体某个会话用什么，在面板上「当前会话」右边那个下拉里改。"))
        type_row = QHBoxLayout()
        type_row.addWidget(_label("关系类型", FONT_MD), 1)
        self.relTypeBox = ComboBox()
        self.relTypeBox.setMinimumWidth(0)
        self.relTypeBox.setAccessibleName("关系类型")
        type_row.addWidget(self.relTypeBox)
        self.relAddButton = PushButton("+ 新增")
        self.relAddButton.clicked.connect(self._rel_add)
        type_row.addWidget(self.relAddButton)
        self.relDelButton = PushButton("删除")
        self.relDelButton.clicked.connect(self._rel_del)
        type_row.addWidget(self.relDelButton)
        box.addLayout(type_row)
        box.addWidget(self._hint(
            "内置这几型（朋友、恋人、暧昧、同事、职场、家人、18+）各有出厂正文，可以随便改；自己新增的"
            "（前女友、老板……）名字和正文都自己写。"))
        self.relNameLabel = _label("这种关系叫什么", FONT_MD)
        box.addWidget(self.relNameLabel)
        self.relNameEdit = LineEdit()
        self.relNameEdit.setPlaceholderText("例如：前女友、老板、房东")
        self.relNameEdit.setAccessibleName("这种关系叫什么")
        self.relNameLabel.setBuddy(self.relNameEdit)
        box.addWidget(self.relNameEdit)
        box.addWidget(_label("按这个关系该怎么说话", FONT_MD))
        self.relTextEdit = TextEdit()
        self.relTextEdit.setPlaceholderText("这段会拼进每次起草的上下文，管称呼、语气和分寸。")
        self.relTextEdit.setFixedHeight(180)
        self.relTextEdit.setAccessibleName("按这个关系该怎么说话")
        box.addWidget(self.relTextEdit)
        rel_row = QHBoxLayout()
        self.relReset = PushButton("重置为内置原文")
        self.relReset.clicked.connect(self._rel_reset)
        rel_row.addWidget(self.relReset)
        rel_row.addStretch(1)
        self.relSave = PrimaryPushButton("保存")  # 跟页面底部那个「保存设置」是同一个动作
        self.relSave.clicked.connect(self._save)
        rel_row.addWidget(self.relSave)
        box.addLayout(rel_row)
        self.relCount = _label("", FONT_XS, _MUTED)
        box.addWidget(self.relCount)
        box.addWidget(self._hint(
            "这段正文拼进每次**起草**的上下文，判断那一步不喂（它只看意图和紧张度）。"
            "改过的版本按关系分开存着，点「重置为内置原文」就回到出厂那份。"
            "想让某个会话临时换成另一型的口气，在面板的「场景模板」里挑，不用在这儿另写一份。"))
        # 信号接在控件都建好之后：addItems 自己会发一次 currentIndexChanged，那会儿框还没建出来
        self.relTypeBox.currentIndexChanged.connect(self._rel_type_changed)
        self.relTextEdit.textChanged.connect(self._rel_count)
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
        # 「记录 AI 调用」原来在这儿，现在归「系统设置」页（跟聊天会话存储摆一起，都是存储）
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
        model = settings.relations_dict()
        self._relTexts = dict(model["texts"])
        self._relCustoms = [dict(c) for c in model["customs"]]
        # _relShown 先清掉：下面填下拉会触发换型，别拿上一轮的键去收框里的字
        self._relShown = ""
        self._fill_rel_boxes(model["default"], model["default"])
        self.contextBox.setValue(settings.context())
        self.roundsBox.setValue(settings.reply_rounds())
        self.targetSwitch.setChecked(settings.reply_target())
        self.openerBox.setValue(settings.opener_minutes())
        self.openerSwitch.setChecked(settings.opener())
        self._opener_toggled(self.openerSwitch.isChecked())  # 值没变时上面不发信号，灰显自己补一次
        self._set_group(self.jev, settings.jev_provider(), settings.jev_model())
        self._set_group(self.draft, settings.draft_provider(), settings.draft_model())
        self.baseEdit.setText(settings.draft_base_url())
        self.relayEdit.setText(settings.relay_base_url())
        self.judgePathEdit.setText(settings.relay_judge_path())
        self.thinkStyleBox.setCurrentIndex(
            _THINK_STYLE_ORDER.index(settings.relay_thinking_style()))
        self.thinkingSwitch.setChecked(settings.thinking())
        self.historySwitch.setChecked(settings.history())
        self.updateSwitch.setChecked(settings.check_update())
        # 自动发送没有连 checkedChanged（它按「保存设置」生效），所以不用 blockSignals
        self.autoSendSwitch.setChecked(settings.auto_send())
        self.set_debug_switch(settings.debug_view())  # 屏蔽信号地拨，别在加载时开关一遍窗口
        self.petSwitch.blockSignals(True)  # 同上：加载时别真去开关宠物
        self.petSwitch.setChecked(settings.pet_enabled())
        self.petSwitch.blockSignals(False)
        self.chatlogSwitch.blockSignals(True)  # 同上：加载时别真去配一遍库
        self.chatlogSwitch.setChecked(settings.chatlog())
        self.chatlogSwitch.blockSignals(False)
        self._sync_chatlog()
        self._sync_model_fields()  # 上面屏蔽了信号，这里补一次
        self._sync_footer()  # 「自动发送」换过之后，页脚那句「发送由你确认」就不能留着了
        self.settingsFeedback.hide()

    def _sync_footer(self):
        """页脚那行字得跟「自动发送」的实际状态一致。

        默认那句「仅填入输入框 · 发送由你确认」在开关拨开之后就是假话了——页脚是应用一直在
        摆着的东西，它说错话比设置页里少一行提示更糟。开着的时候换成警告色，扫一眼就知道
        现在这程序会自己发消息。"""
        note = getattr(self, "footerNote", None)  # _load_settings 在建页脚之前就会跑一次
        if note is None:
            return
        on = settings.auto_send()
        note.setText(f"自动发送已开启 · 候选会自动发出 · v{VERSION}" if on
                     else f"仅填入输入框 · 发送由你确认 · v{VERSION}")
        color = theme.WARN if on else _MUTED
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(note, qss, qss)

    def _save(self):
        jev_provider = self._provider_of(self.jev)
        draft_provider = self._provider_of(self.draft)
        base = self.baseEdit.text().strip()
        # 关系模型：默认哪一型、每型的正文、自建的那几条。`chats`（哪个会话用哪一型）不归这一页管，
        # 但得原样带过去——这里是整份模型的提交，漏了就等于把所有会话的选择清空了。
        relation_model = dict(settings.relations_dict())
        relation_model.update({"default": self._default_rel_key(),
                               "texts": self._rel_dirty(),
                               "customs": [dict(c) for c in self._relCustoms]})
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
            settings.save(self.contextBox.value(),
                          rounds_n=self.roundsBox.value(),
                          relation_model=relation_model,
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
                          opener_on=self.openerSwitch.isChecked(),
                          opener_minutes_n=self.openerBox.value(),
                          thinking_on=self.thinkingSwitch.isChecked(),
                          history_on=self.historySwitch.isChecked(),
                          check_update_on=self.updateSwitch.isChecked(),
                          pet_enabled_on=self.petSwitch.isChecked(),
                          chatlog_on=self.chatlogSwitch.isChecked(),
                          auto_send_on=self.autoSendSwitch.isChecked())
        except Exception:
            self._settings_feedback("保存失败，请检查配置文件是否可写后重试。", error=True)
            return
        self._load_settings()
        self._render_targets()  # 开关刚改过，回到首页时这一行该显该藏得重算一次
        self._render_relations()  # 刚加的关系得马上出现在面板的下拉里，默认关系换了也要跟着变
        self._settings_feedback("设置已保存，将用于下一次回复。")
        self.setupButton.hide()
        if self.on_settings_saved:
            self.on_settings_saved()  # 有些设置要拿眼前的聊天记录现算一次（见 main.on_settings_saved）
        if not self.cands and not self._busy:
            self._empty_text()
            self.set_status("设置已就绪，等待新消息", "idle")

    def _rel_keys(self):
        """「关系类型」下拉里现在有哪些型：内置的几型 + 用户自建的，顺序就是下拉里的顺序。"""
        return [*relations.KEYS, *(c["key"] for c in self._relCustoms)]

    def _rel_name(self, key):
        return relations.label(key, self._relCustoms)

    def _rel_key(self):
        """「关系类型」下拉里选中的键。"""
        keys = self._rel_keys()
        return keys[max(0, self.relTypeBox.currentIndex())] if keys else ""

    def _default_rel_key(self):
        """「默认关系」下拉里选中的键。"""
        keys = self._rel_keys()
        return keys[max(0, self.defaultRelBox.currentIndex())] if keys else relations.DEFAULT

    def _rel_text(self, key):
        """某一型该显示在框里的正文：自建的取它自己那条，内置的取改过的、没改过取出厂原文。"""
        for c in self._relCustoms:
            if c["key"] == key:
                return str(c.get("text") or "")
        return self._relTexts.get(key, relations.default_text(key))

    def _rel_stash(self):
        """把框里正在改的东西收回内存——换型、新增、删除、保存之前都得先收一次。

        名字也跟着收：自建关系的名字就在旁边那个框里，改名字**不改键**，指向它的会话不会丢。"""
        key = self._relShown
        if not key:
            return
        text = self.relTextEdit.toPlainText().strip()
        if relations.is_builtin(key):
            self._relTexts[key] = text
            return
        for c in self._relCustoms:
            if c["key"] == key:
                c["text"] = text
                c["name"] = self.relNameEdit.text().strip()

    def _rel_dirty(self):
        """收回内存之后挑出跟出厂原文不一样的那些——只有这些值得写进 config.json。
        自建的关系不在这儿，它们的正文在 customs 那条里跟着走。"""
        self._rel_stash()
        return {k: t for k, t in self._relTexts.items()
                if relations.is_builtin(k) and t != relations.default_text(k)}

    def _fill_rel_boxes(self, stype="", sdefault=""):
        """按当前的自建关系重填「关系类型」和「默认关系」两个下拉（新增/删除之后要重填）。
        两个下拉共用同一份选项，只是各自拨到不同的键上。"""
        keys = self._rel_keys()
        names = [self._rel_name(k) for k in keys]
        for box, want in ((self.relTypeBox, stype), (self.defaultRelBox, sdefault)):
            box.blockSignals(True)
            box.clear()
            box.addItems(names)
            box.setCurrentIndex(keys.index(want) if want in keys else 0)
            box.blockSignals(False)
        # 重填时屏蔽了信号，换型那一套（名字框显隐、删除按钮亮灰、正文框）得自己补一次
        self._rel_type_changed()

    def _rel_type_changed(self, _=None):
        """换了类型：先把框里那份收好，再换上这一型的（改过的优先，没改过就用出厂原文）。
        自建的那几型多一个名字框——关系叫什么就是它。删除只对自建的开放。"""
        self._rel_stash()
        key = self._rel_key()
        self._relShown = key
        custom = bool(key) and not relations.is_builtin(key)
        self.relNameLabel.setVisible(custom)
        self.relNameEdit.setVisible(custom)
        self.relNameEdit.setText(self._rel_name(key) if custom else "")
        self.relTextEdit.setPlainText(self._rel_text(key))
        self.relReset.setEnabled(bool(key))
        self.relReset.setText("清空" if custom else "重置为内置原文")
        self.relDelButton.setEnabled(custom)
        self._rel_count()

    def _rel_reset(self):
        """回到出厂原文（自建的没有出厂原文，就是清空）。只动框里的字，落盘还是走「保存设置」。"""
        key = self._rel_key()
        if not key:
            return
        if relations.is_builtin(key):
            self._relTexts.pop(key, None)
        else:
            for c in self._relCustoms:
                if c["key"] == key:
                    c["text"] = ""
        self.relTextEdit.setPlainText(relations.default_text(key))

    def _rel_add(self):
        """新增一种关系：键自动编（r1、r2……，编好就不再变），名字和正文空着等人填。"""
        self._rel_stash()
        key = relations.new_key([c["key"] for c in self._relCustoms])
        self._relCustoms.append({"key": key, "name": "", "text": ""})
        self._relShown = ""  # 下面重填会走换型，别把刚建的空条目又当成「上一型」收一遍
        self._fill_rel_boxes(key, self._default_rel_key())
        self.relNameEdit.setFocus()
        self._settings_feedback("填好名字和正文，点「保存设置」生效。")

    def _rel_del(self):
        """删掉一种自建关系。指向它的会话会退回默认关系（落盘时由 _clean_relations 清掉）。"""
        key = self._rel_key()
        if not key or relations.is_builtin(key):
            return
        name = self._rel_name(key)
        self._relShown = ""  # 同上：别把要删的那型又收回来
        self._relCustoms = [c for c in self._relCustoms if c["key"] != key]
        self._relTexts.pop(key, None)
        self._fill_rel_boxes("", self._default_rel_key())
        self._settings_feedback(f"删掉了「{name}」，用它的会话会退回默认关系；点「保存设置」生效。")

    def _rel_count(self):
        """框里多少字。超过 LIMIT 就该掂量一下了——**长不等于好**：抽象形容堆多了，
        模型会照着说明造句，反而盖过 me 自己的口吻样本。有用的长正文是具体的词、例句、标点习惯。"""
        n = len(self.relTextEdit.toPlainText().strip())
        self.relCount.setText(f"{n} 字" + (f"，超过 {relations.LIMIT} 了，会盖过对话本身"
                                           if n > relations.LIMIT else ""))

    def _opener_toggled(self, on):
        """只灰掉/点亮那个分钟数。落盘还是走「保存设置」——跟调试视图那种拨一下立刻生效的
        开关不一样，冷场开场白是下一次生成才用得上的偏好。"""
        self.openerBox.setEnabled(bool(on))

    def _debug_toggled(self, on):
        """调试视图独立于「保存设置」：拨一下就开窗/收窗，顺手落盘，重启还在。"""
        settings.save(debug_view_on=on)
        if self.on_toggle_debug:
            self.on_toggle_debug(on)

    def _sync_chatlog(self):
        """存储那一行的现状。关着的时候**不读库**（读一下 sqlite 就会把库文件建出来，
        违背「关着就一个字都不往磁盘写」），只按文件大小说话。

        `size()` 报的是**整个库文件**——AI 记录和聊天记录两张表合用一个，所以关着的时候
        不能说「硬盘上已经存了聊天记录」，只能说文件占多大（见 core/chatlog.py 头上那段）。"""
        size = chatlog.size()
        if not self.chatlogSwitch.isChecked():
            self.chatlogStats.setText(
                f"已关闭，不再写新的；已经存的不动（库文件占着 {chatlog.human_size(size)}）。" if size
                else "已关闭，硬盘上还没存过东西。")
        else:
            self.chatlogStats.setText(
                f"已存 {chatlog.count()} 条 · 库文件 {chatlog.human_size(size)}。" if size
                else "还没存过东西；有消息就读进来。")
        # 亮不亮看**这张表**有没有东西：库文件可能因为 AI 记录开着而在，那是另一张表
        self.chatlogClear.setEnabled(self.chatlogSwitch.isChecked() and chatlog.count() > 0)

    def _chatlog_toggled(self, on):
        """聊天会话存储：拨一下立刻生效 + 落盘（跟调试视图、桌面宠物一个路子）。"""
        self._sync_chatlog()
        if self.on_toggle_chatlog:
            self.on_toggle_chatlog(on)
        self._sync_chatlog()  # 父进程刚配好库，条数这会儿才读得出来

    def _clear_chatlog(self):
        """清空本机存的聊天记录。**只清库**，界面上正摆着的这些是这次运行读到的，
        内存里那份不动——清了内存反而会让 `_already_read` 放行，屏幕上那几屏又被记一遍。"""
        if not _ConfirmBox("清空聊天记录？",
                           "本机存的聊天记录全部删掉，删了找不回来。关系和设置不受影响，"
                           "AI 记录也不受影响。界面上现在摆着的这些是这次运行读到的，"
                           "关掉应用才会跟着没。", self.win).exec():
            return
        chatlog.clear()
        self._sync_chatlog()
        self._settings_feedback("本机存的聊天记录已清空。")

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
        (self.contextBox if configured else self.jev.keyEdit).setFocus()

    def _switch_tab(self, key):
        """设置页的页签：只切显隐，控件不重建——重建会把用户填了一半的内容弄丢。"""
        for name, button in self.tabButtons.items():
            active = name == key
            button.setChecked(active)
            button.setStyleSheet(_tab_qss(active))
        self.preferenceCard.setVisible(key == "preference")
        self.modelsCard.setVisible(key == "models")
        self.styleCard.setVisible(key == "style")
        self.systemCard.setVisible(key == "system")
        if key == "system":
            self._sync_chatlog()  # 占用多少、有多少条，进来的时候现算一次

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

    def _open_history(self):
        """标题栏那个「AI 记录」：窗的开关在父进程手里（跟调试视图同一个套路）。"""
        if self.on_open_history:
            self.on_open_history()

    def _used(self, index, action):
        """候选条/面板上「填入」「复制」点了哪一条——记进 AI 记录，复盘时才知道最后用了哪条。"""
        if self.on_use:
            self.on_use(index, action)

    def _fill(self, index):
        if self._busy or not self._shown or index >= len(self.cands):
            return
        if self._shown != self._chat:
            # 微信屏幕上开着的不是面板里这个会话，按下去会把话打进另一个人的输入框。
            # 跟随模式下走不到这儿（一换会话候选就作废了），固定模式是常态：面板一直摆着
            # 固定那个会话的候选，微信可能早切走了。复制不受影响（剪贴板不发给谁）。
            self.set_status(self._offline_note(), "warning")
            return
        if not self._fillable(index):
            return  # 候选已经作废、又不在多轮中间：这条旧建议不再往输入框里送
        # 「回复轮数」> 1 时这一条候选是几句连着发的消息：填的是**现在翻到的那一条**，
        # 填完自动翻到下一轮，发出去再点一下就是下一条。**不跨轮跳**：fill() 是追加到输入框
        # 末尾的，上一条还没发出去就接着填下一条，两条会串成一条。
        parts = draft.lines(self.cands[index]) or [self.cands[index]]
        n = min(self._round_view, len(parts) - 1)
        try:
            self.on_fill(parts[n])
            self._used(index, "fill")
            # 记下「这条填到第几条」：候选接下来多半会因为这条被读回来而作废，
            # 到时候靠它认出「还有下一句没发」并放行（见 _fillable）
            self._fill_round[index] = n + 1
        except Exception as e:
            # 状态栏保持友好文案；真实原因和压缩堆栈进聊天记录，认得出是哪一步炸的
            import traceback
            self.set_status("未能填入，请确认聊天窗口可用后重试，或复制回复。", "error")
            self.log(f"[填入失败] {type(e).__name__}: {e}")
            self.log(f"[填入失败堆栈] {' '.join(traceback.format_exc().split())[:300]}")
            return
        if len(parts) > 1 and n + 1 < self._round_max():
            self._set_round(n + 1)  # 还有下一句：自动翻过去，用户接着点「填入」就行
            self.set_status(f"已填入第 {n + 1}/{len(parts)} 条，发出去后再点「填入」接着下一条。",
                            "success")
        else:
            self.set_status("已尝试填入，请确认内容后发送。", "success")

    def _round_max(self):
        """这批候选里最长的那条有几条消息 = 顶部翻页按钮有几个。"""
        return max((len(draft.lines(c)) for c in self.cands), default=1) or 1

    def _set_round(self, n):
        """翻到第 n 条（从 0 起）：三张卡片和候选条一起翻，顶部的按钮跟着高亮。

        按钮在面板顶部（_RoundTabs），**三个候选一起翻**——不是每张卡片各管各的。"""
        self._round_view = n
        for card in self.cards:
            card.set_round(n)
        self.roundTabs.set_rounds(self._round_max(), n)
        self._sync_bar()

    def _copy(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        self.app.clipboard().setText(self.cands[index])
        self._used(index, "copy")
        self.set_status("回复已复制，可粘贴并修改。", "success")

    def _fillable(self, index=None):
        """现在能不能把候选填进去：有候选、不在生成中，而且**微信屏幕上开着的正是面板里这个会话**。

        最后一条是固定模式带出来的：微信切走之后候选还摆在面板上，按钮看着能点，一点却打进
        别人的输入框。填不了的时候按钮就灰着（_sync_fillable），点候选条那条路走 _fill 里那道拦。

        index 非空 = 按那一张卡片算：候选已经作废（标题变「上次建议」）但**这条多轮还没填完**的
        放行。自己刚填进去的那条会被 OCR 读回来当成新消息，一路走到 invalidate_replies——
        不放行的话「点一次填一条」到第二条就点不动了（真机上报过：Ctrl+1 第二次没反应）。
        判据是「这条填过、而且还没填到最后一条」，不是「全局翻到了第几轮」：
        不然单条候选那张也跟着放行，作废的旧建议又能填了。"""
        if self._busy or not self._shown or self._shown != self._chat:
            return False
        if self._current:
            return True
        if index is None or index >= len(self.cands):
            return False
        parts = draft.lines(self.cands[index]) or [self.cands[index]]
        return 0 < self._fill_round.get(index, 0) < len(parts)

    def _sync_fillable(self):
        for card in self.cards:
            card.set_available(self._fillable(card.index))

    def _offline_note(self):
        """微信开着的不是面板里这个会话，说清楚为什么填不了、怎么才能填。

        固定模式下这是常态（微信切走了，面板还钉在固定的那个会话上），所以文案跟「浏览中」
        分开写——那时候用户是自己在翻记录，这时候是微信被切走了。"""
        if self._pinned:
            here = f"「{self._chat}」" if self._chat else "别的会话"
            return f"已固定「{self._pinned}」；微信现在开着{here}，切回去才能填入。"
        return f"正在浏览「{self._shown}」，只看不填；切回这个会话才能用。"

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

    def set_busy(self, busy, opener=False, waited=0):
        """opener=True：这次跑的是开场白，生成中那句话和角标都得跟着换（见 _sync_bar）。
        waited 是「对方多少分钟没回」——生成期间候选还没摆出来，头上的时间得靠它，
        不然那几秒会退回去显示对方上一条消息，跟「对方还没回」这个标题自相矛盾。"""
        self._busy = busy
        self._busy_opener = bool(busy) and opener
        self._opener_wait = waited if self._busy_opener else 0
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
        self._sync_fillable()
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
            # 按卡片算，不是一律灰掉：多轮填到一半的那张要留着能点（见 _fillable）。
            # _round_view 不清——它正是「填到一半」的唯一凭据
            card.set_available(self._fillable(card.index))
        # 候选条跟着清空：新消息一来，旧候选就不该再摆在宠物旁边了
        self._ordered = []
        self._opener.pop(self._shown, None)  # 这批开场白不算数了，「换一批」跟着收掉
        self._sync_bar()

    def has_reply(self):
        """现在有没有可填的候选。main.py 拿它派生 phase，别去读 _current。"""
        return self._current

    def _suggest_text(self):
        """候选条底部那行「建议：xxx · 紧张度 N/9」，口径跟面板的洞察卡一致。
        开场白没跑 Jev 判断，这行没有内容可说，返回空串（不然会摆一句「建议：暂未判断」）。"""
        if self._shown in self._opener:
            return ""
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

    def _opener_again(self):
        """「换一批」：三条开场白都不满意，重新起草一批（走父进程，跟自动那批同一条路）。"""
        if self.on_opener_again:
            self.on_opener_again()

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
        items = []
        for position, (index, score, recommended) in enumerate(self._ordered):
            parts = draft.lines(self.cands[index]) or [self.cands[index]]
            # 条子只有一行宽：多轮的候选摆**当前翻到的那条**（跟面板一致），meta 里标一句
            # 「连发 N 条」，其余几条去面板上翻
            items.append((index, position + 1, parts[min(self._round_view, len(parts) - 1)],
                          score, recommended,
                          f" · 连发 {len(parts)} 条" if len(parts) > 1 else ""))
        # 开场白：头上那句不能再用「对方刚说 X」（那挂的是上一条对方的文字消息，摆在这儿
        # 像是刚收到的），改成「对方还没回 + 隔了多久」。生成中那次也算——见 set_busy。
        # 只在 thinking/ready 两态这么摆：notify 是「对方刚来消息」，那会儿说「还没回」就是撒谎。
        opener = self._phase in ("thinking", "ready") and (self._busy_opener
                                                           or self._shown in self._opener)
        frozen = bool(self._pinned) and self._shown != self._chat  # 固定着，但微信开在别人那儿
        blank = bool(self._opener.get(self._shown, (0, False))[1])  # 这批开场白是空会话那种
        if frozen:
            # 这一行改成说清楚为什么点不动。「对方刚说 XX」那会儿也不该摆——固定这个会话
            # 这会儿根本没在被读，那句话早就不新鲜了。摆在这儿是因为面板可能收着（宠物形态），
            # 点一下没反应会以为程序卡了；条子只有 280px，这句已经占满了，
            # 全话在面板状态栏里（_offline_note）
            heard = "固定中，切回微信才能填"
        elif blank:
            heard = "先开口打个招呼"  # 这个会话一句都还没说过，没有「上一条」可报
        elif self._shown in self._opener:
            heard = f"距你上一条 {self._opener[self._shown][0]} 分钟"
        elif self._busy_opener and self._opener_wait:
            heard = f"距你上一条 {self._opener_wait} 分钟"
        else:
            # 兜底那一句（这个会话还没读到她的文字）不能用微信开着的那个会话的：固定模式下
            # 那是别人的话，摆在「对方刚说」的位置上就张冠李戴了
            heard = self.hers.get(self._shown) or ("" if self._pinned else self.hers.get(self._chat)) or ""
        self.bar.set_items(items, self._suggest_text() if items else "", self._phase, heard,
                           self._voice_text(), opener=opener, blocked=frozen, blank=blank)
        # 顶部的翻页按钮也跟着刷（它是候选级的，跟候选条同一个节奏：候选空了就藏起来）。
        # 放在这儿是因为 _sync_bar 已经是「面板状态变了就刷一遍」的出口
        self.roundTabs.set_rounds(self._round_max(), self._round_view, self._round_hint())

    def _round_hint(self):
        """设置里开着多轮（> 1）、但这一轮 AI 全给的是一句时，那一块摆这句。

        空着的话用户没法判断是 AI 觉得不用连着说、还是新功能压根没生效（2026-10-01 提的）。
        设置是 1 时不摆——那会儿「一轮」本来就是常态，没什么可说的。"""
        if not self.cands or self._round_max() > 1:
            return ""
        return "AI 建议回复一轮" if settings.reply_rounds() > 1 else ""

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
        """宠物右键菜单的内容：暂停采集 / 全屏（主页）/ 会话模式 / 生成开场白 / 设置。

        五项都只是既有入口的快捷方式，不新增数据流。单独拆成一个方法是为了
        tools/preview_ui.py 能直接摆出来截图——exec() 是嵌套事件循环，截图回调进不去。

        「会话模式」跟面板里那个按钮一个口径：**写的是现在是什么模式**（跟随/固定），点一下换一种，
        走的是同一个出口（_toggle_pin → 父进程 set_pin）。为什么不在菜单上写「固定会话」这种
        动词版：那样得先分辨「这是现在的状态还是点完的结果」，两个界面写两套说法更容易乱。

        「生成开场白」按父进程那套前提先灰着：设置里没开冷场开场白、或者采集停着，点了也白花钱
        （start_opener 里也是这么拦的）。灰着的时候把原因写在菜单上，不然用户只看到一条点不动的项。"""
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
        mode = Action(FIF.UNPIN if self._pinned else FIF.PIN,
                      "会话模式：固定" if self._pinned else "会话模式：跟随", menu)
        mode.setEnabled(bool(self._pinned or self._shown))  # 还没认到会话时没什么可固定的
        mode.triggered.connect(self._toggle_pin)
        menu.addAction(mode)
        if not settings.opener():
            why = "（设置里没开）"  # 第一件事就是去设置里把那个开关打开，先说这个
        elif not capturing:
            why = "（采集已暂停）"
        else:
            why = ""
        hello = Action(FIF.CHAT, "生成开场白" + why, menu)
        hello.setEnabled(not why)
        hello.triggered.connect(self._opener_again)
        menu.addAction(hello)
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

    def _reread(self):
        """「重新识别」按钮。干活的在父进程（要叫醒子进程去重读），界面只管叫一声。"""
        if self.on_reread:
            self.on_reread()

    def _import(self):
        """「导入」按钮。弹窗收文本、解析、挑「哪个是我」，然后把活交给父进程。

        写库写上下文都在父进程（`main.import_history`）——这儿只有界面，拿不到
        `chats[会话]["history"]`，而那份才是喂模型的东西。"""
        if not self._shown:
            self.set_status("导入：先切到一个会话，再导。", "warning")
            return
        box = _ImportBox(self.win, self._shown)
        if not box.exec():
            return
        rows, me, replace, labels = box.result_rows()
        # 用户给隐形昵称起的名字存下来，下次导入同一个会话直接套上（只在点了「导入」之后存，
        # 取消不该留下痕迹）。落盘走 save_chat_remark 单键写，不惊动 save() 那套注册表重写。
        for raw, label in labels.items():
            settings.save_chat_remark(self._shown, raw, label)
        if self.on_import:
            self.on_import(self._shown, rows, me, replace)

    def _clear(self):
        """「清除」按钮：把这**一个会话**的记录清干净，好从头重导。

        **跟设置页那个「清空聊天记录」方向是反的，别把两处弄混**（两边都留着说明，因为
        这个区别最容易改错）：
          - 设置页那个（`_clear_chatlog`）清**全库**，而且**故意不动内存**——清了内存会让
            `_already_read` 放行，屏幕上那几屏被当成新消息重读一遍、又写回库里，等于没清干净；
          - 这个只清**当前这个会话**，而且**有意连内存一起清**——用户就是要它从头来过
            （真机上的用法：被几次导入搞脏了，想清干净重导）。
        代价一样：屏幕上还留着的那几屏，下次采集会重新读进来。那是「清除」这个动作本身的
        意思，不是 bug。

        落库和写上下文都在父进程（`main.clear_history`）——这儿拿不到
        `chats[会话]["history"]`，那份才是喂模型的东西。"""
        if not self._shown:
            self.set_status("清除：先切到一个会话，再清。", "warning")
            return
        if not _ConfirmBox(
                "清除这个会话的聊天记录？",
                f"「{self._shown}」在本机存的聊天记录全部删掉，删了找不回来。\n"
                "其它会话、关系、设置都不受影响。\n\n"
                "清完之后，微信里现在还摆在屏幕上的那几屏，下次采集会重新读进来。",
                self.win).exec():
            return
        if self.on_clear:
            self.on_clear(self._shown)

    def _make_shortcut(self):
        """「创建桌面快捷方式」按钮。写 .lnk 走 COM，那摊子事在 app/shortcut.py + main 那边，
        界面只管叫一声——建成了没有由 main 回写到状态栏。"""
        if self.on_make_shortcut:
            self.on_make_shortcut()

    def fix_message(self, chat, who, old, new):
        """某条之前读花了，把记录里那条的就地改掉（重新识别对账用，见 app.ocr.reconcile）。

        跟 `_merge_voice` 一样从后往前找**最近**的那条：同一个人连说两句一模一样的时候，
        改最新的那条才对得上。正文没在 feeds 里（比如早翻出去了）就什么都不做。"""
        lines = self.feeds.get(chat, [])
        for i in range(len(lines) - 1, -1, -1):
            w, text, name, stamp, voice = lines[i]
            if w == who and text == old:
                lines[i] = (w, new, name, stamp, voice)
                self._sync_hers(chat)  # 「对方最近说」跟着换，不然还挂着读花的那句
                if chat == self._shown:
                    self._render_feed()
                return True
        return False

    def _sync_hers(self, chat):
        """按记录重算一遍「对方最近说」。

        补漏/改字之后必须重算，不能按**插入顺序**顺手写：`insert_message` 是从后往前插的，
        先插进去的「嗯」会被后插进去的「?」覆盖掉，明明「嗯」才是最后一条。"""
        hers = [t for who, t, *_ in self.feeds.get(chat, []) if who == "her"]
        if not hers:
            return
        self.hers[chat] = hers[-1]
        if chat == self._shown:
            self._show_latest(hers[-1])

    def feed_index(self, chat, item):
        """`item` 是 (who, text)，在 feeds[chat] 里从后往前找**最近**那条对得上的，返回下标；
        找不到返回 -1。重新识别用它定位「这一段从哪儿开始变」，好把库里对应的尾巴整段重写。"""
        if not item:
            return -1
        lines = self.feeds.get(chat, [])
        for i in range(len(lines) - 1, -1, -1):
            if lines[i][0] == item[0] and lines[i][1] == item[1]:
                return i
        return -1

    def insert_message(self, chat, after, who, text, name=""):
        """补一条漏掉的消息进聊天记录，插在 `after` 那条后面（after 是 (who, text)，None = 插到最前）。

        重新识别专用：漏掉的消息多半在中间（「她 ? 」「我 在」「她 嗯」里那条「?」），直接 append
        到末尾看着就像她最后才说的。锚点找不到（那条已经翻出去了）就退回末尾——宁可顺序差一点，
        也别把这条丢了。

        **不写库**：库里那一段由调用方整段重写（`chatlog.rewrite_tail`），append 追不到中间去。"""
        entry = (who, text, name, datetime.now().strftime("%m-%d %H:%M"), "")
        lines = self.feeds.setdefault(chat, [])
        at = 0 if after is None else len(lines)  # None = 补在开头；锚点找不到才退回末尾
        if after:
            found = self.feed_index(chat, after)
            if found >= 0:
                at = found + 1
        lines.insert(at, entry)
        del lines[:-_LOG_LINES]
        self.counts[chat] = self.counts.get(chat, 0) + 1
        self._sync_hers(chat)  # 按位置重算，不能直接写：从后往前插的时候最后写的不是最后一条
        self._add_chat(chat)
        if chat == self._shown:
            self._render_feed()

    def restore(self, chat, rows):
        """开机把上次存下来的聊天记录填回内存（rows 是 core.chatlog 读出来的五元组，正序）。

        **只填不写库**，跟 log_message 分成两条路是故意的：那条会往库里再插一遍，
        重启一次记录就翻一倍。没配库 / 开关关着的时候没人调它。"""
        lines = [tuple(r) for r in rows][-_LOG_LINES:]
        if not lines:
            return
        self.feeds[chat] = lines
        self.counts[chat] = len(lines)
        hers = [t for who, t, *_ in lines if who == "her"]
        if hers:
            self.hers[chat] = hers[-1]
        self._add_chat(chat)

    def set_feed(self, chat, rows):
        """把某个会话的记录**整段换成** rows（五元组，时间正序）。导入用。

        `rows` 传空就是**清空**这个会话——导入勾了「替换现有记录」时走的就是这条（原来
        这儿有个单独的 `reset_feed`，因为只服务那一个调用点，被这个替掉了）。

        **只动内存**——库里那份由调用方整段重写（`chatlog.replace_chat`），道理跟
        `insert_message` 一样：导入的旧记录要插到现有记录中间，append 追不进去，只能整段来。

        `counts` 和 `hers` 跟着重算：不重算的话角标还挂着旧的条数，候选条上还摆着
        上一句（`_sync_hers` 按位置算，比手写靠谱）。"""
        lines = [tuple(r) for r in (rows or ())]
        del lines[:-_LOG_LINES]
        self.feeds[chat] = lines
        self.counts[chat] = len(lines)
        self.hers.pop(chat, None)
        self._sync_hers(chat)
        self._add_chat(chat)
        if chat == self._shown:
            self._render_feed()
            self._history_title()

    def log_message(self, who, text, name="", timestamp=None, chat=None, voice=""):
        """按会话存一份（内存 + 本地库）；只有正在看的那个会往显示区里写。

        voice 非空 = 这条是语音转出来的字，值是那条语音的时长（`3"`）：能对上前面那条
        「🔊 语音消息 3"」就并成一条（微信那边本来就是一条：气泡 + 转写），对不上就单独
        一条、气泡里标一行「🔊 语音转文字」。"""
        chat = chat or self._shown
        timestamp = timestamp or datetime.now().strftime("%m-%d %H:%M")
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
        # 落库：开关关着 / 没配库时 chatlog 自己空转，这儿不用问（见 core/chatlog.py）
        chatlog.append(chat, who, text, name, timestamp, voice)
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
                # 库里那条占位也跟着换掉：不换的话重启之后画出来的是「🔊 语音消息 3"」，
                # 而这行字早就转成文字了（id 和时间都不动，顺序和显示都一样）
                chatlog.merge_voice(chat, mark, who, text, dur)
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

    def screen_chat(self):
        """微信屏幕上**当前开着**的会话。填入选人的输入框、右键语音点坐标，认的都是它。

        跟 current_chat() 分开是固定模式带出来的：那会儿面板钉在 A 上，微信可能早开到 B 了，
        照着面板去填就会打进 B 的输入框。"""
        return self._chat

    def set_chat(self, title):
        """微信切到了哪个会话：登记进下拉框。跟随模式跟过去，固定模式面板不动（见 set_pin）。"""
        if not title:
            return
        browsing = self._shown != self._chat  # 正看着的就是它、但之前是「浏览中」：也得重画，把填入放开
        self._chat = title
        self._add_chat(title)
        if self._pinned:
            # 固定：面板一直摆着固定那个会话。微信开着的那个（可能是别的）只影响「能不能填」
            self._sync_fillable()
            self._follow_text()
            if self._shown != self._chat:
                self.set_status(self._offline_note(), "idle")
            elif self.cands:
                # 不写一句的话，上一条「切回去才能填入」会赖着不走，看着像还没切回来
                self.set_status(f"已切回「{self._pinned}」，可以填入了。", "idle")
            return
        if title != self._shown or browsing:
            self._select_in_box(title)
            self._switch_to(title)
        self._follow_text()

    def _add_chat(self, title):
        """新会话自动进下拉框；addItem 添第一条时会自己选中，别让它触发切换。"""
        if not title or self.chatBox.findText(title) >= 0:
            return
        self.chatBox.blockSignals(True)
        self.chatBox.addItem(title)
        self.chatBox.blockSignals(False)

    def _select_in_box(self, title):
        """把下拉框拨到某个会话上，别让它触发 _on_chat_selected（那等于用户自己挑的）。"""
        self.chatBox.blockSignals(True)
        self.chatBox.setCurrentIndex(self.chatBox.findText(title))
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
        self._render_relations()  # 关系是按会话存的，换会话就得跟着换
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

    def _render_relations(self):
        """把「关系」和「场景模板」两个下拉拨到这个会话现在的样子。

        选项每次都现取：设置页里刚加的那型关系，回到首页就该出现在下拉里。重填时屏蔽信号，
        别把自己的填充当成用户挑的。还没认到会话就整个灰掉——那会儿拨了也没有对象可改。"""
        chat = self._shown
        self._relKeys = list(settings.relation_choices())
        self._sceneKeys = list(styles.choices(settings.relation_customs()))
        for box, keys, current in (
                (self.relationBox, self._relKeys, settings.relation_of(chat) if chat else ""),
                (self.sceneBox, self._sceneKeys, settings.scene_of(chat) if chat else "")):
            box.blockSignals(True)
            box.clear()
            box.addItems([name for _, name in keys])
            box.setCurrentIndex(next((i for i, (k, _) in enumerate(keys) if k == current), 0))
            box.blockSignals(False)
            box.setEnabled(bool(chat))
        self.sceneBox.setToolTip(
            "挑一型就用那一型的正文说话（比如对一个客户用「同事」那套口气）；"
            "不挑就跟着上面那个「关系」走。正文在「设置 → 个人风格」里按关系改。")

    def _on_relation_selected(self, index):
        """用户给这个会话挑了关系。拨一下立刻写盘，**下次生成才生效**——手上那三条候选是上一个
        关系写出来的，不因为改这个就作废（要马上看新的，回候选条点「换一批」或等新消息）。"""
        if not self._shown or not 0 <= index < len(self._relKeys):
            return
        key, name = self._relKeys[index]
        self.set_status(f"「{self._shown}」按「{name}」来写，下次生成生效。", "idle")
        if self.on_relation_change:
            self.on_relation_change(self._shown, key)

    def _on_scene_selected(self, index):
        """用户给这个会话挑了场景模板（口吻覆盖）。跟「关系」一样，下次生成才生效。"""
        if not self._shown or not 0 <= index < len(self._sceneKeys):
            return
        key, name = self._sceneKeys[index]
        self.set_status(f"「{self._shown}」的口吻跟着「关系」走。" if not key else
                        f"「{self._shown}」按「{name}」那套口气来写，下次生成生效。", "idle")
        if self.on_scene_change:
            self.on_scene_change(self._shown, key)

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

    def set_pin(self, name):
        """固定到某个会话（"" = 跟随微信切）。父进程才是准的那一份，这里只负责摆成它的样子。

        固定之后面板一直摆着这一个：微信切到别的会话也不跟过去（`set_chat` 里那个分叉），
        而且微信屏幕上一旦不是它，就填不了（`_offline_note` / `_fillable`）——粘贴是直接发给
        微信当前会话的，按会话隔离在它那儿不存在，照着面板填就会打进别人的输入框。

        启动时带着上次固定那个会话进来时，`_shown` 还是空的：面板先摆过去，等子进程读到
        微信开着的会话再各归各位。"""
        self._pinned = str(name or "")
        if self._pinned and self._pinned != self._shown:
            self._add_chat(self._pinned)
            self._select_in_box(self._pinned)
            self._switch_to(self._pinned)
        self._follow_text()
        self._sync_fillable()

    def _toggle_pin(self):
        """「跟随/固定」那个按钮：固定 = 钉住现在看的这个会话；再点一下回到跟随。

        只把意图报给父进程，由它落定（名单、消息归谁、要不要收掉在等的判断都在它手里）。"""
        if not self.on_pin_change:
            return
        if not self._pinned and not self._shown:
            self.set_status("还没识别到会话，等它读一帧再固定", "warning")
            return
        self.on_pin_change("" if self._pinned else self._shown)

    def _follow_text(self):
        """那个按钮上写什么、什么颜色：它显示的是**现在是什么模式**，点一下就换一种。

        固定是「我给过指令」的状态，用实底标出来（微信切走了面板还不动，不显眼会让人以为卡了）。"""
        if self._pinned:
            text = "固定"
            tip = (f"已固定「{self._pinned}」：微信切到别的会话也不跟过去，这个会话有消息照样给建议。"
                   "点一下回到跟随。")
            qss = (f"QPushButton {{ color: {theme.PAPER}; background: {theme.SAGE}; border: none;"
                   f" border-radius: {theme.RADIUS_SM}px; padding: 1px 6px; }}")
        else:
            # 还没认到会话就空着（两个都是空串，别当成「跟随」——那看着像已经跟着谁了）
            text = ("跟随" if self._shown == self._chat else "浏览中") if self._chat else ""
            tip = "微信切到哪个会话，面板就跟到哪个；点一下把现在这个固定住，之后切走也不跟。"
            qss = (f"QPushButton {{ color: {_MUTED}; background: transparent; border: none; }}"
                   f"QPushButton:hover {{ color: {theme.SAGE}; }}"
                   f"QPushButton:disabled {{ color: {theme.LINE}; }}")
        self.chatFollow.setText(text)
        self.chatFollow.setToolTip(tip)
        self.chatFollow.setEnabled(bool(self._pinned or self._chat))
        self.chatFollow.setStyleSheet(qss)

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
            self._opener.pop(self._shown, None)
            self._empty_text()
            self._round_view = 0  # 没候选了，翻页按钮跟着收掉（_round_max 也回到 1）
            self.roundTabs.set_rounds(1, 0)
        if self._shown != self._chat:
            self.invalidate_replies()
            self.set_status(self._offline_note())

    def show(self, result):
        """按推荐顺序展示，按钮始终绑定 candidates 的原始索引。

        结果里 opener=True 的是冷场开场白：没跑过 Jev，answers/scores 都是空的，
        所以洞察卡换成开场白那套说法（不摆意图和紧张度，给个「换一批」），状态栏也换一句。"""
        self.cands = result["candidates"]
        opener = bool(result.get("opener"))
        self.set_busy(False)
        self._current = bool(self.cands)
        self._clear_cards()
        # 新的一批候选：从第一轮看起、谁都没填过。**必须在建卡片之前清**——
        # _ReplyCard 构造时就会读它决定摆第几条、按钮写「填入 1/3」还是别的
        self._round_view = 0
        self._fill_round = {}
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
        answers = result.get("answers") or {}
        self._answers = answers
        self._ordered = [(index, scores[index], index == best) for index in order]
        waited = result.get("waited") or 0
        blank = bool(result.get("blank"))  # 空会话的开场白（刚加的好友、只有表情/图片）
        if opener:
            self._opener[self._shown] = (waited, blank)
            self.contextTitle.setText("这个会话还没聊过" if blank else "上次聊到")
            self.insightTitle.setText("开场白 · 还没聊过" if blank else
                                      (f"开场白 · 对方 {waited} 分钟没回" if waited else "开场白"))
            self.summary.setText("还没聊过，先开口打个招呼" if blank else "接着上次的话，主动起个头")
            self.intent.hide()  # 七道题一道都没问，这两行没有内容可摆
            self.tension.hide()
            self.againButton.show()
        else:
            self._opener.pop(self._shown, None)
            self.contextTitle.setText("对方最近说")
            self.insightTitle.setText(f"对话参考 · 回复给 {reply_to}" if reply_to else "对话参考")
            self.summary.setText("建议：" + _choice(answers, "best_action"))
            self.intent.setText("可能意图 · " + _choice(answers, "true_intent") +
                                "\n可能需要 · " + _choice(answers, "she_needs"))
            self.intent.show()
            self.againButton.hide()
            score = (answers.get("danger_level") or {}).get("score")
            valid_score = isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9
            self.tension.setText(f"紧张度 {score:.0f}/9" if valid_score else "紧张度待判断")
            color = theme.WARN if valid_score and score >= 3 else _MUTED
            if valid_score and score >= 6:
                color = theme.DANGER
            qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
            setCustomStyleSheet(self.tension, qss, qss)
            self.tension.show()
        self.empty.setVisible(not self.cands)
        self.insight.setVisible(bool(self.cands))
        self.referenceNote.setVisible(bool(self.cands) and not self._compact)
        self.updated.setText(datetime.now().strftime("%H:%M") + " 更新")
        if self.cands and self._errorDetail:
            # 候选是好的、只是判断/排序那一步挂了：照样能用，但要说清楚为什么没有概率和推荐
            self.set_status("判断服务没应答，这三条是按对话直接起草的；点「查看详情」看原因。",
                            "warning")
        elif self.cands and opener:
            self.set_status("还没聊过，给你起了个头，挑一句发过去" if blank else
                            (f"对方 {waited} 分钟没回，给你起了个头，挑一句发过去"
                             if waited else "给你起了个头，挑一句发过去"), "success")
        elif self.cands:
            self.set_status("建议已更新，选一句适合你的回复", "success")
        else:
            self.set_status("未生成可用回复，请等待下一条新消息。", "error")
        self._sync_fillable()  # 卡片是刚建的，默认是能点的——固定模式下微信切走了就得灰着
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
