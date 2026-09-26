# -*- coding: utf-8 -*-
"""桌面宠物：一个透明置顶的小窗，平时飘在桌面上，有消息才在它旁边弹候选条。

它跟主面板是两个独立的顶层窗口，不是「一个窗变形」——改 setWindowFlags 会重建 HWND，
位置会丢、还会闪；而且宠物窗和面板窗的尺寸/圆角/透明要求差太远。

窗口是 `Qt.Tool`：不进任务栏、不进 Alt-Tab。再叠 `WindowDoesNotAcceptFocus` +
`WA_ShowWithoutActivating`，点宠物不会把微信的前台焦点抢走——这点很要紧，用户正在打字。

素材不落盘、不联网：三张 PNG 从 app/assets/ 读进来，帧只在内存里。
"""
import ctypes
import ctypes.wintypes
import os
import sys
from math import pi, sin

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app import theme

# 三张图对应三个阶段：平时 / 判断中 / 读到新消息
_PHASES = {"thinking": "mascot-think.png", "notify": "mascot-alert.png"}
_IDLE = "mascot-idle.png"

PET_H = 128       # 设计稿里 h-32 = 128px
FLOAT_AMP = 7.0     # 漂浮幅度（设计稿的 translateY(-7px)）
FLOAT_PERIOD = 3.6  # 漂浮周期，秒
THINK_PERIOD = 1.6  # 思考态晃得快点
DRAG_SLOP = 4       # 位移超过这么多像素才算拖动，否则当成点击

if getattr(sys, "_MEIPASS", None):  # 打包后素材在 _MEIPASS/app/assets（onedir 下就是 _internal）
    _ASSETS = os.path.join(sys._MEIPASS, "app", "assets")
else:
    _ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

_user32 = ctypes.windll.user32
# 64 位下句柄必须显式声明，不然默认 c_int 会把 HWND 截断（跟 app/fill.py 开头一个道理）
_user32.RegisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int,
                                   ctypes.c_uint, ctypes.c_uint]
_user32.RegisterHotKey.restype = ctypes.wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = ctypes.wintypes.BOOL


def mascot_pixmap(phase: str, height: int = PET_H) -> QPixmap:
    """按阶段取吉祥物贴图。读不到就返回空 QPixmap，调用方负责降级成「只有面板」。"""
    path = os.path.join(_ASSETS, _PHASES.get(phase, _IDLE))
    pix = QPixmap(path)
    if pix.isNull():
        return pix
    pix = pix.scaledToHeight(height, Qt.SmoothTransformation)
    # 高 DPI 屏上不按缩放比设 dpr 的话，宠物是糊的（main.py 已经 SetProcessDPIAware 了）
    dpr = QPixmap(path).devicePixelRatio() or 1.0
    pix.setDevicePixelRatio(dpr)
    return pix


def _gray(pix: QPixmap) -> QPixmap:
    """去色版本，采集暂停时用。Qt 没有现成的「去饱和」效果，转一次灰度最省事。

    注意 Grayscale8 和 RGB32 都**没有 alpha 通道**，直接转会把透明背景变成不透明的黑，
    表现是暂停时宠物周围糊一圈黑框。所以转完要把原图的 alpha 贴回去。"""
    if pix.isNull():
        return pix
    img = pix.toImage().convertToFormat(QImage.Format_ARGB32)
    out = img.convertToFormat(QImage.Format_Grayscale8).convertToFormat(QImage.Format_ARGB32)
    out.setAlphaChannel(img.convertToFormat(QImage.Format_Alpha8))
    result = QPixmap.fromImage(out)
    result.setDevicePixelRatio(pix.devicePixelRatio())
    return result


