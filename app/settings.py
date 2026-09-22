# -*- coding: utf-8 -*-
"""设置持久化。key 硬约束（docs/KICKOFF.md #6）：只进环境变量，绝不落文件；relationship 不是密钥，落 config.json。

key 的持久化走 Windows 用户环境变量（注册表 HKCU\\Environment，跟 setx 写的是同一个地方）。
读的时候先看进程环境，没有就直接读注册表——IDE 启动时把环境快照拿走了，之后再 Run 继承的还是旧环境，
只靠 os.environ 会「保存了下次打开还是没有」。"""
from __future__ import annotations

import ctypes
import json
import os
import sys  # 只为下面这一处：打包后 __file__ 指向临时解包目录，config.json 得放在 exe 旁边才存得住

from core.relay import (DEFAULT_DRAFT_MODEL, DEFAULT_JEV_MODEL, DEFAULT_JUDGE_PATH,
                        DEFAULT_THINKING_STYLE, THINKING_STYLES, KEY_ENV as _RELAY_ENV)

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_DEFAULT_RELATIONSHIP = "romantic partners"
_DEFAULT_CONTEXT = 10
_ENV = "OPENROUTER_API_KEY"
_DEEPSEEK_ENV = "DEEPSEEK_API_KEY"
_PROVIDERS = ("openrouter", "deepseek", "custom")  # custom = 第三方中转，地址和模型名自填

def _read() -> dict:
    """每次都重新读文件，改设置不用重启进程。读不到 / 坏了 / 根本不是个对象（手改成了数组、数字之类）
    一律当空配置——configured() 在启动路径上，这里崩了整个界面都出不来。"""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def relationship() -> str:
    return _read().get("relationship") or _DEFAULT_RELATIONSHIP

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~30，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read().get("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(30, n))

def style() -> str:
    """用户自己描述的说话风格（可选，自由文本），只喂给起草模型。默认空 = 只照着最近的消息模仿。"""
    return str(_read().get("style") or "")

def draft_provider() -> str:
    """起草走哪家：openrouter（默认）、deepseek 直连、custom 第三方中转。判断/排序默认仍走 OpenRouter。"""
    v = _read().get("draft_provider")
    return v if v in _PROVIDERS else _PROVIDERS[0]

def relay() -> dict:
    """第三方中转的一组设置。缺项/脏数据一律退默认值。
    base_url: 中转商给的地址（写法不统一，core/relay.py 负责归一到口）
    draft_model / jev_model: 中转上的模型名，各家跟官方不一定同名
    judge: 判断/排序也走中转（默认关）——中转转不转那个专用口得实测，没验过就开着会直接报错
    judge_path: 那个口的路径，各家叫法不同（默认 OpenRouter 的 /api/alpha/decisions）
    thinking_style: 思考开关带哪个字段（thinking / reasoning / none），选错了思考关不掉"""
    d = _read()
    style = str(d.get("relay_thinking_style") or DEFAULT_THINKING_STYLE).strip()
    return {"base_url": str(d.get("relay_base_url") or "").strip(),
            "thinking_style": style if style in THINKING_STYLES else DEFAULT_THINKING_STYLE,
            "draft_model": str(d.get("relay_draft_model") or DEFAULT_DRAFT_MODEL).strip(),
            "jev_model": str(d.get("relay_jev_model") or DEFAULT_JEV_MODEL).strip(),
            "judge_path": str(d.get("relay_judge_path") or DEFAULT_JUDGE_PATH).strip(),
            "judge": bool(d.get("relay_judge", False))}


def configured() -> bool:
    """回复服务配好了没：**起草那步和判断那步各自要的东西都齐了**才算配好。
    判断走中转时 OpenRouter key 不是必需的——但起草要是还在用 OpenRouter，它照样必需，
    所以这里两边都得看（只看判断会放行一个「保存成功、之后每条消息都失败」的配置）。
    界面上「先设置，再开始」和开跑前的拦截都按这个判。"""
    r = relay()
    provider = draft_provider()
    if provider == "custom":
        draft_ok = bool(r["base_url"]) and has_relay_key()
    elif provider == "deepseek":
        draft_ok = has_deepseek_key()
    else:
        draft_ok = has_key()
    judge_ok = (bool(r["base_url"]) and has_relay_key()) if r["judge"] else has_key()
    return draft_ok and judge_ok


def reply_target() -> bool:
    """群聊指定回复对象：开了才在界面上选回复给谁、才把对象喂给模型。默认关。"""
    return bool(_read().get("reply_target", False))

def thinking() -> bool:
    """起草时是否开思考模式：慢且贵，默认关。两个来源（OpenRouter/DeepSeek）都吃这个开关。"""
    return bool(_read().get("thinking", False))

def check_update() -> bool:
    """启动时要不要去 GitHub 查一次最新版本号：默认开，只出这一次网，设置里能关。"""
    return bool(_read().get("check_update", True))

def _get_key(env_name: str) -> str:
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

def _set_key(env_name: str, value: str) -> None:
    """只写进程环境 + HKCU\\Environment，不写任何文件。"""
    os.environ[env_name] = value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, env_name, 0, winreg.REG_SZ, value)
        # 广播一下，之后新开的终端/进程就能看到；已经开着的 IDE 看不到也无所谓，启动时会读注册表
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
    except Exception:
        pass  # 非 Windows（本机 Mac 开发）走不到，忽略

