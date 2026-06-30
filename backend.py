import json
import stat
from datetime import datetime
from pathlib import Path
from shutil import copy2, copytree, rmtree

import paramiko

from PySide6.QtCore import QDir, Qt, QUrl, QModelIndex
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileSystemModel,
    QFormLayout,
    QLineEdit,
)


CONNECTIONS_FILE = Path.home() / ".porter" / "connections.json"


def load_connections():
    try:
        return json.loads(CONNECTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_connections(connections):
    CONNECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONNECTIONS_FILE.write_text(json.dumps(connections, indent=2, ensure_ascii=False))


def copy_path(source_path, target_path):
    source = Path(source_path)
    destination = Path(target_path) / source.name
    if source == destination:
        return
    if source.is_dir():
        copytree(source, destination, dirs_exist_ok=True)
        return
    copy2(source, destination)


class SshConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SSH 连接")
        self._connections = load_connections()

        layout = QFormLayout(self)

        self.selector = QComboBox()
        self.selector.addItem("新建连接...")
        for c in self._connections:
            self.selector.addItem(c.get("name", c["host"]))
        self.selector.currentIndexChanged.connect(self._on_select)
        layout.addRow("已保存:", self.selector)

        self.name_edit = QLineEdit()
        layout.addRow("名称:", self.name_edit)

        self.host_edit = QLineEdit()
        self.port_edit = QLineEdit("22")
        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)

        layout.addRow("主机:", self.host_edit)
        layout.addRow("端口:", self.port_edit)
        layout.addRow("用户名:", self.user_edit)
        layout.addRow("密码:", self.pass_edit)

        self.save_check = QCheckBox("保存此连接")
        self.save_check.setChecked(True)
        layout.addRow(self.save_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _on_select(self, idx):
        if idx == 0:
            return
        c = self._connections[idx - 1]
        self.name_edit.setText(c.get("name", ""))
        self.host_edit.setText(c["host"])
        self.port_edit.setText(str(c.get("port", 22)))
        self.user_edit.setText(c["username"])
        self.pass_edit.setText(c.get("password", ""))

    def connection_info(self):
        name = self.name_edit.text().strip() or self.host_edit.text().strip()
        info = {
            "host": self.host_edit.text().strip(),
            "port": int(self.port_edit.text().strip()),
            "username": self.user_edit.text().strip(),
            "password": self.pass_edit.text(),
        }

        if self.save_check.isChecked():
            updated = False
            for c in self._connections:
                if c.get("name") == name or c["host"] == info["host"]:
                    c.update(info)
                    c["name"] = name
                    updated = True
                    break
            if not updated:
                entry = {"name": name, **info}
                self._connections.append(entry)
            save_connections(self._connections)

        return info


class ResourceBackend:
    def resource_type(self):
        raise NotImplementedError()

    def start_path(self):
        raise NotImplementedError()

    def create_model(self, parent):
        raise NotImplementedError()

    def index_for_path(self, model, path):
        raise NotImplementedError()

    def path_for_index(self, model, index):
        raise NotImplementedError()

    def is_dir(self, model, index):
        raise NotImplementedError()

    def path_exists(self, model, path):
        raise NotImplementedError()

    def selected_paths(self, model, selection_model):
        raise NotImplementedError()

    def display_name(self, path):
        raise NotImplementedError()

    def begin_rename(self, tree, index):
        raise NotImplementedError()

    def receive_paths(self, source_paths, target_path):
        raise NotImplementedError()

    def delete_paths(self, paths):
        raise NotImplementedError()

    def parent_path(self, path):
        raise NotImplementedError()

    def open_path(self, path):
        raise NotImplementedError()

    def export_path(self, source_path, local_target):
        raise NotImplementedError()


class LocalFileSystemModel(QFileSystemModel):
    def headerData(self, section, orientation, role=0):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
        ):
            labels = ["名称", "大小", "类型", "修改时间"]
            if section < len(labels):
                return labels[section]
        return super().headerData(section, orientation, role)


