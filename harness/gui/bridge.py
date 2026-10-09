"""Engine <-> Qt bridge. The Runner lives on a QThread worker; the GUI
never blocks it and it never touches widgets. Approval/pick requests cross
threads on a threading.Event owned by the worker side."""

import threading

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from .. import events
from ..agent import Runner


class Worker(QObject):
    started = Signal()
    done = Signal()

    def __init__(self, cancel):
        super().__init__()
        self.cancel = cancel
        self.runner = None

    def _ensure_runner(self, mode="build"):
        if self.runner is None:
            self.runner = Runner(stream=True, cancel_event=self.cancel, mode=mode)
        else:
            self.runner.mode = mode

    @Slot(str, str)
    def run_input(self, text, mode):
        self.cancel.clear()
        self.started.emit()
        try:
            self._ensure_runner(mode)
            self.runner.handle_input(text)
        finally:
            self.done.emit()

    @Slot(str, str)
    def open_session(self, session_id, mode):
        from .. import history
        from .. import session as session_mod

        self.cancel.clear()
        self.started.emit()
        try:
            if session_id:
                messages = session_mod.open_session(session_id)
                history.strip(messages)
                self.runner = Runner(stream=True, cancel_event=self.cancel)
                self.runner.messages = messages
                label = "opened"
            else:
                self.runner = Runner(stream=True, cancel_event=self.cancel, mode=mode or "build")
                label = "new"
            events.emit(events.SESSION, {"label": label, "messages": self.runner.messages,
                                             "id": session_mod.CURRENT})
        finally:
            self.done.emit()


class EngineBridge(QObject):
    event_received = Signal(str, dict)
    approval_needed = Signal(str, object, object)
    pick_needed = Signal(str, list, object, object)
    run_requested = Signal(str, str)
    open_requested = Signal(str, str)
    title_ready = Signal()

    def __init__(self):
        super().__init__()
        self.cancel = threading.Event()
        self.pending_gates = []
        self.running = False
        self.thread = QThread()
        self.worker = Worker(self.cancel)
        self.worker.moveToThread(self.thread)
        self.run_requested.connect(self.worker.run_input, Qt.QueuedConnection)
        self.open_requested.connect(self.worker.open_session, Qt.QueuedConnection)
        self.worker.started.connect(lambda: setattr(self, "running", True))
        self.worker.done.connect(lambda: setattr(self, "running", False))
        self.thread.start()
        events.subscribe(self._forward)
        events.set_responder(self._respond)

    def _forward(self, type, payload):
        self.event_received.emit(type, dict(payload))

    def _respond(self, kind, payload):
        if kind == events.APPROVAL:
            box, gate = {}, threading.Event()
            self.pending_gates.append((box, gate))
            try:
                self.approval_needed.emit(payload.get("reason", ""), box, gate)
                gate.wait()
                return box.get("result", False)
            finally:
                self.pending_gates.remove((box, gate))
        if kind == events.PICK:
            box, gate = {}, threading.Event()
            self.pending_gates.append((box, gate))
            try:
                self.pick_needed.emit(payload.get("title", ""), list(payload.get("rows", [])), box, gate)
                gate.wait()
                return box.get("result", None)
            finally:
                self.pending_gates.remove((box, gate))
        return None

    def submit(self, text, mode="build"):
        if self.running:
            events.emit(events.NOTE, {"text": "already running - press Stop first"})
            return
        self.run_requested.emit(text, mode)

    def open_session(self, session_id, mode="build"):
        self.open_requested.emit(session_id, mode)

    def stop(self):
        self.cancel.set()

    def shutdown(self):
        for box, gate in list(self.pending_gates):
            box["result"] = False
            gate.set()
        events.unsubscribe(self._forward)
        events.set_responder(None)
        self.thread.quit()
        self.thread.wait(5000)
