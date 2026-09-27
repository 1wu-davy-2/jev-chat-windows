# -*- coding: utf-8 -*-
"""设置持久化。key 硬约束（docs/KICKOFF.md #6）：只进环境变量，绝不落文件；其余设置落 config.json。

key 的持久化走 Windows 用户环境变量（注册表 HKCU\\Environment，跟 setx 写的是同一个地方）。
全程只有两把：判断 JEV_API_KEY、起草 LLM_API_KEY，跟选哪家来源无关。
读的时候先看进程环境，没有就直接读注册表——IDE 启动时把环境快照拿走了，之后再 Run 继承的还是旧环境，
只靠 os.environ 会「保存了下次打开还是没有」。"""
from __future__ import annotations

import ctypes
import json
import os
import sys  # 只为下面这一处：打包后 __file__ 指向临时解包目录，config.json 得放在 exe 旁边才存得住

from core import styles
from core.providers import CUSTOM, DRAFT_PROVIDERS, JEV_ENV, JEV_PROVIDERS, LEGACY, LLM_ENV
from core.relay import DEFAULT_JUDGE_PATH, DEFAULT_THINKING_STYLE, THINKING_STYLES

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_HISTORY_DB = os.path.join(_ROOT, "history.db")  # AI 调用记录，见 core/trace.py（已进 .gitignore）
_DEFAULT_RELATIONSHIP = "romantic partners"
_DEFAULT_CONTEXT = 10
_DEFAULT_JEV = "openrouter"
_DEFAULT_DRAFT = "deepseek"
_DEFAULT_OPENER_MINUTES = 30


def _read(name: str, default=None):
    """读 config.json 里的一个字段；每次都重新读文件，改设置不用重启进程。"""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            value = json.load(f).get(name)
    except (OSError, ValueError):
        return default
    return default if value is None else value

def relationship() -> str:
    return str(_read("relationship") or _DEFAULT_RELATIONSHIP)

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~30，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(30, n))

def style() -> str:
    """老字段：一句自由文本口吻。已被「场景模板」取代（见 style_preset / style_texts），
    这里只留着读升级前的配置做迁移，保存一次之后 config.json 里就没有它了。"""
    return str(_read("style") or "")

def style_preset() -> str:
    """选中的场景模板：core/styles 的键、"custom"（自定义），或空串（不用 = 什么都不追加）。"""
    v = str(_read("style_preset") or "").strip()
    if v in styles.KEYS or v == styles.CUSTOM:
        return v
    # 老配置里没有这一项：那句自由文本就当「自定义」接过来，没写就是不用
    return styles.CUSTOM if style().strip() else ""

def style_texts() -> dict:
    """用户改过的模板正文 {类型: 正文}，只存跟内置不一样的（见 save）。认不出的键、非字符串的值丢掉。
    老配置里那句自由文本（style）当「自定义」接过来——升级前只有那一个地方能写字。"""
    v = _read("style_texts")
    if isinstance(v, dict):
        return {k: str(t).strip() for k, t in v.items() if k in styles.KEYS or k == styles.CUSTOM}
    return {styles.CUSTOM: style().strip()} if style().strip() else {}

def scene_text() -> str:
    """这次要追加到起草 prompt 的正文（选中的类型 + 用户改过的版本）。空 = 不追加。"""
    return styles.resolve(style_preset(), style_texts())

def jev_provider() -> str:
    """判断模型走哪家：openrouter（默认）、typesafe 直连，或 relay 第三方中转。"""
    v = _read("jev_provider")
    if v in JEV_PROVIDERS:
        return v
    # 加中转那版没有「判断来源」这一项，只有一个 relay_judge 开关；开着就是走中转
    return "relay" if v is None and _read("relay_judge") else _DEFAULT_JEV

