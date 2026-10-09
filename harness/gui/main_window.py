"""Main window: sessions sidebar, chat, plan/usage panel, composer bar."""

from PySide6.QtCore import QEvent, Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import events
from .. import permissions
from .. import providers
from .. import session as session_mod
from .. import __version__ as APP_VERSION
from .bridge import EngineBridge
from .chat_view import ChatView
from .dialogs import ApprovalDialog, PickDialog, ProviderBar, SettingsDialog
from .widgets import SendStopButton

EFFORTS = ["default", "low", "medium", "high", "xhigh"]
MODES = ["Build", "Plan"]
PERMISSION_LABELS = {"Default": "default", "Strict": "strict", "Allow All": "allow-all", "Autopilot": "autopilot"}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Harness v{APP_VERSION}")
        self.resize(1180, 760)
        self.bridge = EngineBridge()
        self.bridge.event_received.connect(self.on_event)
        self.bridge.approval_needed.connect(self.on_approval)
        self.bridge.pick_needed.connect(self.on_pick)
        self.bridge.title_ready.connect(self.refresh_sessions)
        self.bridge.worker.done.connect(self.on_done)
        self.totals = {}
        self.busy = False
        self.current_id = None

        top = QWidget()
        self.setCentralWidget(top)
        root = QVBoxLayout(top)

        bar_row = QHBoxLayout()
        bar_row.addWidget(QLabel("Harness"))
        self.toggle_left = QPushButton("Sessions")
        self.toggle_left.setCheckable(True)
        self.toggle_left.setChecked(True)
        self.toggle_right = QPushButton("Plan")
        self.toggle_right.setCheckable(True)
        self.toggle_right.setChecked(True)
        bar_row.addWidget(self.toggle_left)
        bar_row.addWidget(self.toggle_right)
        bar_row.addStretch(1)
        root.addLayout(bar_row)

        split = QSplitter()
        root.addWidget(split, 1)

        self.side_panel = QWidget()
        side_layout = QVBoxLayout(self.side_panel)
        side_layout.addWidget(QLabel("Sessions"))
        self.sessions = QListWidget()
        self.sessions.itemDoubleClicked.connect(self.open_selected_session)
        self.sessions.setContextMenuPolicy(Qt.CustomContextMenu)
        self.sessions.customContextMenuRequested.connect(self.session_menu)
        side_layout.addWidget(self.sessions, 1)
        for text, slot in (("New", self.new_session), ("Open", self.open_selected_session),
                           ("Rewind", self.send_rewind), ("Compact", self.send_compact)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            side_layout.addWidget(button)
        split.addWidget(self.side_panel)
        self.toggle_left.toggled.connect(self.side_panel.setVisible)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        self.chat = ChatView()
        center_layout.addWidget(self.chat, 1)
        self.activity = QLabel("ready")
        self.activity.setStyleSheet("color: #888; font-style: italic;")
        center_layout.addWidget(self.activity)
        self.entry = QPlainTextEdit()
        self.entry.setPlaceholderText("Ask anything... (Enter to send, Shift+Enter for newline)")
        self.entry.setMaximumHeight(90)
        self.entry.installEventFilter(self)
        center_layout.addWidget(self.entry)
        composer = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(MODES)
        composer.addWidget(self.mode_combo)
        self.provider_bar = ProviderBar()
        self.provider_bar.on_change = self.change_provider
        composer.addWidget(self.provider_bar, 1)
        self.effort_combo = QComboBox()
        self.effort_combo.addItems(EFFORTS)
        self.effort_combo.currentTextChanged.connect(self.change_effort)
        composer.addWidget(self.effort_combo)
        self.perm_combo = QComboBox()
        self.perm_combo.addItems(list(PERMISSION_LABELS))
        self.perm_combo.currentTextChanged.connect(self.change_permissions)
        composer.addWidget(self.perm_combo)
        self.sendstop = SendStopButton()
        self.sendstop.clicked_send.connect(self.send_message)
        self.sendstop.clicked_stop.connect(self.bridge.stop)
        composer.addWidget(self.sendstop)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self.open_settings)
        composer.addWidget(settings_button)
        center_layout.addLayout(composer)
        self.context_label = QLabel("")
        self.context_label.setStyleSheet("color: #888;")
        center_layout.addWidget(self.context_label)
        split.addWidget(center)

        self.right_panel = QWidget()
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.addWidget(QLabel("Plan"))
        self.plan = QListWidget()
        right_layout.addWidget(self.plan, 1)
        right_layout.addWidget(QLabel("Usage"))
        self.usage = QLabel("no calls yet")
        self.usage.setWordWrap(True)
        right_layout.addWidget(self.usage)
        split.addWidget(self.right_panel)
        self.toggle_right.toggled.connect(self.right_panel.setVisible)
        split.setSizes([200, 700, 220])

        self.setStatusBar(QStatusBar())
        self.refresh_sessions()
        self.refresh_context_label()
        self.sync_effort_combo()

    def refresh_context_label(self):
        import subprocess

        from .. import paths as paths_mod

        name = paths_mod.project_name()
        try:
            branch = subprocess.run("git branch --show-current", shell=True, capture_output=True,
                                    text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            branch = ""
        self.context_label.setText(f"{name}  /  {branch or 'No Git'}")

    def sync_effort_combo(self):
        active_id = providers.get_active_id()
        entry = providers.get_provider(active_id) if active_id else None
        effort = (entry or {}).get("reasoning_effort") or "default"
        index = self.effort_combo.findText(effort)
        self.effort_combo.setCurrentIndex(index if index >= 0 else 0)

    # ---------------------------------------------------------------- input

    def eventFilter(self, obj, event):
        if obj is self.entry and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
                self.send_message()
                return True
        return super().eventFilter(obj, event)

    def current_mode(self):
        return self.mode_combo.currentText().lower()

    def set_busy(self, busy):
        self.busy = busy
        self.sendstop.set_busy(busy)

    def send_message(self):
        text = self.entry.toPlainText().strip()
        if not text or self.busy:
            return
        self.entry.clear()
        self.set_busy(True)
        self.chat.show_thinking(self.current_mode())
        self.bridge.submit(text, self.current_mode())

    def send_rewind(self):
        if not self.busy:
            self.set_busy(True)
            self.bridge.submit("/rewind", self.current_mode())

    def send_compact(self):
        if not self.busy:
            self.set_busy(True)
            self.bridge.submit("/compact", self.current_mode())

    def new_session(self):
        if self.busy:
            return
        self.chat.clear()
        self.plan.clear()
        self.current_id = None
        self.bridge.open_session("", self.current_mode())

    def open_selected_session(self):
        item = self.sessions.currentItem()
        if item is None or self.busy:
            return
        self.bridge.open_session(item.data(256), self.current_mode())

    def session_menu(self, pos):
        item = self.sessions.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        rename = menu.addAction("Rename")
        delete = menu.addAction("Delete")
        chosen = menu.exec(self.sessions.mapToGlobal(pos))
        session_id = item.data(256)
        if chosen == rename:
            name, ok = QInputDialog.getText(self, "Rename session", "Name:", text=item.text())
            if ok and name.strip():
                session_mod.set_title(session_id, name.strip()[:60])
                self.refresh_sessions()
        elif chosen == delete:
            confirm = QMessageBox.question(self, "Delete session", f"Delete '{item.text()}'?")
            if confirm == QMessageBox.Yes:
                session_mod.delete_session(session_id)
                if session_id == self.current_id:
                    self.new_session()
                else:
                    self.refresh_sessions()

    def change_provider(self, provider_id):
        providers.set_active(provider_id)
        self.sync_effort_combo()
        self.statusBar().showMessage("active provider: " + provider_id, 4000)

    def change_effort(self, effort):
        active_id = providers.get_active_id()
        if active_id:
            providers.set_effort(active_id, effort if effort != "default" else None)

    def change_permissions(self, label):
        permissions.set_mode(PERMISSION_LABELS[label])
        if label == "Autopilot":
            self.statusBar().showMessage("AUTOPILOT: prompts auto-allowed, deny list still enforced", 6000)
        elif label == "Strict":
            self.statusBar().showMessage("Strict: writes ask, sandbox where the OS supports it", 4000)
        else:
            self.statusBar().showMessage("permissions: " + label, 3000)

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()
        self.provider_bar.refresh()

    def refresh_sessions(self):
        self.sessions.clear()
        for entry in session_mod.all_sessions():
            item_text = entry["title"]
            self.sessions.addItem(item_text)
            item = self.sessions.item(self.sessions.count() - 1)
            item.setData(256, entry["id"])
            item.setToolTip(entry["id"])
            if entry["id"] == self.current_id:
                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)

    # ------------------------------------------------------------- engine in

    def set_activity(self, text):
        self.activity.setText(text)
        self.statusBar().showMessage(text)

    @Slot(str, dict)
    def on_event(self, type, payload):
        if type == events.MESSAGE:
            self.chat.hide_thinking()
            if payload["role"] == "user":
                self.chat.add_user(payload["content"])
            elif (payload.get("content") or "").strip():
                self.chat.end_stream(payload["content"])
            else:
                self.chat.discard_stream()
            self.set_activity("ready")
        elif type == events.MESSAGE_DELTA:
            self.chat.hide_thinking()
            self.chat.append_delta(payload["text"])
        elif type == events.TOOL_CALL:
            self.chat.hide_thinking()
            if payload["name"] == "write_todos" and payload["args"].get("todos"):
                self.show_plan(payload["args"]["todos"])
            else:
                args = payload.get("args") or {}
                summary = args.get("command", args.get("path", ""))
                self.set_activity(f"running {payload['name']}: {summary}"[:120])
                self.chat.add_tool_line(payload.get("call_id"), payload["name"],
                                        args, payload.get("nested", False))
        elif type == events.TOOL_RESULT:
            if payload["name"] != "write_todos":
                self.chat.finish_tool_line(payload.get("call_id"), payload["result"])
            self.set_activity("thinking...")
        elif type == events.TODOS:
            self.show_plan(payload["todos"])
        elif type == events.USAGE:
            for key, value in payload.items():
                self.totals[key] = self.totals.get(key, 0) + (value or 0)
            self.usage.setText(" · ".join(f"{v:,} {k.replace('_tokens', '')}" for k, v in self.totals.items() if v))
        elif type == events.STATUS:
            label = payload.get("active")
            if label:
                self.set_activity(label)
                self.chat.show_thinking()
            else:
                self.chat.hide_thinking()
        elif type == events.NOTE:
            self.chat.add_note(payload.get("text", ""))
        elif type == events.ERROR:
            self.chat.add_error(payload.get("text", ""))
            self.set_activity("error - see above")
        elif type == events.INJECTION:
            pass
        elif type == events.SESSION:
            self.current_id = payload.get("id")
            self.chat.render_transcript(payload["messages"])
            self.refresh_sessions()
            self.set_activity(payload.get("label", ""))
        elif type == events.COMPACTED:
            self.chat.render_transcript(payload["messages"])
            self.chat.add_note(f"compacted {payload['before']} to {len(payload['messages'])} messages")
        elif type == events.SUBAGENT:
            self.set_activity("subagent exploring...")
            self.chat.add_note("subagent: " + payload.get("description", ""))
        elif type == events.DEBUG:
            pass

    def show_plan(self, plan):
        from ..todos import MARKS

        self.plan.clear()
        for item in plan:
            self.plan.addItem(f"{MARKS.get(item['status'], '?')} {item['content']}")

    @Slot(str, object, object)
    def on_approval(self, reason, box, gate):
        dialog = ApprovalDialog(self, reason)
        box["result"] = dialog.exec() == ApprovalDialog.Accepted
        gate.set()

    @Slot(str, list, object, object)
    def on_pick(self, title, rows, box, gate):
        if not rows:
            QMessageBox.information(self, title, "Nothing to choose from.")
            box["result"] = None
        else:
            dialog = PickDialog(self, title, rows)
            box["result"] = dialog.selected() if dialog.exec() == PickDialog.Accepted else None
        gate.set()

    def on_done(self):
        self.set_busy(False)
        self.refresh_sessions()
        self.set_activity("ready")
        self.maybe_name_session()

    def maybe_name_session(self):
        """Fetch an LLM title once per session, off the Qt threads."""
        import threading

        if not self.current_id:
            try:
                from .. import session as session_mod

                self.current_id = session_mod.CURRENT
            except Exception:  # noqa: BLE001
                return
        session_id = self.current_id
        try:
            from .. import session as session_mod

            if session_mod._titles().get(session_id):
                return
            try:
                messages = session_mod.load(session_id)
            except OSError:
                return
            first = session_mod.title(messages)
            if first in ("(empty)", ""):
                return
        except Exception:  # noqa: BLE001 - titles never break turns
            return

        def fetch():
            try:
                from .. import providers
                from .. import session as session_mod

                name = providers.suggest_title(first)
                if name:
                    session_mod.set_title(session_id, name)
                    self.bridge.title_ready.emit()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=fetch, daemon=True).start()

    def closeEvent(self, event):
        self.bridge.shutdown()
        super().closeEvent(event)
