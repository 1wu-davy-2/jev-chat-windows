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
from collections import Counter
from datetime import datetime

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

# 非文本消息的占位。微信复制出来就是这些方括号前缀，本身**不是谁说的话**——真机上
# 一段 63 条的导入里有 14 条是这种，全进了上下文，模型会以为对方说过一句「[语音] 5"」。
_MEDIA = re.compile(
    r"^\s*\[(?:语音|图片|视频|动画表情|表情|文件|转账|红包|链接|位置|名片|音乐"
    r"|小程序|接龙|聊天记录|视频号|公众号|卡券|收藏|游戏|合并转发)\]")

# 只认「[语音] N"」，用来把它换成跟实时采集同样的「🔊 语音消息 N"」占位。
# 数字后面的引号可有可无——微信复制出来是有的，手打可能没有。
_VOICE = re.compile(r'^\s*\[语音\]\s*(?P<dur>\d+(?:\.\d+)?)\s*["”″]?\s*$')

# 排序用的两种时间戳形状。全的要带日期，只有时分的当「今天」（见 stamp_key）
_STAMP_FULL = re.compile(r"^\s*(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s*$")
_STAMP_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

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


def is_media(text: str) -> bool:
    """这条是不是非文本消息的占位（语音/图片/表情/转账/文件…）。

    判据是**开头的方括号前缀**，不是「正文里出现过方括号」——微信复制出来的格式就是这样。
    「[图片] 微信图片_20260929171551_130.dat」这种带文件名的也算，那串文件名喂给模型
    纯属噪音。宁可多滤一条，也别漏一条进去。"""
    return bool(_MEDIA.match(text or ""))


def voice_duration(text: str) -> str:
    """`[语音] 5"` → `5"`；不是语音消息、或读不出数字就返回空串。

    归一成跟 `main._voice_label` 一样的 `N"`，这样导入的这条占位能跟**之后**真转出来的
    文字对上号、并成一条（见 `Overlay._merge_voice`）。对不上就只是一条占位气泡。"""
    m = _VOICE.match(text or "")
    return f'{m.group("dur")}"' if m else ""


def voice_placeholder(text: str) -> str:
    """`[语音] 5"` → `🔊 语音消息 5"`；不是语音消息就原样返回。

    换成跟实时采集**同一个字串**，`Overlay._merge_voice` 才认得出来——不换的话它就是一条
    普通正文，之后真转了文字会跟它并排摆着，看着像同一句话说了两遍。"""
    dur = voice_duration(text)
    return f"🔊 语音消息 {dur}" if dur else text


def flat(text: str) -> str:
    """判重用的正文指纹：所有空白都去掉。

    OCR 读出来的那份常丢空格（真机：导入是「在便利店门口呢 你车停哪边了」，同一句话实时
    采集到的是「在便利店门口呢你车停哪边了」）。不归一的话同一条会被当成两条。"""
    return re.sub(r"\s+", "", text or "")


def merge_rows(old, new):
    """把导入的新行并进已有记录：去重、按时间戳排。返回 `(合并结果, 跳过几条重复)`。

    两边都是 `(who, 正文, name, 时间戳, voice)` 五元组——跟 `Overlay.feeds` 和
    `chatlog.replace_chat` 要的形状一致，省得调用方来回转换。

    **去重按多重集**：两边都有的那条跳过，但按出现次数一对一抵消——真重复说两遍的「行」
    不会被误删，而 OCR 版和导入版的同一条会被判重（见 `flat`）。

    `sort` 是稳定的，所以同一分钟内的相对先后保持原样——屏幕上那批本来就是按真实先后
    采集来的，别给打乱了。"""
    seen = Counter((w, flat(t)) for w, t, *_ in old)
    kept, dup = [], 0
    for row in new:
        key = (row[0], flat(row[1]))
        if seen.get(key, 0) > 0:
            seen[key] -= 1
            dup += 1
            continue
        kept.append(tuple(row))
    merged = [tuple(r) for r in old] + kept
    merged.sort(key=lambda r: stamp_key(r[3]))
    return merged, dup


def stamp_key(stamp: str, today: datetime | None = None):
    """排序键：`(月, 日, 时, 分)`。

    只有 `时:分` 的按「今天」算——那是加日期**之前**存下的老记录，补不回来了，就近当今天
    总比不排强。认不出来的给 `(0, 0, 0, 0)`，一律排在最前面。

    **不带年份**是故意的：记录跨度就是这几天，跨年那一下会排错，为一年一次的边界多存一个
    字段不划算。"""
    t = today or datetime.now()
    m = _STAMP_FULL.match(stamp or "")
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    m = _STAMP_CLOCK.match(stamp or "")
    if m:
        return (t.month, t.day, int(m.group(1)), int(m.group(2)))
    return (0, 0, 0, 0)


