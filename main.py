import sys
from pathlib import Path

import paramiko

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from backend import SshDockerResourceBackend, load_session, save_session
from container_pane import ContainerPane
from dialogs import BatchUploadDialog, ContainerSelectDialog, SshConnectDialog


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
        tools.addAction("上传文件", self._batch_upload)
        tools.addAction("安装 VS Code Server...", self._install_vscode_server)

    def _batch_upload(self):
        BatchUploadDialog(self).exec()

    def _copy_public_key(self):
        for p in [
            Path.home() / ".ssh" / f"{k}.pub"
            for k in ["id_ed25519", "id_rsa", "id_ecdsa", "id_dsa"]
        ]:
            if p.exists():
                key = p.read_text().strip()
                cmd = f'echo "{key}" >> ~/.ssh/authorized_keys'
                QApplication.clipboard().setText(cmd)
                QMessageBox.information(
                    self, "已复制", f"已从 {p.name} 生成命令并复制到剪贴板"
                )
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
        s = load_session()
        conns = s.get("connections", [])
        conns = [
            c
            for c in conns
            if not (
                isinstance(c, dict)
                and c.get("host") == ssh_info["host"]
                and c.get("port") == ssh_info["port"]
                and c.get("username") == ssh_info["username"]
            )
        ]
        conns.insert(
            0,
            {
                "host": ssh_info["host"],
                "port": ssh_info["port"],
                "username": ssh_info["username"],
                "password": ssh_info["password"],
            },
        )
        s["connections"] = conns[:10]
        s.update(ssh_info)
        s["container_id"] = ci["container_id"]
        s["container_name"] = ci["container_name"]
        save_session(s)
        self._show_browser(backend)

    def _auto_restore(self):
        s = load_session()
        if not s or "host" not in s:
            return False
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                s["host"],
                port=s["port"],
                username=s["username"],
                password=s["password"],
                timeout=5,
            )
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

    def _install_vscode_server(self):
        if not self.pane:
            QMessageBox.warning(self, "未连接", "请先连接容器")
            return

        s = load_session()
        vs = s.get("vscode_server", {})

        dlg = QDialog(self)
        dlg.setWindowTitle("安装 VS Code Server")
        layout = QFormLayout(dlg)

        home_edit = QLineEdit(vs.get("home", "/app"))
        layout.addRow("HOME 路径:", home_edit)

        commit_edit = QLineEdit(vs.get("commit", ""))
        commit_edit.setPlaceholderText("fdb98833154679dbaa7af67a5a29fe19e55c2b73")
        layout.addRow("Commit Hash:", commit_edit)

        server_label = QLabel(Path(vs["server"]).name if vs.get("server") else "未选择")
        server_btn = QPushButton("选择 server-linux-x64.tar.gz...")
        server_path = [vs["server"]] if vs.get("server") else []

        def pick_server():
            f, _ = QFileDialog.getOpenFileName(dlg, "选择 vscode-server-linux-x64.tar.gz")
            if f:
                server_path.clear()
                server_path.append(f)
                server_label.setText(Path(f).name)

        server_btn.clicked.connect(pick_server)
        row = QHBoxLayout()
        row.addWidget(server_label)
        row.addWidget(server_btn)
        layout.addRow("Server:", row)

        cli_label = QLabel(Path(vs["cli"]).name if vs.get("cli") else "未选择 (可选)")
        cli_btn = QPushButton("选择 CLI tar.gz...")
        cli_path = [vs["cli"]] if vs.get("cli") else []

        def pick_cli():
            f, _ = QFileDialog.getOpenFileName(dlg, "选择 CLI tar.gz")
            if f:
                cli_path.clear()
                cli_path.append(f)
                cli_label.setText(Path(f).name)

        cli_btn.clicked.connect(pick_cli)
        row2 = QHBoxLayout()
        row2.addWidget(cli_label)
        row2.addWidget(cli_btn)
        layout.addRow("CLI:", row2)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addRow(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        home = home_edit.text().strip()
        commit = commit_edit.text().strip() or "fdb98833154679dbaa7af67a5a29fe19e55c2b73"

        s["vscode_server"] = {
            "home": home,
            "commit": commit_edit.text().strip(),
        }
        if server_path:
            s["vscode_server"]["server"] = server_path[0]
        if cli_path:
            s["vscode_server"]["cli"] = cli_path[0]
        save_session(s)
        backend = self.pane.backend

        if server_path:
            backend.receive_paths(server_path, "/tmp")
        if cli_path:
            backend.receive_paths(cli_path, "/tmp")

        cmds = f"""export HOME={home}
mkdir -p $HOME/.vscode-server
"""
        if cli_path:
            cli_name = Path(cli_path[0]).name
            cmds += f"cp /tmp/{cli_name} $HOME/.vscode-server/vscode-cli-{commit}.tar.gz.done\n"

        if server_path:
            server_name = Path(server_path[0]).name
            cmds += f"mkdir -p $HOME/.vscode-server/cli/servers/Stable-{commit}/server\n"
            cmds += f"tar -xvzf /tmp/{server_name} --strip-components 1 -C $HOME/.vscode-server/cli/servers/Stable-{commit}/server\n"

        cmds += f"mkdir -p $HOME/.vscode-server/bin\n"
        cmds += f"ln -sf $HOME/.vscode-server/cli/servers/Stable-{commit}/server $HOME/.vscode-server/bin/{commit}\n"

        rc, stdout = backend.run_command(cmds, timeout=120)

        result = QDialog(self)
        result.setWindowTitle("安装结果")
        result.resize(600, 400)
        rl = QVBoxLayout(result)
        text = QTextEdit(stdout or f"返回码: {rc}")
        text.setReadOnly(True)
        rl.addWidget(text)
        result.exec()

    def _show_browser(self, backend):
        if self.pane:
            self.pane.deleteLater()
        self.pane = ContainerPane(backend)
        self.setCentralWidget(self.pane)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    if sys.platform.startswith("win"):
        window.setWindowIcon(QIcon("icon.ico"))
    window.show()
    if not window._auto_restore():
        window.connect_to_docker()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
