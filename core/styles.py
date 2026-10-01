# -*- coding: utf-8 -*-
"""场景模板：给某个会话**临时换个口吻**——用另一型关系的正文说话。

它不再自带正文。原来这里存着恋爱/暧昧/职场/同事/朋友五套模板，跟 core/relations.py 的「关系」
管的是同一件事（都管称呼、语气、分寸），两套正文并存只会互相打架。现在正文**只有一份**，
在 relations 里，按关系维护；这里只回答一个问题：

    这次拼进 prompt 的正文，用哪一型的？
      挑了某一型 → 用那一型的正文（「对一个客户，用同事那套口气说话」）
      没挑（空串）→ 用这个会话**自己的关系**那一型的正文，也就是绝大多数会话走的路

所以面板上下拉框默认就是「跟随关系」，平时根本不用碰它。整套正文的编辑都在设置页的
「关系」那一块，这里没有第二个编辑器——正文是同一份，摆两个框改同样的字只会让人打架。

键跟 core/relations 的键**取成一样**（friend/romance/family…），老配置的 style_texts 才能
原样接过去（见 app/settings.py 的迁移）。
"""
from __future__ import annotations

try:  # 既能当包 import，也能直接 `python core/styles.py` 跑自测（那时候 sys.path 里是 core/）
    from . import relations
except ImportError:
    import relations

FOLLOW = ""  # 「跟随关系」：不覆盖，用这个会话自己那种关系的正文
FOLLOW_NAME = "跟随关系"
LIMIT = relations.LIMIT  # 一段的字数上限，自测和设置页的计数器都用它


def choices(customs=()) -> tuple:
    """面板那个下拉的选项：[(键, 名字)]。第一项是「跟随关系」，后面是内置的几型 + 用户自建的。"""
    return ((FOLLOW, FOLLOW_NAME),
            *((key, relations.label(key, customs)) for key in relations.KEYS),
            *((str(c["key"]), relations.label(str(c["key"]), customs))
              for c in (customs or ()) if c.get("key")))


def resolve(key: str, *, relation: str = "", relation_texts=None, customs=()) -> str:
    """这次要追加的正文。空串 = 不追加。

    挑了哪一型就用哪一型的正文，没挑（空串）就退回这个会话自己的关系。两者都没有正文
    （自建的关系还没写），返回空串——不追加任何东西，提示里那一段整个不出现。"""
    return relations.resolve(key or relation, relation_texts, customs)


if __name__ == "__main__":
    # python core/styles.py
    keys = [k for k, _ in choices()]
    assert keys[0] == FOLLOW, "「跟随关系」得排第一，它才是默认"
    customs = [{"key": "r1", "name": "前女友", "text": "别提复合"}]
    keys = [k for k, _ in choices(customs)]
    assert keys[:len(relations.KEYS) + 1] == [FOLLOW, *relations.KEYS], "内置几型的顺序不对"
    assert keys[-1] == "r1" and len(set(keys)) == len(keys), "自建的没接上，或者键撞了"
    assert len(set(n for _, n in choices(customs))) == len(keys), "界面上的名字重了"
    # 没挑 = 用关系的正文；关系也没正文（自建的还没写）就是不追加
    assert resolve(FOLLOW, relation="friend") == relations.TEXTS["friend"]
    assert resolve("", relation="r1", customs=customs) == "别提复合"
    assert resolve("", relation="没有这个键") == "" and resolve("") == ""
    # 挑了某一型 = 用那一型的正文（包括自建的）
    assert resolve("romance") == relations.TEXTS["romance"]
    assert resolve("r1", customs=customs) == "别提复合"
    assert resolve("romance", relation="friend") == relations.TEXTS["romance"], "挑了的优先"
    # 用户改过的正文照样认
    assert resolve("friend", relation_texts={"friend": " 我改的 "}) == "我改的"
    assert resolve(FOLLOW, relation="friend", relation_texts={"friend": ""}) == ""
    # 上限只有一处定义（relations.LIMIT），这儿只是个别名。别在这儿抄一个数字进来——
    # 以前这行写的是 `assert LIMIT == 200`，改 relations 的上限时它必挂，白挡一次。
    assert LIMIT == relations.LIMIT, "styles 的上限得跟着 relations 走，别自己写死"
    print("styles ok")
