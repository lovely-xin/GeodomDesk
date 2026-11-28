# GeodomDesk 独立 CLI

Windows 使用 ZIP 包，Ubuntu 使用保留执行权限的 TAR.GZ 包。用随包提供的 SHA-256 清单核验压缩包。解压后在 Windows 运行 `.\geodomdesk-cli.exe --help`，在 Ubuntu 运行 `./geodomdesk-cli --help`。帮助中出现 `embedded SSH over TCP` 即为内置 SSH 版本。Ubuntu 由系统提供程序依赖的运行库。被控设备或中转设备继续使用已授权的 GeodomDesk 服务。

Codex 用户可按包内 `skills/INSTALL.md` 安装 `skills/geodomdesk-cli`。技能和 CLI 文件一起列入包内 `SHA256SUMS`。

## 账号登录

```text
geodomdesk-cli account login --user USER
geodomdesk-cli account logout
```

登录时通过无回显提示输入 GeodomDesk 账号密码。CLI 将本次登录的令牌加密保存在当前用户的配置目录，并绑定到当前账号服务。后续进程可复用该登录，重新登录会撤销同一服务的旧令牌。`account logout` 撤销并清理独立 CLI 的令牌。CLI 也可复用仍有效的桌面客户端登录，撤销该令牌需在桌面客户端退出。单次使用其他账号时传入 `--account-user USER`。

## 执行命令

先从最近访问记录核对设备 ID、在线状态和设备身份。一次性执行用 `exec`，在同一目标上连续执行用 `session`。选择 `bash`、`powershell` 或 `pwsh`，脚本可用 `--command`、`--script FILE` 或 `--script -` 提供。

```text
geodomdesk-cli exec --id DEVICE_ID --expect-hostname HOSTNAME --shell bash --command 'hostname' --json
geodomdesk-cli exec --id DEVICE_ID --expect-platform Windows --shell powershell --command 'hostname' --json
```

Bash 默认先通过托管 TCP 隧道尝试 SSH。PowerShell 指定 `--transport ssh` 或 SSH 参数时走 SSH。PowerShell 省略这些参数时使用 GeodomDesk 终端。默认 Bash 仅在 SSH 明确不可用且允许回落时尝试已授权的 GeodomDesk 终端。服务拒绝登录或主机校验失败时直接返回原因。`--transport ssh` 要求使用 SSH，`--transport terminal` 直接使用 GeodomDesk 终端。无 `--shell` 的会话支持在每条请求中选择 shell，并使用 GeodomDesk 终端。

CLI 优先复用独立 CLI 的账号登录和已保存的设备凭据。需要设备密码时加 `--ask-device-password`。SSH 服务按目标操作系统账号认证，支持 `--ssh-user USER`、`--ssh-identity FILE` 和 `--ask-ssh-password`。交互密码和私钥口令在建连前无回显输入，同一 CLI 进程会复用已读入的 SSH 凭据。自动化可通过进程环境传入对应凭据并加 `--non-interactive`。将账号密码、设备密码、目标 SSH 密码和私钥口令分别保管。

## 查找设备和验证 TCP 链路

```text
geodomdesk-cli device list --recent --online --json
geodomdesk-cli doctor --id DEVICE_ID --remote-port 22 --expect-hostname HOSTNAME --pretty
geodomdesk-cli tunnel run --id DEVICE_ID --remote-port 22 --expect-hostname HOSTNAME -- ssh -o HostKeyAlias=TRUSTED_ALIAS -p "{port}" OS_USER@127.0.0.1
```

`device list` 优先读取当前账号匹配的桌面最近访问记录与本地同步缓存。没有可用记录时，通过当前账号查询云端最近访问。`--online` 只返回本次在线查询确认在线的条目。设备认证、功能授权和目标身份由后续连接检查。

`doctor` 省略 `--remote-port` 时检查终端能力。指定端口时检查转发连接，返回阶段、实际链路和下一步。应用协议握手由相应工具完成。`tunnel run` 在本机启动指定程序，并在该程序结束时回收该映射。支持完整参数 `{port}`、`{address}` 以及 `GEODOMDESK_TUNNEL_PORT`。随包版也可用此流程对接系统 OpenSSH、WinRM 或其他 TCP 工具。

## 恢复长会话与查看实时输出

