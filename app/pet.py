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
SHADOW_MARGIN = 18  # 给投影留的余量，不留会被窗口边界裁掉
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
    """去色版本，采集暂停时用。Qt 没有现成的「去饱和」效果，转一次灰度最省事。"""
    if pix.isNull():
        return pix
    img = pix.toImage().convertToFormat(QImage.Format_Grayscale8)
    out = QPixmap.fromImage(img.convertToFormat(QImage.Format_RGB32))
    out.setDevicePixelRatio(pix.devicePixelRatio())
    return out


class _Mascot(QWidget):
    """只负责画吉祥物本身：漂浮靠移动自己，思考态再叠一点旋转。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pix = QPixmap()
        self.angle = 0.0

    def set_pixmap(self, pix: QPixmap):
        self.pix = pix
        if not pix.isNull():
            dpr = pix.devicePixelRatio() or 1.0
            self.setFixedSize(QSize(int(pix.width() / dpr), int(pix.height() / dpr)))
        self.update()

    def paintEvent(self, event):
        if self.pix.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.angle:  # 绕中心转，对应设计稿的 rotate(±2deg)
            p.translate(self.width() / 2, self.height() / 2)
            p.rotate(self.angle)
            p.translate(-self.width() / 2, -self.height() / 2)
        p.drawPixmap(0, 0, self.pix)


class PetWindow(QWidget):
    """桌面宠物窗。clicked / dropped 由 Overlay 接线：前者展开面板，后者记住位置。"""
    clicked = Signal()
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
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        outer.setSpacing(0)
        # 阴影挂 frame，不挂 mascot：一个 QWidget 只能挂一个 QGraphicsEffect，
        # mascot 那个位置要留给别的东西（而且阴影得画在内容之外才有意义）
        self.frame = QWidget()
        self.frame.setAttribute(Qt.WA_TranslucentBackground, True)
        theme.apply_shadow(self.frame, pop=True)
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
        self.badge.move(self.width() - SHADOW_MARGIN - 26, SHADOW_MARGIN + 4)

    def _animate(self):
        """漂浮 + 思考态旋转。窗口尺寸恒定，只挪里面的 mascot，不触发重排。"""
        if self.mascot.pix.isNull():
            return
        self._t += 0.033
        if self._phase == "thinking":
            period, amp = THINK_PERIOD, FLOAT_AMP * 0.6
            self.mascot.angle = 2.0 * sin(2 * pi * self._t / THINK_PERIOD)
        else:
            period, amp = FLOAT_PERIOD, FLOAT_AMP
            self.mascot.angle = 0.0
        dy = amp * sin(2 * pi * self._t / period)
        base_x = (self.frame.width() - self.mascot.width()) // 2
        base_y = (self.frame.height() - self.mascot.height()) // 2
        self.mascot.move(base_x, int(base_y + dy))
        self.mascot.update()

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
