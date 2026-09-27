# -*- coding: utf-8 -*-
"""语音消息转文字：右键那条语音 → 点菜单第一项「语音转文字」。

**为什么是右键菜单，不是悬停药丸。** 微信在鼠标悬停语音气泡时会在气泡外侧冒一个内联的
「转文字」小药丸，看着比菜单安全得多，本文件以前走的就是那条路。但真机上量下来：**合成鼠标
根本叫不出那个药丸**——SetCursorPos 直跳 / 先停在旁边再用 mouse_event 相对移入 / 分 30 步
一点点挪过去 / 停在气泡正中 4 秒，四种都试过，屏幕上连影子都没有（同时拿微信右上角那排
窗口按钮做对照：合成悬停一划过去按钮就亮、移开就暗，所以不是「合成输入进不到微信」）。
真人用鼠标悬停时药丸会出现，但程序没法复现，那条路只能放弃。

右键菜单则是**合成右键一按就出来**，而且第一项就是「语音转文字」（实测菜单从上到下：
语音转文字 / 收藏 / 多选 / 提醒 / @引用 / ……）。安全边界靠这两条：

1. **只点第一项的中心**，偏移量是量出来的（右键点往右下 +60,+21，见 FIRST_ITEM）；
2. **菜单是往右下展开的**。万一它翻到光标上方去了（屏幕底下没地方摆），那 +21 那一下会落在
   **菜单外面**——点了个空，不会回头够到下面的「删除」。所以「点歪」最坏的结果是没反应，
   不会删掉聊天记录。这条是选它、而不是选「按固定偏移点第 6 项」的全部理由。

只干这一件事：把语音转成文字。不发送、不删除、不碰转账红包。
"""
import ctypes
import ctypes.wintypes as w
import time

from app.fill import foreground

u32 = ctypes.windll.user32

# 右键点 → 菜单第一项「语音转文字」**文字框中心**的偏移（真机实测，屏幕坐标）。
# 菜单宽约 130、每项高约 33，第一项文字框 (959,800)-(1060,823)，右键点在 (949,790)。
FIRST_ITEM = (60, 21)
REF_BUBBLE_H = 35  # 量上面那组数时语音气泡的高度。气泡高度跟字号/DPI 一起变，偏移量跟着等比缩放
MENU_WAIT = 0.55   # 右键之后等微信把菜单画出来


def _click(x, y, down=0x2, up=0x4):
    """在 (x, y) 按一下。默认左键（MOUSEEVENTF_LEFTDOWN/LEFTUP），右键传 0x8 / 0x10。"""
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
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
    """把 rect（采集帧坐标，整个语音气泡）那条语音转成文字。

    成功返回 ""；失败返回一句给用户看的中文原因。绝不抛异常——这是用户点出来的动作，
    出错要看得见，不能静默。

    注意：菜单看不见（WGC 按窗口抓图，抓不到微信另开的浮层菜单），所以这两下是**盲点**，
    点完没法当场确认。回的是「点过了」，不是「转成了」——转出来的字会插在气泡正下方，
    由采集链路照常读到（见 app/ocr.py:_under_voice 和 main.convert_voice）。"""
    r = _window_rect(hwnd)
    x0, y0, x1, y1 = rect
    cx = r.left + (x0 + x1) // 2
    cy = r.top + (y0 + y1) // 2
    k = max(0.6, min(2.0, (y1 - y0) / REF_BUBBLE_H))  # 换个 DPI，菜单跟着字号变，偏移量等比缩放
    dx, dy = round(FIRST_ITEM[0] * k), round(FIRST_ITEM[1] * k)
    old = w.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    try:
        foreground(hwnd)  # 得让微信在前台，右键才弹得出菜单
        _click(cx, cy, 0x8, 0x10)  # 右键：弹出菜单
        time.sleep(MENU_WAIT)
        _click(cx + dx, cy + dy)   # 左键：菜单第一项「语音转文字」
        return ""
    except Exception as e:  # 用户点出来的动作，出错要让界面看得见
        return f"转文字失败：{type(e).__name__}: {e}"
    finally:
        u32.SetCursorPos(old.x, old.y)  # 光标还给用户，别留在他没动过的地方
