# 内置 SSH

1. 按[发起端查找流程](cli-discovery.md)定位 CLI，确认帮助包含 `embedded SSH over TCP`。核对已授权设备及服务。单次执行用 `exec`，多条命令用 `session`。
2. 选择目标支持的 `--shell bash`、`--shell powershell` 或 `--shell pwsh`。健康会话可复用。自动模式会先核验 GeodomDesk 账号，再经管理的 TCP 隧道尝试 SSH。默认 SSH 服务不可达或无可用 SSH 凭据时，可按当前版本回退至已授权的 GeodomDesk 终端。显式 SSH、下游主机或专用 SSH 参数按指定路径处理。
3. 按需提供凭据。设备接入使用 GeodomDesk 账号和设备凭据，目标 SSH 登录使用目标系统账号和私钥或密码。设备密码和 SSH 密码分别用 `--ask-device-password`、`--ask-ssh-password` 读取。内置 SSH 的私钥用 `--ssh-identity PATH` 指定，按无回显提示输入私钥口令。身份不匹配、密钥变化或认证拒绝时先核实原因。
4. 核对 `command_transport`、`ssh_destination`、退出码和只读输出。需要从卡住的连接恢复时用 `connection: "fresh"` 或 `op: "reconnect"`。执行状态未知时先检查远端结果。

## 登录与交互

- 使用支持 `account login --user USER` 的独立 CLI 时，可用无回显提示登录。后续进程复用当前用户的登录。`account logout` 撤销该登录。单次登录使用 `--account-user USER`。
- 执行新命令前核验 GeodomDesk 账号。SSH 密码和私钥口令在建立隧道前读取，同一进程换连接时复用已输入的凭据。等待输入时按 Ctrl+C 退出。会话收到 `ready` 后，用 `op: "cancel"` 中断当前请求，并核对本次隧道已释放。

## 中转与常驻

- `--id`、`--expect-hostname` 和 `--expect-platform` 对应中转设备。`--ssh-host`、`--ssh-port`、`--ssh-user` 和 `--shell` 对应最终服务。首次连接用可信来源核验后的 `--ssh-host-fingerprint SHA256:HASH` 或已有的 `--ssh-known-hosts`。
- 服务进程用 `session` 保持 stdin 和 stdout，按 JSON 行发送命令和控制。等 `ready` 后下发，按 ID 关联结果。结束时发送 `op: "close"`，关闭 stdin 并核对退出。
- 内置 SSH 的隧道由当前 CLI 管理。用 `tunnel list` 核对 UUID 和 `private_stream: true`，按需用 `tunnel close` 关闭该 UUID。使用通用 TCP 服务时另建显式端口映射。
