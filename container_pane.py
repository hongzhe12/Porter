import paramiko

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from backend import SshDockerResourceBackend


class UploadThread(QThread):
    finished = Signal()
    progress = Signal(int, int)

    def __init__(self, backend, paths, target_path):
        super().__init__()
        self.backend = backend
        self.paths = paths
        self.target_path = target_path

    def run(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.backend._ssh_host,
            port=self.backend._ssh_port,
            username=self.backend._ssh_username,
            password=self.backend._ssh_password,
            timeout=5,
        )
        self.backend.receive_paths(
            self.paths, self.target_path, client, self.progress.emit
        )
        client.close()
        self.finished.emit()


class ContainerTreeView(QTreeView):
    def __init__(self, owner: "ContainerPane"):
        super().__init__()
        self.owner = owner
        self.setStyleSheet("QTreeView::item { height: 28px; }")
        self.setFont(QFont("Microsoft YaHei", 12))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def show_context_menu(self, position):
        index = self.indexAt(position)
        menu = QMenu(self)

        def before(fn):
            def wrapper():
                menu.close()
                QApplication.processEvents()
                fn()

            return wrapper

        open_action = QAction("打开", self)
        open_action.triggered.connect(before(lambda: self.owner.open_selected(index)))
        menu.addAction(open_action)

        rename_action = QAction("重命名", self)
        rename_action.triggered.connect(
            before(lambda: self.owner.rename_selected(index))
        )
        menu.addAction(rename_action)

        delete_action = QAction("删除", self)
        delete_action.triggered.connect(before(self.owner.delete_selected))
        menu.addAction(delete_action)

        copy_path_action = QAction("复制路径", self)
        copy_path_action.triggered.connect(self.owner.copy_selected_path)
        menu.addAction(copy_path_action)

        extract_action = QAction("解压", self)
        extract_action.triggered.connect(before(self.owner._extract_tar_selected))
        menu.addAction(extract_action)

        refresh_action = QAction("刷新", self)
        refresh_action.triggered.connect(self.owner.refresh)
        menu.addAction(refresh_action)

        path = self.owner.path_from_index(index) if index.isValid() else ""
        is_tar = path.endswith(".tar.gz") or path.endswith(".tgz")
        if not index.isValid():
            open_action.setEnabled(False)
            rename_action.setEnabled(False)
            delete_action.setEnabled(False)
            copy_path_action.setEnabled(False)
            extract_action.setEnabled(False)
        else:
            extract_action.setEnabled(is_tar)

        menu.popup(self.viewport().mapToGlobal(position))


class ContainerPane(QWidget):
    def __init__(self, backend: SshDockerResourceBackend):
        super().__init__()
        self.backend = backend
        self._current_path = backend.start_path()
        self.model = backend.create_model(self)
        self.setAcceptDrops(True)

        self.type_label = QLabel(backend.resource_type())

        self.path_edit = QLineEdit(self._current_path)
        self.open_button = QPushButton("打开")
        self.open_button.clicked.connect(self.open_path)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.path_edit)
        top_bar.addWidget(self.open_button)

        self._search_results = False

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setStyleSheet(
            """
            QProgressBar { background: #e0e0e0; border-radius: 5px; text-align: center; color: #333; font: bold 8pt; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #4facfe, stop:1 #00f2fe); border-radius: 5px; }
        """
        )
        self.progress_bar.setVisible(False)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索文件...")
        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self._search_files)
        self.cmd_btn = QPushButton("命令")
        self.cmd_btn.clicked.connect(self._exec_command)

        search_bar = QHBoxLayout()
        search_bar.addWidget(self.search_edit)
        search_bar.addWidget(self.search_btn)
        search_bar.addWidget(self.cmd_btn)

        self.tree = ContainerTreeView(self)
        self.tree.setModel(self.model)
        self.tree.doubleClicked.connect(self.on_double_clicked)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.set_current_path(self._current_path)

        header = QHBoxLayout()
        header.addStretch()
        header.addWidget(self.type_label)

        body = QVBoxLayout()
        body.addLayout(header)
        body.addLayout(top_bar)
        body.addLayout(search_bar)
        body.addWidget(self.progress_bar)
        body.addWidget(self.tree)
        self.setLayout(body)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self._upload_thread = UploadThread(self.backend, paths, self._current_path)
            self._upload_thread.progress.connect(
                lambda cur, tot: self.progress_bar.setValue(cur * 100 // tot)
            )
            self._upload_thread.finished.connect(self._upload_done)
            self.progress_bar.setMaximum(100)
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(True)
            self._upload_thread.start()

    def _upload_done(self):
        self.progress_bar.setVisible(False)
        self.refresh()

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
        if self._search_results:
            self._search_results = False
            self.set_current_path(self.backend.parent_path(path))
            return
        if self.is_dir(index):
            self.set_current_path(path)
            return
        self.backend.open_path(path)

    def set_current_path(self, path):
        self._search_results = False
        self._current_path = path
        index = self.backend.index_for_path(self.model, path)
        self.tree.setRootIndex(index)
        self.path_edit.setText(path)

    def path_from_index(self, index):
        return self.backend.path_for_index(self.model, index)

    def is_dir(self, index):
        return self.backend.is_dir(self.model, index)

    def selected_paths(self):
        return self.backend.selected_paths(self.model, self.tree.selectionModel())

    def open_selected(self, index):
        if index.isValid():
            self.on_double_clicked(index)

    def rename_selected(self, index):
        if index.isValid():
            self.backend.begin_rename(self.tree, index)

    def delete_selected(self):
        selected = self.selected_paths()
        if not selected:
            return
        names = "\n".join(self.backend.display_name(p) for p in selected)
        if (
            QMessageBox.question(self, "确认删除", f"确认删除以下项目？\n{names}")
            == QMessageBox.StandardButton.Yes
        ):
            self.backend.delete_paths(selected)
            self.refresh()

    def copy_selected_path(self):
        selected = self.selected_paths()
        if selected:
            QApplication.clipboard().setText("\n".join(selected))

    def refresh(self):
        self.set_current_path(self._current_path)

    def _search_files(self):
        keyword = self.search_edit.text().strip()
        if not keyword:
            return
        self.backend.search_index(
            self.model, self.path_edit.text().strip() or "/", keyword
        )
        self._search_results = True

    def _extract_tar_selected(self):
        selected = self.selected_paths()
        for p in selected:
            self.backend.extract_tar(p)
        self.refresh()

    def _exec_command(self):
        cmds = {
            "reload gunicorn": "pkill -HUP -o gunicorn",
            "df -h": "df -h",
            "free -m": "free -m",
            "top -bn1": "top -bn1",
            "ls -la /": "ls -la /",
            "ps aux": "ps aux",
            "uname -a": "uname -a",
        }
        name, ok = QInputDialog.getItem(
            self, "执行命令", "选择或输入命令:", list(cmds), editable=True
        )
        if not ok or not name:
            return
        cmd = cmds.get(name, name)
        rc, stdout = self.backend.run_command(cmd)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"命令: {name}")
        dlg.resize(700, 500)
        dlg.setLayout(QVBoxLayout())
        text = QTextEdit(stdout if stdout else f"(返回码: {rc}, 无输出)")
        text.setReadOnly(True)
        dlg.layout().addWidget(text)
        btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dlg)
        btn.rejected.connect(dlg.accept)
        dlg.layout().addWidget(btn)
        dlg.exec()
