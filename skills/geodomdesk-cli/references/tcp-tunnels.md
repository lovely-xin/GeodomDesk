# TCP 隧道与工具组合

## 建立映射

先确认发起端 `--help` 支持 `tunnel`，再核对目标设备、服务地址和端口。示例使用实际 CLI 路径及已获授权的值。`--remote-host` 从目标设备视角解析，目标自身的回环服务填 `127.0.0.1`，其他主机须在授权范围内。

```text
geodomdesk-cli tunnel open --id "DEVICE_ID" --remote-host SERVICE_ADDRESS --remote-port SERVICE_PORT --local-port 0 --non-interactive --expect-platform TARGET_PLATFORM --expect-hostname EXPECTED_HOST
geodomdesk-cli tunnel list --id "DEVICE_ID"
geodomdesk-cli tunnel close --id "DEVICE_ID" --tunnel-id UUID
```

隧道所有者与 `list`、`close` 在同一宿主机上使用同一用户配置。连接回环端口的工具也在该宿主机的同一网络环境运行。使用只监听发起端回环地址的中继映射。核对返回的监听地址及中继状态。支持时用 `--local-port 0` 让系统分配端口。固定端口占用时换用空闲端口。设备 ID 按发起端支持的格式输入。连接数、闲置与传输超时以当前实现为准，诊断时检查 `last_error` 和实际服务响应。

## 使用与收尾

1. 把 `open` 作为受控子进程启动，保留 stdin，持续读取 stdout 的 JSON Lines 与 stderr。等待 `tunnel_ready`，保存 `tunnel_id`、`local_host`、`local_port`。每个调用者要记录自己创建的 UUID。
2. 用另一进程连接返回的 `127.0.0.1:<local_port>`，按服务协议验证结果。`tunnel_ready` 表示监听已建立，后端仍须做真实请求验证。用 `tunnel list --id` 查看实时的 `state`、`active_connections`、`last_error`。只比较本次持有的 UUID，其他隧道可能由别的任务使用。
3. 工具完成或报错后，在清理路径中向 `open` 的 stdin 写 `{"op":"close"}` 并 flush，或从另一进程按 UUID 执行 `tunnel close`。可以通过 `{"op":"status"}` 查状态。EOF 或正常退出也会释放映射。需持续使用时保留进程与 stdin。
4. 支持逐连接账号校验的独立 CLI 会在接受新数据连接前核验账号。注销后新连接被拒绝时，重新登录并重建映射。收到关闭确认后，等待所有者退出，确认列表中该 UUID 消失。需要端到端验证时，检查正在使用的数据连接结束且本地监听已释放。若另一个进程在关闭后抢到同一随机端口，按 UUID 和所有者判断清理，避免误关新服务。

## 远端设备作为中转机

设 A 为已授权的 GeodomDesk 中转设备，B 为已授权访问的服务所在设备。选择哪种方式取决于工具应在哪台机器运行。

### 发起端运行工具，由 A 转发 TCP

路径是 `发起端回环端口 → GeodomDesk TCP 隧道 → A → B 的指定服务`。GeodomDesk 中继承载到 A 的连接，A 从自己的网络解析服务地址并连接 B。

```text
geodomdesk-cli tunnel open --id "JUMP_DEVICE_ID" --remote-host FINAL_SERVICE_ADDRESS --remote-port FINAL_SERVICE_PORT --local-port 0 --non-interactive --expect-platform JUMP_PLATFORM --expect-hostname JUMP_HOSTNAME
```

- `--id`、`--expect-platform` 和 `--expect-hostname` 对应 A。服务地址、端口、登录凭据和应用协议的身份校验对应 B。B 可以只提供所需的 TCP 服务。缺少账号、凭据或权限时，说明是哪一段、哪个服务，只询问该段需要的信息。
- 先确认 A 允许端口转发，能从其网络访问 B 的指定地址和端口。等待 `tunnel_ready`，再用发起端工具连返回的回环端口。SSH 验证 B 的主机密钥，HTTPS 或 WinRM 保留 B 所需的服务名称与身份校验。
- 支持 `--ssh-host` 的独立 CLI 可直接对 B 建立进程内 SSH。给出 B 的账号、shell 和已核验的主机指纹或信任记录。见 [内置 SSH](embedded-ssh.md)。
- `tunnel list --id` 用 A 的设备 ID 查看映射。任务结束后，用 A 的设备 ID 和本次 UUID 关闭，核对数据连接、监听和记录已释放。

