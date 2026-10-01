# -*- coding: utf-8 -*-
"""对方的资料：性别、星座，外加一小段自由补充。**按会话**记，起草时拼进提示。

为什么要有它：「关系」（core/relations.py）回答的是「你俩是什么关系」，正文是一套通用口径。
可同一个关系里，对面是男是女、做什么的、最近在忙什么，写出来的话该不一样——「朋友」这一型
对哥们儿和对一个刚认识的女生，称呼和分寸差得远，而这些光看聊天记录不一定读得出来（记录还少
的时候更读不出来）。

三条边界：

1. **只喂起草**（回复和开场白都喂），跟「场景模板」正文一个口径——它管称呼、语气、热络到什么
   程度。Jev 判断那一步不喂：那七道题问的是对方最新那条的意图和紧张度，跟这个人是谁无关。
2. **拼出来的那段里明说「别点破」**。资料是背景、不是这一轮要说的事；不打招呼的话模型会写出
   「听说你是天蝎座的？」这种没头没脑的话，比不给资料还糟。
3. 存哪儿、怎么读写是 app/settings.py 的事（现在在 config.json 的 `profiles` 键里，跟
   relations.chats / chat_scenes / chat_remarks 并排）。这个模块只管**词汇表**和**拼提示那段字**，
   平台无关、不碰磁盘、不认识 config.json。

用户填的时候还有个「插入模板」：把 TEMPLATE 塞进那个框里照着填。模板行原样进提示是噪音，
所以 prompt_text() 会把「跟模板里的某一行一模一样」的行丢掉（见 _note_lines）。
"""
from __future__ import annotations

# (键, 界面上的名字)。键写进 config.json，**改键等于把老配置读丢**，所以键取稳定的英文、
# 界面上才用中文名。空串 = 没填（界面上写「不详」，拼提示时整项不加）。
GENDERS = (("", "不详"), ("f", "女"), ("m", "男"))

# 十二星座按月份顺序（白羊从 3 月下旬起）。跟性别一样：键稳定、名字随便改。
ZODIACS = (
    ("", "不详"),
    ("aries", "白羊座"), ("taurus", "金牛座"), ("gemini", "双子座"),
    ("cancer", "巨蟹座"), ("leo", "狮子座"), ("virgo", "处女座"),
    ("libra", "天秤座"), ("scorpio", "天蝎座"), ("sagittarius", "射手座"),
    ("capricorn", "摩羯座"), ("aquarius", "水瓶座"), ("pisces", "双鱼座"),
)

# 「补充信息」最长多少字，跟一段关系正文一个量级（LIMIT 都是 1000）。config.json 是**整份重写**
# 的，这一格不该长到拖慢每次保存；真写不下就该拆成两条关系、而不是把资料堆成一篇作文。
NOTE_LIMIT = 1000

# 「插入模板」塞进那个框里的待填文本。性别和星座有下拉，所以模板只管剩下那些「光看记录看不出
# 来」的事——怎么称呼、多大、在哪儿、最近在忙什么、什么不能提。
TEMPLATE = """- 怎么称呼 TA：
- 大概多大 / 生日：
- 在哪个城市、做什么的：
- 最近在忙什么、关心什么：
- 别碰的话题：
- 别的（口癖、习惯、你俩之间的老梗）："""

_GENDER_KEYS = frozenset(k for k, _ in GENDERS)
_ZODIAC_KEYS = frozenset(k for k, _ in ZODIACS)
# 模板里那几行原样（判「这一行一个字没填」用，见 _note_lines）
_PLACEHOLDER = frozenset(ln.strip() for ln in TEMPLATE.splitlines())


def gender_name(key: str) -> str:
    """键 → 界面上的名字。空/None 是「没填」→「不详」；**认不出的键返回空串**，不是「不详」
    ——调用方要靠这个区分「没填」和「这个键根本不认识」，认不出也返回「不详」的话脏数据
    会一路混到提示里。"""
    return next((name for k, name in GENDERS if k == str(key or "")), "")


def zodiac_name(key: str) -> str:
    """同上。"""
    return next((name for k, name in ZODIACS if k == str(key or "")), "")


def clean(one) -> dict:
    """一份资料收干净：认不出的性别/星座当没填、补充信息截到 NOTE_LIMIT。

    **三个字段全空就返回 {}**——上层拿空 dict 当「这个会话没维护过」，不留一个「三项都是空串」
    的壳子（界面上删干净了就该真的删掉那一格）。

    「只点了插入模板、一个字没填」也当没写：留着一份纯占位行的话，这一格会一直占着下拉里的
    位置、看着像有资料，拼提示时又什么都不加（见 _note_lines）。**只填了几行的照留**——
    占位行是用户的编辑底稿，删了他回头还得重插一遍。"""
    one = one if isinstance(one, dict) else {}
    g = str(one.get("gender") or "").strip()
    z = str(one.get("zodiac") or "").strip()
    note = str(one.get("note") or "").strip()[:NOTE_LIMIT]
    out = {"gender": g if g in _GENDER_KEYS else "",
           "zodiac": z if z in _ZODIAC_KEYS else "",
           "note": note if _note_lines(note) else ""}
    return out if any(out.values()) else {}


