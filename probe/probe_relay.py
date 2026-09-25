# -*- coding: utf-8 -*-
"""中转探针：第三方中转站能不能替掉 OpenRouter / DeepSeek 官方。

问三件事（顺序就是从松到紧，第一个是标准口、第二个才是真正的未知数）：
  1. 起草那条路 {base}/v1/chat/completions + deepseek-flash —— 标准 OpenAI 口，中转一般都转
  2. 判断那条路 —— 各家路径不一样，挨个试（OpenRouter 是 /api/alpha/decisions，
     有的中转是 /v1/systemone）；路径不对时报错里会写它认哪个口
  3. jev-latest 在中转上能不能当普通 chat 模型调（接口没转的话，还有没有别的路可走）

    set RELAY_BASE_URL=https://api.xxx.com      (带不带 /v1、带不带 /chat/completions 都认)
    set RELAY_API_KEY=sk-xxx
    set PYTHONPATH=. && python probe/probe_relay.py
    set RELAY_JUDGE_PATH=/v1/systemone          # 只想试某一个路径时（先试它，再试其余候选）
    set PYTHONPATH=. && python probe/probe_relay.py --control   # 再拿真 OpenRouter 跑一遍对照

key 只从环境变量读，不落文件、不进日志（打出来的 body 一律先脱敏）。第 2 步如果通，会把 8 道题的答案打出来，
跟 probe_laya.py 一样的格式——顺带看一眼这家的 jev 是不是真 jev。
"""
import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")  # 中文 Windows 控制台是 GBK

from core.questions import JUDGE_QUESTIONS, build_rank_question, build_state
from core.relay import chat_url, judge_url

OPENROUTER_DECISIONS = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "typesafe/jev-1.13"
# 判断口的候选路径：第一个是 OpenRouter 官方的，第二个是 PackyCode 那类中转的 typesafe 通道叫法
JUDGE_PATHS = ("/api/alpha/decisions", "/v1/systemone")
TIMEOUT = 30

# 跟 probe_laya.py 同一段对话和候选，好横向比
MSGS = [("her", "你今天是不是又忘了我跟你说过什么？"), ("me", "记得，你先别提示我，让我自己说。"),
        ("her", "那你说。"), ("me", "等一下，我想说完整一点。"), ("her", "你最好是。")]
CANDS = ["我记得，是上周说的那件事，我先去翻一下聊天记录确认", "对不起我真忘了", "别生气嘛，你提示我一下"]
EXPECT = "期望 true_intent≈confirm_you_care, best_action≈check_history, danger 中高, best_reply≈reply_a"


def redact(text: str, key: str) -> str:
    return text.replace(key, "[REDACTED]") if key and key in text else text


def post(url: str, key: str, body: dict) -> tuple[int | None, str]:
    """→ (状态码或 None, 文本)。网络层的错也压成文本返回，探针不抛异常。"""
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json; charset=utf-8",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # 超时/DNS/证书，都算「连不上」
        return None, f"{type(exc).__name__}: {exc}"


