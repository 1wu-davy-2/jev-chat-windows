# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → Jev 判断 → 带着判断起草 3 条 → Jev 排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。
"""
from __future__ import annotations

import time

try:
    from .draft import draft_candidates, draft_openers
    from .jev_client import JevError, ask
    from .questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text
except ImportError:
    from draft import draft_candidates, draft_openers
    from jev_client import JevError, ask
    from questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text

_REPLY_IDX = {"reply_a": 0, "reply_b": 1, "reply_c": 2}


def _add_usage(total: dict, one: dict | None) -> None:
    """两次 Jev 调用的 usage 相加（tokens、cost）；非数字的字段后来的盖掉前面的。"""
    for k, v in (one or {}).items():
        total[k] = total.get(k, 0) + v if isinstance(v, (int, float)) else v


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 30, provider: str = "deepseek",
            base_url: str | None = None, reply_to: str | None = None, scene: str = "",
            thinking: bool = False, jev_provider: str = "openrouter",
            jev_model: str | None = None, judge_path: str = "",
            thinking_style: str = "", rounds: int = 1) -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（core.providers.DRAFT_PROVIDERS），base_url 只有自定义来源要传。
    jev_provider / jev_model: 判断和排序走哪家、哪个模型（core.providers.JEV_PROVIDERS）。
    judge_path: 判断走第三方中转时，那个口在中转上的路径（各家叫法不同，见 core/relay.py）。
    thinking_style: 中转认哪种思考开关（thinking / reasoning / none），只影响起草。
    reply_to: 群聊里指定回复给谁；None = 正常回复。
    scene: 这次说话的口气那段正文（这个会话那种关系的，或场景模板临时换的那一型），只影响
    起草——Jev 判的是意图和紧张度，跟措辞无关。
    thinking: 起草时是否开思考模式，只影响起草，默认关。
    rounds: 「回复轮数」——一个候选最多连着发几句（1~3，默认 1）。> 1 时候选里可能是几条
    用 \\n 连着的消息（切分走 draft.lines），判断和排序照旧按整个候选比。
    model / jev_model = None 用该来源的默认模型。

    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to, trace}。
    scores 是每条候选的胜出概率（0~1），取自 best_reply.probabilities，取不到记 0.0。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。

    三段式（issue #4）：先让 Jev 答 7 道判断题，把判断当小抄喂给起草，最后 Jev 只排序。
    判断那次挂了就退回老路：盲起草 + 判断和排序一次问完，行为跟以前一样。usage 是两次之和。

    trace 是这一轮的原始材料（两段提示原文、模型原始返回、扔掉的候选、judge 的 state、耗时、
    token），给「AI 记录」落库用。起草就挂掉时它挂在 JevError 的 `.trace` 上一起抛出去——
    失败的那轮恰恰最该留痕。
    """
    state = build_state(messages, relationship, keep=context, reply_to=reply_to)
    usage: dict = {}
    answers: dict = {}
    judged = False
    trouble = ""  # 判断/排序为什么没跑成。带给界面显示，别让调用方只看到「生成失败」
    # 一路往里填，哪个环节挂了都有东西可查
    trace: dict = {"messages": list(messages), "relationship": relationship, "context_n": context,
                   "rounds": rounds,
                   "scene": scene, "reply_to": reply_to, "judge_state": state,
                   "judge_provider": jev_provider, "judge_model": jev_model or "",
                   "judge_path": judge_path, "draft_provider": provider,
                   "draft_base_url": base_url or ""}
    started = time.monotonic()
    step = time.monotonic()
    try:
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model,
                    base_url=base_url or "", path=judge_path)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        judged = True
        trace["judge_answers"] = answers
        trace["judge_reply"] = first
        trace["judge_usage"] = first.get("usage") or {}
    except JevError as e:
        trouble = str(e)  # 退回盲起草 + 老的一次合问；错误不打日志（里面可能带请求内容）
        trace["judge_error"] = trouble
    trace["judge_ms"] = int((time.monotonic() - step) * 1000)

    draft_info: dict = {}
    try:
        candidates = draft_candidates(messages, relationship, provider=provider, model=model,
                                      base_url=base_url, timeout=timeout, keep=context,
                                      reply_to=reply_to, scene=scene,
                                      thinking=thinking, thinking_style=thinking_style,
                                      rounds=rounds,
                                      guidance=guidance_text(answers) if judged else None,
                                      info=draft_info)
    except JevError as e:
        # 起草就挂了：这轮照样要留痕（失败的那轮最该查），把材料挂在异常上带走
        trace["draft"] = draft_info
        trace["ms"] = int((time.monotonic() - started) * 1000)
        e.trace = trace
        raise
    trace["draft"] = draft_info
    if not candidates:  # 注入过滤可以把起草结果全扔掉；接着取 [0] 会 IndexError
        trace["ms"] = int((time.monotonic() - started) * 1000)
        e = JevError("起草结果没有可用候选回复")
        e.trace = trace
        raise e

    questions = {} if judged else dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    if questions:
        step = time.monotonic()
        try:
            second = ask(state, questions, timeout=timeout,
                         provider=jev_provider, model=jev_model,
                         base_url=base_url or "", path=judge_path)
        except JevError as e:
            # 排序也挂了：**不再抛**。三条候选已经起草好了，没有概率和推荐也照样能用；
            # 抛出去等于把起草的钱和结果一起扔了，界面还只剩一句「生成失败」。
            # 只把原因记下来，带回去让界面说清楚这次为什么没排序。
            second = {}
            trouble = trouble or str(e)
            trace["rank_error"] = str(e)
        answers = {**answers, **(second.get("answers") or {})}
        _add_usage(usage, second.get("usage"))
        trace["rank_ms"] = int((time.monotonic() - step) * 1000)
        trace["rank_questions"] = questions  # 排的是哪几条候选，复盘时要看
        trace["rank_answers"] = second.get("answers") or {}
        trace["rank_reply"] = second
        trace["rank_usage"] = second.get("usage") or {}
    trace["ms"] = int((time.monotonic() - started) * 1000)

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
        "usage": usage,
        "reply_to": reply_to,
        # judged = 七道判断题答上了；ranked = 排序也做完了。两个都假的时候界面别标「推荐」，
        # 因为 best_index 只是退回第一条，不是模型选出来的
        "judged": judged,
        "ranked": bool(answers.get("best_reply")),
        "trouble": trouble,
        "trace": trace,
    }


