# -*- coding: utf-8 -*-
"""找聊天窗口 + Windows Graphics Capture 盯着它 + 从帧里定位消息区。帧全程内存，绝不落盘。"""
import ctypes
import os
import time

import numpy as np

u32 = ctypes.windll.user32


def find_wechat_hwnd():
    """枚举可见顶层窗口，按进程名挑主窗口，没有就取第一个。
    同进程还有工具窗和看图窗，面积可能更大，所以不能按面积挑。"""
    k32 = ctypes.windll.kernel32
    found = []

    def exe_of(pid):
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return os.path.basename(buf.value).lower() if ok else ""

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exe_of(pid.value) in ("weixin.exe", "wechat.exe"):
            title = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, title, 256)
            found.append((hwnd, title.value))
        return True

    u32.EnumWindows(cb, 0)
    if not found:
        raise RuntimeError("没找到聊天窗口，开着吗？")
    return next((h for h, t in found if t == "微信"), found[0][0])


def unminimize(hwnd):
    """Windows 不渲染最小化的窗口，什么截图法都拿不到画面。发现被最小化就无激活还原，再压到所有窗口最底下——
    看着跟收起来一样，但 DWM 继续画。不抢焦点、不动大小位置。返回是否动了手。"""
    if not u32.IsIconic(hwnd):
        return False
    u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x13)  # HWND_BOTTOM, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE
    return True