def key() -> str:
    return _get_key(_ENV)

def has_key() -> bool:
    return bool(key())

def deepseek_key() -> str:
    return _get_key(_DEEPSEEK_ENV)

def has_deepseek_key() -> bool:
    return bool(deepseek_key())

def relay_key() -> str:
    return _get_key(_RELAY_ENV)

def has_relay_key() -> bool:
    return bool(relay_key())

def save(key_text: str | None, relationship_text: str, context_n: int | None = None,
         deepseek_key_text: str | None = None, provider_text: str | None = None,
         reply_target_on: bool | None = None, style_text: str | None = None,
         thinking_on: bool | None = None, check_update_on: bool | None = None,
         relay_key_text: str | None = None, relay_base_url: str | None = None,
         relay_draft_model: str | None = None, relay_jev_model: str | None = None,
         judge_relay_on: bool | None = None, relay_judge_path: str | None = None,
         relay_thinking_style: str | None = None) -> None:
    """每个参数为空/None = 保留当前值。三个 key 都只写进程环境 + HKCU\\Environment，不写任何文件。"""
    if key_text:
        _set_key(_ENV, key_text)
    if deepseek_key_text:
        _set_key(_DEEPSEEK_ENV, deepseek_key_text)
    if relay_key_text:
        _set_key(_RELAY_ENV, relay_key_text)
    n = context() if context_n is None else max(3, min(30, int(context_n)))
    provider = provider_text if provider_text in _PROVIDERS else draft_provider()  # None 或脏值 = 保留原来的
    target = reply_target() if reply_target_on is None else bool(reply_target_on)
    style_v = style() if style_text is None else str(style_text).strip()  # 空串 = 清掉
    think = thinking() if thinking_on is None else bool(thinking_on)
    check = check_update() if check_update_on is None else bool(check_update_on)
    r = relay()  # 中转那几项同样：None = 保留，空串 = 清掉（模型名清掉就退回默认名）
    relay_v = {"base_url": r["base_url"] if relay_base_url is None else str(relay_base_url).strip(),
               "draft_model": r["draft_model"] if relay_draft_model is None else str(relay_draft_model).strip(),
               "jev_model": r["jev_model"] if relay_jev_model is None else str(relay_jev_model).strip(),
               "judge_path": r["judge_path"] if relay_judge_path is None else str(relay_judge_path).strip(),
               "thinking_style": r["thinking_style"] if relay_thinking_style is None
               else (str(relay_thinking_style).strip() if relay_thinking_style in THINKING_STYLES
                     else r["thinking_style"]),
               "judge": r["judge"] if judge_relay_on is None else bool(judge_relay_on)}
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump({"relationship": relationship_text, "context": n, "draft_provider": provider,
                   "reply_target": target, "style": style_v, "thinking": think,
                   "check_update": check, "relay_base_url": relay_v["base_url"],
                   "relay_draft_model": relay_v["draft_model"], "relay_jev_model": relay_v["jev_model"],
                   "relay_judge_path": relay_v["judge_path"], "relay_judge": relay_v["judge"],
                   "relay_thinking_style": relay_v["thinking_style"]}, f, ensure_ascii=False)
