# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → 起草 3 条 → Jev 一次判断+排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。
"""
from __future__ import annotations

try:
    from .draft import draft_candidates
    from .jev_client import ask
    from .questions import JUDGE_QUESTIONS, build_rank_question, build_state
    from .relay import DEFAULT_JEV_MODEL, KEY_ENV as RELAY_KEY_ENV, judge_url
except ImportError:
    from draft import draft_candidates
    from jev_client import ask
    from questions import JUDGE_QUESTIONS, build_rank_question, build_state
    from relay import DEFAULT_JEV_MODEL, KEY_ENV as RELAY_KEY_ENV, judge_url

_REPLY_IDX = {"reply_a": 0, "reply_b": 1, "reply_c": 2}


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 10, provider: str = "openrouter",
            reply_to: str | None = None, style: str = "", thinking: bool = False,
            base_url: str = "", judge_relay: bool = False, judge_model: str | None = None,
            judge_path: str = "", thinking_style: str = "") -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（openrouter / deepseek 直连 / custom 第三方中转）。
    reply_to: 群聊里指定回复给谁；None = 正常回复。
    style: 用户自己描述的说话风格，只影响起草。
    thinking: 起草时是否开思考模式，只影响起草，默认关。
    model=None 用该来源的默认模型。
    base_url: 第三方中转地址，provider == "custom" 时必填。
    judge_relay: 判断/排序也走中转（默认关，仍走 OpenRouter）——中转发不发那个专用口得实测，
    没验过就开着会直接报错，所以默认关。judge_model=None 用 relay 的默认 jev 模型名；
    judge_path 是中转上那个口的路径（留空用 OpenRouter 的 /api/alpha/decisions，
    PackyCode 那种要填 /v1/systemone）。
    thinking_style: 中转认哪种思考开关（thinking / reasoning / none，见 core/relay.py）。

    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to}。
    scores 是每条候选的胜出概率（0~1），取自 best_reply.probabilities，取不到记 0.0。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。
    """
    candidates = draft_candidates(messages, relationship, provider=provider,
                                  model=model, timeout=timeout, keep=context, reply_to=reply_to,
                                  style=style, thinking=thinking, base_url=base_url,
                                  thinking_style=thinking_style)

    questions = dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    result = ask(build_state(messages, relationship, keep=context, reply_to=reply_to),
                 questions, timeout=timeout,
                 url=judge_url(base_url, judge_path) if judge_relay else None,
                 model=(judge_model or DEFAULT_JEV_MODEL) if judge_relay else None,
                 env=RELAY_KEY_ENV if judge_relay else "OPENROUTER_API_KEY")

    answers = result.get("answers") or {}
    best_key = (answers.get("best_reply") or {}).get("choice")
    best_index = _REPLY_IDX.get(best_key, 0)  # 解析不出就退第一条
    if best_index >= len(candidates):
        best_index = 0

    probabilities = (answers.get("best_reply") or {}).get("probabilities") or {}
    scores = [0.0, 0.0, 0.0]
    for key, idx in _REPLY_IDX.items():
        try:
            scores[idx] = float(probabilities.get(key, 0.0))
        except (TypeError, ValueError):
            scores[idx] = 0.0  # 脏数据一律按 0 处理

    return {
        "candidates": candidates,
        "best_index": best_index,
        "best_reply": candidates[best_index],
        "scores": scores,
        "answers": answers,
        "usage": result.get("usage") or {},
        "reply_to": reply_to,
    }