def _thin_lines(full, bg, x0, x1, y0, y1):
    """面板里那几根**细横线**的 y 坐标（头部下面一根、输入框顶一根）。

    判据：整行几乎都不是底色（<25%）+ 非底色像素基本同一种颜色 + 厚度 ≤4px。
    不能要求「整行同色」——微信 4.x 的输入框画的是个圆角框，左右两头留出面板底色，
    混进来整行的 std 就上去了。真机上就是这么漏判的：y_in 一路退到面板底，裁剪把
    输入框连底下那条工具栏一起吃了进去，工具栏最左那个笑脸图标被 OCR 读成「?」、
    还当成对方发来的消息，白触发一次判断（输入框非空时还会多报一条绿色的「发送」）。
    厚度那道是给窗口底边/阴影那种厚块用的：线是细的，厚块不是。"""
    band = full[y0:y1, x0:x1].astype(int)
    ink = np.abs(band - bg).sum(-1) > 6  # 这一行里哪些像素不是底色
    cnt = ink.sum(1)
    mean = (band * ink[..., None]).sum(1) / np.maximum(cnt, 1)[:, None]  # 非底色像素的均值色
    near = ((np.abs(band - mean[:, None, :]).max(-1) <= 12) | ~ink).sum(1) / np.maximum(cnt, 1)
    thin = (cnt > 0) & (1.0 - cnt / max(1, x1 - x0) < 0.25) & (near > 0.9)
    lines, i = [], 0
    while i < len(thin):
        if not thin[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(thin) and thin[j + 1]:
            j += 1
        if j - i + 1 <= 4:
            lines.append(y0 + i)
        i = j + 1
    return lines


def chat_area(full, header_h=60):
    """消息列表区 (x0, y_top, x1, y_in, 面板底色, y_pane)，全靠像素锚点，不写死坐标，深浅主题通用：
    - 面板底色 = 右半边最常见的颜色（抽样算，全量 np.unique 在 2560 宽的图上要半秒）
    - 面板左/右边界 = 第一/最后一根「底色占比 > 30%」的列（联系人列表是另一种底色，占比 0）
    - y_pane = 面板第一行；会话名就印在 y_pane~y_top 这条头部里（公告条也在里面）
    - 横向分隔线 = 细横线（见 _thin_lines）；输入框顶 y_in = 面板 45% 高度以下第一根；
      公告条下面那根（有的话）= 消息区顶 y_top，没有就用 header_h
    认不出（窗口太小 / 拖到一半布局没铺好）返回 None。
    ponytail: 输入框拉高超过面板一半会认错；header_h 按 100% DPI 给的，缩放了按比例调。"""
    H, W = full.shape[:2]
    right = full[::8, W // 2::8].reshape(-1, 3)
    vals, cnt = np.unique(right, axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    isbg = np.abs(full.astype(int) - bg).sum(-1) <= 6
    col = isbg[H // 4: H * 3 // 4].mean(0)
    x0 = int(np.argmax(col > 0.3))
    x1 = W - int(np.argmax(col[::-1] > 0.3))
    row = isbg[:, x0:x1].mean(1)
    y0 = int(np.argmax(row > 0.9))
    y1 = H - int(np.argmax(row[::-1] > 0.9))
    seps = _thin_lines(full, bg, x0, x1, y0, y1)
    below = [s for s in seps if s > y0 + 0.45 * (y1 - y0)]
    y_in = below[0] if below else y1
    above = [s for s in seps if y0 + header_h < s < y_in - 50]
    y_top = above[-1] if above else y0 + header_h
    if x1 - x0 < 100 or y_in - y_top < 40:
        return None
    return x0, y_top, x1, y_in, bg, y0


class Capture:
    """WGC 盯窗口。采集线程只做「跟上一帧比」；settled() 在画面停稳后交出整帧，中间帧（滚动动画、
    新消息滑入的半截气泡）全跳过。动图表情永远停不稳，所以最多等 max_wait 秒照样交。"""

    def __init__(self, hwnd, settle=0.25, max_wait=1.0):
        from windows_capture import WindowsCapture

        self.settle, self.max_wait = settle, max_wait
        self.shape = self.area = self.last = self.pending = None
        self.last_full = None  # 最近一帧整帧（画面变了才更新），给「重新识别」用，见 snapshot()
        self.t = self.t0 = 0.0
        # 包装层默认 cursor_capture=True，会去调 SetIsCursorCaptureEnabled。
        # 这个属性要 Win10 2004（build 19041）才有，1909 及更早直接抛 CursorConfigUnsupported。
        # 显式 None 走系统默认，不去切换；draw_border 同理。
        cap = WindowsCapture(cursor_capture=None, draw_border=None, window_hwnd=hwnd)
        cap.event(self.on_frame_arrived)
        cap.event(self.on_closed)
        self.ctl = cap.start_free_threaded()

    def on_frame_arrived(self, frame, control):
        full = np.ascontiguousarray(frame.frame_buffer[:, :, :3][:, :, ::-1])  # BGRA → RGB；缓冲区回调后就没了，必须拷
        if full.max() == 0:
            return
        if self.area is None or full.shape != self.shape:
            self.shape, self.area = full.shape, chat_area(full)
        if self.area is None:
            return
        x0, y0, x1, y1 = self.area[:4]  # 拿上一次的消息区做 diff 就够了，光标闪烁在输入框里，不算变化
        # ponytail: diff 不含头部——公告条会滚动，带上它就永远停不稳。切会话时消息区必然也变，照样出帧。
        chat = full[y0:y1, x0:x1]
        if self.last is not None and np.array_equal(chat, self.last):
            return
        self.last = chat
        self.last_full = full  # 画面变了才更新，所以它跟 self.last 是同一张图
        if self.pending is None:
            self.t0 = time.perf_counter()
        self.pending, self.t = full, time.perf_counter()

    def on_closed(self):
        pass

    def settled(self):
        """停稳了就返回整帧，否则 None。"""
        if self.pending is None:
            return None
        now = time.perf_counter()
        if now - self.t < self.settle and now - self.t0 < self.max_wait:
            return None
        full, self.pending = self.pending, None
        return full

    def snapshot(self):
        """不管画面变没变，把手上最近那一帧交出来（「重新识别」用）。

        settled() 只在画面**刚变过、又停稳了**的时候给帧，而用户点重新识别的时候画面多半早就
        静止了——没有这一手，那个按钮点了等于没点。留一份最近整帧的引用不额外占内存：
        pending 本来就压着一帧，两个指向的是同一张图。真的一帧都还没来过就返回 None。"""
        return self.pending if self.pending is not None else self.last_full

    def alive(self):
        return not self.ctl.is_finished()

    def stop(self):
        self.ctl.stop()

    def wait(self):
        self.ctl.wait()  # 采集线程若是报错死的，这里把错误抛出来


if __name__ == "__main__":
    # 合成一帧试 chat_area：面板底色 250，头部下面一根分隔线，一条消息气泡，
    # 输入框顶画成**圆角框的横边**（两头各留 20px 底色，跟微信 4.x 一样）。
    # 老判据「整行同色」在这儿认不出它，y_in 会退到面板底（400），裁剪把底下那条
    # 工具栏一起吃进去——真机上就是这么冒出一条「?」的假消息的。
    frame = np.full((400, 300, 3), 250, np.uint8)
    frame[70, :] = 200            # 头部下面那根分隔线
    frame[120:150, 30:180] = 232  # 一条消息气泡（横不满一行，不算分隔线）
    frame[300, 20:280] = 200      # 输入框顶
    frame[330:350, 30:60] = 90    # 工具栏图标（真机上会被 OCR 读成「?」）
    area = chat_area(frame)
    assert area is not None, "这帧不该认不出来"
    assert area[3] == 300, f"输入框顶该是 300，认成了 {area[3]}"
    assert area[1] == 70, f"消息区顶该是 70，认成了 {area[1]}"
    print("capture.chat_area ok")
