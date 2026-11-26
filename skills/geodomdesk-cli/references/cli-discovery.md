# CLI 发现和版本区别

优先检查用户指定路径、应用安装目录和 `PATH`。Windows 可用 `Get-Command geodomdesk-cli.exe -CommandType Application -All`，再查安装记录的 `InstallLocation`。Linux 可用 `command -v geodomdesk-cli`，再查包管理器清单或独立包解压目录。Windows、WSL 与其他系统用户各有自己的配置和登录状态。优先在具备任务所需登录态和网络的环境运行。

对候选程序运行一次 `--version` 与 `--help`。版本号只标识构建，能力以该程序帮助和对应发布说明为准。独立版的帮助通常包含 `embedded SSH over TCP` 和 `account` 子命令。

| 能力 | 随客户端安装的 CLI | 独立 CLI |
| --- | --- | --- |
| GeodomDesk 账号 | 复用同环境桌面登录，或用 `--account-user` 单次登录 | 支持 `account login` 和 `account logout`，也可复用桌面登录或单次登录。较新构建可用 `account status` |
| SSH 到目标系统 | Bash 可调用本机 OpenSSH，经 CLI 私有回环隧道转发。PowerShell 经 SSH 时，用显式 TCP 映射搭配本机 SSH | 内置 SSH，无需本机 OpenSSH。支持 Bash，以及显式指定 SSH 的 PowerShell 或 pwsh |
| SSH 信任与口令 | `--ssh-known-hosts`、`--ssh-strict-host-key`，按本机 OpenSSH 能力使用 agent 或密钥 | 另支持 `--ssh-host-fingerprint` 和 `GEODOMDESK_SSH_KEY_PASSPHRASE` |
| 显式 TCP 映射 | `tunnel open/list/close` | `tunnel open/list/close` |

本地查询、参数检查和文件检查用当前权限运行。实际网络、用户配置或长连接管理被限制时，按运行器机制申请所需访问。环境拦截应报告为发起端问题。使用长连接前，确认运行器可保持同一子进程和 stdin 可写。
