# Windows PowerShell 和 WinRM

1. **优先 TCP 隧道。** 已确认 OpenSSH 或 WinRM 已启用时，从发起端选择可用路径。独立 CLI 可用 `--transport ssh --shell powershell` 或 `pwsh`。随包 CLI 用 `tunnel open` 映射 SSH 端口，再用本机 OpenSSH。WinRM 使用显式映射和 PowerShell Remoting。脚本较长、大输出或传文件时优先使用显式映射和原生工具。
2. **先查明失败。** 条件不明时，用已授权的其他方式只读检查 `Get-Service sshd,WinRM`、`Get-NetTCPConnection -State Listen` 或 `winrm enumerate winrm/config/listener`，核对实际地址、端口、转发权限和认证。旧系统可用已有工具查看监听。凭据不足时，仅询问缺少的凭据或权限。
3. **确认无法建立隧道才回落。** 目标确实没有可用的 SSH/WinRM 服务或转发不可用，且 GeodomDesk 终端独立获授权时，用 `--transport terminal --shell powershell`。必须使用 `--admin-user` 完成 GeodomDesk 管理员终端认证时，选择其专用终端协议。通过 SSH 或 WinRM 的权限由相应目标账号决定。

```text
geodomdesk-cli exec --id DEVICE_ID --expect-platform Windows --expect-hostname HOSTNAME --transport ssh --shell powershell --command hostname --json
```

上例适用于支持 PowerShell over SSH 的独立 CLI，目标需开放 SSH。显式映射后用本机 OpenSSH 时，预先核对目标主机密钥，用 `HostKeyAlias` 关联实际目标。传复杂 PowerShell 脚本可按 UTF-16LE 编码为 Base64，再交给目标 `powershell.exe -EncodedCommand`。

WinRM 映射目标实际监听端口，确认认证和主机身份。回环端口可连之后，仍需满足 HTTPS 证书名称或 Kerberos SPN 的要求，并实际建立 PowerShell 会话。先 `Remove-PSSession`，再关闭外层隧道。
