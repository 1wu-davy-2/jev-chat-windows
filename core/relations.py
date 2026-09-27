# -*- coding: utf-8 -*-
"""关系：这个会话里的人是谁，以及按这个关系该**怎么说话**。

关系是**按会话**记的（config.json 的 `relations.chats`），没记过的会话用 `relations.default`。
它管两样东西：
1. 拼进起草提示的 `relationship:` 那一句——喂给模型的「你们是什么关系」。
2. 这个人默认的说话方式（下面 PRESETS 里的正文）。

跟 `core/styles.py` 的「场景模板」是两件事，别合并：关系是**长期的**（这个人是朋友还是客户，
定下来基本不变），场景模板是**这一次的**——想给某个会话临时换个口吻，才在面板上挑一型
覆盖掉关系的正文（见 styles.resolve 的第二个参数）。

内置这六型各有一份默认正文，设置页那个大框改的就是它；改过的按类型分开存在
`relations.texts` 里（只存跟内置不一样的，这样以后改内置文案，没动过手的人能跟着更新）。
剩下的是用户自己加的关系（`relations.customs`：前女友、老板……），名字和正文都自己写，
键是 `r1` `r2` 这种自动编的。

一段控制在 200 字以内（跟场景模板同一个道理）：再长就盖过对话本身了，模型开始照着说明造句，
而不是照着 me 平时的说话习惯模仿。
"""
from __future__ import annotations

# (键, 界面上的名字, 正文)。**朋友排第一**：它是默认关系（DEFAULT），下拉框里也该在最前面。
# 键会写进 config.json，改键等于把老配置丢掉——romance/flirty/workplace/colleague/friend
# 这五个跟老的场景模板键**故意取成一样**，老配置的 style_texts 才能原样接过来（见 settings）。
PRESETS = (
    ("friend", "朋友",
     "朋友：熟人，怎么舒服怎么来，可以贫、可以损，别端着。\n"
     "接梗比讲道理重要：对方抛个话头就顺着往下接，能开玩笑就别正经，该夸张就夸张。\n"
     "对方吐槽就跟着骂两句、顺手添一句损的，别给建议（除非 TA 主动问）；TA 报喜就捧场，别泼冷水。\n"
     "不客套——「麻烦你了」「非常感谢」这种是生分；也别突然抒情、别上价值。\n"
     "短，多数几个字到一句话，连着发两三条也正常。"),

    ("romance", "恋人",
     "恋人：你们是情侣。称呼用你平时叫 TA 的那个，别临时换个甜的。\n"
     "可以撒娇、可以说想念、可以直接要陪伴，但别卑微也别审问。\n"
     "对方示弱时先接情绪，别急着给方案；对方忙就短回一句，别追问在干嘛。\n"
     "不查岗、不阴阳怪气、不翻旧账、不说「你是不是不爱我了」这种试探；吵架了就事论事，"
     "不升级、不提分手。\n"
     "多数回一两个短句，别每句都用问号收尾。"),

    ("flirty", "暧昧",
     "暧昧：还没挑明，正在互相试探。分寸是进半步、留半步——比朋友热一点，但别把话说满。\n"
     "可以接梗、可以调侃、可以偶尔撩一句；一次只递一个钩子，别连发几条等回复。\n"
     "称呼用名字或者玩笑式的叫法，别提前上情侣那套。\n"
     "不表白、不逼对方表态、不问「你到底什么意思」；对方没接住就顺势聊别的，别追着问。\n"
     "短句为主，一条一个意思，留白比说满好。"),

    ("colleague", "同事",
     "同事：平级同事，能开玩笑但终究是工作关系。\n"
     "比职场松，可以直接说事，也可以吐槽两句无关痛痒的（加班、天气、食堂），但别掏心窝子。\n"
     "称呼用名字或平时的叫法，不用「您」。\n"
     "约饭、拼单、换班这种直接把时间地点说清楚；求人帮忙先问一句方便吗，别用命令句。\n"
     "不议论领导和同事、不传八卦、不聊工资和晋升、不在群里替别人表态。\n"
     "一两句，别铺垫。"),

    ("workplace", "职场",
     "职场：工作往来，目标是把事说清楚，不是拉近关系。\n"
     "称呼按对方身份来（X 总、X 老师、X 经理），拿不准就「您」加名字，别自来熟。\n"
     "先给结论，再补必要信息；时间、数字、谁做什么写明确，别含糊。\n"
     "客气但不谄媚，不用「呢」「呀」「哦」，也不甩「随便」「都行」。\n"
     "不吐槽同事和公司、不抱怨工作量、不带情绪、不开工作以外的玩笑。\n"
     "两三句说完，能一条说完就别拆两条。"),

    ("family", "家人",
     "家人：家里人，说话不用铺垫，也别端着。\n"
     "可以直说事、可以唠叨两句、可以问吃了没；关心落到实处（几点回、要不要留饭），"
     "别只说「注意身体」。\n"
     "长辈的话顺着点，不同意也别顶着说，换个说法再讲；平辈之间随意些。\n"
     "不翻旧账、不拿别人家孩子比、不催婚催生、不评判 TA 的生活选择。\n"
     "一两句说完，家常话不用讲究措辞。"),
)

