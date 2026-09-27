# -*- coding: utf-8 -*-
"""设计 token 层：颜色、字号、圆角、间距，外加两个绕开 Qt 短板的工具函数。

色板照搬 grok-ui/src/styles.css 的 @theme（暖纸色系）。界面代码一律从这里取值，
别在 app/overlay.py 里再写死十六进制色或裸字号——不然下次换风格又得全文件手改。

Qt 没有 CSS 的 box-shadow，所以有两件事要在这里代劳：
1. `card_qss()` 用 1px border 模拟 CSS 的 `0 0 0 1px rgba(...)` 那圈描边环；
2. `apply_shadow()` 用 QGraphicsDropShadowEffect 近似那层柔和投影（只能给一层，
   取 CSS 三层里视觉最重的那层）。**阴影画在控件矩形之外**，所以用了它的卡片，
   父容器边距和卡片间距都不能小于 SHADOW_PAD，否则阴影会被裁掉。
"""
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect

# ── 颜色 ──
PAPER = "#fffcf8"      # 卡片底（暖白）
CREAM = "#f4efe7"      # 窗口底
INK = "#1c1917"        # 主文字（近黑暖灰）
MUTED = "#78716c"      # 次要文字
LINE = "#e7e0d6"       # 描边
SAGE = "#3f7a58"       # 强调色（比原来的 #18794e 更灰更沉）
SAGE_SOFT = "#e7f2ea"  # 推荐卡底
CARAMEL = "#c4a07a"    # 点缀色
WARN = "#b45309"       # 警告
DANGER = "#b44832"     # 错误
OCR = "#3d7ea6"        # 调试视图的识别框用色
IM_MINE = "#95ec69"    # 聊天记录里自己那条的底色（跟 grok-ui 的 --color-im-mine 一个值，就是微信那个绿）

# CSS 里的 rgba(28,25,23,x) 就是 INK 的三个通道
_INK_RGB = (28, 25, 23)

# ── 字号 ──
# 档位对着 grok-ui 的实际用量取：11(次要标签) / 12(小字) / 14(正文) / 15(候选正文)
# / 20(标题 Jev) / 23(页面大标题)，17 和 30 是这边原有的两个特殊用途
FONT_XS = 11
FONT_SM = 12
FONT_MD = 14
FONT_LG = 15
FONT_XL = 17      # 空态标题
FONT_2XL = 20     # 标题栏那个「Jev」
FONT_H1 = 23      # 页面大标题
FONT_DISPLAY = 30  # 只给空态那个「…」用

# ── 圆角 ──
RADIUS_XS = 4
RADIUS_SM = 8
RADIUS_MD = 12
RADIUS_LG = 16
RADIUS_XL = 22

# ── 间距 ──
GAP_XS = 4
GAP_SM = 8
GAP_MD = 12
GAP_LG = 16
GAP_XL = 24

def mix(color, alpha, base=CREAM):
    """把 color 按 alpha 压到 base 上，返回一个实色。

    CSS 里写 `bg-caramel/40` 是半透明叠底，Qt 这边 QSS 的 rgba() 在部分控件上不生效
    （背景会整块不画），所以直接算成实色最稳。用在聊天记录的头像底色这类地方。"""
    a = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    b = tuple(int(base.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02x%02x%02x" % tuple(round(x * alpha + y * (1 - alpha)) for x, y in zip(a, b))


def card_qss(bg=PAPER, radius=RADIUS_MD, ring=True):
    """卡片样式串：底色 + 圆角 + 1px 描边环。

    Qt 的 border 是往内画的，跟 CSS 的 `0 0 0 1px`（往外扩一圈）位置差一点点，
    但在这个尺寸下看不出来。"""
    border = f"border: 1px solid {LINE};" if ring else "border: none;"
    return f"background: {bg}; border-radius: {radius}px; {border}"


# 每种阴影要留的最小余量，顺序是 (左, 上, 右, 下)。
# QGraphicsDropShadowEffect 把边界按 blurRadius 向外扩、再按 offset 往下移，
# 所以下方要多留一个 offset 的量。留不够脏区就顶到窗口矩形外——在透明窗上
# （WA_TranslucentBackground）表现为 UpdateLayeredWindowIndirect failed，阴影被裁。
SHADOW_PAD = 26                       # 卡片：blur 28 / offset 10
SHADOW_PAD_POP = 56                   # 弹层：blur 40 / offset 16
SHADOW_PAD_FLOAT = (28, 28, 28, 40)   # 候选条那种浮动窗：(左, 上, 右, 下)，blur 28 / offset 12
SHADOW_PAD_MASCOT = (18, 18, 18, 32)  # 宠物窗：(左, 上, 右, 下)，blur 18 / offset 12

_SHADOWS = {  # kind -> (blurRadius, offsetY, alpha)
    "card": (28, 10, 34),    # CSS 三层里最重的 0 10px 28px rgba(28,25,23,.08)，抬高一点凑厚度
    "pop": (40, 16, 41),     # 对应 CSS 的 0 16px 40px rgba(28,25,23,.16)
    # 浮动窗：跟 pop 一样的 16% 浓度，但 blur 收到 28。透明窗的留白是实打实占鼠标事件的，
    # blur 40 要留 56px 一圈，对一条 280 宽的候选条来说太浪费
    "float": (28, 12, 41),
    "mascot": (18, 12, 56),  # 设计稿里吉祥物的 drop-shadow(0 12px 18px rgba(28,25,23,.22))
}


def apply_shadow(widget, kind="card"):
    """给控件挂一层柔和投影，返回那个 effect（留着以后调参用）。

    注意 Qt 只支持一层阴影，CSS 里那三层叠不出，这里取视觉上最重的一层近似。
    加了阴影的容器，**父布局必须按 SHADOW_PAD* 留够余量**，否则阴影被裁。"""
    blur, offset, alpha = _SHADOWS[kind]
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, offset)
    effect.setColor(QColor(*_INK_RGB, alpha))
    widget.setGraphicsEffect(effect)
    return effect