def analyze_opener(messages: list, relationship: str, model: str | None = None,
                   timeout: float = 30, context: int = 30, provider: str = "deepseek",
                   base_url: str | None = None, reply_to: str | None = None, scene: str = "",
                   thinking: bool = False, thinking_style: str = "",
                   rounds: int = 1) -> dict:
    """冷场时的一批开场白（最后一句是 me 说的、对方一直没回）：只起草，**不过 Jev**。

    七道判断题问的全是「对方最新那条什么意思」，而这里的场景恰恰是对方没有新话——问不出东西，
    问了也是白花钱。所以 judged / ranked 都是 False（界面据此不标「推荐」，那张卡片上的百分比
    也不会出现），best_index 只是退回第一条。

    返回值形状跟 analyze() 一样，多一个 opener=True 让界面换一套说法（「对方刚说」→「对方还没回」，
    洞察卡不摆意图和紧张度）。参数含义同 analyze()，只是没有 jev_* 那几项——用不上。
    trace 里也只有起草那半边（没有判断和排序），见 analyze()。

    messages 为空也是合法的（刚加的好友、只发过表情/图片）：这批就是「先开口打个招呼」，
    返回的 blank=True 让界面别摆「对方 N 分钟没回」——那会儿根本没人被晾着。
    """
    trace: dict = {"messages": list(messages), "relationship": relationship, "context_n": context,
                   "rounds": rounds,
                   "scene": scene, "reply_to": reply_to, "draft_provider": provider,
                   "draft_base_url": base_url or ""}
    started = time.monotonic()
    try:
        draft_info: dict = {}
        candidates = draft_openers(messages, relationship, provider=provider, model=model,
                                   base_url=base_url, timeout=timeout, keep=context,
                                   reply_to=reply_to, scene=scene, thinking=thinking,
                                   thinking_style=thinking_style, rounds=rounds, info=draft_info)
        trace["draft"] = draft_info
        if not candidates:  # 注入过滤可以把起草结果全扔掉；接着取 [0] 会 IndexError
            raise JevError("起草结果没有可用候选")
    except JevError as e:
        trace["ms"] = int((time.monotonic() - started) * 1000)
        e.trace = trace  # 起草挂了也要留痕，由调用方落库
        raise
    trace["ms"] = int((time.monotonic() - started) * 1000)
    return {
        "candidates": candidates,
        "best_index": 0,
        "best_reply": candidates[0],
        "scores": [0.0] * len(candidates),
        "answers": {},
        "usage": {},
        "reply_to": reply_to,
        "judged": False,
        "ranked": False,
        "trouble": "",
        "opener": True,
        "blank": not messages,  # 空会话（没有可接的上下文）：界面据此说「还没聊过」而不是「对方没回」
        "trace": trace,
    }


