import json
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from shutil import rmtree

import paramiko

from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

SESSION_FILE = Path.home() / ".porter" / "session.json"


def save_session(data):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def load_session():
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class SshConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SSH 连接")
        layout = QFormLayout(self)

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

        s = load_session()
        if s:
            self.host_edit.setText(s.get("host", ""))
            self.port_edit.setText(str(s.get("port", 22)))
            self.user_edit.setText(s.get("username", ""))
            self.pass_edit.setText(s.get("password", ""))

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

        self.container_combo = QComboBox()
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
        channel = self._ssh.get_transport().open_session()
        try:
            channel.exec_command("docker ps --format '{{.ID}}\t{{.Image}}\t{{.Names}}'")
            stdout = channel.makefile("r", -1)
            output = stdout.read()
            exit_code = channel.recv_exit_status()
            if exit_code != 0:
                self.container_combo.addItem("Docker 未运行或不可用")
                return
        except Exception:
            self.container_combo.addItem("无法获取容器列表")
            return
        finally:
            channel.close()

        text = output.decode("utf-8") if isinstance(output, bytes) else output
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            self.container_combo.addItem("没有正在运行的容器")
            return

        for line in lines:
            parts = line.split("\t", 2)
            if len(parts) >= 2:
                cid = parts[0]
                cimage = parts[1]
                cname = parts[2] if len(parts) > 2 else cid
                display = f"{cid[:12]}  {cimage}  {cname}"
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


