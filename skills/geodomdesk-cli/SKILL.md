---
name: geodomdesk-cli
description: "使用 GeodomDesk CLI 操作已授权的设备。优先通过 TCP 隧道运行远程终端、经设备中转、传输文件或访问服务，并验证结果和清理连接。"
---

# GeodomDesk CLI

## 使用原则

- **在发起端宿主机运行。** 在已获授权的宿主机非沙箱会话中运行 GeodomDesk CLI。登录、探测、远程执行和隧道管理都依赖发起端的网络环境。需要额外权限时，先走执行工具的授权流程。
- **TCP 隧道优先。** 用户指定通道时按其选择。默认复用同一目标和服务的健康隧道。执行命令时优先通过受管 SSH 或显式 TCP 映射连接目标终端。中转时，发起端的终端优先经中转设备的 TCP 隧道连接最终服务。
- **按需预检。** 地址、端口、服务或权限不明时，可先用现有配置、已授权的 CLI 终端、SSH 或管理工具做最小只读检查。信息明确后，在准备使用的 TCP 隧道内验证握手并执行实际任务。见 [建隧道前的确认](references/tcp-tunnels.md#建隧道前的确认)。
- **CLI 优先。** 只有根据当前版本能力、实际错误或图形界面独有操作，确认 CLI 和隧道内的工具无法完成当前步骤时，才使用电脑操作技能。单次超时先诊断，身份或权限错误先解决原因。所有路径遵守同一授权范围。
- **先查权限，按需询问。** 优先使用已授权的账号和凭据，核对目标账号能否完成当前操作。确实缺账号时询问目标账号，确实缺密码时请用户通过对应工具的安全输入方式提供。账号已登录但权限不足时，询问所需的最小授权或可用账号。见 [权限检查与认证](references/session-recovery.md)。

## 快速选路

| 任务 | TCP 路径 | 条件与备选 |
| --- | --- | --- |
| Linux Bash | 支持时用 `exec` 或 `session` 配合 `--transport ssh --shell bash`。 | 也可显式建隧道并使用已有的 SSH 客户端。 |
| Windows PowerShell | 支持该 Shell 的独立 CLI 用 `--transport ssh --shell powershell`。目标有 PowerShell 7 时可选 `--shell pwsh`。 | 用显式 TCP 隧道访问目标已有的 SSH 或 WinRM。按帮助确认本机 CLI 支持哪种 Shell。 |
| SSH、SFTP、SCP、HTTPS 等服务 | `tunnel open`，让对应工具连接返回的本地回环端口。 | 从目标设备视角填写服务地址和端口，优先让系统分配本地端口。 |
| 中转到另一台设备 | `--id` 填中转设备。受管 SSH 用 `--ssh-host` 和 `--ssh-port`。显式映射用 `--remote-host` 和 `--remote-port`。 | 终端在发起端运行，Shell 和服务凭据对应最终设备。见 [远端设备作为中转机](references/tcp-tunnels.md#远端设备作为中转机)。 |

## 执行闭环

1. **确认两端能力与权限。** 按[发起端 CLI 查找流程](references/cli-discovery.md)定位并验证可执行文件，用 `--help` 核对当前版本支持的命令和参数。必要时用支持的探测或最小只读检查，确认目标平台、服务、当前账号和所需权限。支持时用 `--expect-platform`、`--expect-hostname` 防止选错目标。设备接入、服务登录和操作权限各自验证。
2. **执行。** 使用双方可用的通道。会话等 `ready` 再按支持的协议发送请求。显式隧道等 `tunnel_ready` 后使用返回端口，保持创建它的进程和 stdin。
3. **验证结果。** 有结构化结果时检查 `ok`、`exit_code`、`execution_state`，按返回编码解码。会话按支持的字段关联请求与连接。外部工具检查退出状态与实际结果。执行结果未知时先核对远端状态，再决定是否继续。
4. **关闭并核对。** 按本次 UUID 关闭隧道，确认数据连接、监听和记录已释放。关闭确认丢失时可重试原 UUID。会话按需要释放连接或结束进程。

TCP 建连失败时，先用其他已授权方式[定位原因](references/tcp-tunnels.md#建连失败后的诊断与回落)。修复可恢复的问题后重试 TCP。确认当前条件下 TCP 路径不可用、命令尚未发出且任务允许替代通道时，才回落到已授权的 GeodomDesk 终端。支持时用 `--transport terminal` 选终端，按需加 `--relay` 选中继路由。核对实际通道用 `command_transport` 或服务协议握手。单有 `transport: Relay` 只说明底层走中继。

## 按需查阅

| 需要 | 参考 |
| --- | --- |
| 内置 SSH、中转和常驻会话 | [内置 SSH](references/embedded-ssh.md) |
| 建立、复用和关闭 TCP 映射，搭配原生工具 | [TCP 隧道与工具组合](references/tcp-tunnels.md) |
| Windows SSH、WinRM 和终端备选 | [Windows PowerShell 经 TCP 隧道](references/windows-powershell.md) |
| CLI 凭据、脚本、会话控制和故障恢复 | [脚本、会话与恢复](references/session-recovery.md) |
| 真实设备、数据通道和客户端界面的验证条件 | [实测方法](references/verification.md) |