def jev_model() -> str:
    """判断模型 id；空 = 用该来源的默认模型。"""
    if jev_provider() == "relay":  # 中转的模型名在旧字段里叫 relay_jev_model
        return str(_read("jev_model") or _read("relay_jev_model") or "").strip() \
            or JEV_PROVIDERS["relay"].default
    return str(_read("jev_model") or "") or JEV_PROVIDERS[jev_provider()].default

def draft_provider() -> str:
    """起草走哪家（见 core/providers.DRAFT_PROVIDERS）。老配置里的 openrouter/deepseek 照样认；
    加中转那版的 "custom" 就是指中转，换成现在这个名字。"""
    v = _read("draft_provider")
    if v == "custom":
        return "relay"
    return v if v in DRAFT_PROVIDERS else _DEFAULT_DRAFT

def draft_provider_name() -> str:
    return DRAFT_PROVIDERS[draft_provider()].name

def draft_model() -> str:
    """起草模型 id；空 = 用该来源的默认模型（有的来源没有默认，那就得自己选）。
    中转的模型名在旧字段里叫 relay_draft_model。"""
    stored = str(_read("draft_model") or "").strip()
    if not stored and draft_provider() == "relay":
        stored = str(_read("relay_draft_model") or "").strip()
    return stored or DRAFT_PROVIDERS[draft_provider()].default

def draft_base_url() -> str:
    """自定义来源的 Base URL；其余来源用表里的，这里返回空。
    中转的地址不走这个字段（它那个是判断也共用的），见 relay_base_url()。"""
    p = draft_provider()
    return str(_read("draft_base_url") or "") if p in CUSTOM and p != "relay" else ""

def relay_base_url() -> str:
    """第三方中转的地址，起草和判断共用同一个。空 = 没配。"""
    return str(_read("relay_base_url") or "").strip()

def relay_judge_path() -> str:
    """中转上判断/排序那个口的路径。留空用 OpenRouter 那个默认；
    PackyCode 的 typesafe 通道那种要填 /v1/systemone（见 core/relay.py）。"""
    return str(_read("relay_judge_path") or "").strip() or DEFAULT_JUDGE_PATH

def relay_thinking_style() -> str:
    """中转认哪种思考开关（thinking / reasoning / none）。传错派系不报错、只被无视——
    思考照开、max_tokens 全被推理吃掉，表现为「起草结果解析不出候选」。"""
    v = str(_read("relay_thinking_style") or "").strip()
    return v if v in THINKING_STYLES else DEFAULT_THINKING_STYLE

def reply_target() -> bool:
    """群聊指定回复对象：开了才在界面上选回复给谁、才把对象喂给模型。默认关。"""
    return bool(_read("reply_target", False))

def opener() -> bool:
    """冷场开场白：最后一句是自己说的、对方一直没有回复，等够 opener_minutes() 就起草一批
    开场白让人挑。默认关——关着的时候调模型的条件还是「只有对方来了新消息」。"""
    return bool(_read("opener", False))

def history() -> bool:
    """记不记 AI 调用（每一轮发了什么提示、模型回了什么、最后用了哪条）。默认开，
    存本机 history.db，只有「AI 记录」窗口读它。关掉就一次都不写。"""
    return bool(_read("history", True))

def history_db() -> str:
    """记录库的路径：跟 config.json 并排。路径的算法只在这儿一处（core/trace 不认识 app）。"""
    return _HISTORY_DB

