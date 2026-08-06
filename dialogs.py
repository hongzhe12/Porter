from pathlib import Path

import paramiko

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from backend import (
    SshDockerResourceBackend,
    list_containers,
    load_session,
    save_session,
)
from widgets import SearchableComboBox


class SshConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SSH 连接")
        layout = QFormLayout(self)

        s = load_session()
        self.connections = s.get("connections", [])

        self.history_combo = QComboBox()
        self.history_combo.addItem("-- 新连接 --")
        for conn in self.connections:
            if isinstance(conn, dict):
                self.history_combo.addItem(
                    f"{conn.get('username','')}@{conn.get('host','')}:{conn.get('port',22)}"
                )
        self.history_combo.currentIndexChanged.connect(self._on_history_select)
        layout.addRow("历史:", self.history_combo)

        self.host_edit = QLineEdit()
        self.port_edit = QLineEdit("22")
        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)

        layout.addRow("主机:", self.host_edit)
        layout.addRow("端口:", self.port_edit)
        layout.addRow("用户名:", self.user_edit)
        layout.addRow("密码:", self.pass_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        if s.get("host"):
            self.host_edit.setText(s.get("host", ""))
            self.port_edit.setText(str(s.get("port", 22)))
            self.user_edit.setText(s.get("username", ""))
            self.pass_edit.setText(s.get("password", ""))

    def _on_history_select(self, index):
        if index <= 0:
            return
        conn = self.connections[index - 1]
        self.host_edit.setText(conn.get("host", ""))
        self.port_edit.setText(str(conn.get("port", 22)))
        self.user_edit.setText(conn.get("username", ""))
        self.pass_edit.setText(conn.get("password", ""))

    def connection_info(self):
        return {
            "host": self.host_edit.text().strip(),
            "port": int(self.port_edit.text().strip()),
            "username": self.user_edit.text().strip(),
            "password": self.pass_edit.text(),
        }


class ContainerSelectDialog(QDialog):
    def __init__(self, parent, ssh_client, ssh_host=""):
        super().__init__(parent)
        self.setWindowTitle("选择 Docker 容器")
        self._ssh = ssh_client
        self._container_id = None
        self._container_name = None

        layout = QFormLayout(self)
        if ssh_host:
            layout.addRow(QLabel(f"通过 SSH {ssh_host} 连接到 Docker"))

        self.container_combo = SearchableComboBox()
        self.container_combo.lineEdit().setPlaceholderText(
            "输入 ID/镜像名/容器名 搜索..."
        )
        layout.addRow("容器:", self.container_combo)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._refresh)
        layout.addRow("", self.refresh_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self._refresh()

    def _refresh(self):
        self.container_combo.clear()
        containers = list_containers(self._ssh)
        if not containers:
            self.container_combo.addItem("Docker 未运行或没有正在运行的容器")
            return
        for cid, image, cname in containers:
            display = f"{cid[:12]}  {image}  {cname}"
            self.container_combo.addItem(display, (cid, cname))

    def _on_accept(self):
        data = self.container_combo.currentData()
        if data is None:
            QMessageBox.warning(self, "未选择容器", "请选择一个运行中的 Docker 容器")
            return
        self._container_id = data[0]
        self._container_name = data[1]
        self.accept()

    @property
    def container_info(self):
        return {
            "container_id": self._container_id,
            "container_name": self._container_name,
        }


class BatchUploadThread(QThread):
    status = Signal(int, str)
    done = Signal()

    def __init__(self, jobs, files):
        super().__init__()
        self.jobs = jobs
        self.files = files

    def run(self):
        for client, info, cid, cname, row in self.jobs:
            try:
                backend = SshDockerResourceBackend(
                    client=client,
                    container_id=cid,
                    container_name=cname,
                    name=cname,
                    ssh_host=info["host"],
                    ssh_port=info["port"],
                    ssh_username=info["username"],
                    ssh_password=info["password"],
                )
                backend.receive_paths(
                    self.files,
                    "/tmp",
                    progress_callback=lambda cur, tot, row=row: self.status.emit(
                        row, f"上传中 {cur * 100 // tot}%"
                    ),
                )
                self.status.emit(row, "完成")
            except Exception as e:
                self.status.emit(row, f"失败: {e}")
        self.done.emit()


class BatchUploadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("上传")
        self.resize(780, 480)
        self._clients = []
        self._files = []
        self._thread = None

        self.file_label = QLabel("未选择文件")
        file_btn = QPushButton("选择文件...")
        file_btn.clicked.connect(self._pick_files)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["主机", "容器", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setStyleSheet(
            """
            QTableWidget { outline: 0; }
            QTableWidget::item:focus { outline: 0; border: none; }
            QTableWidget::item:selected { background: #4facfe; color: white; }
            """
        )

        add_btn = QPushButton("添加主机/容器...")
        add_btn.clicked.connect(self._add_host)
        remove_btn = QPushButton("移除选中")
        remove_btn.clicked.connect(self._remove_selected)
        start_btn = QPushButton("开始下发")
        start_btn.clicked.connect(self._start)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        btn_row.addWidget(start_btn)
        btn_row.addWidget(close_btn)

        self.status_bar = QStatusBar()

        body = QVBoxLayout(self)
        body.addWidget(self.file_label)
        body.addWidget(file_btn)
        body.addWidget(QLabel("目标路径: /tmp (固定)"))
        body.addWidget(self.table)
        body.addLayout(btn_row)
        body.addWidget(self.status_bar)

        self._restore()

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择要下发的文件")
        if files:
            self._files = files
            names = ", ".join(Path(f).name for f in files)
            self.file_label.setText(f"已选 {len(files)} 个文件: {names}")

    def _add_host(self):
        dlg = SshConnectDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        info = dlg.connection_info()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
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
        containers = list_containers(client)
        if not containers:
            QMessageBox.warning(self, "无容器", "该主机没有正在运行的容器")
            client.close()
            return
        selected = self._pick_containers(containers)
        if not selected:
            client.close()
            return
        self._clients.append((client, info))
        ci = len(self._clients) - 1
        for cid, cname in selected:
            self._add_row(ci, cid, cname, info["host"])
        self._save()

    def _pick_containers(self, containers):
        dlg = QDialog(self)
        dlg.setWindowTitle("选择容器 (可多选)")
        lst = QListWidget()
        for cid, image, cname in containers:
            item = QListWidgetItem(f"{cid[:12]}  {image}  {cname}")
            item.setData(Qt.ItemDataRole.UserRole, (cid, cname))
            lst.addItem(item)
        lst.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        body = QVBoxLayout(dlg)
        body.addWidget(lst)
        body.addWidget(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return []
        return [item.data(Qt.ItemDataRole.UserRole) for item in lst.selectedItems()]

    def _add_row(self, ci, cid, cname, host):
        row = self.table.rowCount()
        self.table.insertRow(row)
        host_item = QTableWidgetItem(host)
        host_item.setData(Qt.ItemDataRole.UserRole, (ci, cid, cname))
        self.table.setItem(row, 0, host_item)
        self.table.setItem(row, 1, QTableWidgetItem(cname))
        self.table.setItem(row, 2, QTableWidgetItem("等待"))

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._save()

    def _start(self):
        if not self._files:
            QMessageBox.warning(self, "未选择文件", "请先选择要下发的本地文件")
            return
        if self._thread and self._thread.isRunning():
            return
        jobs = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item:
                continue
            ci, cid, cname = item.data(Qt.ItemDataRole.UserRole)
            client, info = self._clients[ci]
            jobs.append((client, info, cid, cname, row))
        self.status_bar.showMessage("开始下发...")
        self._thread = BatchUploadThread(jobs, self._files)
        self._thread.status.connect(self._on_status)
        self._thread.done.connect(self._on_done)
        self._thread.start()

    def _on_status(self, row, text):
        item = self.table.item(row, 2)
        if item:
            item.setText(text)

    def _on_done(self):
        failed = any(
            "失败" in self.table.item(r, 2).text()
            for r in range(self.table.rowCount())
            if self.table.item(r, 2)
        )
        self.status_bar.showMessage(
            "有失败，请查看表格" if failed else "全部完成", 0 if failed else 5000
        )

    def _save(self):
        data = load_session()
        batch = []
        for idx, (_, info) in enumerate(self._clients):
            containers = []
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                if item and item.data(Qt.ItemDataRole.UserRole):
                    ci, cid, cname = item.data(Qt.ItemDataRole.UserRole)
                    if ci == idx:
                        containers.append([cid, cname])
            if containers:
                batch.append({**info, "containers": containers})
        data["batch"] = batch
        save_session(data)

    def _restore(self):
        for entry in load_session().get("batch", []):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    entry["host"],
                    port=entry.get("port", 22),
                    username=entry["username"],
                    password=entry.get("password", ""),
                    timeout=5,
                )
            except Exception:
                continue
            self._clients.append((client, entry))
            ci = len(self._clients) - 1
            for cid, cname in entry.get("containers", []):
                self._add_row(ci, cid, cname, entry["host"])

    def closeEvent(self, event):
        self._save()
        for client, _ in self._clients:
            try:
                client.close()
            except Exception:
                pass
        super().closeEvent(event)