def parse(text: str, skip_media: bool = False):
    """微信复制出来的文本 → 消息行。

    返回 `(rows, names, skipped, media)`：
      - `rows`   `[(昵称, 正文, 时间戳)]`，**昵称是原样的**，谁是我由调用方定
      - `names`  出现过的昵称，按首次出现排序（喂给界面那个「哪个是我」的下拉）
      - `skipped` 认不出来的段落数（界面上要说一句，不能默默吞掉）
      - `media`  被 `skip_media` 滤掉的非文本消息条数（同上，不能默默吞掉）

    `skip_media=True` 时语音/图片/表情/转账/文件那类**不进 rows**，但 `names` 照收——
    「哪个是我」问的是身份，不是「哪些消息留下来了」，少一个选项会让用户找不到自己。

    按空行切段，每段要求「第一行是昵称、第二行是时间戳」，剩下的都是正文。
    正文里本来就可能带空行（微信复制时原样带出来），所以切歪了的那段**回填给上一条**
    当正文续行，而不是当成新消息——真发过一句带空行的消息，比格式错乱常见得多。
    """
    rows: list[tuple[str, str, str]] = []
    names: list[str] = []
    skipped = 0
    media = 0

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
            if name not in names:  # 滤媒体之前先收名字，见 docstring
                names.append(name)
            if skip_media and is_media(body):
                media += 1
                continue
            rows.append((name, body, stamp_of(block[1])))
        elif rows:
            # 切歪了：多半是上一条正文里的空行把它劈开了，补回去
            rows[-1] = (rows[-1][0], rows[-1][1] + "\n" + "\n".join(block).strip(),
                        rows[-1][2])
        else:
            skipped += 1

    if len(rows) > MAX_ROWS:  # 留最近的，跟 chatlog.recent 一个口径
        rows = rows[-MAX_ROWS:]
    return rows, names, skipped, media


def to_messages(rows, me, keep_name: bool = False):
    """把昵称换成 who + 归一后的时间戳。

    返回 `[(who, text, name, stamp)]`——比项目里那个 `(who, text, name)` 多一个时间戳，
    因为调用方要拿它写 `chatlog`（库里那列叫 stamp）。少给一个的话那边还得回头去
    zip 原始 rows，白绕一圈。

    `me` 是**你本人那些昵称**：单个字符串，或者一组。**是多个**，因为同一个人在不同场合
    昵称会变——真机上一个人既是那串隐形昵称、又是转发记录里的 `ZBK`，只认一个的话另一个
    会被当成对方，语气就学反了。给的这些 → `"me"`，其余一律 `"her"`（群聊里别人不止一个，
    都是对方）。

    name 只在群聊里有意义——单聊时用昵称反而不对：微信里对方昵称可能是一串空白字符
    （见 `visible`），模型看了会懵。"""
    me_set = {me} if isinstance(me, str) else set(me or ())
    out = []
    for name, text, stamp in rows:
        who = "me" if name in me_set else "her"
        out.append((who, text, name if (keep_name and who != "me") else None, stamp))
    return out


