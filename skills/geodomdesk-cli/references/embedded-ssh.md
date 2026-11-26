# SSH、账号和主机信任

- **选路。** 已确认 SSH 可用且要求走隧道时，选择 `--transport ssh`。独立版可在私有受管 TCP 隧道内直接执行。随包版的 Bash SSH 依赖发起端 OpenSSH。Windows 随包版按 [Windows PowerShell](windows-powershell.md) 建显式映射。需要流式传输、长任务或大输出时，使用 [显式 TCP 映射](tcp-tunnels.md)。
- **默认模式。** Bash 的 `auto` 会尝试 SSH。独立版不同构建的 PowerShell `auto` 和建连失败回落条件可能不同。需要走 TCP 隧道时显式指定 `--transport ssh` 或使用 `tunnel open`，检查实际通道、目标身份、退出码和输出。账号拒绝、主机密钥变化和执行状态未知时先定位原因。
- **账号。** 独立版用 `account login --user USER` 持久登录。`account logout` 清除并请求撤销独立 CLI 的登录令牌。桌面会话仍有效时，后续调用可以复用它。帮助列出 `account status` 时可核对账号来源，否则用隔离配置中的只读 `probe` 检查登录是否有效。支持短期账号校验缓存的构建仅在同一进程内复用该缓存。
- **目标 SSH。** 按需指定 `--ssh-user`、`--ssh-port`、`--ssh-identity`，或用 `--ask-ssh-password`。独立版的加密私钥使用无回显提示，非交互时由受控环境提供 `GEODOMDESK_SSH_KEY_PASSPHRASE`。随包版通过本机 OpenSSH 的密钥与 agent 机制解锁。GeodomDesk 账号、设备密码、SSH 密码和私钥口令分别核对。
- **经 A 到 B。** 使用独立 CLI 内置 SSH 时，`--id` 和预期主机信息用于 A，显式指定 `--ssh-host B` 时同时传 `--ssh-user` 和 `--shell`。用已核验的 `--ssh-host-fingerprint` 或 `--ssh-known-hosts` 核对 B。密钥变化时先通过可信渠道复核。

独立版内部 SSH 隧道的 `private_stream: true` 表示数据流留在 CLI 进程内。结束 `session` 后，用 `tunnel list` 核对所属记录已释放。
