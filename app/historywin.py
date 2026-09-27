# -*- coding: utf-8 -*-
"""AI 记录窗：每一轮问了什么、模型回了什么、最后用了哪条，都摆在这儿。

数据全在 core/trace.py 那个本地 SQLite 里（跟 config.json 并排的 history.db），这个窗只读。
左边一轮一行，右边是选中那轮的完整材料——**包括发出去的提示原文和模型原始返回**，
所以出问题（起草跑偏、判断卡住、答案莫名其妙）时能翻回来看到底哪一步歪的。

跟识别调试窗一样是独立小窗（Qt.Tool，不占任务栏）：面板最宽 640px，prompt 动辄一两千字，
塞进面板根本读不动；这个窗能拉到 1000px 宽，也能跟面板并排摆着。

刷新：开窗时拉一次，「刷新」按钮再拉一次；开着的时候每 3 秒看一眼有没有新记录
（比的是最大 id），有就重建列表并**尽量停在原来那一轮**，别把人正在看的记录顶掉。
"""
from __future__ import annotations

import json
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidgetItem, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import ListWidget, PlainTextEdit, PushButton

from app import theme
from core import trace
from core.questions import CHOICE_LABELS, QUESTION_LABELS

_KIND = {"reply": "回复", "opener": "开场白"}
# 「最后用了哪条」的动作名。auto = 系统设置里那个「自动发送」调试开关替人发的
# （trace.mark_used 的 action 参数，加一个新动作就往这儿加一条）
_USED_LABELS = {"fill": "填入", "copy": "复制", "auto": "自动发送"}
_REFRESH_MS = 3000


def _stamp(ms) -> str:
    """毫秒时间戳 → 本地时间。空值给个占位，接口没回时间的那轮也照样能看。"""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "—"