def _note_lines(note) -> list:
    """补充信息按行拆开，顺手丢掉「模板里那一行、一个字没填」的占位行。

    判据是**整行跟模板里的某一行一模一样**：用户自己写的「- 别碰的话题：工作」不会被误伤
    （那一行不等于模板那行），而只点了「插入模板」、什么都没填的时候，提示里不会平白多出
    六行没用的标签。"""
    out = []
    for line in str(note or "").splitlines():
        line = line.strip()
        if line and line not in _PLACEHOLDER:
            out.append(line)
    return out


def prompt_text(one) -> str:
    """一份资料 → 拼进起草提示的那一段。**空资料返回空串**（提示里一个字都不加）。

    性别和星座并成一行（「女，天蝎座」），补充信息按用户排的版原样贴上去。返回的这段自带
    抬头那行，调用方直接接在 `relationship:` 那句后面就行（见 core/draft._prompt）。"""
    one = clean(one)
    if not one:  # 空资料 / 认不出的键全被 clean 丢光了：一个字都不加
        return ""
    # 空串在 clean 之后就是「没填」：别再拿它去问名字——那会问出一个「不详」来
    bits = [name_of(key) for key, name_of in ((one.get("gender"), gender_name),
                                              (one.get("zodiac"), zodiac_name)) if key]
    lines = (["- " + "，".join(bits)] if bits else []) + _note_lines(one.get("note"))
    if not lines:
        return ""
    return ("对方是谁（写的时候照着来：称呼、语气、热络到什么程度；这是背景，"
            "别在话里点破你知道这些）:\n" + "\n".join(lines))


if __name__ == "__main__":
    # 词汇表：认得出的给中文名，认不出的一律空串（脏数据不许混进提示）
    assert gender_name("f") == "女" and gender_name("m") == "男" and gender_name("") == "不详"
    assert gender_name(None) == "不详", "没填 = 不详；认不出才是空串，两件事别混"
    assert gender_name("女") == "" and gender_name("x") == ""
    assert zodiac_name("scorpio") == "天蝎座" and zodiac_name("") == "不详"
    assert zodiac_name("天蝎座") == "" and zodiac_name("13") == ""
    assert len(ZODIACS) == 13 and len(GENDERS) == 3  # 各自都带一个「不详」

    # 收脏数据：认不出的键当没填、补充信息截断、三项全空就整个丢掉
    assert clean(None) == {} and clean("不是 dict") == {} and clean([]) == {}
    assert clean({"gender": "f", "zodiac": "", "note": "   "}) == \
        {"gender": "f", "zodiac": "", "note": ""}
    assert clean({"gender": "外星人", "zodiac": "蛇夫座"}) == {}
    assert clean({"note": "x" * 2000})["note"] == "x" * NOTE_LIMIT
    assert clean({"note": "  补一句  "})["note"] == "补一句", "两头空白要 strip"
    # 只点了「插入模板」、一个字没填 = 没写（不然下拉里会一直挂着一个空壳会话）
    assert clean({"note": TEMPLATE}) == {} and clean({"note": TEMPLATE + "\n\n"}) == {}
    # 填过一行就照留，占位行也一起留着——那是用户回填的底稿，删了他得重插一遍
    half = clean({"note": "- 怎么称呼 TA：小金\n" + TEMPLATE.splitlines()[-1]})
    assert half["note"].splitlines() == ["- 怎么称呼 TA：小金", TEMPLATE.splitlines()[-1]]

    # 拼提示：空资料一个字都不加
    assert prompt_text({}) == "" and prompt_text(None) == "" and prompt_text({"gender": "z"}) == ""
    # 只点了「插入模板」、什么都没填：六行占位一个都不许进提示
    assert prompt_text({"note": TEMPLATE}) == "", "占位行不算资料"
    assert prompt_text({"note": TEMPLATE + "\n- 别碰的话题：工作"}) \
        .endswith("- 别碰的话题：工作")

    got = prompt_text({"gender": "f", "zodiac": "scorpio"})
    assert got.startswith("对方是谁（") and got.endswith(":\n- 女，天蝎座"), got
    assert "别在话里点破" in got, "不打招呼模型会写「听说你是天蝎座的？」"
    # 只有性别（或只有星座）时不摆一个空的一半
    assert prompt_text({"gender": "m"}).endswith("- 男")
    assert prompt_text({"zodiac": "leo"}).endswith("- 狮子座")
    # 补充信息按用户排的版原样贴，自己写的行不会被当成占位行丢掉
    mixed = prompt_text({"gender": "f", "note": "- 怎么称呼 TA：小坤\n\n- 同事，别聊加班"})
    assert mixed.splitlines() == ["对方是谁（写的时候照着来：称呼、语气、热络到什么程度；"
                                "这是背景，别在话里点破你知道这些）:",
                                 "- 女", "- 怎么称呼 TA：小坤", "- 同事，别聊加班"], mixed
    print("profile ok")
