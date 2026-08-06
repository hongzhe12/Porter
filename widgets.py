from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


class SearchableComboBox(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._selected_data = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("输入搜索...")
        layout.addWidget(self._edit)

        self._list = QListWidget()
        self._list.setMaximumHeight(200)
        self._edit.textChanged.connect(self._filter)
        self._list.itemClicked.connect(self._select_item)
        layout.addWidget(self._list)

    def addItem(self, text, userData=None):
        self._items.append((text, userData))
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, userData)
        self._list.addItem(item)

    def clear(self):
        self._items.clear()
        self._list.clear()
        self._selected_data = None

    def lineEdit(self):
        return self._edit

    def currentData(self):
        return self._selected_data

    def currentText(self):
        return self._edit.text()

    def count(self):
        return self._list.count()

    def _filter(self, text):
        self._list.blockSignals(True)
        self._list.clear()
        for display, data in self._items:
            if not text or text.lower() in display.lower():
                item = QListWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, data)
                self._list.addItem(item)
        self._list.blockSignals(False)
        self._list.setVisible(True)

    def _select_item(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        self._selected_data = data
        self._edit.blockSignals(True)
        self._edit.setText(item.text())
        self._edit.blockSignals(False)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._filter(self._edit.text())
