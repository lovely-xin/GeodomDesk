---
name: geodomdesk-cli
description: "使用 GeodomDesk CLI 操作已授权的设备。按任务选择 TCP 隧道、远程命令、文件传输或服务访问，管理会话并验证结果。适用于用户要求通过 GeodomDesk 操作设备或管理隧道时。"
---

# GeodomDesk CLI

## 使用原则

- **在发起端宿主机运行。** 在已获授权的宿主机非沙箱会话中运行 GeodomDesk CLI。登录、探测、远程执行和隧道管理都依赖发起端的网络环境。使用隧道的本地工具也在同一宿主机网络中运行。先按执行工具的权限流程取得宿主机网络访问，再测试设备连通性。
- **TCP 隧道优先。** 用户指定通道时按其选择。优先复用同一目标和服务的健康会话。执行命令时先用已授权的内置 SSH，再选择已配置的 TCP 服务路径或 GeodomDesk 终端。当前对端的能力决定使用结构化还是兼容终端。
- **CLI 优先。** 只有根据当前版本能力、实际错误或图形界面独有操作，确认 CLI 和隧道内的工具无法完成当前步骤时，才使用电脑操作技能。单次超时先诊断，身份或权限错误先解决原因。所有路径遵守同一授权范围。
- **先查权限，按需询问。** 优先使用已授权的账号和凭据，核对目标账号能否完成当前操作。缺少登录信息时，按 GeodomDesk 账号、设备接入、目标服务三个环节定位。请用户通过安全入口补齐所需信息。见 [权限检查与认证](references/session-recovery.md)。

## 快速选路

| 任务 | TCP 路径 | 条件与备选 |
| --- | --- | --- |
| Linux Bash | 支持独立内置 SSH 时用 `exec --shell bash` 或 `session --shell bash`。 | 用 `probe --transport ssh --shell bash` 确认显式 SSH。按当前版本支持的参数使用终端回退。 |
| Windows PowerShell | 支持独立内置 SSH 时用 `exec --shell powershell` 或 `session --shell powershell`。目标有 PowerShell 7 时可用 `--shell pwsh`。 | 已授权的 WinRM 可通过 TCP 隧道访问。按需使用 GeodomDesk 终端回退或专用管理员终端。 |
| SSH、SFTP、SCP、HTTPS 等服务 | `tunnel open`，让原生工具连接 `127.0.0.1:<local_port>`。同一服务映射可供多个工具使用。 | 从目标设备视角填写获准的服务地址和端口。优先让系统分配本地端口。 |
| 经远端设备访问另一台机器 | 以中转设备作为 `--id`。内置 SSH 用 `--ssh-host` 和 `--ssh-port`，通用 TCP 服务用 `--remote-host` 和 `--remote-port`。 | 分别验证中转设备和最终服务身份。见 [远端设备作为中转机](references/tcp-tunnels.md)。 |

## 执行闭环

1. **确认两端能力与权限。** 按[发起端 CLI 查找流程](references/cli-discovery.md)定位可执行文件，用 `--version` 和 `--help` 核对版本及功能。用已知主机名和平台校验目标。需要实测时，按任务选择只读命令或已知服务握手。
2. **执行。** 使用双方可用的通道。会话等 `ready` 再按支持的协议发送请求。显式隧道等 `tunnel_ready` 后使用返回端口，保持创建它的进程和 stdin。
3. **验证结果。** 有结构化结果时检查 `ok`、`exit_code`、`execution_state`，按返回编码解码。会话按支持的字段关联请求与连接。外部工具检查退出状态与实际结果。执行结果未知时先核对远端状态，再决定是否继续。
4. **关闭并核对。** 按本次 UUID 关闭隧道，确认数据连接、监听和记录已释放。关闭确认丢失时可重试原 UUID。会话按需要释放连接或结束进程。

发起端和目标端可以使用不同版本。显式 SSH 或终端用 `--transport` 选择。`--relay` 请求 GeodomDesk 中继路由。按当前版本的帮助与实际返回确认所用通道。

## 按需查阅

| 需要 | 参考 |
| --- | --- |
| 内置 SSH、中转和常驻会话 | [内置 SSH](references/embedded-ssh.md) |
| 建立、复用和关闭 TCP 映射，搭配原生工具 | [TCP 隧道与工具组合](references/tcp-tunnels.md) |
| Windows SSH、WinRM 和终端备选 | [Windows PowerShell 经 TCP 隧道](references/windows-powershell.md) |
| CLI 凭据、脚本、会话控制和故障恢复 | [脚本、会话与恢复](references/session-recovery.md) |
| 真实设备、数据通道和客户端界面的验证条件 | [实测方法](references/verification.md) |
