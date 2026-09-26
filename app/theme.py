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

# 卡片四周要给投影留的余量。容器边距、卡片之间的 spacing 都别小于这个数。
SHADOW_PAD = 26


def card_qss(bg=PAPER, radius=RADIUS_MD, ring=True):
    """卡片样式串：底色 + 圆角 + 1px 描边环。

    Qt 的 border 是往内画的，跟 CSS 的 `0 0 0 1px`（往外扩一圈）位置差一点点，
    但在这个尺寸下看不出来。"""
    border = f"border: 1px solid {LINE};" if ring else "border: none;"
    return f"background: {bg}; border-radius: {radius}px; {border}"


def apply_shadow(widget, pop=False):
    """给控件挂一层柔和投影，返回那个 effect（留着以后调参用）。

    注意 Qt 只支持一层阴影，CSS 里那三层叠不出，这里取视觉上最重的一层近似。
    加了阴影的卡片，**父布局必须给它留出 SHADOW_PAD 的余量**，否则阴影被裁。"""
    effect = QGraphicsDropShadowEffect(widget)
    if pop:  # 弹层（紧凑候选条那种浮在桌面上的）：对应 CSS 的 0 16px 40px rgba(28,25,23,.16)
        effect.setBlurRadius(40)
        effect.setOffset(0, 16)
        effect.setColor(QColor(*_INK_RGB, 41))
    else:  # 普通卡片：CSS 是三层叠出来的（6% 描边环 + 4% 近影 + 8% 远影），
            # Qt 只能给一层，所以把最重那层的 8% 往上抬一点，凑出接近的厚度感
        effect.setBlurRadius(28)
        effect.setOffset(0, 10)
        effect.setColor(QColor(*_INK_RGB, 34))
    widget.setGraphicsEffect(effect)
    return effect
