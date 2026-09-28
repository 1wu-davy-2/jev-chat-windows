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

from core import relations as rel
from core import styles
from core.providers import CUSTOM, DRAFT_PROVIDERS, JEV_ENV, JEV_PROVIDERS, LEGACY, LLM_ENV
from core.relay import DEFAULT_JUDGE_PATH, DEFAULT_THINKING_STYLE, THINKING_STYLES

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_HISTORY_DB = os.path.join(_ROOT, "history.db")  # AI 调用记录，见 core/trace.py（已进 .gitignore）
_CHATLOG_DB = os.path.join(_ROOT, "chatlog.db")  # 聊天记录，见 core/chatlog.py（已进 .gitignore）
_DEFAULT_CONTEXT = 10
# 老配置里 relationship 存的是这几个英文（安卓原版传下来的），迁移时折回内置的键
_LEGACY_RELATION_KEYS = {"romantic partners": "romance", "friends": "friend",
                         "colleagues": "colleague", "family": "family"}
_LEGACY_CUSTOM = "custom"  # 老「场景模板」里那一型自定义的键
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

def _clean_relations(raw) -> dict:
    """把存下来的那份关系模型收拾成能用的形状：脏数据、认不出的键一律丢掉；
    指到一个已经删掉的关系上的会话退回默认（不然面板上是个没有名字的下拉）。"""
    raw = raw if isinstance(raw, dict) else {}
    customs = []
    for c in raw.get("customs") or ():
        if isinstance(c, dict) and c.get("key"):
            customs.append({"key": str(c["key"]), "name": str(c.get("name") or "").strip(),
                            "text": str(c.get("text") or "").strip()})
    known = set(rel.KEYS) | {c["key"] for c in customs}
    texts = {str(k): str(v).strip() for k, v in (raw.get("texts") or {}).items()
             if str(k) in known}
    default = str(raw.get("default") or "")
    return {"default": default if default in known else rel.DEFAULT,
            "texts": texts, "customs": customs,
            "chats": {str(k): str(v) for k, v in (raw.get("chats") or {}).items()
                      if str(v) in known}}


def _migrate_relations() -> dict:
    """老配置接过来，形状换成上面那个 dict。三处源头：

    - `style_texts`：老「场景模板」里用户改过的正文。它的键跟 core/relations 故意取成一样，
      认得出的那几型直接搬过来当关系的正文——老配置里那段字本来就是每轮都拼进 prompt 的，
      接到关系上行为一点不变。
    - `style_preset` + `relationship`：老的场景模板是全局一份、真正在管措辞的那个，认得出
      某一型就以它为准当默认关系；认不出再看 relationship，那是用户自己填的一句话，
      接成一条自建关系（名字就是那句话）。
    - 更老的 `style` / `style_texts["custom"]`：一句自由文本，同样接成一条自建关系。"""
    old = _read("style_texts")
    texts = {str(k): str(v).strip() for k, v in (old if isinstance(old, dict) else {}).items()
             if k in rel.KEYS and str(v).strip() != rel.default_text(k)}
    spare = str((old or {}).get(_LEGACY_CUSTOM) or "").strip() if isinstance(old, dict) else ""
    spare = spare or style().strip()

    customs: list = []
    free = str(_read("relationship") or "").strip()
    key = _LEGACY_RELATION_KEYS.get(free)
    if not key and free and free not in rel.NAMES.values():
        key = rel.new_key([c["key"] for c in customs])
        customs.append({"key": key, "name": free, "text": spare})
        spare = ""  # 已经用掉了，别再建一条
    preset = str(_read("style_preset") or "").strip()
    if preset in rel.KEYS:
        key = preset
    elif preset == _LEGACY_CUSTOM and spare:
        key = rel.new_key([c["key"] for c in customs])
        customs.append({"key": key, "name": "自定义", "text": spare})
        spare = ""
    if spare:
        # 那段字现在没在用（用户换过型）也是他自己写的，留成一条自建关系，别在迁移里弄丢
        customs.append({"key": rel.new_key([c["key"] for c in customs]),
                        "name": "自定义", "text": spare})
    return {"default": key or rel.DEFAULT, "texts": texts, "customs": customs, "chats": {}}


def relations_dict() -> dict:
    """整个关系模型 {default, texts, customs, chats}。老配置在这儿一次性接过来，
    保存一次之后 config.json 里就只剩新形状了——跟原来 style_preset 接老 style 是一个套路。"""
    raw = _read("relations")
    return _clean_relations(raw) if isinstance(raw, dict) else _migrate_relations()

