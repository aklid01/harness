"""Modal dialogs: approval, pick-one, provider editor, settings."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import providers


class ApprovalDialog(QDialog):
    def __init__(self, parent, reason):
        super().__init__(parent)
        self.setWindowTitle("Approval required")
        layout = QVBoxLayout(self)
        label = QLabel(reason)
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Allow")
        buttons.button(QDialogButtonBox.Cancel).setText("Deny")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class PickDialog(QDialog):
    def __init__(self, parent, title, rows):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.addItems(rows)
        if rows:
            self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        layout.addWidget(self.list)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        row = self.list.currentRow()
        return row if row >= 0 else None


class ProviderDialog(QDialog):
    def __init__(self, parent, entry=None):
        super().__init__(parent)
        self.setWindowTitle("Edit provider" if entry else "Add provider")
        entry = entry or {}
        layout = QFormLayout(self)
        self.name = QLineEdit(entry.get("name", ""))
        self.base_url = QLineEdit(entry.get("base_url", "https://"))
        self.model = QLineEdit(entry.get("model", ""))
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("leave blank to keep stored key" if entry else "paste API key")
        self.disabled = QCheckBox()
        self.disabled.setChecked(bool(entry.get("disabled", False)))
        self.effort = QComboBox()
        self.effort.addItems(["default", "low", "medium", "high", "xhigh"])
        current = (entry.get("reasoning_effort") or "default").lower()
        self.effort.setCurrentIndex(self.effort.findText(current) if self.effort.findText(current) >= 0 else 0)
        self.temperature = QLineEdit("" if entry.get("temperature") is None else str(entry.get("temperature")))
        self.temperature.setPlaceholderText("provider default")
        layout.addRow("Name:", self.name)
        layout.addRow("Base URL:", self.base_url)
        layout.addRow("Model:", self.model)
        layout.addRow("Reasoning effort:", self.effort)
        layout.addRow("Temperature:", self.temperature)
        layout.addRow("API key:", self.api_key)
        layout.addRow("Disabled:", self.disabled)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        effort = self.effort.currentText()
        return {
            "name": self.name.text().strip(),
            "base_url": self.base_url.text().strip(),
            "model": self.model.text().strip(),
            "key": self.api_key.text().strip(),
            "disabled": self.disabled.isChecked(),
            "reasoning_effort": effort if effort != "default" else None,
            "temperature": self.temperature.text().strip() or None,
        }


class SettingsDialog(QDialog):
    COLUMNS = ("Name", "Base URL", "Model", "Key", "State")

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Providers")
        self.resize(720, 380)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        for text, slot in (("Add", self.add), ("Edit", self.edit), ("Delete", self.delete),
                           ("Test", self.test), ("Set active", self.activate), ("Migrate keys", self.migrate)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.refresh()

    def _selected_id(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(256)

    def refresh(self):
        entries = providers.list_providers()
        self.table.setRowCount(len(entries))
        for i, entry in enumerate(entries):
            status = providers.provider_status(entry["id"]) or {}
            active = "active" if status.get("active") else ""
            state = ", ".join(s for s in (active, "disabled" if entry.get("disabled") else "") if s)
            key = status.get("key", "missing")
            values = (entry.get("name"), entry.get("base_url"), entry.get("model"), key, state)
            for j, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setData(256, entry["id"])
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()

    def add(self):
        dialog = ProviderDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        if not values["base_url"]:
            return
        self._store(values, provider_id=values["base_url"])
        self.refresh()

    def edit(self):
        provider_id = self._selected_id()
        if provider_id is None:
            return
        dialog = ProviderDialog(self, providers.get_provider(provider_id))
        if dialog.exec() != QDialog.Accepted:
            return
        self._store(dialog.values(), provider_id=provider_id)
        self.refresh()

    def _store(self, values, provider_id):
        existing = providers.get_provider(provider_id) or {}
        # Keep the model inside its provider, whatever that provider's id is.
        providers.save_provider({
            "id": provider_id,
            "provider_id": existing.get("provider_id") or provider_id,
            "name": values["name"] or provider_id,
            "base_url": values["base_url"],
            "model": values["model"],
            "headers": {},
            "disabled": values["disabled"],
            "reasoning_effort": values.get("reasoning_effort"),
            "temperature": values.get("temperature"),
        })
        if values["key"]:
            providers.set_key(provider_id, values["key"])

    def delete(self):
        provider_id = self._selected_id()
        if provider_id is None:
            return
        providers.delete_provider(provider_id)
        self.refresh()

    def test(self):
        provider_id = self._selected_id()
        if provider_id is None:
            return
        self.status.setText("testing...")
        ok, detail = providers.test_provider(provider_id)
        self.status.setText(("PASS: " if ok else "FAIL: ") + detail)

    def activate(self):
        provider_id = self._selected_id()
        if provider_id is None:
            return
        providers.set_active(provider_id)
        self.refresh()

    def migrate(self):
        try:
            count = providers.migrate_from_settings()
        except Exception as failure:  # noqa: BLE001 - shown in the dialog
            self.status.setText(f"migration failed: {type(failure).__name__}: {failure}")
            return
        self.status.setText(f"imported {count} key(s) into the OS keyring")
        self.refresh()


class ProviderBar(QWidget):
    """Top-bar provider picker shared by the main window."""

    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Provider:"))
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self._changed)
        layout.addWidget(self.combo, 1)
        self.on_change = None
        self.refresh()

    def refresh(self):
        self.combo.blockSignals(True)
        self.combo.clear()
        for entry in providers.list_providers():
            if entry.get("disabled"):
                continue
            self.combo.addItem(f"{entry.get('name')} ({entry.get('model')})", entry["id"])
        active = providers.get_active_id()
        if active:
            index = self.combo.findData(active)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)

    def _changed(self, index):
        provider_id = self.combo.itemData(index)
        if provider_id and self.on_change:
            self.on_change(provider_id)
