import json
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from shutil import rmtree

import paramiko

from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtGui import QStandardItem, QStandardItemModel

SESSION_FILE = Path.home() / ".porter" / "session.json"


def save_session(data):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def load_session():
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def list_containers(client):
    channel = client.get_transport().open_session()
    channel.exec_command("docker ps --format '{{.ID}}\t{{.Image}}\t{{.Names}}'")
    output = channel.makefile("r", -1).read()
    rc = channel.recv_exit_status()
    channel.close()
    if rc != 0:
        return []
    text = output.decode("utf-8") if isinstance(output, bytes) else output
    result = []
    for line in text.splitlines():
        parts = line.strip().split("\t", 2)
        if len(parts) >= 2:
            result.append(
                (parts[0], parts[1], parts[2] if len(parts) > 2 else parts[0])
            )
    return result


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

    def search(self, path, keyword):
        if not keyword:
            return []
        rc, stdout, _ = self._exec(
            f"docker exec {self._container_id} find {shlex.quote(path)} -name '*{keyword}*' -maxdepth 5 2>/dev/null | head -200"
        )
        return stdout.splitlines() if rc == 0 else []

    def search_index(self, model, path, keyword):
        model.clear()
        model.setHorizontalHeaderLabels(["匹配文件"])
        for r in self.search(path, keyword):
            item = QStandardItem(r)
            item.setEditable(False)
            item.setData(r, Qt.ItemDataRole.UserRole)
            item.setData(False, Qt.ItemDataRole.UserRole + 1)
            model.appendRow([item])
        return QModelIndex()

    def run_command(self, command, timeout=15):
        rc, stdout, _ = self._exec(
            f"docker exec {self._container_id} {command}", timeout
        )
        return rc, stdout

    def extract_tar(self, path):
        parent = path[: path.rfind("/")] if "/" in path else "/"
        self._exec(
            f"docker exec {self._container_id} tar -xzf {shlex.quote(path)} -C {shlex.quote(parent)}"
        )

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

    def receive_paths(
        self, source_paths, target_path, client=None, progress_callback=None
    ):
        c = client or self._client
        sftp = c.open_sftp()
        total = sum(Path(p).stat().st_size for p in source_paths)
        uploaded = 0
        for local_path in source_paths:
            local_name = Path(local_path).name
            remote_target = f"{target_path}/{local_name}"
            ts = int(time.time())
            tmp = f"/tmp/porter_cp_{ts}"
            sftp.put(
                local_path,
                tmp,
                callback=lambda x, y, base=uploaded: (
                    progress_callback(base + x, total) if progress_callback else None
                ),
            )
            uploaded += Path(local_path).stat().st_size
            chan = c.get_transport().open_session()
            chan.exec_command(
                f"docker cp {shlex.quote(tmp)} {shlex.quote(self._container_id + ':' + remote_target)}"
            )
            chan.recv_exit_status()
            chan.close()
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
        subprocess.run(
            f'code --wait "{local}"', shell=True, capture_output=True, text=True
        )
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
