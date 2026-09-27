# -*- coding: utf-8 -*-
"""个人风格的场景模板：恋爱 / 暧昧 / 职场 / 同事 / 朋友，外加一个空白的「自定义」。

设置里选中的那一型会拼进**起草**的 prompt（core/draft.py），不喂判断模型——Jev 那七道题问的是
意图、紧张度和该不该现在回，跟怎么措辞没关系，塞进去只会稀释题目。

这里的内置正文是**默认值**：设置页把正文摊在一个大框里，用户可以随手改，改过的版本按类型分开
存在 config.json 的 `style_texts` 里；「重置为内置原文」就是把它删掉、回到这里这份。所以改文案
直接改这儿，没动过手的用户下次开设置就看到新的。

一段控制在 200 字以内：再长就盖过对话本身了，模型开始照着说明造句，而不是照着 me 平时的
说话习惯模仿（那是 draft.SYSTEM 里「优先模仿样本」那一条在管的事）。模板只管这个场景里
**怎么说话**——称呼、分寸、节奏、禁忌、长度；「不排比」「不客套」那些通病 SYSTEM 已经管了，
这里不重复。
"""
from __future__ import annotations

# (键, 界面上的名字, 拼进 prompt 的正文)。键会写进 config.json，改键等于把老配置丢掉。
PRESETS = (
    ("romance", "恋爱",
     "恋爱：你们是情侣。称呼用你平时叫 TA 的那个，别临时换个甜的。\n"
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

    ("workplace", "职场",
     "职场：工作往来，目标是把事说清楚，不是拉近关系。\n"
     "称呼按对方身份来（X 总、X 老师、X 经理），拿不准就「您」加名字，别自来熟。\n"
     "先给结论，再补必要信息；时间、数字、谁做什么写明确，别含糊。\n"
     "客气但不谄媚，不用「呢」「呀」「哦」，也不甩「随便」「都行」。\n"
     "不吐槽同事和公司、不抱怨工作量、不带情绪、不开工作以外的玩笑。\n"
     "两三句说完，能一条说完就别拆两条。"),

    ("colleague", "同事",
     "同事：平级同事，能开玩笑但终究是工作关系。\n"
     "比职场松，可以直接说事，也可以吐槽两句无关痛痒的（加班、天气、食堂），但别掏心窝子。\n"
     "称呼用名字或平时的叫法，不用「您」。\n"
     "约饭、拼单、换班这种直接把时间地点说清楚；求人帮忙先问一句方便吗，别用命令句。\n"
     "不议论领导和同事、不传八卦、不聊工资和晋升、不在群里替别人表态。\n"
     "一两句，别铺垫。"),

    ("friend", "朋友",
     "朋友：熟人，怎么舒服怎么来。\n"
     "可以贫、可以损、可以敷衍（「行」「懂了」「笑死」），不用每句都接住。\n"
     "对方吐槽就跟着骂两句，别讲道理、别给建议，除非 TA 主动问；TA 说好事就捧场，别泼冷水。\n"
     "不客套——「麻烦你了」「非常感谢」这种是生分；不端着，也别突然抒情。\n"
     "短，多数几个字到一句话，连着发两三条也正常。"),
)

KEYS = tuple(key for key, _, _ in PRESETS)
NAMES = {key: name for key, name, _ in PRESETS}
TEXTS = {key: text for key, _, text in PRESETS}
LIMIT = 200  # 一段的字数上限，自测拿它卡
CUSTOM = "custom"  # 自定义：没有内置正文，框里写什么就是什么。键不能跟上面几个撞
CUSTOM_NAME = "自定义"


def default_text(key: str) -> str:
    """某一型的内置原文；「自定义」没有内置的，返回空串（重置就是清空）。"""
    return TEXTS.get(key, "")


def resolve(key: str, texts=None) -> str:
    """选中的类型 + 用户改过的正文 → 这次要追加的正文。空串 = 不追加。

    texts 里存着的优先，**哪怕是个空串**——那是用户特意清空的，不该退回内置原文。
    类型是空串（设置里选的「不用」）或者根本不认识，都返回空串。"""
    if not key:
        return ""
    if key in (texts or {}):
        return str(texts[key]).strip()
    return default_text(key)


if __name__ == "__main__":
    # 长度是硬约束（见文件头），改文案先跑这个：python core/styles.py
    for key, name, text in PRESETS:
        assert 0 < len(text) <= LIMIT, f"{name} 有 {len(text)} 字，超过 {LIMIT} 了"
        assert text.strip() == text and "\n\n" not in text, f"{name} 有多余空行"
    assert len(set(KEYS)) == len(KEYS), "键重了，config.json 里分不出谁是谁"
    assert len(set(NAMES.values())) == len(NAMES), "界面上的名字重了，分不出点的是哪个"
    assert CUSTOM not in TEXTS and CUSTOM not in KEYS, "自定义的键跟内置的撞了"
    assert default_text(CUSTOM) == "" and default_text("没有这个键") == ""
    # 没选 / 认不出的类型 = 不追加
    assert resolve("", {"romance": "改过的"}) == ""
    assert resolve("没有这个键", {}) == ""
    assert resolve(None) == ""
    # 改过的优先；特意清空的（空串）也算数，不退回内置
    assert resolve("friend") == TEXTS["friend"]
    assert resolve("friend", {"friend": " 我改的 "}) == "我改的"
    assert resolve("friend", {"friend": ""}) == ""
    assert resolve(CUSTOM, {CUSTOM: "哥哥视角"}) == "哥哥视角"
    assert resolve(CUSTOM, {}) == "" and resolve(CUSTOM, {CUSTOM: "  "}) == ""
    print("styles ok")