def show(title: str, url: str, status: int | None, body: str, key: str) -> dict | None:
    """打一行结果；body 是 JSON 就返回解析后的 dict。"""
    print(f"\n--- {title}\n    {url}")
    if status is None:
        print(f"    连不上：{redact(body, key)[:200]}")
        return None
    print(f"    HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError:
        print(f"    {redact(body, key)[:300]}")
        return None
    if status != 200:
        print(f"    {redact(json.dumps(data, ensure_ascii=False), key)[:300]}")
        return None
    return data


def fmt(ans: dict) -> str:
    """跟 probe_laya.py 一个格式，方便横向比答案质量。"""
    if "noul" in ans:
        return f"{ans.get('noul', 0):.2f}"
    if "score" in ans:
        return f"{ans.get('score', 0):.1f}/9 (conf {ans.get('confidence', 0):.2f})"
    probs = ans.get("probabilities") or {}
    top = sorted(probs.items(), key=lambda kv: -kv[1])[:3]
    return (f"{ans.get('choice')} (conf {ans.get('confidence', 0):.2f})  "
            + " ".join(f"{k}={v:.2f}" for k, v in top))


def judge(url: str, key: str, model: str) -> tuple[bool, str | None]:
    """跑一遍 8 道题，打答案。→ (通没通, 失败原因)"""
    questions = dict(JUDGE_QUESTIONS)
    questions.update(build_rank_question(CANDS))
    status, body = post(url, key, {"model": model, "state": build_state(MSGS, "romantic partners"),
                                   "questions": questions})
    data = show(f"判断 {model}", url, status, body, key)
    answers = (data or {}).get("answers")
    if not answers:
        return False, f"HTTP {status}" if status is not None else "连不上"
    print(f"    {EXPECT}")
    for q, ans in answers.items():
        print(f"      {q:<18} {fmt(ans) if isinstance(ans, dict) else ans}")
    return True, None


def main() -> int:
    ap = argparse.ArgumentParser(description="第三方中转能不能替掉 OpenRouter / DeepSeek 官方")
    ap.add_argument("--control", action="store_true", help="再拿真 OpenRouter 跑一遍判断题做对照")
    ap.add_argument("--draft-model", default=os.environ.get("RELAY_DRAFT_MODEL", "deepseek-flash"))
    ap.add_argument("--jev-model", default=os.environ.get("RELAY_JEV_MODEL", "jev-latest"))
    args = ap.parse_args()

    base = os.environ.get("RELAY_BASE_URL", "").strip()
    key = os.environ.get("RELAY_API_KEY", "").strip()
    if not base or not key:
        print("先设 RELAY_BASE_URL 和 RELAY_API_KEY 两个环境变量（key 只读环境变量，别写进文件）")
        return 2
    chat = chat_url(base)

    # 1 起草：标准 OpenAI 口
    status, body = post(chat, key, {
        "model": args.draft_model,
        "messages": [
            {"role": "system", "content": "你是「me」本人，正在微信里打字。只输出一个 JSON 数组，恰好 3 个字符串。"},
            {"role": "user", "content": "对话原文：\nher: 周六想吃火锅，你有空吗？\n\n输出恰好 3 条候选，JSON 数组，每条一句。"}],
        "temperature": 1.2, "max_tokens": 400, "stream": False})
    data = show(f"1. 起草 {args.draft_model}（/v1/chat/completions）", chat, status, body, key)
    draft_ok = bool((data or {}).get("choices"))
    if draft_ok:
        print(f"    {str(data['choices'][0].get('message', {}).get('content', ''))[:200]}")

    # 2 判断：各家路径不一样，挨个试。这才是卡点
    first = os.environ.get("RELAY_JUDGE_PATH", "").strip()
    paths = [p for p in ([first] if first else []) + list(JUDGE_PATHS) if p]
    judge_ok, why, good_path = False, "", ""
    for i, path in enumerate(dict.fromkeys(paths)):
        if i:
            print(f"    （换下一个路径试）")
        judge_ok, why = judge(judge_url(base, path), key, args.jev_model)
        if judge_ok:
            good_path = path
            break

    # 3 jev 当普通 chat 模型（接口没转的话，看看模型本身在不在）
    status, body = post(chat, key, {"model": args.jev_model, "max_tokens": 20, "stream": False,
                                    "messages": [{"role": "user", "content": "只回两个字：在的"}]})
    data = show(f"3. {args.jev_model} 当普通 chat 模型调", chat, status, body, key)
    chat_ok = bool((data or {}).get("choices"))

    if args.control:
        okey = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if okey:
            judge(OPENROUTER_DECISIONS, okey, OPENROUTER_MODEL)
        else:
            print("\n（--control 要 OPENROUTER_API_KEY，没设就跳过）")

    print("\n=== 结论 ===")
    print(f"  起草走中转：{'可以，换 base_url + key 就行' if draft_ok else '不行'}")
    if judge_ok:
        print(f"  判断走中转：可以，接口路径是 {good_path}")
        print(f"    → 设置页把「判断 · Jev」的来源选成「第三方中转」，「判断接口路径」填 {good_path}")
    else:
        print(f"  判断走中转：不行（{why}）—— 试过的路径：{'、'.join(paths)}")
        print(f"  jev 模型在中转上：{'在，但只能按它自己的协议调' if chat_ok else '按 chat 口调不到'}")
    return 0 if draft_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
