# -*- coding: utf-8 -*-
"""飞书 Windows 桌面端能不能用 UIA 读到聊天文字？（阶段 0 探针）

背景见 docs/飞书接入.md。微信那条路 UIA 是死的（界面自绘在 GPU 画布上、控件树为空），
所以本仓库只能截图 + OCR。飞书桌面端是 Electron（窗口类 `Chrome_WidgetWin_1`），
有希望直接读控件树——那样 `app/capture.py` + `app/ocr.py` 那 700 多行整个不用带过去。

**Chromium 的坑：无障碍树是懒建的，不 poke 就是空的。** 直接 UIA 遍历只会看到一个
光杆 Window 节点（实测 1 个）。得先用 MSAA 的 `AccessibleObjectFromWindow` 捅一下，
Chromium 才会把树建出来（实测捅完 ControlView 从 1 个涨到 56 个、RawView 131 个）。
所以这个探针里那次 poke 不是可选项。

只读，绝不写：不点击、不输入、不 Invoke、不发送、不碰任何按钮。不出图、不落盘。

用法（Windows，Python 3.9+）:
    pip install uiautomation
    python probe/probe_feishu.py            # 自动找飞书窗口
    python probe/probe_feishu.py --list     # 只列顶层窗口，不 dump
    python probe/probe_feishu.py --pick 3   # 手动指定第几个顶层窗口
    python probe/probe_feishu.py --raw      # 连正文一起打印
    python probe/probe_feishu.py --unminimize   # 飞书最小化着的话先无激活还原

最小化是个硬门槛：Windows 不渲染最小化窗口，Chromium 也就不建无障碍树——实测最小化时
131 个节点全是界面壳子（60 个按钮、19 个页签），坐标一片 -32000，一个正文都没有。
`--unminimize` 走的是跟 `app/capture.py:unminimize()` 同一套：SW_SHOWNOACTIVATE 还原、
再压到最底层，不抢焦点。

隐私：**默认把每个节点的文字遮成「长度 + 字符类型」，只报结构**，正文只有 `--raw` 才出来。
要把结论贴给别人看的时候别加 `--raw`。

结论怎么看:
    找到 messenger-chat 容器 + 成片的中文 Text 节点  -> UIA 可行，走阶段 1
    节点数很少 / 只有界面按钮、没有正文              -> UIA 不行，退截图 + OCR（或官方 API）
"""
import argparse
import ctypes
import ctypes.wintypes as wintypes
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import uiautomation as auto
except ImportError:
    sys.exit("缺依赖：pip install uiautomation")

FEISHU_EXES = {"feishu.exe", "lark.exe"}

# IAccessible 的 IID。poke 只要返回码，拿到对象也不解引用。
_IID_IACCESSIBLE = "{618736E0-3C3D-11CF-810C-00AA00389B71}"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def poke_msaa(hwnd):
    """用 MSAA 捅一下窗口，逼 Chromium 把无障碍树建出来。返回 HRESULT。"""
    guid = _GUID()
    if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(_IID_IACCESSIBLE), ctypes.byref(guid)) != 0:
        return -1
    oleacc = ctypes.oledll.oleacc
    oleacc.AccessibleObjectFromWindow.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
    ]
    oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long
    p = ctypes.c_void_p()
    # OBJID_CLIENT = 0xFFFFFFFC
    return oleacc.AccessibleObjectFromWindow(wintypes.HWND(hwnd), 0xFFFFFFFC,
                                             ctypes.byref(guid), ctypes.byref(p))


def unminimize(hwnd):
    """无激活还原，再压到所有窗口最底下。跟 app/capture.py:unminimize() 同一套。
    最小化的窗口 Chromium 不建无障碍树，不还原这个探针什么都读不到。"""
    u32 = ctypes.windll.user32
    if not u32.IsIconic(wintypes.HWND(hwnd)):
        return False
    u32.ShowWindow(wintypes.HWND(hwnd), 4)  # SW_SHOWNOACTIVATE
    # HWND_BOTTOM=1, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE=0x13
    u32.SetWindowPos(wintypes.HWND(hwnd), 1, 0, 0, 0, 0, 0x13)
    return True