- Linux 有 SSH 和 tmux 时，用上面的命令打开隧道，在远端执行 `tmux new-session -A -s NAME`。用 `Ctrl+b d` 挂起后，重建隧道即可用 `tmux attach -t NAME` 恢复。用 `tmux list-sessions` 查看，用 `tmux kill-session -t NAME` 结束本次会话。
- Windows 已启用 WinRM 时，经隧道连接预先配置的 WinRM 端点，按现有证书及账号策略认证。用 `Disconnect-PSSession` 保留会话，重连后用 `Connect-PSSession` 或 `Receive-PSSession` 恢复，完成后用 `Remove-PSSession` 删除。
- 实时输出由 `tunnel run` 启动的工具直接传出，可分别处理 stdout、stderr 和退出码。需要结构化结果时用 `exec --json`，多条短命令可复用 `session`。

## 中转到下游设备

`--id` 选择已授权的 GeodomDesk 中转设备，`--ssh-host` 和 `--ssh-port` 选择中转设备能够访问的下游 SSH 服务。中转设备负责 TCP 转发，下游服务负责操作系统账号认证和执行。命令的 `--expect-hostname` 和 `--expect-platform` 校验中转设备，SSH 主机密钥校验下游设备。

```text
geodomdesk-cli exec --id RELAY_DEVICE_ID --expect-hostname RELAY_HOSTNAME --transport ssh --ssh-host DESTINATION --ssh-port 22 --ssh-user OS_USER --ssh-host-fingerprint SHA256:TRUSTED_FINGERPRINT --ssh-identity KEY_FILE --shell bash --command 'hostname' --json
```

首次使用下游设备时，从管理员取得主机指纹并传给 `--ssh-host-fingerprint`，或提供已经配置的 `--ssh-known-hosts`。后续会话复用按中转设备、目的地址和端口保存的主机密钥。已有匹配记录时可以使用只读信任文件。首次保存新密钥时使用可写的信任目录。发现密钥变化时先通过可信渠道核实，再更新信任记录。

## 常驻会话和回收

```text
geodomdesk-cli session --id DEVICE_ID --shell powershell --expect-hostname HOSTNAME --non-interactive
```

保持标准输入打开，每行发送一个 JSON 请求。会话逐条执行并返回 JSON，`stdout` 和 `stderr` 为 Base64。

```json
{"id":"first","script":"hostname"}
{"id":"check","op":"status"}
{"id":"fresh","connection":"fresh","script":"hostname"}
{"id":"close","op":"close"}
```

健康连接优先复用。每条新命令发出前都会重新校验 GeodomDesk 账号。`connection: "fresh"` 或 `op: "reconnect"` 重新建立连接。启动时可用 Ctrl+C 退出凭据输入。收到 `ready` 后，`op: "cancel"` 可中断当前请求并清理本次连接。结果为 `execution_state: "unknown"` 时，先用只读操作核对远端状态，再决定是否重新提交。关闭输入或结束进程会释放会话和所属隧道。

`tunnel list --id DEVICE_ID` 显示内置 SSH 管理的隧道。`private_stream: true` 表示 SSH 流只在 CLI 进程内使用。访问 HTTP、HTTPS、RDP 等已知服务时，使用 `tunnel open` 管理本地 TCP 端口，并用服务实际握手核验。独立 CLI 会在建立映射和接受新数据连接时核验账号。`tunnel close --id DEVICE_ID --tunnel-id UUID` 在释放监听端口和数据连接后返回关闭确认。

## 构建

在新的已审核 RustDesk 源码目录中检出递归子模块。按平台安装项目的本地库依赖，使用 Rust 1.89.0 构建。GitHub 的 `Build standalone GeodomDesk CLI` 工作流会在 Windows 和 Ubuntu 上运行测试，解压并核验生成的包。主分支构建通过后发布带 `cli-v` 标签的独立 CLI。

```text
python customization/packaging/prepare_client.py --source UPSTREAM_DIRECTORY --version UPSTREAM_VERSION --build-number BUILD_NUMBER --standalone-cli
cd UPSTREAM_DIRECTORY
cargo +1.89.0 test --locked --release --features embedded-ssh --lib terminal_exec::
cargo +1.89.0 test --locked --release --features embedded-ssh --lib port_forward::
cargo +1.89.0 build --locked --release --features embedded-ssh --bin geodomdesk-cli
```
