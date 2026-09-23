# -*- coding: utf-8 -*-
"""自建/第三方中转的两处归一化：判断口的地址拼接，和思考开关的派系。

起草走 core/llm.py 的官方 SDK，地址由 core/providers.py 的 Base URL 直接给 SDK，不用这里拼。
判断不一样：那个口不是标准路径，各家中转叫法不同（OpenRouter 官方是 /api/alpha/decisions，
PackyCode 的 typesafe 通道是 /v1/systemone），所以地址和路径分开填，由 judge_url() 拼起来。

key 跟别的来源一样只从环境变量读，绝不落文件、绝不进日志。
"""
from __future__ import annotations

# 判断/排序的默认接口路径。路径对不上的时候，接口回的那句报错（404 或者
# 「only supports ... protocol」）就是线索，照它写进设置里的「判断接口路径」。
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
    """思考模式要额外带进请求体的字段。风格认不出就按不传处理。
    只给自定义来源用——表里那 11 家预设各自认什么字段是写死的（core/providers.py 的 extra）。"""
    return THINKING_STYLES.get((style or "").strip(), THINKING_STYLES["none"])(on)


def root(base: str) -> str:
    """base → API 根：把 /v1、/chat/completions 这些尾巴去掉。"""
    b = (base or "").strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if b.endswith(suffix):
            b = b[: -len(suffix)]
            break
    return b[:-3] if b.endswith("/v1") else b


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
    # 自测（不联网）：中转商给的几种 base 写法都得归到同一个判断口
    for b in ("https://api.x.com", "https://api.x.com/", "https://api.x.com/v1",
              "https://api.x.com/v1/chat/completions"):
        assert judge_url(b) == "https://api.x.com/api/alpha/decisions", b
    assert judge_url("https://x.com/api/v1") == "https://x.com/api/alpha/decisions"
    # base 自带 /api 前缀的：用户自己填的路径按 API 根拼，保留那个前缀
    assert judge_url("https://x.com/api/v1", "/v1/systemone") == "https://x.com/api/v1/systemone"
    assert judge_url("https://x.com", "v1/systemone") == "https://x.com/v1/systemone"  # 少个斜杠也认
    # 但默认路径（自带 /api）不能叠成 /api/api
    assert judge_url("https://x.com/api") == "https://x.com/api/alpha/decisions"
    assert judge_url("") == "/api/alpha/decisions"  # 空 base 由调用方拦住，这里只保证不炸
    # 思考开关：认不出的风格按「不传」处理，别把脏值当键名塞进请求体
    assert thinking_extra("thinking", True) == {"thinking": {"type": "enabled"}}
    assert thinking_extra("thinking", False) == {"thinking": {"type": "disabled"}}
    assert thinking_extra("reasoning", True) == {"reasoning": {"enabled": True}}
    assert thinking_extra("none", True) == {} and thinking_extra("乱写", True) == {}
    print("core/relay.py 自测通过")