def exe_of_pid(pid):
    """从 pid 拿进程 exe 名，纯 ctypes，不装 psutil。拿不到就返回空串。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        k32.CloseHandle(h)


def top_windows():
    """所有顶层窗口 (control, exe)。"""
    root = auto.GetRootControl()
    out = []
    for win in root.GetChildren():
        try:
            out.append((win, exe_of_pid(win.ProcessId)))
        except Exception:
            pass
    return out


def char_kind(s):
    """文字构成统计——不把正文本身带出来。"""
    cjk = dig = lat = other = 0
    for ch in s:
        c = ord(ch)
        if 0x4E00 <= c <= 0x9FFF:
            cjk += 1
        elif 0x30 <= c <= 0x39:
            dig += 1
        elif 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A:
            lat += 1
        elif not ch.isspace():
            other += 1
    return cjk, dig, lat, other


def mask(s):
    """默认的显示形式：只给长度和字符类型，不给原文。"""
    if not s:
        return "-"
    cjk, dig, lat, other = char_kind(s)
    return f"len={len(s)} cjk={cjk} dig={dig} lat={lat} oth={other}"


class Node:
    __slots__ = ("depth", "type", "name", "aid", "cls", "rect")

    def __init__(self, depth, type_, name, aid, cls, rect):
        self.depth, self.type, self.name = depth, type_, name
        self.aid, self.cls, self.rect = aid, cls, rect


def walk(control, out, depth=0, max_depth=60, cap=20000, deadline=None):
    """递归收集节点。cap 和 deadline 都是防跑飞——UIA 遍历 Chromium 可能很慢。"""
    if len(out) >= cap or (deadline and time.monotonic() > deadline):
        return
    try:
        r = control.BoundingRectangle
        rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        rect = (0, 0, 0, 0)
    try:
        out.append(Node(depth, control.ControlTypeName, control.Name or "",
                        control.AutomationId or "", control.ClassName or "", rect))
    except Exception:
        return
    if depth >= max_depth:
        return
    try:
        children = control.GetChildren()
    except Exception:
        return
    for c in children:
        walk(c, out, depth + 1, max_depth, cap, deadline)


def analyze_messages(nodes, raw=False):
    """把消息列表单独抠一遍——这才是 UIA 这条路真正的产出，也是阶段 1 采集器的雏形。

    2026-09-29 实测形状（飞书 Windows，Chromium 暴露的是 DOM class）：

        js-message-item message-item message-not-self message-item-first message-me-read text-message
          Aid = 7690500277727661005        <- 消息 ID，19 位数字，去重可以直接用它
          ├─ message-left   (宽 69)        <- 头像栏（**不是**收发方向！）
          ├─ message-right  (宽 1318)      <- 内容栏
          │   ├─ message-info-name         <- 群聊里的发言人名
          │   ├─ NewMessageContextMenuTrigger  Aid=同一个消息 ID
          │   │   └─ message-content → limit-height-container
          │   │        └─ TextControl      <- 正文挂在它的 Name 上
          │   └─ message-section-right     <- 时间 / 已读状态
          └─ tips / reply-meta-tips
    """
    items = [n for n in nodes if "js-message-item" in n.cls]
    print(f"\n=== 消息列表：{len(items)} 条 js-message-item ===")
    if not items:
        print("  （没有。窗口不在聊天页，或者 a11y 树没建起来）")
        return

    # 方向 / 类型标记都写在 class 串上，统计一遍就知道这个会话里有什么
    marks = {}
    for n in items:
        for tok in n.cls.split():
            if tok.startswith("message-") or tok in (
                    "text-message", "post-message", "im-image-message", "system-text-background"):
                marks[tok] = marks.get(tok, 0) + 1
    print("  标记统计（方向 + 类型）:")
    for k, v in sorted(marks.items(), key=lambda kv: -kv[1]):
        print(f"    x{v:<3} {k}")
    if "message-not-self" in marks and "message-self" not in marks:
        print("  ⚠ 只有 message-not-self，没看到 message-self——这个会话里你可能没发过言。")
        print("    收发方向的 self 侧标记还没验，找个自己说过话的会话再跑一遍。")

    # 正文挂在哪：取前三条，看子树里带文字/带 Aid 的节点
    for i, item in enumerate(items[:3]):
        sub = [n for n in nodes if n is not item]
        print(f"\n  --- 消息 #{i}  class={item.cls[:110]}  Aid={item.aid}")
        # nodes 是前序遍历的，item 之后的深度更大且没回到 <= item.depth 的都算它的子树
        try:
            k = nodes.index(item)
        except ValueError:
            continue
        for n in nodes[k + 1:]:
            if n.depth <= item.depth:
                break
            if n.depth > item.depth + 9:
                continue
            if n.name or n.aid:
                cjk = char_kind(n.name)[0]
                txt = n.name if raw else mask(n.name)
                print(f"      D+{n.depth - item.depth:<3} {n.type:<16} cjk={cjk:<3} "
                      f"Aid={n.aid[:22]:<22} Cls={n.cls[:44]:<44} {txt}")

    # 虚拟化：列表容器比可视区高得多的话，滚出去的消息**仍然留在树里**（负坐标）
    print("\n  列表容器（高过可视区 => 树里有屏幕外的消息，读的时候必须按视口过滤）:")
    for n in nodes:
        if "list_items" in n.cls or "messageList-footer" in n.cls:
            x, y, w, h = n.rect
            print(f"    D={n.depth:<3} Cls={n.cls[:40]:<40} y={y:<8} {w}x{h}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", type=int, help="手动指定第几个顶层窗口（从 --list 看序号）")
    ap.add_argument("--list", action="store_true", help="只列窗口不 dump")
    ap.add_argument("--raw", action="store_true", help="连正文一起打印（默认遮掉）")
    ap.add_argument("--unminimize", action="store_true",
                    help="窗口最小化着的话先无激活还原（不还原就读不到树，见文件头）")
    ap.add_argument("--messages", action="store_true",
                    help="只分析消息列表（条数 / 收发标记 / 正文挂在哪 / 虚拟化），不铺全量节点")
    ap.add_argument("--depth", type=int, default=60, help="最大递归深度，默认 60")
    ap.add_argument("--seconds", type=float, default=45, help="遍历时间上限，默认 45 秒")
    args = ap.parse_args()

    wins = top_windows()
    print("=== 顶层窗口 ===")
    for i, (win, exe) in enumerate(wins):
        try:
            print(f"[{i:2}] exe={exe or '?':16} class={win.ClassName:28} name={win.Name!r}")
        except Exception as e:
            print(f"[{i:2}] <读不到: {e}>")
    if args.list:
        return

    if args.pick is not None:
        if not 0 <= args.pick < len(wins):
            sys.exit(f"--pick 越界，只有 {len(wins)} 个顶层窗口")
        target = wins[args.pick][0]
    else:
        cands = [win for win, exe in wins if exe.lower() in FEISHU_EXES]
        if not cands:
            sys.exit("没自动找到飞书窗口。先把飞书开着、并停在一个聊天窗口，或用 --pick 手动指定。")
        # 挑有标题的那个（飞书会起一堆同名进程，只有一个有主窗口）
        target = next((c for c in cands if (c.Name or "").strip()), cands[0])

    print(f"\n=== 目标窗口: class={target.ClassName} name={target.Name!r} "
          f"pid={target.ProcessId} ===")

    hwnd = target.NativeWindowHandle
    if args.unminimize:
        acted = unminimize(hwnd)
        print(f"unminimize: {'已还原（无激活）' if acted else '窗口本来就没最小化'}")
        if acted:
            time.sleep(1.5)  # 等 DWM 和 Chromium 都醒过来

    # --- 关键一步：poke 之前树是空的 ---
    hr = poke_msaa(hwnd)
    print(f"MSAA poke hwnd={hwnd} hr=0x{hr & 0xFFFFFFFF:08X}")
    if hr != 0:
        print("  （poke 失败，下面的结果大概率是空的；换个窗口或用 --pick 试试）")
    time.sleep(2.0)  # 给 Chromium 建树的时间

    # --- 遍历 ---
    t0 = time.time()
    nodes = []
    walk(target, nodes, max_depth=args.depth, deadline=time.monotonic() + args.seconds)
    ms = int((time.time() - t0) * 1000)
    print(f"遍历到 {len(nodes)} 个节点，耗时 {ms}ms")

    if args.messages:
        analyze_messages(nodes, raw=args.raw)
        return

    # --- 用户要看的那几个问题的答案 ---
    named = [n for n in nodes if n.name or n.aid]
    cjk_text = [n for n in nodes if char_kind(n.name)[0] > 0]
    chatish = [n for n in named if any(k in n.aid.lower() for k in
                                       ("chat", "messag", "conversation", "msg"))]
    types = {}
    for n in nodes:
        types[n.type] = types.get(n.type, 0) + 1

    print("\n=== 控件类型分布 ===")
    for k, v in sorted(types.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<24} {v}")

    print("\n=== AutomationId 里带 chat / message / conversation 的节点 ===")
    if chatish:
        for n in chatish:
            x, y, w, h = n.rect
            print(f"  D={n.depth:<2} {n.type:<20} Aid={n.aid:<28} @({x},{y} {w}x{h})")
    else:
        print("  （没有）")

    print(f"\n=== 含中文的节点：{len(cjk_text)} 个 ===")
    show = cjk_text if args.raw else cjk_text[:40]
    for n in show:
        x, y, w, h = n.rect
        text = n.name if args.raw else mask(n.name)
        print(f"  D={n.depth:<2} {n.type:<20} y={y:<6} x={x:<6} {w}x{h:<5} Aid={n.aid:<24} {text}")
    if not args.raw and len(cjk_text) > len(show):
        print(f"  …（还有 {len(cjk_text) - len(show)} 个，加 --raw 看全）")

    print("\n=== 全部节点（按屏幕顺序）===")
    for n in sorted(nodes, key=lambda n: (n.rect[1], n.rect[0])):
        x, y, w, h = n.rect
        text = n.name if args.raw else mask(n.name)
        print(f"  D={n.depth:<2} {n.type:<20} y={y:<6} x={x:<6} {w}x{h:<5} "
              f"Aid={n.aid:<24} Cls={n.cls:<22} {text}")

    # --- 一句话判定 ---
    print("\n=== 判定 ===")
    if chatish and len(cjk_text) >= 5:
        print(f"  有聊天容器 + {len(cjk_text)} 个中文节点 -> UIA 这条路有戏。")
        print("  下一步：确认正文是不是在这些节点里、以及怎么分 me/her（看 x 或 Aid 规律）。")
    elif len(nodes) <= 3:
        print("  树是空的（只有顶层窗口）-> poke 没生效，或者窗口不在聊天页。")
    else:
        print(f"  节点 {len(nodes)} 个、中文 {len(cjk_text)} 个、聊天容器 "
              f"{'有' if chatish else '没有'} -> 更像只有界面控件。")
        print("  先把飞书停在一个有消息的聊天窗口再跑一遍；还是这样就得退截图 + OCR。")


if __name__ == "__main__":
    main()
