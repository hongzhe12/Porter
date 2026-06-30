import sys
import tempfile
from pathlib import Path

import shutil

from PySide6.QtCore import QDir, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from backend import (
    LocalResourceBackend,
    ResourceBackend,
    SshConnectDialog,
    SshResourceBackend,
)


WINDOWS = []


class ResourceTreeView(QTreeView):
    _drag_source_owner = None

    def __init__(self, owner: "ResourcePane"):
        super().__init__()
        self.owner = owner
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def startDrag(self, supportedActions):
        ResourceTreeView._drag_source_owner = self.owner
        try:
            super().startDrag(supportedActions)
        finally:
            ResourceTreeView._drag_source_owner = None

    def dragEnterEvent(self, event):
        event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        target_path = self.owner.current_path()
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and self.owner.is_dir(index):
            target_path = self.owner.path_from_index(index)

        if event.mimeData().hasUrls():
            self.owner.receive_paths(
                [url.toLocalFile() for url in event.mimeData().urls()],
                target_path,
            )
            event.acceptProposedAction()
            return

        source = ResourceTreeView._drag_source_owner
        if source is not None and source is not self.owner:
            source.host.copy_between(source, self.owner)
            event.acceptProposedAction()
            return

        super().dropEvent(event)

    def show_context_menu(self, position):
        index = self.indexAt(position)
        menu = QMenu(self)

        open_action = QAction("打开", self)
        open_action.triggered.connect(lambda: self.owner.open_selected(index))
        menu.addAction(open_action)

        rename_action = QAction("重命名", self)
        rename_action.triggered.connect(lambda: self.owner.rename_selected(index))
        menu.addAction(rename_action)

        delete_action = QAction("删除", self)
        delete_action.triggered.connect(self.owner.delete_selected)
        menu.addAction(delete_action)

        copy_to_other_action = QAction(
            self.owner.copy_action_text(),
            self,
        )
        copy_to_other_action.triggered.connect(self.owner.copy_to_other_pane)
        menu.addAction(copy_to_other_action)

        copy_path_action = QAction("复制路径", self)
        copy_path_action.triggered.connect(self.owner.copy_selected_path)
        menu.addAction(copy_path_action)

        refresh_action = QAction("刷新", self)
        refresh_action.triggered.connect(self.owner.refresh)
        menu.addAction(refresh_action)

        if not index.isValid():
            open_action.setEnabled(False)
            rename_action.setEnabled(False)
            delete_action.setEnabled(False)
            copy_to_other_action.setEnabled(False)
            copy_path_action.setEnabled(False)

        menu.exec(self.viewport().mapToGlobal(position))


