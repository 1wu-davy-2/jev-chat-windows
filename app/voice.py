# -*- coding: utf-8 -*-
"""语音消息转文字：右键那条语音 → 点菜单里的「语音转文字」。

**为什么只能盲点第一项。** 两条路都堵死了：
1. WGC 按窗口抓图看不见弹出菜单——实测过（拿一个纯蓝的 Qt 窗当靶子，菜单压在它上面，
   抓到的帧客户区 99.4% 还是蓝的，菜单该占的那 8.5% 一个像素都没有）。所以没法先 OCR
   菜单、确认「语音转文字」在哪个位置再点。
2. UIA 也读不到——微信整个界面自绘在一块 GPU 画布上，控件树是空的（见 README「为什么走 OCR」）。

**所以安全边界靠「确认菜单真的弹出来了」而不是「确认点的是哪一项」。** 右键之后枚举顶层窗口，
只有多出来一个菜单大小的新窗口、位置又贴着刚才那一下，才动手点；没多出来就当场放弃。
菜单里第 6 项是「删除」，所以宁可放弃也不能瞎点。

只干这一件事：把语音转成文字。不发送、不删除、不碰转账红包。
"""
import ctypes
import ctypes.wintypes as w
import time

from app.fill import foreground

u32 = ctypes.windll.user32

# 第一项中心在菜单里的纵向位置，按真实截图量的：菜单卡片 162x224，第一项高亮区 y 34..65，
# 中心 y = 卡片顶 + 21.5 ≈ 卡片高的 9.6%。用比例不用像素，换 DPI 时跟着一起缩放。
_FIRST_ITEM_Y = 0.096
_MENU_WAIT = 1.2     # 右键之后等菜单弹出来，最多这么久
_MENU_W, _MENU_H = (80, 480), (60, 700)  # 菜单窗口的合理尺寸范围，用来把它和别的弹窗区分开


def _visible_windows():
    """当前所有可见顶层窗口 → {hwnd: (left, top, right, bottom)}。"""
    out = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            r = w.RECT()
            if u32.GetWindowRect(hwnd, ctypes.byref(r)):
                out[int(hwnd)] = (r.left, r.top, r.right, r.bottom)
        return True

    u32.EnumWindows(cb, 0)
    return out


def _find_menu(before, after, x, y):
    """在「右键前后」的窗口集合里找出那个菜单：新出现的、菜单大小、位置贴着刚才点的地方。

    位置这条最要紧——它把菜单和「刚好这时候弹出来的别的通知」区分开。找不到就返回 None，
    调用方据此放弃（宁可不转，也不能点到「删除」上）。"""
    for hwnd, (l, t, r, b) in after.items():
        if hwnd in before:
            continue
        wd, ht = r - l, b - t
        if not (_MENU_W[0] <= wd <= _MENU_W[1] and _MENU_H[0] <= ht <= _MENU_H[1]):
            continue
        # 菜单左上角就在光标附近（Windows 把菜单摆在光标处，放不下才翻边）
        if not (l - 120 <= x <= r + 120 and t - 120 <= y <= b + 120):
            continue
        return l, t, r, b
    return None


def _click(x, y, right=False):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
    down, up = (0x8, 0x10) if right else (0x2, 0x4)
    u32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.03)
    u32.mouse_event(up, 0, 0, 0, 0)


def _window_rect(hwnd):
    """跟 WGC 帧对齐的窗口矩形（扩展边界）。跟 app/fill.py 一个口径，别改成 GetWindowRect。"""
    r = w.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def convert(hwnd, rect):
    """右键 rect（采集帧坐标）那条语音，点菜单第一项。

    成功返回 ""；失败返回一句给用户看的中文原因（界面直接显示）。绝不抛异常——
    这是用户点出来的动作，出错要看得见，不能静默。"""
    r = _window_rect(hwnd)
    x0, y0, x1, y1 = rect
    x = r.left + (x0 + x1) // 2
    y = r.top + (y0 + y1) // 2
    old = w.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    try:
        foreground(hwnd)  # 右键得落在前台窗口上，不然菜单弹不出来
        before = _visible_windows()
        _click(x, y, right=True)
        deadline = time.time() + _MENU_WAIT
        menu = None
        while time.time() < deadline:
            time.sleep(0.08)
            menu = _find_menu(before, _visible_windows(), x, y)
            if menu:
                break
        if not menu:
            u32.keybd_event(0x1B, 0, 0, 0)  # Esc：万一菜单其实开了只是没认出来，别留着
            u32.keybd_event(0x1B, 0, 2, 0)
            return "右键菜单没弹出来，请手动右键那条语音选「语音转文字」"
        left, top, right_, bottom = menu
        # 第一项在菜单顶部往下 9.6% 处；横向取中间，避开图标和文字的长度差异
        _click((left + right_) // 2, top + (bottom - top) * _FIRST_ITEM_Y)
        return ""
    except Exception as e:  # 用户点出来的动作，出错要让界面看得见
        return f"转文字失败：{type(e).__name__}: {e}"
    finally:
        u32.SetCursorPos(old.x, old.y)  # 光标还给用户，别留在他没动过的地方
