"""Small custom widgets: animated send/stop button."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QPushButton


class SendStopButton(QPushButton):
    clicked_send = Signal()
    clicked_stop = Signal()

    def __init__(self):
        super().__init__()
        self.busy = False
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.setFixedSize(38, 38)
        self.setToolTip("Send / Stop")
        self.clicked.connect(self._click)

    def set_busy(self, busy):
        self.busy = busy
        if busy:
            self.timer.start(60)
        else:
            self.timer.stop()
        self.update()

    def _tick(self):
        self.angle = (self.angle + 30) % 360
        self.update()

    def _click(self):
        if self.busy:
            self.clicked_stop.emit()
        else:
            self.clicked_send.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)
        if self.busy:
            pen = QPen(painter.pen())
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawArc(rect, self.angle * 16, 270 * 16)
        else:
            painter.drawText(rect, Qt.AlignCenter, "\u2191")
