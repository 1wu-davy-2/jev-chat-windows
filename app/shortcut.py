# -*- coding: utf-8 -*-
"""桌面快捷方式：打包版第一次跑的时候在桌面放一个，省得每次都翻进文件夹双击 exe。

**为什么是 COM 而不是拉 PowerShell**：.lnk 没有公开的简单 Win32 API，写它的二进制格式
比调 COM 还麻烦；剩下的路就是 `New-Object -ComObject WScript.Shell`。但一个没签名的 exe
去拉脚本是杀软和 EDR 的经典拦截形状（这台机器是 Enterprise LTSC），而且每次都要等
PowerShell 冷启动。IShellLinkW 是进程内的，一次调用几毫秒。

**桌面路径不能拼 `%USERPROFILE%\\Desktop`**：Win10 起很多人被 OneDrive 重定向过，那个目录
是空的。`SHGetKnownFolderPath` 问的是系统，重定向了也给对。

**只在打包版建**：源码跑的时候 `sys.executable` 是 python.exe，指过去等于在桌面放一个
打不开的图标。`can_create()` 返回原因，调用方拿它说人话。

整个模块都是 Windows 专有，`core/` 那边一概不认识它（也不该认识）。
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

_ole32 = ctypes.oledll.ole32
_shell32 = ctypes.windll.shell32

LINK_NAME = "jev-chat.lnk"

# 那几个 GUID 写成字符串再转，省得为几个常量去引 uuid
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"

# vtable 下标，**按各自接口从 0 数**（IUnknown 那三个在谁身上都是 0/1/2）。写错一个就是
# 访问野指针，别凭记忆改：
#   IShellLinkW: 3 GetPath ... 9 SetWorkingDirectory ... 17 SetIconLocation ... 20 SetPath
#   IPersistFile: 3 GetClassID(从 IPersist 继承), 4 IsDirty, **5 Load**, **6 Save**,
#                 7 SaveCompleted, 8 GetCurFile
# IPersistFile 那几个是接着它自己的 IUnknown 数的，**不是**接在 IShellLinkW 后面接着数
_VT_QUERY_INTERFACE = 0
_VT_RELEASE = 2
_VT_GET_PATH = 3
_VT_LOAD = 5
_VT_SAVE = 6
_VT_SET_WORKING_DIR = 9
_VT_SET_ICON_LOCATION = 17
_VT_SET_PATH = 20

_CLSCTX_INPROC_SERVER = 1
_COINIT_APARTMENTTHREADED = 0x2


class _GUID(ctypes.Structure):
    """只认 `{8-4-4-4-12}` 这一种写法（上面那几个常量都是这么写的）。"""
    _fields_ = (("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8))

    def __init__(self, text: str):
        t = str(text).strip("{}").replace("-", "")
        if len(t) != 32:
            raise ValueError(text)
        self.Data1 = int(t[:8], 16)
        self.Data2 = int(t[8:12], 16)
        self.Data3 = int(t[12:16], 16)
        for i in range(8):
            self.Data4[i] = int(t[16 + i * 2:18 + i * 2], 16)


def _method(ptr, index: int, restype, *argtypes):
    """取 COM 接口指针上第 index 个虚函数。**每个都显式声明 restype/argtypes**：64 位下
    ctypes 默认按 c_int 走，接口指针会被截断成垃圾值（`fill.py` 头上踩过同一个坑）。"""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(ptr) -> None:
    if ptr:
        try:
            _method(ptr, _VT_RELEASE, ctypes.c_ulong)(ptr)
        except Exception:
            pass


_shell32.SHGetKnownFolderPath.argtypes = (ctypes.POINTER(_GUID), wintypes.DWORD,
                                          wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p))
_shell32.SHGetKnownFolderPath.restype = ctypes.c_long
_ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
_ole32.CoTaskMemFree.restype = None
_ole32.CoCreateInstance.argtypes = (ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
_ole32.CoCreateInstance.restype = ctypes.c_long


def _hr(hr: int) -> str:
    return "0x%08X" % (hr & 0xFFFFFFFF)


def _ensure_com() -> None:
    """保证**当前线程**初始化过 COM。

    COM 是按线程算的：在 A 线程初始化、B 线程调 CoCreateInstance，拿到的是
    「尚未调用 CoInitialize」（0x800401F0），而这里所有失败都只是返回一句原因，
    界面上看不见——所以每个入口都得自己保证，别指望调用方记得。

    正常路径下 Qt 建 QApplication 时已经在主线程 OleInitialize 过（也是 STA），
    这里拿到 S_FALSE（=1，不算失败，oledll 不会抛）。RPC_E_CHANGED_MODE 说明线程
    已经以别的套间模型初始化过了，同样能用（IShellLink 是进程内的），一并吞掉。
    进程活多久线程就活多久，所以不配对 CoUninitialize。"""
    try:
        _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    except OSError:
        pass


def desktop_dir() -> str:
    """桌面目录。拿不到返回空串（调用方当失败处理）。"""
    if os.name != "nt":
        return ""
    buf = ctypes.c_wchar_p()
    try:
        if _shell32.SHGetKnownFolderPath(ctypes.byref(_GUID(_FOLDERID_DESKTOP)), 0, None,
                                         ctypes.byref(buf)) != 0:
            return ""
        return buf.value or ""
    except Exception:
        return ""
    finally:
        if buf:
            _ole32.CoTaskMemFree(buf)


def link_path() -> str:
    """那个 .lnk 该放哪儿。桌面都找不到就返回空串。"""
    d = desktop_dir()
    return os.path.join(d, LINK_NAME) if d else ""


def can_create() -> str:
    """能不能建：返回 "" 或者一句给用户看的原因。"""
    if os.name != "nt":
        return "只有 Windows 有桌面快捷方式"
    if not getattr(sys, "frozen", False):
        return "源码运行时没有 exe 可以指，打包版才有"
    return ""


def create() -> str:
    """在桌面建一个指向当前 exe 的快捷方式（有了就覆盖）。

    **返回 "" 表示成功，否则是给用户看的原因**——跟 `app/voice.convert()` 一个约定。
    **永不抛**：这是锦上添花的事，最多是没建成，不能把启动带崩。"""
    why = can_create()
    if why:
        return why
    link = link_path()
    if not link:
        return "找不到桌面目录"
    return write(os.path.abspath(sys.executable), link)


def write(target: str, link: str) -> str:
    """真正干活的那个：把 target 写成一个 .lnk。返回 "" 表示成功。

    **跟 can_create() 分开**是为了自测能真跑一遍 COM——源码跑的时候 create() 是被拒绝的，
    那条路就永远测不到，等打包版第一次跑才发现 vtable 写错就晚了。"""
    _ensure_com()
    shell_link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        hr = _ole32.CoCreateInstance(ctypes.byref(_GUID(_CLSID_SHELLLINK)), None,
                                     _CLSCTX_INPROC_SERVER,
                                     ctypes.byref(_GUID(_IID_ISHELLLINKW)),
                                     ctypes.byref(shell_link))
        if hr != 0 or not shell_link:
            return "创建失败（" + _hr(hr) + "）"
        _method(shell_link, _VT_SET_PATH, ctypes.c_long, ctypes.c_wchar_p)(shell_link, target)
        _method(shell_link, _VT_SET_WORKING_DIR, ctypes.c_long, ctypes.c_wchar_p)(
            shell_link, os.path.dirname(target))
        # 图标直接用 exe 自己的（第 0 个），不然桌面上是个白板
        _method(shell_link, _VT_SET_ICON_LOCATION, ctypes.c_long, ctypes.c_wchar_p,
                ctypes.c_int)(shell_link, target, 0)
        hr = _method(shell_link, _VT_QUERY_INTERFACE, ctypes.c_long, ctypes.POINTER(_GUID),
                     ctypes.POINTER(ctypes.c_void_p))(
            shell_link, ctypes.byref(_GUID(_IID_IPERSISTFILE)), ctypes.byref(persist))
        if hr != 0 or not persist:
            return "创建失败（" + _hr(hr) + "）"
        # Save(pszFileName, fRemember=1)：记住这个路径，别让它回头去找 exe 的原始位置
        hr = _method(persist, _VT_SAVE, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int)(
            persist, link, 1)
        if hr != 0:
            return "创建失败（" + _hr(hr) + "）"
        return ""
    except Exception as e:  # 桌面只读、COM 被组策略挡了……都只是没建成
        return f"创建失败：{type(e).__name__}: {e}"
    finally:
        _release(persist)
        _release(shell_link)


def _read_target(link: str) -> str:
    """把 .lnk 读回来，看里面指的到底是谁。**只给自测用**——「文件建出来了」不等于
    「它指向 exe」，SetPath 那一步写错了照样能生成一个空壳图标。"""
    _ensure_com()
    shell_link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        hr = _ole32.CoCreateInstance(ctypes.byref(_GUID(_CLSID_SHELLLINK)), None,
                                     _CLSCTX_INPROC_SERVER,
                                     ctypes.byref(_GUID(_IID_ISHELLLINKW)),
                                     ctypes.byref(shell_link))
        if hr != 0 or not shell_link:
            raise OSError("CoCreateInstance " + _hr(hr))
        hr = _method(shell_link, _VT_QUERY_INTERFACE, ctypes.c_long, ctypes.POINTER(_GUID),
                     ctypes.POINTER(ctypes.c_void_p))(
            shell_link, ctypes.byref(_GUID(_IID_IPERSISTFILE)), ctypes.byref(persist))
        if hr != 0 or not persist:
            raise OSError("QueryInterface " + _hr(hr))
        hr = _method(persist, _VT_LOAD, ctypes.c_long, ctypes.c_wchar_p,
                     wintypes.DWORD)(persist, link, 0)  # STGM_READ
        if hr != 0:
            raise OSError("Load " + _hr(hr))
        buf = ctypes.create_unicode_buffer(1024)
        hr = _method(shell_link, _VT_GET_PATH, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int,
                     ctypes.c_void_p, wintypes.DWORD)(shell_link, buf, 1024, None, 0)
        if hr != 0:
            raise OSError("GetPath " + _hr(hr))
        return buf.value
    finally:
        _release(persist)
        _release(shell_link)


if __name__ == "__main__":
    # 自测：在**临时目录**里真建一个 .lnk，读回来看它指的是不是那个 target，然后删掉。
    # 全程不碰用户桌面。跑法：python app/shortcut.py
    import shutil
    import tempfile

    # COM 的初始化由 _ensure_com() 自己管（这儿没有 Qt 替我们做），下面 write/read 会调到

    assert _GUID(_IID_IPERSISTFILE).Data1 == 0x0000010B, "GUID 解析写反了"
    assert _GUID(_CLSID_SHELLLINK).Data4[0] == 0xC0 and _GUID(_CLSID_SHELLLINK).Data4[7] == 0x46
    try:
        _GUID("太短了")
        raise SystemExit("坏 GUID 该抛")
    except ValueError:
        pass

    assert desktop_dir() and os.path.isdir(desktop_dir()), desktop_dir()
    assert link_path().endswith(LINK_NAME), link_path()

    # 源码跑：create() 必须被拒（sys.executable 是 python.exe），原因跟 can_create 一致
    if not getattr(sys, "frozen", False):
        assert can_create(), "源码跑应当拒绝建"
        assert create() == can_create(), "拒绝的原因要跟 can_create 一致"
    else:
        assert can_create() == ""

    # 但 write() 本身要能跑通——上面那条路源码跑时是死的，不在这儿验就永远验不到
    tmp_dir = tempfile.mkdtemp(prefix="jev-shortcut-")
    tmp = os.path.join(tmp_dir, "test.lnk")
    assert write(sys.executable, tmp) == "", "写 .lnk 失败了"
    assert os.path.exists(tmp) and os.path.getsize(tmp) > 0, "文件没生成"
    with open(tmp, "rb") as f:
        head = f.read(20)
    assert head[:4] == b"\x4c\x00\x00\x00", head[:4]  # LinkHeader 长度，写歪了就不是 .lnk
    assert head[4:20] == bytes.fromhex("0114020000000000c000000000000046"), head[4:20].hex()
    assert os.path.normcase(_read_target(tmp)) == os.path.normcase(os.path.abspath(sys.executable)), \
        _read_target(tmp)
    # 覆盖已有的也该成功（用户挪了文件夹之后点「重建」走的就是这条）
    assert write(sys.executable, tmp) == "" and _read_target(tmp), "覆盖已有的失败了"
    shutil.rmtree(tmp_dir, ignore_errors=True)  # 整个目录带走，别在 %TEMP% 里攒垃圾
    print("shortcut ok")
