# -*- coding: utf-8 -*-
"""语音消息转文字：悬停那条语音 → 点微信自己冒出来的「转文字」按钮。

**为什么不走右键菜单。** 原先走的是「右键 → 点菜单第一项『语音转文字』」，实测发现：
1. WGC 按窗口抓图**看不见弹出菜单**，所以没法先 OCR 菜单确认点的是哪一项，只能按固定偏移盲点；
2. 那个菜单里有「删除」，点歪了就是真删掉一条聊天记录。

改用悬停之后这两条都没了：微信在鼠标悬停语音气泡时会**在气泡外侧冒一个内联的「转文字」药丸**，
而它**在 WGC 帧里看得见**（调试视图里能直接读到「转文字」三个字），所以位置是量出来的、
不是猜的，而且这个按钮点歪了顶多没反应——旁边是空的聊天区，没有破坏性操作。

按钮的位置：挂在气泡**外侧**，离气泡边缘的距离固定（her 在右边、me 在左边），
实测 her 的药丸中心在气泡右边缘 +48px、纵向和气泡中心齐平。离 OCR 框的距离不能用——
气泡宽度是随语音时长变的，OCR 框只框住里面的字。所以上层存的是气泡的完整范围。

只干这一件事：把语音转成文字。不发送、不删除、不碰转账红包。
"""
import ctypes
import ctypes.wintypes as w
import time

from app.fill import foreground

u32 = ctypes.windll.user32

BTN_OFFSET = 48    # 药丸中心离气泡外侧边缘多远（实测值，见模块开头）
BTN_W = 50         # 药丸宽度，用来判断点下去有没有落在按钮上
HOVER_WAIT = 0.45  # 悬停之后等微信把按钮画出来


def _click(x, y):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
    u32.mouse_event(0x2, 0, 0, 0, 0)
    time.sleep(0.03)
    u32.mouse_event(0x4, 0, 0, 0, 0)


def _window_rect(hwnd):
    """跟 WGC 帧对齐的窗口矩形（扩展边界）。跟 app/fill.py 一个口径，别改成 GetWindowRect。"""
    r = w.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def convert(hwnd, rect, kind):
    """把 rect（采集帧坐标，整个语音气泡）那条语音转成文字。

    成功返回 ""；失败返回一句给用户看的中文原因。绝不抛异常——这是用户点出来的动作，
    出错要看得见，不能静默。"""
    r = _window_rect(hwnd)
    x0, y0, x1, y1 = rect
    cy = r.top + (y0 + y1) // 2
    # her 的按钮在气泡右边、me 的在左边
    bx = r.left + (x1 + BTN_OFFSET) if kind != "me" else r.left + (x0 - BTN_OFFSET)
    hover = (r.left + (x0 + x1) // 2, cy)
    old = w.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    try:
        foreground(hwnd)  # 悬停得悬在前台窗口上，不然微信不冒按钮
        u32.SetCursorPos(hover[0], hover[1])   # 先悬停，让微信把按钮画出来
        time.sleep(HOVER_WAIT)
        _click(bx, cy)
        return ""
    except Exception as e:  # 用户点出来的动作，出错要让界面看得见
        return f"转文字失败：{type(e).__name__}: {e}"
    finally:
        u32.SetCursorPos(old.x, old.y)  # 光标还给用户，别留在他没动过的地方