def relation_default() -> str:
    """新会话、还没指定过关系的聊天用哪一型。设置页里那个「默认关系」管它。"""
    return relations_dict()["default"]

def relation_customs() -> list:
    """用户自己加的关系 [{"key","name","text"}]，按添加顺序。"""
    return relations_dict()["customs"]

def relation_choices() -> tuple:
    """下拉框的选项 [(键, 名字)]：内置那几型（朋友在最前）+ 用户自建的。"""
    customs = relation_customs()
    return ((*((k, rel.NAMES[k]) for k in rel.KEYS),
             *((c["key"], rel.label(c["key"], customs)) for c in customs)))

def relation_of(chat: str) -> str:
    """这个会话用哪一型关系：自己挑过的优先，没挑过用默认那个。"""
    return relations_dict()["chats"].get(str(chat or "")) or relation_default()

def relation_name(key: str) -> str:
    """键 → 界面上显示的名字。"""
    return rel.label(key, relation_customs())

def relation_text(key: str) -> str:
    """某一型关系的正文（改过的优先，没改过用内置的）。"""
    d = relations_dict()
    return rel.resolve(key, d["texts"], d["customs"])

def scene_of(chat: str) -> str:
    """这个会话的场景模板：挑的是哪一型，空串 = 跟随关系（用这个会话自己那一型的正文）。"""
    v = _read("chat_scenes")
    return str((v or {}).get(str(chat or "")) or "") if isinstance(v, dict) else ""

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~30，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(30, n))

def style() -> str:
    """老字段：一句自由文本口吻。它是「场景模板」之前那一版的东西，现在只在迁移里用得上
    （接成一条自建关系，见 _migrate_relations），保存一次之后 config.json 里就没有它了。"""
    return str(_read("style") or "")

def scene_label(chat: str) -> str:
    """这个会话的场景模板在界面上该显示成什么（没挑就是「跟随关系」）。"""
    key = scene_of(chat)
    return rel.label(key, relation_customs()) if key else styles.FOLLOW_NAME

def scene_text(chat: str = "") -> str:
    """这次要追加到起草 prompt 的正文。挑了场景就用那一型的，没挑就用这个会话自己关系的。
    空 = 不追加（自建的关系还没写正文时就是这样）。"""
    d = relations_dict()
    return styles.resolve(scene_of(chat), relation=relation_of(chat),
                          relation_texts=d["texts"], customs=d["customs"])

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

def chatlog() -> bool:
    """聊天记录存不存本地（界面那串气泡 + 喂模型的上下文，见 core/chatlog.py）。

    **默认源码跑开着、打包版关着**：本地调试时不想丢记录，而装出去的默认不往磁盘写聊天原文，
    要存自己去设置里开。判断依据是 `sys.frozen`（打包后为真），不用额外配置。"""
    return bool(_read("chatlog", not getattr(sys, "frozen", False)))

def chatlog_db() -> str:
    """聊天记录库的路径。跟 history.db 分开：两个开关、两个清空入口，各管各的。"""
    return _CHATLOG_DB