def opener_minutes() -> int:
    """等多少分钟算冷场。1~720，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("opener_minutes", _DEFAULT_OPENER_MINUTES))
    except (TypeError, ValueError):
        return _DEFAULT_OPENER_MINUTES
    return max(1, min(720, n))

def thinking() -> bool:
    """起草时是否开思考模式：慢且贵，默认关。只有 DeepSeek / OpenRouter / Anthropic / Gemini 吃它。"""
    return bool(_read("thinking", False))

def check_update() -> bool:
    """启动时要不要去 GitHub 查一次最新版本号：默认开，只出这一次网，设置里能关。"""
    return bool(_read("check_update", True))

def debug_view() -> bool:
    """调试视图：另开一个窗口实时画识别框。默认关，开了子进程才往队列里送帧。"""
    return bool(_read("debug_view", False))

def pet_enabled() -> bool:
    """宠物优先形态：平时桌面上只有宠物，有消息才在它旁边弹候选条，点宠物展开完整面板。默认开。"""
    return bool(_read("pet_enabled", True))

def pet_pos():
    """宠物上次停在屏幕哪儿，返回 (x, y)；没存过或数据脏就返回 None，由界面放默认角落。
    这里不判断「还在不在屏幕里」——那要问 Qt，交给界面层。"""
    v = _read("pet_pos")
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return int(v[0]), int(v[1])
        except (TypeError, ValueError):
            return None
    return None

def _read_env(env_name: str) -> str:
    """进程环境优先；没有就读注册表并带进进程环境，之后 core/ 里按 os.environ 读就有了。"""
    v = os.environ.get(env_name, "").strip()
    if not v:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, env_name)[0]).strip()
        except Exception:  # 非 Windows / 没这个值
            v = ""
        if v:
            os.environ[env_name] = v
    return v

def _get_key(env_name: str) -> str:
    """两把 key 之一。新名字空着就退回老版本按来源存的变量（下次保存会抄进新名字）。"""
    return _read_env(env_name) or _read_env(LEGACY[env_name])

def _set_key(env_name: str, value: str) -> None:
    """只写进程环境 + HKCU\\Environment，不写任何文件。"""
    os.environ[env_name] = value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, env_name, 0, winreg.REG_SZ, value)
    except Exception:
        pass  # 非 Windows（本机 Mac 开发）走不到，忽略


def _notify_env() -> None:
    """告诉别的进程环境变量变了。不能用 SendMessageTimeout 对 HWND_BROADCAST：
    它会逐个窗口等回复，超时 5 秒还按窗口数累加，保存按钮在界面线程上就卡死。
    SendNotifyMessage 把消息交出去就返回。"""
    try:
        fn = ctypes.windll.user32.SendNotifyMessageW
        fn.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p)
        fn.restype = ctypes.c_int
        fn(0xFFFF, 0x001A, 0, "Environment")  # HWND_BROADCAST, WM_SETTINGCHANGE
    except Exception:
        pass

def jev_key() -> str:
    """判断那把 key，两家来源共用。"""
    return _get_key(JEV_ENV)

def has_jev_key() -> bool:
    return bool(jev_key())

def llm_key() -> str:
    """起草那把 key，所有语言模型来源共用。"""
    return _get_key(LLM_ENV)

def has_llm_key() -> bool:
    return bool(llm_key())

has_key = has_jev_key  # 旧名字：界面上「配没配好」问的就是判断模型这把 key

def save(relationship_text: str | None = None, context_n: int | None = None, *,
         jev_provider_text: str | None = None, jev_key_text: str | None = None,
         jev_model_text: str | None = None, draft_provider_text: str | None = None,
         llm_key_text: str | None = None, draft_model_text: str | None = None,
         draft_base_url_text: str | None = None, reply_target_on: bool | None = None,
         style_preset_text: str | None = None, style_texts_dict: dict | None = None,
         thinking_on: bool | None = None,
         check_update_on: bool | None = None, debug_view_on: bool | None = None,
         relay_base_url_text: str | None = None, relay_judge_path_text: str | None = None,
         relay_thinking_style_text: str | None = None,
         pet_enabled_on: bool | None = None, opener_on: bool | None = None,
         opener_minutes_n: int | None = None, history_on: bool | None = None) -> None:
    """每个参数为空/None = 保留当前值。两把 key 写进程环境 + HKCU\\Environment，不写任何文件。"""
    jev = jev_provider_text if jev_provider_text in JEV_PROVIDERS else jev_provider()
    draft = draft_provider_text if draft_provider_text in DRAFT_PROVIDERS else draft_provider()
    # 没重填就把老变量里的值抄进新名字，迁移一次性做完（_get_key 已经退回读过老的了）
    wrote_key = False
    for env, typed in ((JEV_ENV, jev_key_text), (LLM_ENV, llm_key_text)):
        value = typed or ("" if _read_env(env) else _get_key(env))
        if value:
            _set_key(env, value)
            wrote_key = True
    if wrote_key:
        _notify_env()
    n = context() if context_n is None else max(3, min(30, int(context_n)))
    opener_n = (opener_minutes() if opener_minutes_n is None
                else max(1, min(720, int(opener_minutes_n))))
    # 空串 = 清掉，None = 原样留着（读原始字段，别读补过默认值的那个）
    keep = lambda new, name: str(_read(name) or "") if new is None else str(new).strip()
    flag = lambda new, now: now() if new is None else bool(new)
    # 场景模板：None = 原样留着。正文只存跟内置原文**不一样**的那些——这样以后改 core/styles.py
    # 的文案，没动过手的人能跟着更新，动过手的那一型则原样保留他改的版本
    preset = style_preset() if style_preset_text is None else str(style_preset_text).strip()
    if preset not in styles.KEYS and preset != styles.CUSTOM:
        preset = ""  # 认不出的类型按「不用」处理，别写个坏值进去
    texts = style_texts() if style_texts_dict is None else {
        str(k): str(v).strip() for k, v in dict(style_texts_dict).items()
        if k in styles.KEYS or k == styles.CUSTOM}
    texts = {k: t for k, t in texts.items() if t != styles.default_text(k)}
    # 整个 dict 必须在 open(..., "w") **之前**拼好：open 一上来就把文件截断，
    # 之后再 _read() 读到的是空文件，None 那几项就不是「保留」而是被清空了。
    data = {
        # 关系为空 = 只改别的开关（调试视图那种单项保存），别把它写没了
        "relationship": relationship_text or relationship(), "context": n,
        "style_preset": preset, "style_texts": texts,
        "jev_provider": jev, "jev_model": keep(jev_model_text, "jev_model"),
        "draft_provider": draft, "draft_model": keep(draft_model_text, "draft_model"),
        "draft_base_url": keep(draft_base_url_text, "draft_base_url"),
        "relay_base_url": keep(relay_base_url_text, "relay_base_url"),
        "relay_judge_path": keep(relay_judge_path_text, "relay_judge_path"),
        "relay_thinking_style": keep(relay_thinking_style_text, "relay_thinking_style"),
        "reply_target": flag(reply_target_on, reply_target),
        "opener": flag(opener_on, opener), "opener_minutes": opener_n,
        "history": flag(history_on, history),
        "thinking": flag(thinking_on, thinking),
        "check_update": flag(check_update_on, check_update),
        "debug_view": flag(debug_view_on, debug_view),
        "pet_enabled": flag(pet_enabled_on, pet_enabled),
        # 宠物位置不归这儿管（save_pet_pos 单独写），但**必须原样带过去**：这里是把整份
        # 配置重写一遍，漏了哪个键就等于把它删了——以前漏了 pet_pos，点一次「保存设置」
        # 宠物下次就跳回默认角落。
        "pet_pos": _load_all().get("pet_pos"),
    }
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_all() -> dict:
    """整个 config.json 读成 dict；文件不在或坏了就当空的。"""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_pet_pos(x: int, y: int) -> None:
    """只更新宠物位置，别的字段原样带过去。

    不走 save()：save() 每次都把两把 key 重写一遍注册表、再广播一次 WM_SETTINGCHANGE，
    拖一次宠物就来这么一下没必要。这里读全量、改一个键、写全量，不会弄丢别的设置。"""
    data = _load_all()
    data["pet_pos"] = [int(x), int(y)]
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
