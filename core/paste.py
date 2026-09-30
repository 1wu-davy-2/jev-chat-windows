# -*- coding: utf-8 -*-
"""把微信「复制」出来的聊天文本解析成消息行。

微信多选消息复制出来的形状是**三行一段、段间空行**：

    <昵称>
    2026年09月29日 13:58
    正文

     <下一条…>

昵称可能是看不见的字符（真机上见过 `ㅤㅤㅤㅤㅤ`，五个 U+3164 HANGUL FILLER），
所以**不能用 strip() 判空**——那玩意儿 strip 不掉 U+3164，`bool()` 判它是真，
在界面上显示出来却是一片白。`visible()` 就是给这种情况用的。

这里只做「文本 → 消息行」，不碰界面也不碰库。谁是我由调用方给（用户在下拉里选的），
因为同一个昵称在不同人眼里是不同的角色，脚本猜不出来。

自测：python core/paste.py
"""
from __future__ import annotations

import re
import unicodedata

# 时间戳。主要认微信那个「2026年09月29日 13:58」，顺带认几种常见的写法。
# 秒可有可无——微信复制出来的只有分。
_STAMP = re.compile(
    r"""^\s*(?:
        (?P<y>\d{4})\s*[年/\-.]\s*(?P<m>\d{1,2})\s*[月/\-.]\s*(?P<d>\d{1,2})\s*日?
        |(?P<md_m>\d{1,2})\s*[月/\-]\s*(?P<md_d>\d{1,2})\s*日?
    )?
    \s*(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?\s*$""",
    re.X,
)

# 解析结果里最多留多少条：粘一整年记录进来会把 deque 和界面都撑爆
MAX_ROWS = 2000

# 真空白的类别
_SPACE_CATS = {"Cf", "Cc", "Zs", "Zl", "Zp"}
# 看不见、但**类别不是空白**的那一族：Hangul filler 们，Unicode 把 U+3164 归成 Lo（字母），
# 所以 `"ㅤㅤㅤㅤㅤ".strip()` 拿它一点办法没有，`if not name.strip()` 也判不出它空。
# 这正是「看不见的昵称」的由来——真机上见过整个昵称就是五个 U+3164。
_FILLERS = frozenset("ㅤﾠᅟᅠ")


def is_blank(line: str) -> bool:
    """真空行（只含空白字符）。

    **故意不把 U+3164 那类算进来**——它们常被拿来做「看不见的昵称」，是**昵称行**不是分隔行。
    当成空行切掉的话整段就错位了：昵称被吃掉、时间戳顶到第一行，后面全解析不出来。"""
    return not any(unicodedata.category(ch) not in _SPACE_CATS for ch in line)


def visible(name: str) -> str:
    """给下拉框显示用的昵称：看不见的字符换成 `·`，免得选项栏里是一片白。

    比 is_blank 宽——空白类别**和** Hangul filler 都换掉。取名字的时候用不了它
    （换了就不是原来那个昵称了），只有显示走这儿。"""
    return "".join(
        "·" if (unicodedata.category(ch) in _SPACE_CATS or ch in _FILLERS) else ch
        for ch in name)


def needs_label(name: str) -> bool:
    """这个昵称是不是「认不出来」——一个可见字符都没有（全隐形，或者是空的）。

    认不出来就得让用户起个名，不然：① 下拉框里几个选项长得一模一样，挑不出「哪个是我」；
    ② 存进库、喂给模型的 `name` 是一串隐形字符，模型看了只会懵。
    空串也算——微信复制时偶尔给不出昵称，同样是「不知道这是谁」。"""
    return all((unicodedata.category(ch) in _SPACE_CATS or ch in _FILLERS)
               for ch in name)


def stamp_of(raw: str) -> str:
    """时间戳归一成 `MM-DD HH:MM`。微信给的是全的（2026年09月29日 13:58），
    但界面上气泡里挂全称太长，只留 「月-日 时:分」——导入的记录跨天，
    只留 HH:MM 的话跟今天刚说的话分不出来。认不出来就原样返回，不编一个。"""
    m = _STAMP.match(raw or "")
    if not m:
        return (raw or "").strip()
    hh, mm = int(m.group("H")), int(m.group("M"))
    if m.group("y"):
        mo, dd = int(m.group("m")), int(m.group("d"))
    elif m.group("md_m"):
        mo, dd = int(m.group("md_m")), int(m.group("md_d"))
    else:
        return f"{hh:02d}:{mm:02d}"
    return f"{mo:02d}-{dd:02d} {hh:02d}:{mm:02d}"