def opener_minutes() -> int:
    """等多少分钟算冷场。1~720，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("opener_minutes", _DEFAULT_OPENER_MINUTES))
    except (TypeError, ValueError):
        return _DEFAULT_OPENER_MINUTES
    return max(1, min(720, n))

def chat_pin() -> str:
    """固定盯着哪个会话（"" = 跟随微信切到哪个就跟哪个）。界面上「当前会话」右边那个小按钮管的，
    存了重启还算数——固定是「我就盯着这个人」的意思，不该开一次应用就没了。"""
    return str(_read("chat_pin") or "").strip()

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

def auto_send() -> bool:
    """**调试用**：模型排过序（ranked）时自动把匹配度最高的候选填入输入框并回车发出。

    这是「绝不自动发送」那条硬约束唯一的例外，所以**默认关**，而且要用户自己在设置里拨开、
    再点一次「保存设置」才生效（跟「桌面宠物」那种拨一下就生效的不一样）。判定在
    main.auto_send_reply()，别在别处读这个开关去发消息。"""
    return bool(_read("auto_send", False))

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
    """两把 key 之一。新名字空着就按顺序退回老版本按来源存的变量（下次保存会抄进新名字）。

    LEGACY 里挂的是**一串**老名字（元组），得展开成一个个名字逐个试。以前是把整个元组
    塞进 _read_env 的，于是变成 os.environ.get(元组) → TypeError: str expected——
    而且只在「新名字没设」的机器上炸（新名字有值时 or 就短路了，右边根本不求值），
    所以本机一直没发现，装到别人机器上启动即崩（崩在 Overlay 构造里，连设置页都进不去）。
    写法跟 core/jev_client._api_key 保持一致。"""
    for name in (env_name, *LEGACY.get(env_name, ())):
        v = _read_env(name)
        if v:
            return v
    return ""

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

def save(context_n: int | None = None, *,
         jev_provider_text: str | None = None, jev_key_text: str | None = None,
         jev_model_text: str | None = None, draft_provider_text: str | None = None,
         llm_key_text: str | None = None, draft_model_text: str | None = None,
         draft_base_url_text: str | None = None, reply_target_on: bool | None = None,
         relation_model: dict | None = None,
         thinking_on: bool | None = None,
         check_update_on: bool | None = None, debug_view_on: bool | None = None,
         relay_base_url_text: str | None = None, relay_judge_path_text: str | None = None,
         relay_thinking_style_text: str | None = None,
         pet_enabled_on: bool | None = None, opener_on: bool | None = None,
         opener_minutes_n: int | None = None, history_on: bool | None = None,
         chatlog_on: bool | None = None, auto_send_on: bool | None = None,
         chat_pin_text: str | None = None) -> None:
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
    # 关系模型：None = 原样留着。正文只存跟内置原文**不一样**的那些——这样以后改
    # core/relations.py 的文案，没动过手的人能跟着更新，动过手的那一型则原样保留他改的版本。
    # 自建的关系（customs）没有内置原文，正文在它自己那条里，不走 texts。
    model = _clean_relations(relation_model) if relation_model is not None else relations_dict()
    model["texts"] = {k: t for k, t in model["texts"].items()
                      if t != rel.default_text(k) and rel.is_builtin(k)}
    scenes = _read("chat_scenes")
    # 整个 dict 必须在 open(..., "w") **之前**拼好：open 一上来就把文件截断，
    # 之后再 _read() 读到的是空文件，None 那几项就不是「保留」而是被清空了。
    data = {
        "context": n, "relations": model,
        # 每个会话挑了哪一型场景模板也不归这儿管（面板上拨一下走 save_chat_scene），
        # 但同样**必须带过去**：整份重写，漏了就等于把它删了。
        "chat_scenes": dict(scenes) if isinstance(scenes, dict) else {},
        "jev_provider": jev, "jev_model": keep(jev_model_text, "jev_model"),
        "draft_provider": draft, "draft_model": keep(draft_model_text, "draft_model"),
        "draft_base_url": keep(draft_base_url_text, "draft_base_url"),
        "relay_base_url": keep(relay_base_url_text, "relay_base_url"),
        "relay_judge_path": keep(relay_judge_path_text, "relay_judge_path"),
        "relay_thinking_style": keep(relay_thinking_style_text, "relay_thinking_style"),
        "reply_target": flag(reply_target_on, reply_target),
        "opener": flag(opener_on, opener), "opener_minutes": opener_n,
        "history": flag(history_on, history),
        "chatlog": flag(chatlog_on, chatlog),
        "thinking": flag(thinking_on, thinking),
        "check_update": flag(check_update_on, check_update),
        "debug_view": flag(debug_view_on, debug_view),
        "pet_enabled": flag(pet_enabled_on, pet_enabled),
        # 「绝不自动发送」那条硬约束唯一的例外（默认关）。见 auto_send() 的说明
        "auto_send": flag(auto_send_on, auto_send),
        # 「固定盯着哪个会话」也不归这儿管（界面上那个小按钮走 save_chat_pin），但同样**必须
        # 带过去**：这里是把整份配置重写一遍，漏了哪个键就等于把它删了。
        "chat_pin": keep(chat_pin_text, "chat_pin"),
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


def save_chat_pin(name: str) -> None:
    """只更新「固定盯着哪个会话」，别的字段原样带过去。

    跟 save_pet_pos 一个道理：这不是设置页里保存一次的那种改动，界面上拨一下就得写盘，
    走 save() 的话每次都要把两把 key 重写一遍注册表、再广播一次 WM_SETTINGCHANGE。"""
    data = _load_all()
    data["chat_pin"] = str(name or "").strip()
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _save_spot(key: str, name: str, value: str) -> None:
    """往 config.json 的一张 {会话名: 值} 表里改一格，别的字段原样带过去。
    空值 = 把这一格删掉（回到默认），不留一个没用的空条目。"""
    data = _load_all()
    spot = data.get(key)
    spot = dict(spot) if isinstance(spot, dict) else {}
    if value:
        spot[str(name)] = str(value)
    else:
        spot.pop(str(name), None)
    data[key] = spot
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def save_chat_relation(name: str, relation: str) -> None:
    """只更新「这个会话是哪一型关系」，别的字段原样带过去。面板上拨一下就得写盘——
    走 save() 的话每次都要把两把 key 重写一遍注册表、再广播一次 WM_SETTINGCHANGE。"""
    data = _load_all()
    model = _clean_relations(data.get("relations"))
    if relation:
        model["chats"][str(name)] = str(relation)
    else:
        model["chats"].pop(str(name), None)
    data["relations"] = _clean_relations(model)  # 认不出的键（那型刚被删了）当场清掉，别落盘
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def save_chat_scene(name: str, scene: str) -> None:
    """只更新「这个会话挑了哪一型场景模板」（空串 = 跟随关系），别的字段原样带过去。"""
    _save_spot("chat_scenes", name, scene)


def save_pet_pos(x: int, y: int) -> None:
    """只更新宠物位置，别的字段原样带过去。

    不走 save()：save() 每次都把两把 key 重写一遍注册表、再广播一次 WM_SETTINGCHANGE，
    拖一次宠物就来这么一下没必要。这里读全量、改一个键、写全量，不会弄丢别的设置。"""
    data = _load_all()
    data["pet_pos"] = [int(x), int(y)]
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


if __name__ == "__main__":
    # 关系模型的迁移 + 存盘：python app/settings.py
    # 全程写在一个临时目录里，不碰本机那份 config.json，也不碰注册表里的 key。
    import tempfile

    def _dump(data: dict) -> None:
        with open(_CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    _CONFIG = os.path.join(tempfile.mkdtemp(prefix="jev-settings-"), "config.json")
    _set_key = lambda *a: None       # 别把真实 key 重写进注册表
    _notify_env = lambda: None       # 也别为一次自测广播 WM_SETTINGCHANGE

    # 全新安装：默认朋友，其余空的；没指定过的会话直接拿到朋友的正文
    _dump({})
    assert relations_dict() == {"default": "friend", "texts": {}, "customs": [], "chats": {}}
    assert relation_of("谁") == "friend" and scene_of("谁") == ""
    assert scene_text("谁") == rel.TEXTS["friend"] and relation_name("friend") == "朋友"

    # 老配置（全局一份关系 + 一份场景模板）——场景模板才是真正在管措辞的那个，以它为准；
    # 改过的正文按型接过来；那段没人用的自由正文留成一条自建关系，不能在迁移里弄丢
    _dump({"relationship": "family", "style_preset": "flirty",
           "style_texts": {"romance": "我改的", "custom": "哥哥视角"}})
    m = relations_dict()
    assert m["default"] == "flirty", "老的场景模板该盖过 relationship"
    assert m["texts"] == {"romance": "我改的"}
    assert [c["text"] for c in m["customs"]] == ["哥哥视角"]
    assert scene_text("谁") == rel.TEXTS["flirty"], "改过的是恋爱那型，跟暧昧无关"

    # relationship 是自己填的一句话：接成一条自建关系，名字就是那句话，并且当默认
    _dump({"relationship": "刚认识的朋友，正在慢慢熟悉", "style": "别太热情"})
    m = relations_dict()
    assert m["default"] == m["customs"][0]["key"] and m["customs"][0]["key"] == "r1"
    assert m["customs"][0]["name"] == "刚认识的朋友，正在慢慢熟悉"
    assert m["customs"][0]["text"] == "别太热情"
    assert scene_text("谁") == "别太热情"

    # 场景模板选的是「自定义」：那段正文接成默认关系，而不是退回朋友
    _dump({"style_preset": "custom", "style_texts": {"custom": "哥哥视角"}})
    m = relations_dict()
    assert m["default"] == "r1" and m["customs"][0]["text"] == "哥哥视角"

    # 存一次：新形状落盘、老键没了、不归这次保存管的键（宠物位置）原样带过去
    _dump({"relationship": "friends", "pet_pos": [11, 22], "style": "老掉牙的"})
    save(12)
    saved = _load_all()
    assert "relationship" not in saved and "style" not in saved and "style_texts" not in saved
    assert saved["context"] == 12 and saved["pet_pos"] == [11, 22]
    assert saved["relations"]["default"] == "friend" and saved["chat_scenes"] == {}

    # 面板上拨下拉：只改这一格，别的字段一个都不能少
    save_chat_relation("张三", "workplace")
    save_chat_relation("李四", "r9")  # 不存在的键：当场清掉，等于没设过
    save_chat_scene("张三", "friend")
    assert relation_of("张三") == "workplace" and scene_of("张三") == "friend"
    assert scene_text("张三") == rel.TEXTS["friend"], "挑了场景就用那一型的正文"
    assert relation_of("李四") == "friend", "认不出的键要退回默认，不能是个没名字的型"
    assert relation_of("王五") == "friend" and scene_of("王五") == ""
    assert _load_all()["pet_pos"] == [11, 22]
    save_chat_scene("张三", "")  # 空串 = 回到「跟随关系」
    assert scene_of("张三") == "" and scene_text("张三") == rel.TEXTS["workplace"]
    save_chat_relation("张三", "")
    assert relation_of("张三") == "friend"

    # 设置页整份提交：自建关系的名字/正文、默认关系、每个会话的选择都得在
    save_chat_relation("张三", "r1")
    save(10, relation_model={"default": "r1", "texts": {"friend": "我改的朋友"},
                             "customs": [{"key": "r1", "name": "前女友", "text": "别提复合"}],
                             "chats": _load_all()["relations"]["chats"]})
    assert relation_of("张三") == "r1" and relation_name("r1") == "前女友"
    assert scene_text("张三") == "别提复合"
    assert relation_of("李四") == "r1", "默认关系换了，没指定过的会话跟着换"
    assert relation_text("friend") == "我改的朋友"

    # 聊天记录开关：源码跑默认开、打包版默认关；拨过之后按存的来
    assert chatlog() is True, "自测是源码跑，默认该是开的"
    save(10, chatlog_on=False)
    assert chatlog() is False and _load_all()["chatlog"] is False
    save(10, chatlog_on=True)
    assert chatlog() is True
    assert chatlog_db().endswith("chatlog.db") and chatlog_db() != history_db(), "两个库别用同一个文件"

    # 自动发送（「绝不自动发送」唯一的例外）：必须默认关，而且别的键写一遍不能把它带开
    assert auto_send() is False, "这个是调试开关，默认必须是关的"
    save(8)
    assert auto_send() is False, "save 整份重写，没提到它就得原样留着（默认仍是关）"
    save(10, auto_send_on=True)
    assert auto_send() is True and _load_all()["auto_send"] is True
    save(10, pet_enabled_on=False)
    assert auto_send() is True, "改别的开关不能顺手把它关掉"

    # 老名字迁移：新名字空着要**逐个**试老名字——LEGACY 里挂的是一串（元组），不是单个名字。
    # 以前是把整个元组塞进 _read_env 的，于是变成 os.environ.get(元组) → TypeError:
    # str expected；而且只在「新名字没设」的机器上炸（有值就被 or 短路了，右边根本不求值），
    # 本机一直没发现，装出去启动即崩（崩在 Overlay 构造里，连设置页都进不去）。
    # 下面这个替身卡的就是这件事：_read_env 只许收到 str，收到元组就当场炸。
    env = {"OPENROUTER_API_KEY": "老名字的判断 key"}

    def _read_env(name):  # noqa: F811 —— 换掉真的那个：它要读注册表，自测不许碰
        assert isinstance(name, str), f"_read_env 只吃单个名字，收到 {name!r}"
        return env.get(name, "")

    assert _get_key(JEV_ENV) == "老名字的判断 key", "新名字空着要退回老名字"
    env["JEV_API_KEY"] = "新名字的判断 key"
    assert _get_key(JEV_ENV) == "新名字的判断 key", "新名字有值就用新的，别再翻老的"
    env.clear()
    assert _get_key(JEV_ENV) == "", "两处都没有就返回空串，界面据此提示去设置里填"
    env["RELAY_API_KEY"] = "中转的老 key"
    assert _get_key(LLM_ENV) == "中转的老 key", "起草那把的第二个老名字也要试到"
    print("settings ok")
