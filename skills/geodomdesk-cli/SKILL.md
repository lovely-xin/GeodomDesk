---
name: geodomdesk-cli
description: "使用 GeodomDesk CLI 操作已授权的设备。区分随包版和独立版，优先经 TCP 隧道执行命令、传输文件或经中转设备访问服务，核对结果并释放连接。"
---

# GeodomDesk CLI

1. **核对发起端和目标。** 首次用 `--version`、`--help` 确认 CLI 路径和能力，后续复用。按用户指定的目标操作，能力验收未指定目标时，优先选取最近访问中已授权的在线设备。已知平台和主机名时传入 `--expect-platform`、`--expect-hostname`。仅在所需网络或用户配置访问受限时申请对应权限。支持 `device list --recent --online --json` 时用它找候选，再按设备 ID 核对目标。
2. **优先 TCP 隧道。** Linux Bash、Windows PowerShell 和经 A 中转访问 B 都优先使用可用的 TCP 隧道。已知服务和权限时直接建立。轻量 Shell 可选该 CLI 支持的显式 `--transport ssh`，大输出、文件、长任务或其他 TCP 服务用 `tunnel open` 配合原生工具。中转时核对 A 的设备身份和 B 的服务身份。单个本地工具可用 `tunnel run` 托管隧道生命周期。
3. **查清原因再回落。** 建隧道失败时定位账号、设备认证、转发权限、网络及目标服务握手。信息不足时用已有授权通道做最小只读检查，确认服务、地址和端口后再试隧道。只有确认 TCP 隧道不可用且替代通道获授权，才回落到 `--transport terminal` 或其他适用方式。`--relay` 选择 GeodomDesk 中继线路。遇到凭据拒绝、身份不匹配或主机密钥异常，先处理对应问题。仅在确认 CLI 无法完成且用户明确授权时，才使用电脑操作能力。支持 `doctor` 时，用它检查指定转发端口并读取故障阶段。
4. **分层核对权限。** 区分 GeodomDesk 账号、设备接入、终端或转发权限，以及目标系统或服务凭据。优先复用有效登录与信任记录，只询问阻塞该步骤的账号、权限或凭据信息。密码与口令通过无回显提示或受控凭据环境输入。
5. **验证后关闭。** 以应用握手或脚本真实结果确认成功。`execution_state: "unknown"` 时先只读核查远端状态再决定是否重试。`session` 用 `{"op":"exit"}` 或关闭 stdin 结束，`close` 只断开当前会话连接。显式隧道用 `{"op":"close"}` 或本次 UUID 执行 `tunnel close`，再核对监听和列表。失败时报告阶段、脱敏错误和下一步。

## 按需查阅

- [CLI 发现和版本区别](references/cli-discovery.md)
- [SSH、账号和主机信任](references/embedded-ssh.md)
- [TCP 隧道、原生工具和中转](references/tcp-tunnels.md)
- [Windows PowerShell 和 WinRM](references/windows-powershell.md)
- [会话控制和故障恢复](references/session-recovery.md)
- [实测和清理验证](references/verification.md)
