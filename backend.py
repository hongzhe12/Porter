import json
import re
import shlex
import stat
import subprocess
import tempfile
import time
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
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
)


CONNECTIONS_FILE = Path.home() / ".porter" / "connections.json"
SESSION_FILE = Path.home() / ".porter" / "session.json"


def load_connections():
    try:
        return json.loads(CONNECTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_connections(connections):
    CONNECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONNECTIONS_FILE.write_text(json.dumps(connections, indent=2, ensure_ascii=False))


def save_session(data: dict):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def load_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


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

        pp = self.parent_path(path)
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
        self._connect()
        try:
            self._sftp.stat(path)
            return True
        except OSError:
            return False

    def selected_paths(self, model, selection_model):
        rows = selection_model.selectedRows()
        result = []
        for index in rows:
            path = self.path_for_index(model, index)
            if path:
                result.append(path)
        return result

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
        p = path.rstrip("/")
        if not p:
            return "/"
        idx = p.rfind("/")
        return p[:idx] if idx > 0 else "/"

    def open_path(self, path):
        self._connect()
        try:
            tmp = Path(tempfile.mkdtemp())
            local = tmp / self.display_name(path)
            self._sftp.get(path, str(local))
            before_size = local.stat().st_size
            subprocess.run(f'code --wait "{local}"', shell=True)
            after_size = local.stat().st_size
            if after_size != before_size:
                print(f"文件已修改: {local.name} ({before_size} → {after_size} 字节)")
                self._sftp.put(str(local), path)
            else:
                print(f"文件未修改: {local.name}，跳过上传")
            rmtree(tmp)
        except Exception as e:
            print(f"打开文件失败 ({path}): {e}")

    def export_path(self, source_path, local_target):
        self._connect()
        self._sftp.get(source_path, local_target)

    def connection_info(self):
        return {
            "host": self._host,
            "port": self._port,
            "username": self._username,
            "password": self._password,
        }

    @property
    def ssh_client(self):
        self._connect()
        return self._client

class ContainerSelectDialog(QDialog):
    def __init__(self, parent, ssh_backend: SshResourceBackend):
        super().__init__(parent)
        self.setWindowTitle("选择 Docker 容器")
        self._ssh = ssh_backend
        self._container_id = None
        self._container_name = None

        layout = QFormLayout(self)

        layout.addRow(QLabel(f"通过 SSH {ssh_backend._host} 连接到 Docker"))

        self.container_combo = QComboBox()
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._refresh)
        layout.addRow("容器:", self.container_combo)
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
        channel = self._ssh.ssh_client.get_transport().open_session()
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


class SshDockerResourceBackend(ResourceBackend):
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
            stderr = (
                stderr_raw.decode("utf-8")
                if isinstance(stderr_raw, bytes)
                else stderr_raw
            )
            return rc, stdout, stderr
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
        rows = selection_model.selectedRows()
        result = []
        for index in rows:
            path = self.path_for_index(model, index)
            if path:
                result.append(path)
        return result

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
            try:
                ts = int(time.time())
                tmp = f"/tmp/porter_cp_{ts}"
                sftp.put(local_path, tmp)
                self._exec(
                    f"docker cp {shlex.quote(tmp)} {shlex.quote(self._container_id + ':' + remote_target)}"
                )
                sftp.remove(tmp)
            except Exception:
                pass
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
        try:
            tmp = Path(tempfile.mkdtemp())
            local = tmp / self.display_name(path)
            self.export_path(path, str(local))
            before_size = local.stat().st_size
            subprocess.run(
                f'code --wait "{local}"',
                shell=True, capture_output=True, text=True,
            )
            after_size = local.stat().st_size
            if after_size != before_size:
                print(f"文件已修改: {local.name} ({before_size} → {after_size} 字节)")
            else:
                print(f"文件未修改: {local.name}，跳过上传")
                rmtree(tmp)
                return
            self.receive_paths([str(local)], self.parent_path(path))
            rmtree(tmp)
        except Exception as e:
            print(f"打开文件失败 ({path}): {e}")

    def export_path(self, source_path, local_target):
        sftp = self._client.open_sftp()
        try:
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
        except Exception:
            pass
        sftp.close()
