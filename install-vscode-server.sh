#!/usr/bin/env bash

export HOME=/app # 容器和宿主机不一样

mkdir -p $HOME/.vscode-server
cp /tmp/vscode_cli_alpine_x64_cli.tar.gz $HOME/.vscode-server/vscode-cli-fdb98833154679dbaa7af67a5a29fe19e55c2b73.tar.gz.done


mkdir -p $HOME/.vscode-server/cli/servers/Stable-fdb98833154679dbaa7af67a5a29fe19e55c2b73/server
tar -xvzf /tmp/vscode-server-linux-x64.tar.gz --strip-components 1 -C $HOME/.vscode-server/cli/servers/Stable-fdb98833154679dbaa7af67a5a29fe19e55c2b73/server


mkdir -p $HOME/.vscode-server/bin
ln -s $HOME/.vscode-server/cli/servers/Stable-fdb98833154679dbaa7af67a5a29fe19e55c2b73/server $HOME/.vscode-server/bin/fdb98833154679dbaa7af67a5a29fe19e55c2b73