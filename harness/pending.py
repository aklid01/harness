"""Pending user input between turns: steers, queued messages, attachments.

One Mailbox per tab. The transport writes into it while a turn runs; the
agent drains it at safe points, so nothing the user types mid-turn is lost
or injected mid-thought.
"""

import itertools
import threading


class Mailbox:
    """Pending steer/queue messages a user filed while a turn was running.

    Lives on the Session. The runner drains steers at each model-call
    boundary; the Session drains whatever is left when a turn ends well.
    Every mutation fires ``on_change`` from any thread so the UI strip and
    the engine never disagree about what is pending.
    """

    MAX_ITEMS = 10

    def __init__(self):
        self._items: list = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.on_change = None

    def _notify(self):
        if self.on_change is not None:
            self.on_change(self.items())

    def add(self, text, kind, attachments=None):
        """File one message; None when the mailbox is full."""
        with self._lock:
            if len(self._items) >= self.MAX_ITEMS:
                return None
            item = {
                "id": f"m{next(self._ids)}",
                "kind": kind,
                "text": text,
                "attachments": list(attachments or []),
            }
            self._items.append(item)
        self._notify()
        return item["id"]

    def edit(self, item_id, text):
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    item["text"] = text
                    break
            else:
                return False
        self._notify()
        return True

    def set_attachments(self, item_id, attachments):
        """Replace the files of one pending message."""
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    item["attachments"] = list(attachments or [])
                    break
            else:
                return False
        self._notify()
        return True

    def remove(self, item_id):
        with self._lock:
            remaining = [i for i in self._items if i["id"] != item_id]
            if len(remaining) == len(self._items):
                return False
            self._items = remaining
        self._notify()
        return True

    def take_steers(self):
        """Pop every pending steer, oldest first (the runner's drain point)."""
        with self._lock:
            steers = [i for i in self._items if i["kind"] == "steer"]
            if steers:
                self._items = [i for i in self._items if i["kind"] != "steer"]
        if steers:
            self._notify()
        return steers

    def take_next(self):
        """Pop the oldest pending message - the next turn to auto-run."""
        with self._lock:
            item = self._items.pop(0) if self._items else None
        if item is not None:
            self._notify()
        return item

    def discard_queues(self):
        """Drop every queue item (a failed turn cannot run them); steers stay."""
        with self._lock:
            discarded = [i for i in self._items if i["kind"] == "queue"]
            if discarded:
                self._items = [i for i in self._items if i["kind"] != "queue"]
        if discarded:
            self._notify()
        return discarded

    def items(self):
        with self._lock:
            return [dict(i) for i in self._items]