class LocalResourceBackend(ResourceBackend):
    def __init__(self, start_path: str | None = None):
        self._start_path = start_path or QDir.homePath()

    def resource_type(self):
        return "本地"

    def start_path(self):
        return self._start_path

    def create_model(self, parent):
        model = LocalFileSystemModel(parent)
        model.setRootPath(self._start_path)
        return model

    def index_for_path(self, model, path):
        return model.index(path)

    def path_for_index(self, model, index):
        return model.filePath(index)

    def is_dir(self, model, index):
        return model.isDir(index)

    def path_exists(self, model, path):
        return self.index_for_path(model, path).isValid()

    def selected_paths(self, model, selection_model):
        return [model.filePath(index) for index in selection_model.selectedRows()]

    def display_name(self, path):
        return Path(path).name or path

    def begin_rename(self, tree, index):
        tree.edit(index)

    def receive_paths(self, source_paths, target_path):
        for path in source_paths:
            copy_path(path, target_path)

    def delete_paths(self, paths):
        for path in paths:
            item = Path(path)
            if item.is_dir():
                rmtree(item)
                continue
            item.unlink()

    def parent_path(self, path):
        return str(Path(path).parent)

    def open_path(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def export_path(self, source_path, local_target):
        source = Path(source_path)
        if source.is_dir():
            copytree(source, local_target, dirs_exist_ok=True)
            return
        copy2(source, local_target)


class SshResourceBackend(ResourceBackend):
    def __init__(self, host, port, username, password):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client = None
        self._sftp = None
        self._current_path = "/"

    def _connect(self):
        if self._client is None:
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._client.connect(
                self._host,
                port=self._port,
                username=self._username,
                password=self._password,
            )
            self._sftp = self._client.open_sftp()

    def resource_type(self):
        return f"SSH {self._host}"

    def start_path(self):
        self._connect()
        return self._sftp.normalize(".")

    def create_model(self, parent):
        return QStandardItemModel(parent)

    def _fmt_size(self, size):
        if size < 1024:
            return f"{size} B"
        if size < 1024**2:
            return f"{size / 1024:.0f} KB"
        return f"{size / 1024 ** 2:.1f} MB"

    def _fmt_time(self, ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    def index_for_path(self, model, path):
        self._current_path = path
        self._connect()
        model.clear()
        model.setHorizontalHeaderLabels(["名称", "大小", "类型", "修改时间"])

        pp = str(Path(path).parent)
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

        try:
            for entry in self._sftp.listdir_attr(path):
                is_dir = stat.S_ISDIR(entry.st_mode)
                full_path = f"{path}/{entry.filename}".replace("//", "/")

                name_item = QStandardItem(entry.filename)
                name_item.setEditable(False)
                name_item.setData(full_path, Qt.ItemDataRole.UserRole)
                name_item.setData(is_dir, Qt.ItemDataRole.UserRole + 1)

                size_item = QStandardItem()
                size_item.setEditable(False)
                if not is_dir:
                    size_item.setText(self._fmt_size(entry.st_size))

                type_item = QStandardItem("文件夹" if is_dir else "文件")
                type_item.setEditable(False)

                time_item = QStandardItem()
                time_item.setEditable(False)
                if entry.st_mtime:
                    time_item.setText(self._fmt_time(entry.st_mtime))

                model.appendRow([name_item, size_item, type_item, time_item])
        except OSError:
            pass

        return QModelIndex()

    def path_for_index(self, model, index):
        item = model.itemFromIndex(index)
        return item.data(Qt.ItemDataRole.UserRole)

    def is_dir(self, model, index):
        item = model.itemFromIndex(index)
        return item.data(Qt.ItemDataRole.UserRole + 1)

    def path_exists(self, model, path):
        self._connect()
        try:
            self._sftp.stat(path)
            return True
        except OSError:
            return False

    def selected_paths(self, model, selection_model):
        return [
            self.path_for_index(model, index)
            for index in selection_model.selectedRows()
        ]

    def display_name(self, path):
        return Path(path).name or path

    def begin_rename(self, tree, index):
        tree.edit(index)

    def _put_recursive(self, local_path, remote_path):
        local = Path(local_path)
        if local.is_dir():
            try:
                self._sftp.mkdir(remote_path)
            except OSError:
                pass
            for child in local.iterdir():
                self._put_recursive(str(child), f"{remote_path}/{child.name}")
            return
        self._sftp.put(str(local_path), remote_path)

    def receive_paths(self, source_paths, target_path):
        self._connect()
        for path in source_paths:
            local_name = Path(path).name
            remote_target = f"{target_path}/{local_name}"
            self._put_recursive(path, remote_target)

    def delete_paths(self, paths):
        self._connect()
        for path in paths:
            try:
                self._sftp.remove(path)
            except OSError:
                self._sftp.rmdir(path)

    def parent_path(self, path):
        return str(Path(path).parent)

    def open_path(self, path):
        pass

    def export_path(self, source_path, local_target):
        self._connect()
        self._sftp.get(source_path, local_target)
