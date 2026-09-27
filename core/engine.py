# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → Jev 判断 → 带着判断起草 3 条 → Jev 排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。
"""
from __future__ import annotations

try:
    from .draft import draft_candidates
    from .jev_client import JevError, ask
    from .questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text
except ImportError:
    from draft import draft_candidates
    from jev_client import JevError, ask
    from questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text

_REPLY_IDX = {"reply_a": 0, "reply_b": 1, "reply_c": 2}


def _add_usage(total: dict, one: dict | None) -> None:
    """两次 Jev 调用的 usage 相加（tokens、cost）；非数字的字段后来的盖掉前面的。"""
    for k, v in (one or {}).items():
        total[k] = total.get(k, 0) + v if isinstance(v, (int, float)) else v


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 10, provider: str = "deepseek",
            base_url: str | None = None, reply_to: str | None = None, scene: str = "",
            thinking: bool = False, jev_provider: str = "openrouter",
            jev_model: str | None = None, judge_path: str = "",
            thinking_style: str = "") -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（core.providers.DRAFT_PROVIDERS），base_url 只有自定义来源要传。
    jev_provider / jev_model: 判断和排序走哪家、哪个模型（core.providers.JEV_PROVIDERS）。
    judge_path: 判断走第三方中转时，那个口在中转上的路径（各家叫法不同，见 core/relay.py）。
    thinking_style: 中转认哪种思考开关（thinking / reasoning / none），只影响起草。
    reply_to: 群聊里指定回复给谁；None = 正常回复。
    scene: 这次要追加的场景正文（设置里选的场景模板，或用户改过的版本），只影响起草——
    Jev 判的是意图和紧张度，跟措辞无关。
    thinking: 起草时是否开思考模式，只影响起草，默认关。
    model / jev_model = None 用该来源的默认模型。

    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to}。
    scores 是每条候选的胜出概率（0~1），取自 best_reply.probabilities，取不到记 0.0。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。

    三段式（issue #4）：先让 Jev 答 7 道判断题，把判断当小抄喂给起草，最后 Jev 只排序。
    判断那次挂了就退回老路：盲起草 + 判断和排序一次问完，行为跟以前一样。usage 是两次之和。
    """
    state = build_state(messages, relationship, keep=context, reply_to=reply_to)
    usage: dict = {}
    answers: dict = {}
    judged = False
    trouble = ""  # 判断/排序为什么没跑成。带给界面显示，别让调用方只看到「生成失败」
    try:
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model,
                    base_url=base_url or "", path=judge_path)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        judged = True
    except JevError as e:
        trouble = str(e)  # 退回盲起草 + 老的一次合问；错误不打日志（里面可能带请求内容）

    candidates = draft_candidates(messages, relationship, provider=provider, model=model,
                                  base_url=base_url, timeout=timeout, keep=context,
                                  reply_to=reply_to, scene=scene,
                                  thinking=thinking, thinking_style=thinking_style,
                                  guidance=guidance_text(answers) if judged else None)
    if not candidates:  # 注入过滤可以把起草结果全扔掉；接着取 [0] 会 IndexError
        raise JevError("起草结果没有可用候选回复")

    questions = {} if judged else dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    if questions:
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
        answers = {**answers, **(second.get("answers") or {})}
        _add_usage(usage, second.get("usage"))

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
    print("engine ok")
