"""Chat transcript widgets: user bubbles, markdown agent bubbles, tool cards."""

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

MAX_RESULT_LINES = 12


class ToolLine(QWidget):
    """One compact row per tool call. Output stays collapsed unless opened."""

    _seq = 0

    def __init__(self, name, args, nested=False):
        super().__init__()
        ToolLine._seq += 1
        self.seq = ToolLine._seq
        self.finished = False
        row = QVBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.status = QLabel("...")
        self.status.setStyleSheet("color: #888;")
        head.addWidget(self.status)
        summary = args.get("command", json.dumps(args)) if isinstance(args, dict) else str(args)
        if len(summary) > 90:
            summary = summary[:87] + "..."
        self.title = QLabel(f"#{self.seq} {name}: {summary}" + (" (subagent)" if nested else ""))
        self.title.setStyleSheet("color: #888;")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self.title, 1)
        self.toggle = QPushButton("output")
        self.toggle.setCheckable(True)
        self.toggle.setEnabled(False)
        self.toggle.setMaximumWidth(70)
        head.addWidget(self.toggle)
        row.addLayout(head)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setStyleSheet("font-family: monospace; color: #888;")
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result.hide()
        self.toggle.toggled.connect(lambda on: self.result.setVisible(on))
        row.addWidget(self.result)

    def set_result(self, text):
        self.finished = True
        lowered = (text or "").lower()
        if lowered.startswith("the user denied") or lowered.startswith("blocked by policy"):
            self.status.setText("NOT RUN")
            self.status.setStyleSheet("font-weight: bold; color: #e06c75;")
        elif lowered.startswith("plan mode"):
            self.status.setText("SKIPPED")
            self.status.setStyleSheet("font-weight: bold; color: #e0af68;")
        else:
            self.status.setText("done")
            self.status.setStyleSheet("color: #9ece6a;")
        lines = (text or "(no output)").strip().splitlines()
        shown = lines[:MAX_RESULT_LINES]
        extra = len(lines) - len(shown)
        self.result.setText("\n".join(shown) + (f"\n... {extra} more lines" if extra > 0 else ""))
        self.toggle.setEnabled(True)


class ChatView(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.addStretch()
        self.setWidget(self.body)
        self.stream_bubble = None
        self.stream_text = []
        self.cards = {}
        self.thinking = None

    def _scroll_bottom(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def add_user(self, text):
        row = QHBoxLayout()
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("font-weight: bold;")
        row.addWidget(label)
        self.rows.insertLayout(self.rows.count() - 1, row)
        self._scroll_bottom()

    def begin_stream(self):
        self.stream_text = []
        self.stream_bubble = QTextBrowser()
        self.stream_bubble.setOpenExternalLinks(False)
        self.stream_bubble.setStyleSheet("border: 1px solid #333;")
        self.rows.insertWidget(self.rows.count() - 1, self.stream_bubble)

    def append_delta(self, text):
        if self.stream_bubble is None:
            self.begin_stream()
        self.stream_text.append(text)
        self.stream_bubble.setPlainText("".join(self.stream_text))
        self._scroll_bottom()

    def end_stream(self, full_text):
        if not (full_text or "").strip():
            self.discard_stream()
            return
        if self.stream_bubble is None:
            bubble = QTextBrowser()
            bubble.setOpenExternalLinks(False)
            self.rows.insertWidget(self.rows.count() - 1, bubble)
        else:
            bubble = self.stream_bubble
            self.stream_bubble = None
        bubble.setMarkdown(full_text)
        self.stream_text = []
        self._scroll_bottom()

    def discard_stream(self):
        if self.stream_bubble is not None:
            self.stream_bubble.deleteLater()
            self.stream_bubble = None
        self.stream_text = []

    def show_thinking(self, mode=None):
        if self.thinking is None and self.stream_bubble is None:
            self.thinking = QLabel(f"thinking... [{mode}]" if mode else "thinking...")
            self.thinking.setStyleSheet("color: #888; font-style: italic;")
            self.rows.insertWidget(self.rows.count() - 1, self.thinking)
            self._scroll_bottom()

    def hide_thinking(self):
        if self.thinking is not None:
            self.thinking.deleteLater()
            self.thinking = None

    def add_error(self, text):
        self.hide_thinking()
        self.discard_stream()
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet("border: 1px solid #e06c75;")
        layout = QVBoxLayout(frame)
        title = QLabel("Error - no response produced")
        title.setStyleSheet("font-weight: bold; color: #e06c75;")
        layout.addWidget(title)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(body)
        self.rows.insertWidget(self.rows.count() - 1, frame)
        self._scroll_bottom()

    def add_tool_line(self, call_id, name, args, nested=False):
        line = ToolLine(name, args or {}, nested)
        self.cards[call_id or id(line)] = line
        self.rows.insertWidget(self.rows.count() - 1, line)
        self._scroll_bottom()
        return line

    def finish_tool_line(self, call_id, result):
        line = self.cards.get(call_id)
        if line is None:
            open_lines = [c for c in self.findChildren(ToolLine) if not c.finished]
            line = open_lines[-1] if open_lines else None
        if line is not None:
            line.set_result(result)

    def add_note(self, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: #888; font-style: italic;")
        self.rows.insertWidget(self.rows.count() - 1, label)
        self._scroll_bottom()

    def clear(self):
        while self.rows.count() > 1:
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    child = sub.takeAt(0)
                    if child.widget() is not None:
                        child.widget().deleteLater()
        self.stream_bubble = None
        self.stream_text = []
        self.cards = {}
        self.thinking = None

    def render_transcript(self, messages):
        self.clear()
        results = {m.get("tool_call_id"): m.get("content", "") for m in messages if m.get("role") == "tool"}
        for message in messages:
            if message.get("role") == "user" and "<summary>" not in (message.get("content") or ""):
                content = message.get("content") or ""
                if not content.startswith(("<env>", "<system-reminder>")):
                    self.add_user(content)
            elif message.get("role") == "assistant":
                if (message.get("content") or "").strip():
                    self.end_stream(message["content"])
                for call in message.get("tool_calls") or []:
                    try:
                        args = json.loads(call["function"]["arguments"])
                    except (ValueError, KeyError):
                        args = {}
                    card = self.add_tool_line(call.get("id"), call["function"]["name"], args)
                    card.set_result(results.get(call.get("id"), ""))