def parse(text: str) -> tuple[list[tuple[str, str, str]], list[str], int]:
    """微信复制出来的文本 → 消息行。

    返回 `(rows, names, skipped)`：
      - `rows`   `[(昵称, 正文, 时间戳)]`，**昵称是原样的**，谁是我由调用方定
      - `names`  出现过的昵称，按首次出现排序（喂给界面那个「哪个是我」的下拉）
      - `skipped` 认不出来的段落数（界面上要说一句，不能默默吞掉）

    按空行切段，每段要求「第一行是昵称、第二行是时间戳」，剩下的都是正文。
    正文里本来就可能带空行（微信复制时原样带出来），所以切歪了的那段**回填给上一条**
    当正文续行，而不是当成新消息——真发过一句带空行的消息，比格式错乱常见得多。
    """
    rows: list[tuple[str, str, str]] = []
    names: list[str] = []
    skipped = 0

    # 先按空行切段
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if is_blank(line):
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)

    for block in blocks:
        # 认得出「昵称 + 时间戳」开头才算一条新消息
        if len(block) >= 2 and _STAMP.match(block[1]):
            name = block[0].strip() or block[0]
            body = "\n".join(block[2:]).strip()
            if not body:
                skipped += 1
                continue
            if name not in names:
                names.append(name)
            rows.append((name, body, stamp_of(block[1])))
        elif rows:
            # 切歪了：多半是上一条正文里的空行把它劈开了，补回去
            rows[-1] = (rows[-1][0], rows[-1][1] + "\n" + "\n".join(block).strip(),
                        rows[-1][2])
        else:
            skipped += 1

    if len(rows) > MAX_ROWS:  # 留最近的，跟 chatlog.recent 一个口径
        rows = rows[-MAX_ROWS:]
    return rows, names, skipped


def to_messages(rows, me: str, keep_name: bool = False):
    """把昵称换成 who + 归一后的时间戳。

    返回 `[(who, text, name, stamp)]`——比项目里那个 `(who, text, name)` 多一个时间戳，
    因为调用方要拿它写 `chatlog`（库里那列叫 stamp）。少给一个的话那边还得回头去
    zip 原始 rows，白绕一圈。

    `me` 是用户在下拉里选的那个昵称，它 → `"me"`，其余一律 `"her"`（群聊里别人不止一个，
    都是对方）。name 只在群聊里有意义——单聊时用昵称反而不对：微信里对方昵称可能是
    一串空白字符（见 `visible`），模型看了会懵。"""
    out = []
    for name, text, stamp in rows:
        who = "me" if name == me else "her"
        out.append((who, text, name if (keep_name and who != "me") else None, stamp))
    return out