### 由 A 运行工具，再连接 B

当工具、网络路径或受控认证只在 A 可用时，先通过已授权的 GeodomDesk 会话在 A 上运行工具，再连接 B。可用 A 已配置的原生客户端，也可用 A 上支持的 GeodomDesk CLI 连接获准的第二段设备。第二段用 A 自己的网络和凭据，分别验证身份与能力。若在 A 启动了 TCP 隧道，其回环端口和 UUID 由 A 上的发起进程管理。先清理 A 上由本次任务创建的连接，再释放外层会话。

排查时依次核对发起端到 A 的设备认证、A 到 B 的地址与端口、B 的协议握手及身份认证。各段分别记录结果。业务命令结果未知时先核对最终服务状态，再决定是否继续。

## 组合方式

| 用途 | 组合 |
| --- | --- |
| Linux Bash | 支持内置 SSH 时用 `exec` 或 `session` 管理 Bash 隧道，也可用显式 TCP 隧道搭配外部 SSH。任务需要文件传输时，按授权使用 SFTP 或 SCP。 |
| Windows PowerShell | 优先用独立 CLI 的内置 SSH，经授权的 TCP 中继运行 PowerShell。可按版本使用显式 TCP 隧道访问已有的 SSH 或 WinRM。见 [Windows PowerShell 隧道](windows-powershell.md)。 |
| SSH/SFTP/SCP 客户端 | 可在同一条显式 SSH 映射上先做只读检查，再传输已授权的文件。`ssh` 用 `-p`，`sftp` 和 `scp` 用 `-P`。传输后核对目标、文件大小或哈希。 |
| HTTPS 健康检查或管理服务 | 映射目标服务的实际 TCP 端口。保留原 HTTPS 主机名用于 Host、SNI 和证书校验，例如 `curl --connect-to`。按需设请求超时。 |
| 其他原生 TCP 客户端 | 已明确获准的服务可连接本地端口，例如数据库客户端或 Windows 原生远程桌面客户端。按该协议核对服务身份、认证与加密。一次任务可分别打开所需的已授权映射。 |

外部 SSH 用已核验的主机密钥文件及与目标 SSH 端口绑定的稳定别名。下列示例分别通过隧道连接 SSH、SFTP 和 HTTPS。

```text
ssh -p LOCAL_PORT -o HostKeyAlias=VERIFIED_ALIAS -o StrictHostKeyChecking=yes -o UserKnownHostsFile=TRUSTED_FILE USER@127.0.0.1
sftp -P LOCAL_PORT -o HostKeyAlias=VERIFIED_ALIAS -o StrictHostKeyChecking=yes -o UserKnownHostsFile=TRUSTED_FILE USER@127.0.0.1
curl --fail --show-error --noproxy SERVICE_NAME --connect-timeout CONNECT_SECONDS --max-time REQUEST_SECONDS --connect-to SERVICE_NAME:URL_PORT:127.0.0.1:LOCAL_PORT https://SERVICE_NAME:URL_PORT/REQUEST_PATH
```

`SERVICE_NAME` 应与服务的 TLS 证书匹配。`--connect-to` 的来源端口要与 URL 一致，目标端口使用 `tunnel_ready.local_port`。Windows PowerShell 用 `curl.exe` 调用 curl。主机密钥变化、证书不符或服务认证失败时先核实目标。通用 TCP 载荷的加密与认证由应用协议承担。依赖 TCP 半关闭的协议要单独验证，旧转发协议的半关闭语义有限。

## 界面核对与异常清理

- 需要验证界面且客户端支持显示 CLI 映射时，在同一操作系统、登录用户和配置目录下打开对应设备的 TCP 隧道页。跨环境时先核对配置来源。只操作本次 UUID 对应的 CLI 行。
- CLI 映射由创建它的进程持有。点该映射的关闭按钮会结束其数据通道。手工映射和其他 UUID 有各自的生命周期。关闭内置 SSH 隧道可能使 Bash 或 PowerShell 会话断开，正在执行的结果按 `unknown` 核对。
- `tunnel close` 允许对自己的 UUID 重试。`already_closed: true` 表示已关闭或记录已清理。控制端口确认拒绝连接时，`list` 或 `close` 可清理异常退出留下的记录。控制超时时保留 UUID，查询状态或有限重试关闭。只在所有者自身失去响应且能确认进程属于本次调用时，才结束该进程树。
