# TCP 隧道、原生工具和中转

显式映射适合 SFTP、SCP、数据库、HTTPS、WinRM、大输出和长任务。转发原始 TCP 数据，应用工具自行控制超时、认证和加密。多路复用取决于双方能力，以运行时 `multiplexed` 为准。

```text
geodomdesk-cli tunnel open --id DEVICE_ID --remote-host SERVICE_HOST --remote-port SERVICE_PORT --expect-platform PLATFORM --expect-hostname DEVICE_HOST
geodomdesk-cli tunnel list --id DEVICE_ID
geodomdesk-cli tunnel close --id DEVICE_ID --tunnel-id TUNNEL_UUID
```

1. 保持 `tunnel open` 的进程和 stdin 打开。等待 `tunnel_ready`，记录本次 UUID、`local_host` 和 `local_port`。省略 `--local-port` 让 CLI 选择空闲端口。
2. 在能访问该回环地址的发起端使用原生工具完成实际应用握手。`tunnel_ready` 表示映射已建成，仍需验证监听后的目标服务。SSH 校验目标主机密钥，HTTPS 保留实际 Host 和 SNI。文件传输按需要核对目标路径、大小或哈希。
3. 收尾时给隧道进程发送一行 `{"op":"close"}`，或在同一用户和配置环境另起 `tunnel close`。只关闭本次任务持有的 UUID。核对 `tunnel_closed`、列表已无该记录且本地监听已释放。仅对精确识别的本次子进程处理退出异常。

## 单个本地工具

新版 CLI 可用外部工具的生命周期托管映射。以下示例假设 SSH 已授权，并已经有目标的可信主机密钥记录。

```text
geodomdesk-cli doctor --id DEVICE_ID --remote-port 22 --expect-hostname HOSTNAME --pretty
geodomdesk-cli tunnel run --id DEVICE_ID --remote-port 22 --expect-hostname HOSTNAME -- ssh -o HostKeyAlias=TRUSTED_ALIAS -p "{port}" OS_USER@127.0.0.1
```

`doctor` 的 TCP 成功表示转发到指定端口已建立。根据应用协议验证 SSH 指纹、HTTPS 证书或 WinRM 会话。工具参数可用独立的 `{port}` 和 `{address}`，工具进程也可读取 `GEODOMDESK_TUNNEL_HOST` 和 `GEODOMDESK_TUNNEL_PORT`。CLI 原样返回工具输出和常规退出码，工具退出后回收本次隧道。工具需在前台持有连接。Linux 长会话可经 SSH 使用 tmux。Windows 已启用 WinRM 时可用可断开的 PowerShell 会话。旧版本使用 `tunnel open/list/close`。

## 经 A 到 B

`--id` 和预期平台、主机名指向 A。`--remote-host` 和 `--remote-port` 填 A 能访问的 B 地址和服务端口。访问 A 自己的服务时，按实际监听填地址，例如 `127.0.0.1`。向 B 验证它自己的服务身份和凭据。

建连失败时把问题拆成发起端到 A、A 到 B、B 的应用握手三段。可经已授权终端在 A 上只读核对 DNS、连通性和端口，再试显式转发。确认无法经 A 转发，且允许由 A 主动连接 B 时，可在 A 上使用获授权的工具。收尾先释放 A 上创建的会话，再关外层通道。