def who_conflict(old, new):
    """这次导入的「哪个是我」是不是跟已有记录**反了**。返回 `(重合条数, 发言方相反的条数)`。

    判据是「两边都有、但一个说是你说的、另一个说是他说的」。真人不会换身份，所以反过来的
    比例一高就说明这次选错了——真机上踩过：全库 176 条里同一个昵称同时出现在 `me` 和
    `her` 两边，语气模仿直接学反。

    比对用 `flat(正文)`：OCR 那份和导入那份的空格不一样（见 `flat`）。同一句正文对应多个
    昵称时（群聊），只要这次判的 who **在**已有那组里就不算反。"""
    was = {}
    for w, t, *_ in old:
        was.setdefault(flat(t), set()).add(w)
    both = opp = 0
    for w, t, *_ in new:
        seen = was.get(flat(t))
        if not seen:
            continue
        both += 1
        if w not in seen:
            opp += 1
    return both, opp


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
    rows, names, skipped, _ = parse(text)
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
    rows, names, skipped, _ = parse(text)
    check(len(rows) == 2, f"带空行的正文应该并成 2 条，实际 {len(rows)}: {rows!r}")
    check("第二段接着说" in rows[0][1], f"正文续行没补回去: {rows[0]!r}")
    check(skipped == 0, f"不该有跳过的，实际 {skipped}")

    # --- 认不出的段：开头就是乱文本 ---
    rows, names, skipped, _ = parse("这不是聊天记录\n随便一段话\n")
    check(rows == [] and skipped == 1, f"应该整段跳过，实际 rows={rows!r} skipped={skipped}")

    # --- 有昵称有时间但没正文：跳过，别造一条空消息 ---
    rows, names, skipped, _ = parse("ya\n2026年09月29日 10:00\n\n")
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
    rows, _, _, _ = parse(f"ya\n2026年09月29日 17:15\n[图片] 微信图片_20260929171551_484.dat\n")
    check(rows[0][1].startswith("[图片]"), f"占位没保留: {rows[0]!r}")

    # --- 上限 ---
    big = "\n\n".join(f"ya\n2026年09月29日 10:00\n第 {i} 条" for i in range(MAX_ROWS + 50))
    rows, _, _, _ = parse(big)
    check(len(rows) == MAX_ROWS, f"该截到 {MAX_ROWS}，实际 {len(rows)}")
    check(rows[-1][1] == f"第 {MAX_ROWS + 49} 条", "截断要留最近的那些")

    # --- 空输入不炸 ---
    check(parse("") == ([], [], 0, 0), "空输入")
    check(parse(None) == ([], [], 0, 0), "None 也不炸")

    # --- 非文本消息：认得出来，且能让调用方滤掉 ---
    # 判据是**开头的**方括号，不是「正文里有方括号」
    for t in ('[语音] 5"', "[图片] 微信图片_20260929171551_130.dat", "[动画表情]",
              "[转账]", "[文件] index.html", "[视频]", "[位置]", "[小程序] x"):
        check(is_media(t), f"该判成非文本: {t!r}")
    for t in ("晚上吃啥", "", None, "他说[图片]发你了", "【图片】中文方括号", "[不是媒体]"):
        check(not is_media(t), f"这些都不是非文本消息: {t!r}")
    check(is_media("  [链接] 也算"), "前导空格不影响判断")

    # 语音时长归一成 N"，好跟之后真转出来的文字对上号
    check(voice_duration('[语音] 5"') == '5"', voice_duration('[语音] 5"'))
    check(voice_duration("[语音] 12″") == '12"', voice_duration("[语音] 12″"))
    check(voice_duration("[语音] 3") == '3"', "没有引号也要认")
    check(voice_duration("[语音]") == "", "光有 [语音] 没有时长，给空串")
    check(voice_duration("[图片] a.png") == "", "不是语音就给空串")
    check(voice_duration("") == "" and voice_duration(None) == "", "空输入不炸")

    # --- skip_media：真机那段 63 条里 14 条是占位 ---
    text = ('ya\n2026年09月27日 10:45\n[语音] 5"\n\n'
            f'{BLANK}\n2026年09月27日 10:46\n在便利店门口呢\n\n'
            f'ya\n2026年09月27日 18:19\n[图片] 微信图片_2026.dat\n\n'
            f'{BLANK}\n2026年09月27日 18:20\n收到\n')
    rows, names, skipped, media = parse(text, skip_media=True)
    check(len(rows) == 2 and media == 2, f"该滤掉 2 条媒体，rows={len(rows)} media={media}")
    check([r[1] for r in rows] == ["在便利店门口呢", "收到"], f"留下的不对: {rows!r}")
    check(names == ["ya", BLANK], f"滤掉的昵称也要留在下拉里: {names!r}")
    # 不滤时一条不少
    rows, _, _, media = parse(text)
    check(len(rows) == 4 and media == 0, f"不滤就该全留: rows={len(rows)}")

    # --- stamp_key：排序用 ---
    check(stamp_key("09-27 10:45") < stamp_key("09-30 09:37"), "跨天要能排对")
    check(stamp_key("09-27 10:45") < stamp_key("09-27 12:40"), "同天比时刻")
    check(stamp_key("垃圾") == (0, 0, 0, 0), "认不出排最前")
    # 只有时分的（加日期之前的老数据）当今天——跟今天的全时间戳同月同日
    now = datetime(2026, 10, 1, 20, 0)
    check(stamp_key("16:50", now) == (10, 1, 16, 50), stamp_key("16:50", now))
    check(stamp_key("16:50", now) > stamp_key("09-30 09:37", now),
          "老数据的时分当今天（10-01），该排在 09-30 后面")

    # --- 语音占位：换成跟实时采集同一个字串，之后转文字才并得上 ---
    check(voice_placeholder('[语音] 5"') == '🔊 语音消息 5"', voice_placeholder('[语音] 5"'))
    check(voice_placeholder("晚上吃啥") == "晚上吃啥", "不是语音就原样")
    check(voice_placeholder("[图片] a.png") == "[图片] a.png", "别的媒体不动")

    # --- merge_rows：真机那段「实时采到的 + 导入的同一段对话」 ---
    # 实时那份 OCR 把空格吃掉了，导入那份还留着；时间戳一个是今天的、一个是 09-27
    old = [("me", "在便利店门口呢你车停哪边了", "", "10-01 16:50", ""),
           ("her", "在楼下，在楼下。", "", "10-01 16:50", '2"')]
    new = [("me", "在便利店门口呢 你车停哪边了", "", "09-27 10:46", ""),   # 跟 old[0] 是同一条
           ("her", '[语音] 2"', "", "09-27 12:40", ""),                     # 占位，old 里没有
           ("me", "早上9点半吧", "", "09-30 23:26", "")]
    merged, dup = merge_rows(old, new)
    check(dup == 1, f"该判出 1 条重复（空格差异要能认出来），实际 {dup}")
    check(len(merged) == 4, f"2 + 3 - 1 = 4，实际 {len(merged)}")
    check([r[3] for r in merged] == ["09-27 12:40", "09-30 23:26", "10-01 16:50", "10-01 16:50"],
          f"该按时间正序排，实际 {[r[3] for r in merged]}")
    check(merged[-1][1] == "在楼下，在楼下。", "同一分钟的相对顺序要保住（稳定排序）")

    # 真重复说两遍的不该被误删：old 里两条「行」，new 里两条「行」，一条都不该少
    old2 = [("me", "行", "", "10-01 16:50", ""), ("me", "行", "", "10-01 16:51", "")]
    new2 = [("me", "行", "", "09-29 13:58", ""), ("me", "行", "", "09-29 13:58", "")]
    merged2, dup2 = merge_rows(old2, new2)
    check(dup2 == 2 and len(merged2) == 2, f"两条对两条该全判重，实际 dup={dup2} n={len(merged2)}")

    # old 空（勾了「替换」）就是纯排序
    merged3, dup3 = merge_rows([], new)
    check(dup3 == 0 and [r[3] for r in merged3] == ["09-27 10:46", "09-27 12:40", "09-30 23:26"],
          f"替换模式只排序不去重: {[r[3] for r in merged3]}")

    # --- 一个人可以有多个昵称 ---
    rows = [(BLANK, "在便利店门口呢", "09-27 10:46"),
            ("ZBK", "我等会去健身", "09-23 11:27"),
            ("ya", "行，几点", "09-27 23:26")]
    check([m[0] for m in to_messages(rows, "ya")] == ["her", "her", "me"], "单个字符串仍然吃")
    check([m[0] for m in to_messages(rows, [BLANK, "ZBK"])] == ["me", "me", "her"],
          "隐形昵称和 ZBK 都是本人（真机上的情况）")
    check(to_messages(rows, [])[0][0] == "her", "一个都不勾就全是对方")
    check(to_messages(rows, None)[0][0] == "her", "None 也不炸")

    # --- who_conflict：勾反了要发现 ---
    old = [("me", "在便利店门口呢你车停哪边了", "", "10-01 16:50", ""),   # OCR 版（无空格）
           ("her", "在楼下，在楼下。", "", "10-01 16:50", '2"')]
    # 勾对了：同一句判成 me，跟 old 一致
    ok_new = [("me", "在便利店门口呢 你车停哪边了", "", "09-27 10:46", ""),
              ("her", "行，几点", "", "09-27 23:26", "")]
    both, opp = who_conflict(old, ok_new)
    check((both, opp) == (1, 0), f"勾对时不该有反向: {(both, opp)}")
    # 勾反了：同一句判成 her
    bad_new = [("her", "在便利店门口呢 你车停哪边了", "", "09-27 10:46", "")]
    both, opp = who_conflict(old, bad_new)
    check((both, opp) == (1, 1), f"勾反了要报出来: {(both, opp)}")
    check(who_conflict([], bad_new) == (0, 0), "没有旧记录就无从谈反不反")
    # 群聊里同一句正文对应多个昵称：判的 who 在已有那组里就不算反
    old_multi = [("her", "收到", "", "10-01 16:50", ""), ("her", "收到", "", "10-01 16:51", "")]
    check(who_conflict(old_multi, [("her", "收到", "", "09-27 10:00", "")]) == (1, 0),
          "who 对得上就不算反")

    print("ok")
