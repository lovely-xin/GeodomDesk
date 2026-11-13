# Windows PowerShell 经 TCP 隧道

## 适用条件

发起端支持 TCP 隧道时，优先用目标已配置并获准访问的 SSH 或 WinRM。通过原生协议执行命令可避免依赖兼容终端的回显和脚本上传流程。实际稳定性还取决于中继链路、目标服务与认证。选择能完成身份验证和只读检查的通道。

| 目标已提供的能力 | 用法 |
| --- | --- |
| SSH 可登录并允许执行命令 | 通过 TCP 隧道运行 `powershell.exe` 或已安装的 `pwsh`。适合单次命令或普通 SSH 交互。 |
| 已配置 PowerShell Remoting 的 SSH 子系统 | 两端支持 SSH Remoting 时，经 TCP 隧道用 `New-PSSession` 建立会话，再用 `Enter-PSSession` 交互或 `Invoke-Command -Session` 执行多条命令。保留 SSH 主机校验，关闭隧道前用 `Remove-PSSession` 释放会话。 |
| 已配置 WinRM 会话端点 | 即使 SSH 未启用，也可用 TCP 隧道连接现有 WinRM 端点，再用 `New-PSSession` 或 `Invoke-Command`。见下方 WinRM 步骤。 |

PowerShell Remoting 的 SSH 模式需要目标 SSH 服务配置相应子系统。根据实际配置选择通道。 [PowerShell Remoting over SSH](https://learn.microsoft.com/en-us/powershell/scripting/security/remoting/ssh-remoting-in-powershell) · [PowerShell 远程连接要求](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_remote_requirements)

## SSH 路径

先确认 SSH 服务地址、端口、登录方式及已核验的主机密钥。独立 CLI 支持时优先用 `exec --shell powershell` 或 `session --shell powershell`。显式要求 SSH 时加 `--transport ssh`。见 [内置 SSH](embedded-ssh.md)。使用 Windows OpenSSH 服务时，可通过已授权的终端只读查询 `Get-Service sshd -ErrorAction SilentlyContinue`，再核对实际监听和登录能力。

```text
geodomdesk-cli tunnel open --id "DEVICE_ID" --remote-host SSH_SERVICE_ADDRESS --remote-port SSH_PORT --local-port 0 --expect-platform Windows --expect-hostname EXPECTED_HOST --non-interactive
```

`SSH_PORT` 是已确认的目标 SSH 端口。等待 `tunnel_ready` 并保存 `local_port` 和 `tunnel_id`。由原进程保持 stdin 打开。外部 SSH 使用自己的 agent、密钥或交互登录。GeodomDesk 设备密码用于建立设备连接，`GEODOMDESK_SSH_PASSWORD` 是 CLI 内置 SSH 的操作系统登录凭据。用按设备和端口固定的 `HostKeyAlias` 匹配事先核验的 OpenSSH 可信记录。需要指定单独文件时可在对应的 OpenSSH 配置里设置 `UserKnownHostsFile`。

### 用 SSH 执行只读 PowerShell

将示例作为发起端 PowerShell 脚本运行。传入 `tunnel_ready.local_port`、目标 SSH 账号和已核验的主机别名。示例用密钥或 agent 认证。需要交互登录时去掉 `-n` 和 `'-o', 'BatchMode=yes'`，在发起端终端按提示输入。目标安装了 `pwsh` 时可选择该程序。

```powershell
param(
    [Parameter(Mandatory)][ValidateRange(1, 65535)][int]$localPort,
    [Parameter(Mandatory)][string]$sshUser,
    [Parameter(Mandatory)][string]$hostAlias,
    [Parameter(Mandatory)][ValidateScript({ $_ -gt 0 })][int]$connectSeconds
)
$script = @'
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::WriteLine((Get-Location).Path)
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
$sshArgs = @('-T', '-n', '-p', "$localPort", '-l', $sshUser,
    '-o', "HostKeyAlias=$hostAlias", '-o', 'StrictHostKeyChecking=yes',
    '-o', 'BatchMode=yes', '-o', "ConnectTimeout=$connectSeconds", '127.0.0.1')
& ssh @sshArgs "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded"
exit $LASTEXITCODE
```

`-EncodedCommand` 用 UTF-16LE 编码保留脚本引号和 Unicode。凭据用独立认证。`ConnectTimeout` 管建连，调用方为整次执行另设时限。结合 SSH 返回的退出码、stderr 和预期输出核对结果。执行中途掉线时先核对远端状态，再决定是否提交新请求。较长的脚本可经已授权的 SFTP 传送并验证文件，再按任务范围执行和清理。 [Windows OpenSSH 配置](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration) · [PowerShell 命令参数](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)

## WinRM 路径

1. 核对目标已有的 WinRM 监听地址、端口、协议和 PowerShell 会话端点。用 `tunnel open` 映射该服务，等待 `tunnel_ready`。见 [TCP 隧道与工具组合](tcp-tunnels.md)。
2. 使用能把预期服务名路由到隧道端口的现有连接配置。WinRM 经 HTTPS 时，连接名称须匹配证书。使用域认证时，服务身份应满足 Kerberos 要求。连接回环端口后，再用实际会话核对证书与账号认证。 [WinRM 远程连接排查](https://learn.microsoft.com/powershell/module/microsoft.powershell.core/about/about_remote_troubleshooting) · [WinRM HTTPS 证书要求](https://learn.microsoft.com/en-us/troubleshoot/windows-client/system-management-components/configure-winrm-for-https)
3. 用实际认证建立会话，先运行只读命令确认目标身份和输出。业务结束后释放 PSSession，再关闭隧道。遇到意外的证书或身份不符时先核实原因。现有连接配置无法通过隧道保留所需服务身份时，可在核实目标且业务命令尚未发出的前提下，使用已授权的 GeodomDesk 终端中继。

## 复用、备选与清理

- 同一条 TCP 映射可供多个 PowerShell 命令或 SFTP 使用。SSH 会话由 SSH 客户端管理。要共享同一 Shell 的变量和目录，在同一脚本或同一交互 SSH 会话中完成。结束后按原 UUID 关闭并验证，见 [TCP 隧道与工具组合](tcp-tunnels.md)。
- 先核对发起端 `--transport ssh` 支持的 shell。旧版可用显式 TCP 隧道搭配已有的 SSH 工具执行 PowerShell。缺少可用的隧道执行路径、命令尚未发出且终端已获授权时，使用当前 CLI 支持的 `exec --shell powershell --transport terminal --relay` 或 `session --shell powershell --transport terminal --relay`。专用 Windows 管理员终端按其独立凭据流程操作，见 [脚本、会话与恢复](session-recovery.md)。