def _clock(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return "—"


def _secs(ms) -> str:
    try:
        return f"{int(ms) / 1000:.1f}s"
    except (TypeError, ValueError):
        return "—"


def _tokens(row: dict, side: str) -> str:
    """某一侧的 token 用量。接口不回 usage（有些中转不回）就什么都不写，别编个 0 出来。"""
    i, o = row.get(f"{side}_in"), row.get(f"{side}_out")
    if not isinstance(i, int) and not isinstance(o, int):
        return ""
    return f"输入 {i or 0} / 输出 {o or 0} tok"


def _loads(text, default=None):
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return default


def _blank(text) -> str:
    """空值统一显示成「—」，免得详情里到处是 None。"""
    text = "" if text is None else str(text)
    return text if text.strip() else "—"


def _head(title: str, row: dict, side: str) -> str:
    """一节的开头那行：「══ 起草（deepseek / deepseek-flash · 3.2s · 输入 812 / 输出 96 tok）══」。
    token、思考模式这些不一定有，缺了就不写，别摆个 0 出来。"""
    text = f"══ {title}（{_blank(row.get(f'{side}_provider'))} / {_blank(row.get(f'{side}_model'))}"
    if row.get(f"{side}_ms") is not None:
        text += f" · {_secs(row.get(f'{side}_ms'))}"
    if _tokens(row, side):
        text += f" · {_tokens(row, side)}"
    if side == "draft" and row.get("draft_thinking") is not None:
        text += f" · 思考模式{'开' if row['draft_thinking'] else '关'}"
    return text + "）══"


def _lines(row: dict) -> list[str]:
    """一条记录 → 一整页纯文本。分节写，每节一段，方便直接选中复制出去。"""
    out = [f"{_stamp(row.get('created_at'))} · {_KIND.get(row.get('kind'), row.get('kind') or '?')}"
           f" · {_blank(row.get('chat'))}",
           f"触发：{_blank(row.get('trigger'))}    总耗时：{_secs(row.get('ms'))}"]
    if row.get("trouble"):
        out += ["", f"⚠ 这一轮有环节没跑成：{row['trouble']}"]

    # ── 上下文 ──
    msgs = _loads(row.get("messages"), [])
    out += ["", f"══ 发出去的上下文（{len(msgs)} 条 · 关系 {_blank(row.get('relationship'))}"
                f" · 设置里要 {_blank(row.get('context_n'))} 条）══"]
    for m in msgs:
        if isinstance(m, list) and len(m) >= 2:
            who, text = m[0], m[1]
            name = m[2] if len(m) > 2 and m[2] else who
            out.append(f"  {name}: {text}")
    scene = (row.get("scene") or "").strip()
    if scene:
        # 这段正文按会话现取：场景模板挑了就用挑的那一型，没挑就是这个会话自己那种关系的正文
        out += ["", "  这次的口吻正文（关系 / 场景模板，跟着一起发出去的）：",
                "  " + scene.replace("\n", "\n  ")]

    # ── 起草 ──
    out += ["", _head("起草", row, "draft")]
    if row.get("draft_error"):
        out += [f"  起草就挂了：{row['draft_error']}"]
    if row.get("draft_system"):
        out += ["", "── 系统提示 ──", _blank(row.get("draft_system"))]
    if row.get("draft_prompt"):
        out += ["", "── 用户提示（上下文原文就是从这儿发出去的）──", _blank(row.get("draft_prompt"))]
    if row.get("draft_reply"):
        out += ["", "── 模型原始返回 ──", _blank(row.get("draft_reply"))]
    cands = _loads(row.get("candidates"), [])
    if cands:
        out += ["", f"── 出口过滤之后的候选（{len(cands)} 条）──"]
        out += [f"  {i}. {c}" for i, c in enumerate(cands, 1)]
    dropped = _loads(row.get("draft_dropped"), [])
    if dropped:
        out += ["", f"── 被出口过滤扔掉的（{len(dropped)} 条：重复、或跟对方原话一模一样）──"]
        out += [f"  - {d}" for d in dropped]
    if row.get("draft_retry_reply"):
        out += ["", "── 只给了一两条、追问补齐那次 ──",
                "追问：", _blank(row.get("draft_retry_prompt")), "", _blank(row.get("draft_retry_reply"))]

    # ── 判断 ──（开场白那轮压根不问 Jev，整节都不摆，免得读者以为它问了）
    if row.get("judge_provider") or row.get("judge_error"):
        out += ["", _head("判断 · Jev 七道题", row, "judge")]
        if row.get("judge_error"):
            out += [f"  这次没答上来（退回盲起草，不用判断小抄）：{row['judge_error']}"]
        if row.get("judge_state"):
            out += ["", "── 发给 Jev 的 state（它看到的全部输入）──", _blank(row.get("judge_state"))]
        out += _answers(_loads(row.get("judge_answers"), {}))

    # ── 排序 ──
    if row.get("rank_reply") is not None or row.get("rank_error"):
        out += ["", _head("排序 · 再问一次 Jev：哪条候选最合适", row, "rank")]
        if row.get("rank_error"):
            out += [f"  排序挂了：{row['rank_error']}"]
        out += _answers(_loads(row.get("rank_answers"), {}))

    # ── 结果 + 人干了什么 ──
    scores = _loads(row.get("scores"), [])
    out += ["", "══ 最后摆在界面上的 ══"]
    mark = "（推荐）" if row.get("ranked") else "（没排序，按起草顺序摆的）"
    for i, c in enumerate(cands):
        pct = f"  {round(float(scores[i]) * 100)}%" if i < len(scores) and scores[i] else ""
        out.append(f"  {i + 1}. {c}{pct}" + (mark if i == row.get("best_index") else ""))
    out += ["", "══ 人最后用了哪条 ══"]
    if row.get("used_action"):
        # 认不出的动作原样打出来，别再退回「复制」——那会把「自动发送」说成复制（加新动作时
        # 记得往 _USED_LABELS 里加一条，不然就是这句兜底在显示 raw 值）
        out.append(f"  {_clock(row.get('used_at'))} "
                   + _USED_LABELS.get(row["used_action"], row["used_action"])
                   + f"第 {(row.get('used_index') or 0) + 1} 条：{_blank(row.get('used_text'))}")
    else:
        out.append("  （没用这一轮的候选——自己手打的，或者这轮被后来的消息顶掉了）")
    return out


def _answers(answers: dict) -> list[str]:
    """判断题答案 → 几行中文。认不出的形状原样贴 JSON，别把信息丢了。"""
    if not answers:
        return ["", "  （没有答案）"]
    out = [""]
    for name, answer in answers.items():
        title = QUESTION_LABELS.get(name, name)
        if not isinstance(answer, dict):
            out.append(f"  {title}：{answer}")
            continue
        choice = answer.get("choice")
        if choice is not None:
            label = (CHOICE_LABELS.get(name) or {}).get(choice)
            out.append(f"  {title}：{choice}" + (f"（{label}）" if label else ""))
        elif "noul" in answer:
            value = answer.get("noul")
            word = "是" if isinstance(value, (int, float)) and value >= 0.5 else "否"
            out.append(f"  {title}：{word}（{value}）")
        elif "score" in answer:
            out.append(f"  {title}：{answer.get('score')}/9")
        else:
            out.append(f"  {title}：{json.dumps(answer, ensure_ascii=False)}")
    return out


class HistoryWindow(QWidget):
    """独立小窗。showEvent 时拉一次，开着的时候每 3 秒看有没有新记录。"""

    def __init__(self, on_close=None, db_path: str = ""):
        super().__init__()
        self.on_close = on_close
        # 库路径自己拿着，每次刷新前 configure 一遍：core.trace 是全局单例，别的地方
        # （某次写库失败之类）把它清掉之后，这窗还能自愈，不至于一直显示空
        self._db = str(db_path or "")
        self._rows: list[dict] = []
        self._last_id = 0
        self.setWindowTitle("AI 记录")
        self.setWindowFlags(Qt.Tool)
        self.resize(1000, 680)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 10)
        outer.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("AI 记录", self)
        title.setStyleSheet(f"color: {theme.INK}; font-size: 15px; font-weight: 600;")
        head.addWidget(title)
        self.summary = QLabel("", self)
        self.summary.setStyleSheet(f"color: {theme.MUTED};")
        head.addWidget(self.summary, 1)
        self.refreshButton = PushButton("刷新", self)
        self.refreshButton.clicked.connect(self.refresh)
        head.addWidget(self.refreshButton)
        self.clearButton = PushButton("清空记录", self)
        self.clearButton.setToolTip("把本机的 history.db 清空；关掉设置里的「记录 AI 调用」就不再写新的")
        self.clearButton.clicked.connect(self._clear)
        head.addWidget(self.clearButton)
        outer.addLayout(head)

        note = QLabel("每一轮 AI 调用都记在这儿：发出去的提示原文、模型原始返回、判断答案、"
                      "最后用了哪条。整个库都在本机，不含密钥（写库前统一脱敏）。", self)
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.MUTED}; background: {theme.CREAM}; "
                           f"border: 1px solid {theme.LINE}; border-radius: 8px; padding: 6px 10px;")
        outer.addWidget(note)

        split = QSplitter(Qt.Horizontal, self)
        self.list = ListWidget(self)
        self.list.setMinimumWidth(240)
        self.list.currentRowChanged.connect(self._show_row)
        split.addWidget(self.list)
        self.detail = PlainTextEdit(self)
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("左边选一轮，这里显示它从头到尾发生了什么。")
        split.addWidget(self.detail)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([330, 660])
        outer.addWidget(split, 1)

        self.footer = QLabel("", self)
        self.footer.setStyleSheet(f"color: {theme.MUTED};")
        outer.addWidget(self.footer)

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._poll)
        self.refresh()

    # ── 数据 ──
    def refresh(self) -> None:
        """重拉列表。尽量停在原来那一轮上——每 3 秒一次重画，别把人正在看的记录顶掉。"""
        if self._db:
            trace.configure(self._db)  # 已经配好就是空操作
        keep = self._selected_id()
        self._rows = trace.recent(limit=500)
        self._last_id = trace.latest_id()
        self.list.blockSignals(True)
        self.list.clear()
        for row in self._rows:
            item = QListWidgetItem(self._item_text(row))
            item.setToolTip(f"{_stamp(row.get('created_at'))} · {_blank(row.get('chat'))}")
            self.list.addItem(item)
        self.list.blockSignals(False)
        failed = sum(1 for r in self._rows if r.get("trouble") or r.get("draft_error")
                     or r.get("judge_error") or r.get("rank_error"))
        self.summary.setText(f"共 {len(self._rows)} 轮"
                             + (f"，其中 {failed} 轮有环节没跑成" if failed else "")
                             + f" · 库 {trace.human_size(trace.size())}")
        self.footer.setText("记录库：" + trace.human_size(trace.size())
                            + "　·　关掉设置里「记录 AI 调用」就不再写新的，"
                              "「清空记录」把已有的全删掉")
        self._select(keep if keep else (self._rows[0]["id"] if self._rows else 0))

    def _item_text(self, row: dict) -> str:
        """列表一行。**单行**：qfluentwidgets 的 ListWidget 行高是固定的，塞 \n 会被裁掉。"""
        bad = "⚠ " if (row.get("trouble") or row.get("draft_error")
                       or row.get("judge_error") or row.get("rank_error")) else ""
        cands = _loads(row.get("candidates"), [])
        return (f"{bad}{_clock(row.get('created_at'))}  {_KIND.get(row.get('kind'), '?')} · "
                f"{_blank(row.get('chat'))} · {len(cands)} 条 · {_secs(row.get('ms'))}")

    def _selected_id(self) -> int:
        row = self.list.currentRow()
        return self._rows[row]["id"] if 0 <= row < len(self._rows) else 0

    def _select(self, run_id: int) -> None:
        for i, row in enumerate(self._rows):
            if row["id"] == run_id:
                self.list.setCurrentRow(i)
                return
        if self._rows:
            self.list.setCurrentRow(0)
        else:
            self.detail.setPlainText("还没有记录。\n\n"
                                     "回微信里收一条新消息，或者开着「冷场开场白」等它到点，\n"
                                     "这儿就会出现一轮——发了什么、模型回了什么、你最后用了哪条。")

    def _show_row(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            self.detail.setPlainText("\n".join(_lines(self._rows[index])))

    def _poll(self) -> None:
        """只在新记录出现时重建，平时什么都不做（开着窗看老记录时不该每 3 秒闪一下）。"""
        if trace.latest_id() != self._last_id:
            self.refresh()

    def _clear(self) -> None:
        if trace.clear():
            self.refresh()
            self.footer.setText("记录已清空。")

    # ── 窗口 ──
    def showEvent(self, event):
        self.refresh()
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._timer.stop()
        if self.on_close:
            self.on_close()
        super().closeEvent(event)
