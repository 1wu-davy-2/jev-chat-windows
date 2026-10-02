# -*- coding: utf-8 -*-
"""CPU 探针：把真正的 `worker.run` 跑起来，量它一个子进程烧多少 CPU、花在哪儿。

    set PYTHONPATH=. && python probe/probe_cpu.py [秒数，默认 45]

为什么要它：任务管理器上「空闲时二十几、来消息冲到五十」这种账，光看代码猜不出来——
WGC 在画面**一个像素没变**时也照发 ~72 帧/秒，整帧 BGRA→RGB 一次 9.4ms；一次 OCR
1.3~6.6 秒 CPU。到底是哪一头，得把「回调次数 / 停稳次数 / OCR 次数和 CPU」分开数。
结论和改动理由写在 CLAUDE.md「技术坑 → CPU 占用」那一条里，别在这儿重复。

量的是 `time.process_time()`——它把进程里所有线程都算上，正好是任务管理器那个数。
**只读**：不点微信、不发消息、不写任何文件（OCR 引擎和 WGC 会话都在内存里）。

注意：它跟跑着的应用**同时抓同一个窗口**是没问题的（WGC 允许多个会话），
所以想量线上那份的真实数字，别用这个，直接 `Get-Process` 采样（见 CLAUDE.md 里那行）。
"""
import ctypes
import multiprocessing
import os
import sys
import threading
import time

ctypes.windll.user32.SetProcessDPIAware()
from app import capture, ocr, worker  # noqa: E402

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
n = {"cb": 0, "settled": 0, "read": 0, "read_cpu": 0.0, "read_wall": 0.0, "lines": 0,
     "kinds": {}}

_orig_cb = capture.Capture.on_frame_arrived
_orig_settled = capture.Capture.settled
_orig_read = ocr.Reader.read
_orig_new_lines = ocr.Reader.new_lines


def cb(self, frame, control):
    n["cb"] += 1
    return _orig_cb(self, frame, control)


# WindowsCapture.event() 按**函数名**分发，包装函数必须还叫这个名，不然它不认
cb.__name__ = "on_frame_arrived"


def settled(self):
    r = _orig_settled(self)
    if r is not None:
        n["settled"] += 1
    return r


def read(self, *a, **kw):
    n["read"] += 1
    c0, t0 = time.process_time(), time.perf_counter()
    r = _orig_read(self, *a, **kw)
    n["read_cpu"] += time.process_time() - c0
    n["read_wall"] += time.perf_counter() - t0
    return r


def new_lines(self, *a, **kw):
    r = _orig_new_lines(self, *a, **kw)
    n["lines"] += len(r)
    return r


capture.Capture.on_frame_arrived = cb
capture.Capture.settled = settled
ocr.Reader.read = read
ocr.Reader.new_lines = new_lines

q = multiprocessing.Queue()
enabled = multiprocessing.Event()
debug_on = multiprocessing.Event()
enabled.set()


def drain():
    while True:
        try:
            m = q.get(timeout=0.5)
        except Exception:
            continue
        n["kinds"][m[0]] = n["kinds"].get(m[0], 0) + 1


threading.Thread(target=drain, daemon=True).start()
hwnd = capture.find_wechat_hwnd()
print("hwnd", hwnd, flush=True)
c0, w0 = time.process_time(), time.perf_counter()
threading.Thread(target=worker.run, args=(q, hwnd, enabled, debug_on), daemon=True).start()
time.sleep(DUR)
cpu, wall = time.process_time() - c0, time.perf_counter() - w0

print(f"cpu {cpu:.1f}s / {wall:.1f}s = {cpu/wall*100:.0f}% of one core")
print(f"回调 {n['cb']} 次 ({n['cb']/wall:.1f}/s)   停稳交出 {n['settled']} 次   "
      f"OCR {n['read']} 次 ({n['read_cpu']:.1f}s cpu / {n['read_wall']:.1f}s wall)")
print(f"认出的行 {n['lines']}   队列 {n['kinds']}")
sys.stdout.flush()
os._exit(0)  # worker 那个线程是死循环，正常退不出去