if __name__ == "__main__":
    # 候选被过滤光时要抛 JevError，不能在取第一条时 IndexError。
    from unittest.mock import patch

    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=[]):
        try:
            analyze([("her", "hello")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "没有可用候选" in str(e)

    # 判断一路上挂了（key 被停用那种）：三条照样交出去，只是没判断、没排序、带一句原因。
    # 以前这儿是把起草好的候选一起扔掉的。
    with patch("__main__.ask", side_effect=JevError("Jev HTTP 401: 令牌已被停用")), \
         patch("__main__.draft_candidates", return_value=["甲", "乙", "丙"]):
        r = analyze([("her", "hello")], "friends")
    assert r["candidates"] == ["甲", "乙", "丙"], r["candidates"]
    assert r["judged"] is False and r["ranked"] is False
    assert "401" in r["trouble"], r["trouble"]
    assert r["scores"] == [0.0, 0.0, 0.0] and r["best_index"] == 0
    assert r["answers"] == {}

    # 「回复轮数」原样传到起草那一层（判断和排序不看它——整条候选一起比）
    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=["甲", "乙", "丙"]) as fake:
        analyze([("her", "hello")], "friends", rounds=3)
    assert fake.call_args.kwargs["rounds"] == 3, fake.call_args

    # 判断答上了、只有排序那一步挂了：judged 真 / ranked 假，原因照样带回来
    first = {"answers": {"true_intent": {"choice": "casual_chat"}}, "usage": {}}
    with patch("__main__.ask", side_effect=[first, JevError("排序口 500")]), \
         patch("__main__.draft_candidates", return_value=["甲", "乙", "丙"]):
        r = analyze([("her", "hello")], "friends")
    assert r["judged"] is True and r["ranked"] is False and "500" in r["trouble"]
    assert r["candidates"] == ["甲", "乙", "丙"]

    # 一切正常：ranked 真、trouble 空
    ranked = {"answers": {"best_reply": {"choice": "reply_b",
                                         "probabilities": {"reply_b": 0.8}}}, "usage": {}}
    with patch("__main__.ask", side_effect=[first, ranked]), \
         patch("__main__.draft_candidates", return_value=["甲", "乙", "丙"]):
        r = analyze([("her", "hello")], "friends")
    assert r["judged"] and r["ranked"] and r["trouble"] == ""
    assert r["best_index"] == 1 and r["scores"][1] == 0.8

    # 开场白：只起草，一次都不问 Jev——那七道题问的是「对方最新那条什么意思」，这儿对方没说话。
    # judged/ranked 全假、scores 全 0，界面据此不标「推荐」。
    with patch("__main__.ask", side_effect=AssertionError("开场白不该调 Jev")), \
         patch("__main__.draft_openers", return_value=["在忙吗", "那家店还去吗", "睡了吗"]):
        r = analyze_opener([("her", "在忙吗"), ("me", "刚忙完")], "friends")
    assert r["opener"] is True and r["candidates"] == ["在忙吗", "那家店还去吗", "睡了吗"]
    assert r["judged"] is False and r["ranked"] is False and r["trouble"] == ""
    assert r["scores"] == [0.0, 0.0, 0.0] and r["answers"] == {} and r["best_index"] == 0

    # 候选被过滤光时跟 analyze 一样抛，别在取第一条时 IndexError
    with patch("__main__.draft_openers", return_value=[]):
        try:
            analyze_opener([("me", "在忙吗")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "没有可用候选" in str(e)

    # trace：这一轮的原始材料（「AI 记录」存的就是它）。判断挂了也留着，谁挂了一目了然
    with patch("__main__.ask", side_effect=JevError("Jev HTTP 401")), \
         patch("__main__.draft_candidates", return_value=["甲", "乙", "丙"]):
        r = analyze([("her", "hello")], "friends")
    tr = r["trace"]
    assert tr["messages"] == [("her", "hello")] and tr["relationship"] == "friends"
    assert tr["judge_state"]["chat"]["messages"][0]["text"] == "hello", "judge 收到的 state 要留着"
    assert tr["judge_error"] == "Jev HTTP 401" and "judge_answers" not in tr
    assert tr["judge_ms"] >= 0 and tr["ms"] >= 0 and "draft" in tr
    assert tr["draft_provider"] == "deepseek" and tr["context_n"] == 30
    assert tr["rounds"] == 1, "默认一句一回；这一轮用了几条连发要留痕"

    # 起草就挂了：材料得挂在异常上一起抛出去，调用方照样能落库（失败的那轮最该查）
    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", side_effect=JevError("起草结果解析不出候选")):
        try:
            analyze([("her", "hello")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.trace["relationship"] == "friends" and "judge_ms" in e.trace
    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=[]):
        try:
            analyze([("her", "hello")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "draft" in e.trace, "候选被过滤光也要带材料"

    # 开场白那轮没有判断和排序，trace 里也就只有起草那半边
    with patch("__main__.draft_openers", return_value=["在忙吗", "睡了吗"]):
        op = analyze_opener([("me", "刚忙完")], "friends")
    assert "draft_provider" in op["trace"] and "judge_ms" not in op["trace"]
    assert op["blank"] is False, "有上下文就不是空会话"

    # 空会话的开场白（刚加的好友、只发过表情/图片）：messages 为空也照起草，
    # blank=True 让界面说「还没聊过」而不是「对方 N 分钟没回」——那会儿没人被晾着
    with patch("__main__.draft_openers", return_value=["嗨", "在忙啥呢", "好久不见"]) as fake:
        op = analyze_opener([], "friends")
    assert fake.call_args[0][0] == [], "空列表原样传下去，别在这儿替换成别的"
    assert op["blank"] is True and op["candidates"] == ["嗨", "在忙啥呢", "好久不见"]
    assert op["opener"] is True and op["judged"] is False and op["ranked"] is False
    assert op["trace"]["messages"] == []
    print("engine ok")