class _Mascot(QWidget):
    """只负责画吉祥物本身。

    漂浮**不是**靠 move() 挪自己：这个控件在布局里，手动 move 会和布局打架，
    而且往上飘时负偏移会把脏区顶到窗口外面——透明窗上表现为
    `UpdateLayeredWindowIndirect failed ... (参数错误)`，画面干脆画不出来。
    所以控件尺寸固定、比贴图高出两倍浮动幅度，浮动和旋转都在 paintEvent 里做。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pix = QPixmap()
        self.angle = 0.0
        self.dy = 0.0  # 漂浮偏移，只在绘制时用

    def set_pixmap(self, pix: QPixmap):
        self.pix = pix
        if not pix.isNull():
            dpr = pix.devicePixelRatio() or 1.0
            # 上下各留 FLOAT_AMP，贴图才有地方飘
            self.setFixedSize(QSize(int(pix.width() / dpr),
                                    int(pix.height() / dpr) + int(2 * FLOAT_AMP)))
        self.update()

    def paintEvent(self, event):
        if self.pix.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        top = FLOAT_AMP + self.dy  # 贴图在控件内的落点
        if self.angle:  # 绕贴图中心转，对应设计稿的 rotate(±2deg)
            dpr = self.pix.devicePixelRatio() or 1.0
            cx, cy = self.width() / 2, top + self.pix.height() / dpr / 2
            p.translate(cx, cy)
            p.rotate(self.angle)
            p.translate(-cx, -cy)
        p.drawPixmap(0, int(top), self.pix)


class PetWindow(QWidget):
    """桌面宠物窗。

    手势分开：悬停 / 单击 → 弹紧凑候选条；双击 / 右键 → 展开完整面板；
    拖动 → 记住位置。四个信号都由 Overlay 接线，这里只管发。"""
    hovered = Signal()
    clicked = Signal()
    expand = Signal()
    dropped = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.PointingHandCursor)
        self._phase = "idle"
        self._t = 0.0
        self._drag = None
        self._moved = False
        self._paused = False
        self._pix = {}  # {阶段: (彩色, 灰色)}，只在第一次用到时算

        outer = QVBoxLayout(self)
        # 投影余量：QGraphicsDropShadowEffect 按 blurRadius 向外扩、再按 offset 下移，
        # 所以下方要多留一个 offset。留不够脏区就顶到窗口矩形外——透明窗上表现为
        # UpdateLayeredWindowIndirect failed，阴影直接被裁掉。
        outer.setContentsMargins(*theme.SHADOW_PAD_MASCOT)
        outer.setSpacing(0)
        # 阴影挂 frame，不挂 mascot：一个 QWidget 只能挂一个 QGraphicsEffect，
        # mascot 那个位置要留给别的东西（而且阴影得画在内容之外才有意义）
        self.frame = QWidget()
        self.frame.setAttribute(Qt.WA_TranslucentBackground, True)
        theme.apply_shadow(self.frame, kind="mascot")
        outer.addWidget(self.frame)

        stage = QVBoxLayout(self.frame)
        stage.setContentsMargins(0, 0, 0, 0)
        self.mascot = _Mascot(self.frame)
        stage.addWidget(self.mascot, 0, Qt.AlignCenter)

        self.badge = QLabel("1", self.frame)
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.setFixedSize(22, 22)
        self.badge.setStyleSheet(
            f"QLabel {{ background: {theme.WARN}; color: {theme.PAPER}; "
            f"border-radius: 11px; font-weight: 700; }}"
        )
        self.badge.hide()

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30fps，够顺滑了
        self._timer.timeout.connect(self._animate)

        self.set_phase("idle")
        self._timer.start()

    def set_phase(self, phase):
        """换贴图 + 换动画节奏。同态重复调用是空操作。"""
        if phase == self._phase and not self.mascot.pix.isNull():
            return
        self._phase = phase
        self._t = 0.0
        self._apply_pixmap()
        self.badge.setVisible(phase in ("notify", "ready"))

    def set_paused(self, paused):
        """采集暂停时整体淡下去、去个色，一眼能看出「现在没在读屏」。"""
        if paused == self._paused:
            return
        self._paused = paused
        self.setWindowOpacity(0.7 if paused else 1.0)
        self._apply_pixmap()

    def _apply_pixmap(self):
        if self._phase not in self._pix:
            color = mascot_pixmap(self._phase)
            self._pix[self._phase] = (color, _gray(color))
        color, gray = self._pix[self._phase]
        self.mascot.set_pixmap(gray if self._paused else color)
        self.adjustSize()
        self._place_badge()

    def _place_badge(self):
        pad_left, pad_top = theme.SHADOW_PAD_MASCOT[0], theme.SHADOW_PAD_MASCOT[1]
        self.badge.move(self.width() - pad_left - 26, pad_top + 4)

    def _animate(self):
        """漂浮 + 思考态旋转。控件位置不动，只改绘制偏移，脏区永远落在窗口内。"""
        if self.mascot.pix.isNull():
            return
        self._t += 0.033
        if self._phase == "thinking":
            period, amp = THINK_PERIOD, FLOAT_AMP * 0.6
            self.mascot.angle = 2.0 * sin(2 * pi * self._t / THINK_PERIOD)
        else:
            period, amp = FLOAT_PERIOD, FLOAT_AMP
            self.mascot.angle = 0.0
        self.mascot.dy = amp * sin(2 * pi * self._t / period)
        self.mascot.update()

    def move_to_default(self):
        """没存过位置就摆到右下角（设计稿里吉祥物也在那儿），别让 Qt 随便丢一个地方。"""
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 40)

    def clamp_to_screen(self):
        """换显示器/拔了外接屏之后别让宠物落在屏幕外。找不到所在屏就回主屏。"""
        from PySide6.QtGui import QGuiApplication

        center = self.frameGeometry().center()
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = min(max(self.x(), area.left()), area.right() - self.width())
        y = min(max(self.y(), area.top()), area.bottom() - self.height())
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def enterEvent(self, event):
        """鼠标一搭上宠物就把候选条摆出来——不用先点一下。"""
        self.hovered.emit()
        super().enterEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = None  # 双击的第一下已经被当成单击了，这里别再判一次
            self.expand.emit()
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        """右键 = 展开完整面板（不弹菜单，少一步操作）。"""
        self.expand.emit()
        event.accept()

    # ── 拖动 / 点击 ──
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.pos()
            self._moved = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            p = event.globalPosition().toPoint() - self._drag
            if (p - self.pos()).manhattanLength() > DRAG_SLOP:
                self._moved = True  # 手抖一下不算拖动，否则点宠物永远展不开面板
            self.move(p)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            if self._moved:
                self.clamp_to_screen()
                self.dropped.emit(self.x(), self.y())
            else:
                self.clicked.emit()
        self._drag = None
        super().mouseReleaseEvent(event)


class HotkeyHost(QWidget):
    """只用来收 WM_HOTKEY 的隐藏窗：不 show、不进任务栏、活到进程结束。

    不挂在宠物窗上是因为宠物可能被设置关掉；不挂在面板窗上是因为它可能被 close()。"""
    hotkey = Signal(int)

    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000  # 不带这个的话按住 Ctrl+1 会连发，剪贴板被反复覆写
    WM_HOTKEY = 0x0312

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool)
        self.hwnd = int(self.winId())  # 强制建 HWND；隐藏窗照样收得到 WM_HOTKEY
        self._registered = []

    def register(self, hotkey_id: int, vk: int) -> bool:
        """注册 Ctrl+<vk>。返回 False 只说明被别的程序占了，不是错误，调用方降级即可。"""
        ok = bool(_user32.RegisterHotKey(self.hwnd, hotkey_id,
                                         self.MOD_CONTROL | self.MOD_NOREPEAT, vk))
        if ok:
            self._registered.append(hotkey_id)
        return ok

    def unregister_all(self):
        for hotkey_id in self._registered:
            _user32.UnregisterHotKey(self.hwnd, hotkey_id)
        self._registered = []

    def nativeEvent(self, eventType, message):
        """PySide6 这里必须返回 (handled, result) 元组——返回裸 True/False 会静默失效。"""
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(ctypes.wintypes.MSG)).contents
        except (TypeError, ValueError):
            return False, 0
        if msg.message == self.WM_HOTKEY:
            self.hotkey.emit(int(msg.wParam))
            return True, 0
        return False, 0