class ResourcePane(QWidget):
    def __init__(self, title, host: "MainWindow", backend: ResourceBackend):
        super().__init__()
        self.host = host
        self.backend = backend

        root_path = self.backend.start_path()

        self.title_label = QLabel(title)
        self.type_label = QLabel(self.backend.resource_type())

        self.path_edit = QLineEdit(root_path)
        self.open_button = QPushButton("打开")
        self.open_button.clicked.connect(self.open_path)

        header = QHBoxLayout()
        header.addWidget(self.title_label)
        header.addStretch()
        header.addWidget(self.type_label)

        self.up_button = QPushButton("↑")
        self.up_button.clicked.connect(self.go_up)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.path_edit)
        top_bar.addWidget(self.up_button)
        top_bar.addWidget(self.open_button)

        self.model = self.backend.create_model(self)

        self.tree = ResourceTreeView(self)
        self.tree.setModel(self.model)
        self.tree.doubleClicked.connect(self.on_double_clicked)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.set_current_path(root_path)

        body = QVBoxLayout()
        body.addLayout(header)
        body.addLayout(top_bar)
        body.addWidget(self.tree)
        self.setLayout(body)

    def display_name(self):
        return f"{self.title_label.text()} {self.type_label.text()}"

    def go_up(self):
        parent = self.backend.parent_path(self.current_path())
        if parent and parent != self.current_path():
            self.set_current_path(parent)

    def open_path(self):
        path = self.path_edit.text().strip()
        if not path:
            return
        if not self.backend.path_exists(self.model, path):
            QMessageBox.warning(self, "路径不存在", path)
            return
        self.set_current_path(path)

    def on_double_clicked(self, index):
        path = self.path_from_index(index)
        if self.is_dir(index):
            self.set_current_path(path)
            return
        self.backend.open_path(path)

    def current_path(self):
        return self.path_from_index(self.tree.rootIndex())

    def set_current_path(self, path):
        index = self.backend.index_for_path(self.model, path)
        self.tree.setRootIndex(index)
        self.path_edit.setText(path)

    def set_backend(self, title, backend):
        self.title_label.setText(title)
        self.type_label.setText(backend.resource_type())
        self.backend = backend
        self.model = backend.create_model(self)
        self.tree.setModel(self.model)
        root_path = backend.start_path()
        self.set_current_path(root_path)

    def path_from_index(self, index):
        return self.backend.path_for_index(self.model, index)

    def is_dir(self, index):
        return self.backend.is_dir(self.model, index)

    def selected_paths(self):
        return self.backend.selected_paths(
            self.model,
            self.tree.selectionModel(),
        )

    def receive_paths(self, source_paths, target_path=None):
        destination = target_path or self.current_path()
        self.backend.receive_paths(source_paths, destination)
        self.set_current_path(destination)

    def copy_action_text(self):
        other_pane = self.host.other_pane(self)
        return f"复制到{other_pane.display_name()}"

    def copy_to_other_pane(self):
        self.host.copy_between(self)

    def open_selected(self, index):
        if not index.isValid():
            return
        self.on_double_clicked(index)

    def rename_selected(self, index):
        if not index.isValid():
            return
        self.backend.begin_rename(self.tree, index)

    def delete_selected(self):
        selected_paths = self.selected_paths()
        if not selected_paths:
            return

        names = "\n".join(self.backend.display_name(path) for path in selected_paths)
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确认删除以下项目？\n{names}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.backend.delete_paths(selected_paths)
        self.refresh()

    def copy_selected_path(self):
        selected_paths = self.selected_paths()
        if not selected_paths:
            return
        QApplication.clipboard().setText("\n".join(selected_paths))

    def refresh(self):
        self.set_current_path(self.current_path())


class MainWindow(QMainWindow):
    def __init__(self, start_path: str | None = None):
        super().__init__()
        self.setWindowTitle("Porter")
        self.resize(1280, 720)

        self.left_pane = ResourcePane(
            "左侧",
            self,
            LocalResourceBackend(start_path),
        )
        self.right_pane = ResourcePane(
            "右侧",
            self,
            LocalResourceBackend(start_path),
        )

        container = QWidget()
        layout = QHBoxLayout()
        layout.addWidget(self.left_pane, 1)
        layout.addWidget(self.right_pane, 1)
        container.setLayout(layout)
        self.setCentralWidget(container)

        file_menu = self.menuBar().addMenu("文件")
        new_window_action = file_menu.addAction("新建窗口")
        new_window_action.triggered.connect(self.open_new_window)

        ssh_action = file_menu.addAction("SSH 连接...")
        ssh_action.triggered.connect(self.open_ssh_connection)

    def open_ssh_connection(self):
        dialog = SshConnectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        info = dialog.connection_info()
        backend = SshResourceBackend(**info)
        self.right_pane.set_backend("右侧", backend)

    def open_new_window(self):
        window = MainWindow(self.left_pane.current_path() or QDir.homePath())
        window.show()
        WINDOWS.append(window)

    def other_pane(self, source_pane):
        if source_pane is self.left_pane:
            return self.right_pane
        return self.left_pane

    def copy_between(self, source_pane, target_pane=None):
        selected_paths = source_pane.selected_paths()
        if not selected_paths:
            QMessageBox.information(self, "未选择文件", "请先选择要复制的文件")
            return

        target = target_pane or self.other_pane(source_pane)
        source_backend = source_pane.backend

        if isinstance(source_backend, LocalResourceBackend):
            target.receive_paths(selected_paths)
            return

        temp_dir = Path(tempfile.mkdtemp())
        try:
            local_paths = []
            for path in selected_paths:
                local_path = temp_dir / source_backend.display_name(path)
                source_backend.export_path(path, str(local_path))
                local_paths.append(str(local_path))
            target.receive_paths(local_paths)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    WINDOWS.append(window)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