if __name__ == "__main__":
    # 自测：不联网、不碰库、不碰界面。跑完打印 ok。
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    BLANK = "ㅤ" * 5  # 真机上见过的那种空白昵称

    def check(cond, what):
        assert cond, what

    # --- 正常：两条一来一回 ---
    text = (
        f"{BLANK}\n2026年09月29日 13:58\n晚上吃啥\n\n"
        f"ya\n2026年09月29日 13:58\n火锅鸡吧 地锅鸡上次吃了有点咸\n\n"
        f"{BLANK}\n2026年09月29日 13:59\n行 我再点杯蜜雪冰城\n"
    )
    rows, names, skipped = parse(text)
    check(len(rows) == 3, f"应该解析出 3 条，实际 {len(rows)}")
    check(skipped == 0, f"不该有跳过的，实际 {skipped}")
    check(rows[0] == (BLANK, "晚上吃啥", "09-29 13:58"), f"第一条不对: {rows[0]!r}")
    check(rows[1][0] == "ya" and rows[1][1].startswith("火锅鸡吧"), f"第二条不对: {rows[1]!r}")
    check(rows[2][2] == "09-29 13:59", f"时间戳不对: {rows[2][2]!r}")
    check(names == [BLANK, "ya"], f"昵称顺序不对: {names!r}")

    # 空白昵称：U+3164 的类别是 Lo（字母），不是空白——所以 strip() 去不掉它，
    # 也**不能**被 is_blank 当成空行（当成空行的话昵称会被吃掉，整段解析全错位）
    check(BLANK.strip(), "前提：U+3164 strip 不掉（strip 之后还是原样）")
    check(len(_FILLERS) == 4, f"_FILLERS 应该是 4 个码位，实际 {len(_FILLERS)}")
    check(not is_blank(BLANK), "隐形昵称不是空行——这是整个解析的前提")
    check(is_blank(""), "真空行是空行")
    check(is_blank("   "), "空格行也是空行")
    check(not is_blank("晚上吃啥"), "有字的不算空行")
    check(visible(BLANK) == "·····", f"visible 不对: {visible(BLANK)!r}")
    check(visible("ya") == "ya", "可见字符不该被动")

    # 「认不出来」= 一个可见字符都没有。这种要让用户起个名（见 needs_label 的说明）
    check(needs_label(BLANK), "全隐形的昵称要起名")
    check(needs_label(""), "没昵称也要起名")
    check(needs_label("   "), "纯空格也要起名")
    check(needs_label("​ㅤ"), "零宽空格 + filler 混着也认不出来")
    check(not needs_label("ya"), "有字的不用起名")
    check(not needs_label(" 阿杰 "), "带空格的正常昵称不用起名")
    check(not needs_label("·"), "中点是个真字符，看得见，不该要求起名")

    # --- 正文带空行：劈开了要补回上一条，不能变成新消息 ---
    text = f"ya\n2026年09月29日 10:00\n第一段\n\n第二段接着说\n\n{BLANK}\n2026年09月29日 10:01\n收到\n"
    rows, names, skipped = parse(text)
    check(len(rows) == 2, f"带空行的正文应该并成 2 条，实际 {len(rows)}: {rows!r}")
    check("第二段接着说" in rows[0][1], f"正文续行没补回去: {rows[0]!r}")
    check(skipped == 0, f"不该有跳过的，实际 {skipped}")

    # --- 认不出的段：开头就是乱文本 ---
    rows, names, skipped = parse("这不是聊天记录\n随便一段话\n")
    check(rows == [] and skipped == 1, f"应该整段跳过，实际 rows={rows!r} skipped={skipped}")

    # --- 有昵称有时间但没正文：跳过，别造一条空消息 ---
    rows, names, skipped = parse("ya\n2026年09月29日 10:00\n\n")
    check(rows == [] and skipped == 1, f"空正文该跳过，实际 {rows!r} skipped={skipped}")

    # --- 时间戳几种写法 ---
    check(stamp_of("2026年09月29日 13:58") == "09-29 13:58", stamp_of("2026年09月29日 13:58"))
    check(stamp_of("2026/9/9 9:05") == "09-09 09:05", stamp_of("2026/9/9 9:05"))
    check(stamp_of("13:58") == "13:58", stamp_of("13:58"))
    check(stamp_of("2026年09月29日 13:58:07") == "09-29 13:58", "秒要能吃下去")
    check(stamp_of("不是时间") == "不是时间", "认不出来就原样留着，别编")

    # --- who 的映射 ---
    rows = [(BLANK, "晚上吃啥", "09-29 13:58"), ("ya", "火锅鸡吧", "09-29 13:58")]
    msgs = to_messages(rows, me="ya")
    check(msgs[0][0] == "her" and msgs[1][0] == "me", f"who 映射反了: {msgs!r}")
    check(msgs[0][2] is None and msgs[1][2] is None, "单聊不带 name")
    check(msgs[0][3] == "09-29 13:58", f"时间戳要一起带出来: {msgs[0]!r}")
    msgs = to_messages(rows, me="ya", keep_name=True)
    check(msgs[0][2] == BLANK and msgs[1][2] is None, f"群聊只给对方的 name: {msgs!r}")
    check(to_messages(rows, me=BLANK)[0][0] == "me", "选另一个当我也能反过来")

    # --- 图片/表情那类占位：原样留着，不特判 ---
    rows, _, _ = parse(f"ya\n2026年09月29日 17:15\n[图片] 微信图片_20260929171551_484.dat\n")
    check(rows[0][1].startswith("[图片]"), f"占位没保留: {rows[0]!r}")

    # --- 上限 ---
    big = "\n\n".join(f"ya\n2026年09月29日 10:00\n第 {i} 条" for i in range(MAX_ROWS + 50))
    rows, _, _ = parse(big)
    check(len(rows) == MAX_ROWS, f"该截到 {MAX_ROWS}，实际 {len(rows)}")
    check(rows[-1][1] == f"第 {MAX_ROWS + 49} 条", "截断要留最近的那些")

    # --- 空输入不炸 ---
    check(parse("") == ([], [], 0), "空输入")
    check(parse(None) == ([], [], 0), "None 也不炸")

    print("ok")