class SshDockerResourceBackend:
    def __init__(
        self,
        client,
        container_id: str,
        container_name: str,
        name: str = "",
        ssh_host: str = "",
        ssh_port: int = 22,
        ssh_username: str = "",
        ssh_password: str = "",
    ):
        self._client = client
        self._container_id = container_id
        self._container_name = container_name
        self._name = name or container_name
        self._ssh_host = ssh_host
        self._ssh_port = ssh_port
        self._ssh_username = ssh_username
        self._ssh_password = ssh_password
        self._current_path = "/"

    def _exec(self, command, timeout=15):
        try:
            chan = self._client.get_transport().open_session()
            chan.settimeout(timeout)
            chan.exec_command(command)
            stdout_raw = chan.makefile("r", -1).read()
            stderr_raw = chan.makefile_stderr("r", -1).read()
            rc = chan.recv_exit_status()
            stdout = (
                stdout_raw.decode("utf-8")
                if isinstance(stdout_raw, bytes)
                else stdout_raw
            )
            return rc, stdout, stderr_raw
        except Exception:
            return -1, "", ""

    def resource_type(self):
        return f"容器 {self._name}"

    def start_path(self):
        return "/"

    def create_model(self, parent):
        return QStandardItemModel(parent)

    def _fmt_size(self, size):
        if size < 1024:
            return f"{size} B"
        if size < 1024**2:
            return f"{size / 1024:.0f} KB"
        return f"{size / 1024 ** 2:.1f} MB"

    def index_for_path(self, model, path):
        self._current_path = path
        model.clear()
        model.setHorizontalHeaderLabels(["名称", "大小", "类型", "修改时间"])

        pp = self.parent_path(path)
        if pp != path:
            parent_row = [
                QStandardItem(".."),
                QStandardItem(),
                QStandardItem(),
                QStandardItem(),
            ]
            for item in parent_row:
                item.setEditable(False)
            parent_row[0].setData(pp, Qt.ItemDataRole.UserRole)
            parent_row[0].setData(True, Qt.ItemDataRole.UserRole + 1)
            model.appendRow(parent_row)

        rc, stdout, _ = self._exec(
            f"docker exec {self._container_id} ls -la {shlex.quote(path)}"
        )
        if rc != 0:
            return QModelIndex()

        for line in stdout.splitlines():
            if not line.strip() or line.startswith("total"):
                continue
            fields = line.split(None, 8)
            if len(fields) < 9:
                continue
            perms = fields[0]
            size_str = fields[4]
            month = fields[5]
            day = fields[6]
            time_or_year = fields[7]
            name_field = fields[8]
            name = re.sub(r"\s+->\s+.*", "", name_field)
            if name in (".", ".."):
                continue
            is_dir = perms.startswith("d") or perms.startswith("l")
            full_path = f"{path}/{name}".replace("//", "/")

            name_item = QStandardItem(name)
            name_item.setEditable(False)
            name_item.setData(full_path, Qt.ItemDataRole.UserRole)
            name_item.setData(is_dir, Qt.ItemDataRole.UserRole + 1)

            size_item = QStandardItem()
            size_item.setEditable(False)
            if not perms.startswith("d") and size_str.isdigit():
                size_item.setText(self._fmt_size(int(size_str)))

            type_item = QStandardItem(
                "文件夹"
                if perms.startswith("d")
                else ("链接" if perms.startswith("l") else "文件")
            )
            type_item.setEditable(False)

            time_item = QStandardItem(f"{month} {day} {time_or_year}")
            time_item.setEditable(False)

            model.appendRow([name_item, size_item, type_item, time_item])

        return QModelIndex()

    def path_for_index(self, model, index):
        item = model.itemFromIndex(index.siblingAtColumn(0))
        if item is None:
            return ""
        return item.data(Qt.ItemDataRole.UserRole) or ""

    def is_dir(self, model, index):
        item = model.itemFromIndex(index.siblingAtColumn(0))
        if item is None:
            return False
        return item.data(Qt.ItemDataRole.UserRole + 1) or False

    def path_exists(self, model, path):
        rc, _, _ = self._exec(
            f"docker exec {self._container_id} test -e {shlex.quote(path)}"
        )
        return rc == 0

    def selected_paths(self, model, selection_model):
        return [
            self.path_for_index(model, index)
            for index in selection_model.selectedRows()
            if self.path_for_index(model, index)
        ]

    def display_name(self, path):
        return Path(path).name or path

    def begin_rename(self, tree, index):
        item = tree.model().itemFromIndex(index.siblingAtColumn(0))
        old_path = item.data(Qt.ItemDataRole.UserRole) or ""
        old_name = Path(old_path).name
        item.setEditable(True)

        def on_item_changed(changed_item):
            if changed_item is not item:
                return
            new_name = changed_item.text().strip()
            item.setEditable(False)
            tree.model().itemChanged.disconnect(on_item_changed)
            if not new_name or new_name == old_name:
                return
            parent_dir = str(Path(old_path).parent)
            new_path = f"{parent_dir}/{new_name}".replace("//", "/")
            self._exec(
                f"docker exec {self._container_id} mv "
                f"{shlex.quote(old_path)} {shlex.quote(new_path)}"
            )

        tree.model().itemChanged.connect(on_item_changed)
        tree.edit(index)

    def receive_paths(self, source_paths, target_path):
        sftp = self._client.open_sftp()
        for local_path in source_paths:
            local_name = Path(local_path).name
            remote_target = f"{target_path}/{local_name}"
            ts = int(time.time())
            tmp = f"/tmp/porter_cp_{ts}"
            sftp.put(local_path, tmp)
            self._exec(
                f"docker cp {shlex.quote(tmp)} {shlex.quote(self._container_id + ':' + remote_target)}"
            )
            sftp.remove(tmp)
        sftp.close()

    def delete_paths(self, paths):
        for path in paths:
            self._exec(f"docker exec {self._container_id} rm -rf {shlex.quote(path)}")

    def parent_path(self, path):
        p = path.rstrip("/")
        if not p:
            return "/"
        idx = p.rfind("/")
        return p[:idx] if idx > 0 else "/"

    def open_path(self, path):
        tmp = Path(tempfile.mkdtemp())
        local = tmp / self.display_name(path)
        self.export_path(path, str(local))
        before = local.stat().st_size
        subprocess.run(f'code --wait "{local}"', shell=True, capture_output=True, text=True)
        if local.stat().st_size != before:
            self.receive_paths([str(local)], self.parent_path(path))
        rmtree(tmp)

    def export_path(self, source_path, local_target):
        sftp = self._client.open_sftp()
        ts = int(time.time())
        tmp = f"/tmp/porter_out_{ts}"
        self._exec(
            f"docker cp {shlex.quote(self._container_id + ':' + source_path)} {shlex.quote(tmp)}"
        )
        target = Path(local_target)
        if target.is_dir():
            target = target / Path(source_path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(tmp, str(target))
        sftp.remove(tmp)
        sftp.close()
