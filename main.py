import sys
from pathlib import Path
from PySide6.QtGui import QIcon
import paramiko

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from backend import (
    ContainerSelectDialog,
    SshConnectDialog,
    SshDockerResourceBackend,
    load_session,
    save_session,
)


class ContainerTreeView(QTreeView):
    def __init__(self, owner: "ContainerPane"):
        super().__init__()
        self.owner = owner
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
        rename_action.triggered.connect(before(lambda: self.owner.rename_selected(index)))
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

        self.type_label = QLabel(backend.resource_type())

        self.path_edit = QLineEdit(self._current_path)
        self.open_button = QPushButton("打开")
        self.open_button.clicked.connect(self.open_path)
        self.up_button = QPushButton("↑")
        self.up_button.clicked.connect(self.go_up)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.path_edit)
        top_bar.addWidget(self.up_button)
        top_bar.addWidget(self.open_button)

        self._search_results = False

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
        body.addWidget(self.tree)
        self.setLayout(body)

    def go_up(self):
        if self._search_results:
            self.set_current_path(self._current_path)
            return
        parent = self.backend.parent_path(self._current_path)
        if parent and parent != self._current_path:
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
        self.backend.search_index(self.model, self.path_edit.text().strip() or "/", keyword)
        self._search_results = True

    def _extract_tar_selected(self):
        selected = self.selected_paths()
        for p in selected:
            self.backend.extract_tar(p)
        self.refresh()

    def _exec_command(self):
        cmds = {"重启gunicorn": "kill -HUP $(ps -C gunicorn -o pid | sed -n '2p' | xargs)",
                "df -h": "df -h", "free -m": "free -m", "top -bn1": "top -bn1",
                "ls -la /": "ls -la /", "ps aux": "ps aux", "uname -a": "uname -a"}
        name, ok = QInputDialog.getItem(self, "执行命令", "选择或输入命令:", list(cmds), editable=True)
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Porter")
        self.resize(960, 720)
        self.pane = None

        menu = self.menuBar().addMenu("文件")
        menu.addAction("连接 Docker 容器...", self.connect_to_docker)

        tools = self.menuBar().addMenu("工具")
        tools.addAction("复制公钥命令", self._copy_public_key)

    def _copy_public_key(self):
        for p in [Path.home() / ".ssh" / f"{k}.pub" for k in ["id_ed25519", "id_rsa", "id_ecdsa", "id_dsa"]]:
            if p.exists():
                key = p.read_text().strip()
                cmd = f'echo "{key}" >> ~/.ssh/authorized_keys'
                QApplication.clipboard().setText(cmd)
                QMessageBox.information(self, "已复制", f"已从 {p.name} 生成命令并复制到剪贴板")
                return
        QMessageBox.warning(self, "未找到公钥", "~/.ssh/id_*.pub 文件不存在")

    def connect_to_docker(self):
        dialog = SshConnectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        info = dialog.connection_info()
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                info["host"],
                port=info["port"],
                username=info["username"],
                password=info["password"],
                timeout=5,
            )
        except Exception as e:
            QMessageBox.critical(self, "连接失败", f"SSH 连接失败: {e}")
            return
        self._select_container(client, info)

    def _select_container(self, client, ssh_info):
        dialog2 = ContainerSelectDialog(self, client, ssh_info["host"])
        if dialog2.exec() != QDialog.DialogCode.Accepted:
            return
        ci = dialog2.container_info
        backend = SshDockerResourceBackend(
            client=client,
            container_id=ci["container_id"],
            container_name=ci["container_name"],
            ssh_host=ssh_info["host"],
            ssh_port=ssh_info["port"],
            ssh_username=ssh_info["username"],
            ssh_password=ssh_info["password"],
        )
        save_session({**ssh_info, "container_id": ci["container_id"], "container_name": ci["container_name"]})
        self._show_browser(backend)

    def _auto_restore(self):
        s = load_session()
        if not s or "host" not in s:
            return False
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(s["host"], port=s["port"], username=s["username"], password=s["password"], timeout=5)
        except Exception:
            return False
        if "container_id" in s:
            backend = SshDockerResourceBackend(
                client=client,
                container_id=s["container_id"],
                container_name=s["container_name"],
                ssh_host=s["host"],
                ssh_port=s["port"],
                ssh_username=s["username"],
                ssh_password=s["password"],
            )
            self._show_browser(backend)
            return True
        self._select_container(client, s)
        return True

    def _show_browser(self, backend):
        if self.pane:
            self.pane.deleteLater()
        self.pane = ContainerPane(backend)
        self.setCentralWidget(self.pane)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    # 判断是否为windows系统，如果是则设置窗口图标为icon.ico
    if sys.platform.startswith("win"):
        window.setWindowIcon(QIcon("icon.ico"))
    window.show()
    if not window._auto_restore():
        window.connect_to_docker()
    sys.exit(app.exec())


if __name__ == "__main__":
    
    main()
