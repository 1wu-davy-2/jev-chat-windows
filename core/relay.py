# -*- coding: utf-8 -*-
"""第三方中转（OpenAI 兼容）的地址归一化和默认模型名。

中转商给的 base 写法很不统一：带不带 /v1、带不带 /chat/completions、有没有 /api 前缀都见过，
这里统一归到站点根再拼。起草走 {root}/v1/chat/completions（标准口，中转一般都转）；
判断走 {root}/api/alpha/decisions（OpenRouter 的 alpha 专用口，转不转得实测——
probe/probe_relay.py 就是干这个的，通不了就把判断来源换回 OpenRouter）。

起草那条现在由 core/llm.py 的 OpenAI SDK 发，SDK 自己往 base_url 后面接 /chat/completions，
所以给它的是 openai_base()（带 /v1 的 API 根），不是 chat_url()。

key 跟别的来源一样只从环境变量读，绝不落文件、绝不进日志。中转用的是全局那两把
（起草 LLM_API_KEY、判断 JEV_API_KEY）——同一个中转商两边都用的话，两处填同一个 key。
KEY_ENV 是旧版留下的中转专用槽，只当兜底读，保存时会抄进新名字。
"""
from __future__ import annotations

KEY_ENV = "RELAY_API_KEY"
DEFAULT_DRAFT_MODEL = "deepseek-flash"  # 中转上 DeepSeek 的 id，跟官方直连一致
DEFAULT_JEV_MODEL = "jev-latest"        # 中转上 typesafe 的 jev
# 判断/排序的接口路径。OpenRouter 官方是 /api/alpha/decisions；中转商各有各的叫法，
# 比如 PackyCode 的 typesafe 通道是 /v1/systemone（拿 alpha/decisions 打它会 404，
# 拿 jev-latest 打 /v1/chat/completions 会回「typesafe channel only supports POST /v1/systemone」）。
# 路径对不上的时候，这句报错就是线索。
DEFAULT_JUDGE_PATH = "/api/alpha/decisions"
# 思考模式怎么关，各家中转也不一样（跟官方两家一样分两派）：
#   thinking  —— DeepSeek 官方那套 {"thinking": {"type": "disabled"}}，实测 PackyCode 认这个
#   reasoning —— OpenRouter 那套 {"reasoning": {"enabled": false}}
#   none      —— 不传（中转自己会关、或不吃这两个字段时用）
# 传错派系的后果是「被无视」而不是报错：思考照开，max_tokens 全被 reasoning 吃掉，
# 起草会拿到空 content。所以选错了会表现为「起草结果解析不出候选」。
DEFAULT_THINKING_STYLE = "thinking"
THINKING_STYLES = {
    "thinking": lambda on: {"thinking": {"type": "enabled" if on else "disabled"}},
    "reasoning": lambda on: {"reasoning": {"enabled": on}},
    "none": lambda on: {},
}


def thinking_extra(style: str, on: bool) -> dict:
    """思考模式要额外带进请求体的字段。风格认不出就按不传处理。"""
    return THINKING_STYLES.get((style or "").strip(), THINKING_STYLES["none"])(on)


def root(base: str) -> str:
    """base → 站点根：把 /v1、/chat/completions 这些尾巴去掉。"""
    b = (base or "").strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if b.endswith(suffix):
            b = b[: -len(suffix)]
            break
    return b[:-3] if b.endswith("/v1") else b


def chat_url(base: str) -> str:
    """起草的口（完整地址）。probe 和自测用；真正起草走 openai_base()。"""
    return root(base) + "/v1/chat/completions"


def openai_base(base: str) -> str:
    """喂给 OpenAI SDK 的 base_url：SDK 自己会接 /chat/completions，这里只到 /v1。"""
    return root(base) + "/v1"


def site_root(base: str) -> str:
    """站点根：root 再去掉尾部的 /api。默认判断路径本身就带 /api，base 里也有的话会叠成 /api/api。"""
    r = root(base)
    return r[:-4] if r.endswith("/api") else r


def judge_url(base: str, path: str = "") -> str:
    """判断/排序的口 = 站点根 + 路径。path 留空用 OpenRouter 那个默认；少写开头的斜杠也认。
    默认路径按站点根拼（它自带 /api），用户自己填的路径按 API 根拼（他多半是照着报错原文抄的）。"""
    p = (path or "").strip()
    if not p:
        return site_root(base) + DEFAULT_JUDGE_PATH
    return root(base) + (p if p.startswith("/") else "/" + p)


if __name__ == "__main__":
    # 自测（不联网）：中转商给的几种 base 写法都得归到同一个口
    for b in ("https://api.x.com", "https://api.x.com/", "https://api.x.com/v1",
              "https://api.x.com/v1/chat/completions"):
        assert chat_url(b) == "https://api.x.com/v1/chat/completions", b
        assert openai_base(b) == "https://api.x.com/v1", b
        # SDK 拿到 openai_base 自己接这一段，接出来必须跟 chat_url 一模一样
        assert openai_base(b) + "/chat/completions" == chat_url(b), b
        assert judge_url(b) == "https://api.x.com/api/alpha/decisions", b
    assert chat_url("https://x.com/api/v1") == "https://x.com/api/v1/chat/completions"
    # base 自带 /api 前缀的：用户自己填的路径按 API 根拼，保留那个前缀
    assert judge_url("https://x.com/api/v1", "/v1/systemone") == "https://x.com/api/v1/systemone"
    assert judge_url("https://x.com", "v1/systemone") == "https://x.com/v1/systemone"  # 少个斜杠也认
    # 但默认路径（自带 /api）不能叠成 /api/api
    assert judge_url("https://x.com/api/v1") == "https://x.com/api/alpha/decisions"
    assert judge_url("https://x.com/api") == "https://x.com/api/alpha/decisions"
    assert judge_url("https://x.com") == "https://x.com/api/alpha/decisions"
    assert chat_url("") == "/v1/chat/completions"  # 空 base 由调用方拦住，这里只保证不炸
    print("relay ok")  # 纯 ASCII：CI 的 stdout 是 cp1252，中文 print 会 UnicodeEncodeError