DEFAULT = "friend"  # 没指定过关系的会话按这个来（朋友：最不容易出错的那一型）
KEYS = tuple(key for key, _, _ in PRESETS)
NAMES = {key: name for key, name, _ in PRESETS}
TEXTS = {key: text for key, _, text in PRESETS}
LIMIT = 200  # 一段的字数上限，自测拿它卡


def default_text(key: str) -> str:
    """某一型的内置正文；自建的关系没有内置的，返回空串（空 = 那段只有名字、不追加正文）。"""
    return TEXTS.get(key, "")


def is_builtin(key: str) -> bool:
    return key in TEXTS


def label(key: str, customs=()) -> str:
    """键 → 界面上显示的名字。自建的去 customs 里找（[{"key","name","text"}]）。

    自建的刚建出来还没起名字，显示「未命名」——**不能回退成键**（r1），那看着像个乱码；
    也不能是空串，下拉框里会是一条看不见的项。"""
    if key in NAMES:
        return NAMES[key]
    for item in customs or ():
        if item.get("key") == key:
            return str(item.get("name") or "").strip() or "未命名"
    return key or ""


def resolve(key: str, texts=None, customs=()) -> str:
    """某一型的正文。texts 里存着的优先——**哪怕是个空串**，那是用户特意清空的，不该退回内置。
    认不出的键（老配置里删掉的那型、自建又删了的）返回空串，不追加任何东西。"""
    if not key:
        return ""
    if key in (texts or {}):
        return str(texts[key]).strip()
    if key in TEXTS:
        return TEXTS[key]
    for item in customs or ():
        if item.get("key") == key:
            return str(item.get("text") or "").strip()
    return ""


def new_key(existing) -> str:
    """给自建的关系编一个没人用过的键：r1、r2…… 键一旦落盘就不再变（改名字不改键），
    不然用户改个名字，所有指到这个关系的会话就全丢了。"""
    used = set(existing or ())
    n = 1
    while f"r{n}" in used:
        n += 1
    return f"r{n}"


if __name__ == "__main__":
    # 长度是硬约束（见文件头），改文案先跑这个：python core/relations.py
    for key, name, text in PRESETS:
        assert 0 < len(text) <= LIMIT, f"{name} 有 {len(text)} 字，超过 {LIMIT} 了"
        assert text.strip() == text and "\n\n" not in text, f"{name} 有多余空行"
    assert len(set(KEYS)) == len(KEYS), "键重了，config.json 里分不出谁是谁"
    assert len(set(NAMES.values())) == len(NAMES), "界面上的名字重了，分不出点的是哪个"
    assert DEFAULT in KEYS, "默认关系得是内置的一型，不然新用户开局就没正文"
    # 正文都以自己的名字打头，模型一眼知道这段是讲哪种关系的（也方便用户对着改）
    for key, name, text in PRESETS:
        assert text.startswith(name + "："), f"{name} 的正文没有以自己的名字打头"
    # 认不出的键 = 不追加；改过的优先；特意清空的也算数
    assert resolve("") == "" and resolve("没有这个键") == "" and resolve(None) == ""
    assert resolve("friend") == TEXTS["friend"]
    assert resolve("friend", {"friend": " 我改的 "}) == "我改的"
    assert resolve("friend", {"friend": ""}) == ""
    assert resolve("r1", {}, [{"key": "r1", "name": "前女友", "text": " 别提复合 "}]) == "别提复合"
    assert resolve("r1", {"r1": "改过的"}, [{"key": "r1", "name": "前女友", "text": "x"}]) == "改过的"
    # 自建的键不会跟内置的撞
    assert new_key([]) == "r1" and new_key(["r1", "r2"]) == "r3"
    assert new_key(KEYS) == "r1"
    assert label("friend") == "朋友"
    assert label("r1", [{"key": "r1", "name": "前女友"}]) == "前女友"
    assert label("没有这个键") == "没有这个键" and label("", []) == ""
    assert is_builtin("friend") and not is_builtin("r1")
    print("relations ok")
