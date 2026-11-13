# 脚本、会话与恢复

## CLI 内置执行

以下示例适用于具备相应子命令和参数的发起端。用实际的 CLI 路径、设备 ID、目标主机名和登录名替换占位值。Windows PowerShell 中，CLI 路径保存在 `$cli` 时用 `& $cli ...` 调用。

```text
geodomdesk-cli probe --id "DEVICE_ID" --transport ssh --ssh-user LOGIN --expect-platform Linux --expect-hostname EXPECTED_HOST --non-interactive
geodomdesk-cli exec --id "DEVICE_ID" --transport ssh --shell bash --ssh-user LOGIN --expect-platform Linux --expect-hostname EXPECTED_HOST --non-interactive --command 'pwd' --json
geodomdesk-cli session --id "DEVICE_ID" --transport ssh --shell bash --ssh-user LOGIN --expect-platform Linux --expect-hostname EXPECTED_HOST --non-interactive
geodomdesk-cli probe --id "DEVICE_ID" --relay --expect-platform Windows --expect-hostname EXPECTED_HOST --non-interactive
geodomdesk-cli exec --id "DEVICE_ID" --shell powershell --transport terminal --relay --expect-platform Windows --expect-hostname EXPECTED_HOST --non-interactive --command 'Get-Location' --json
```

- 支持内置 Bash SSH 时优先使用受管隧道。用 `--help` 确认 `--shell bash` 的默认路由，支持时可用 `--transport ssh` 明确选择。目标 SSH 服务使用实际端口和账号，必要时加 `--ssh-port` 和 `--ssh-user`。有会话支持时可复用 SSH 连接，每个请求独立执行。若只支持通用 TCP 映射，可搭配外部 SSH。
- Windows 优先查阅 [PowerShell 经 TCP 隧道](windows-powershell.md)。显式选择 GeodomDesk 终端时使用 `--transport terminal`。需要经 GeodomDesk 中继时再加 `--relay`。`protocol: exec` 表示结构化终端，`protocol: legacy` 表示兼容终端。`terminal_exec` 和 `terminal` 是对端能力字段。兼容终端可能合并 stderr 与 stdout，大输出按规模设置超时。创建会话时选择通道和 Shell，后续单条请求改变 Shell 不等于换通道。
- `probe` 可检查设备认证与能力。支持时用 `probe --transport ssh` 核对 SSH 通道。结果若包含 `protocol: ssh` 或 `command_transport: ssh-relay`，可用于确认内置 SSH 路径。`transport: Relay` 只标记底层走中继，终端中继也可返回这个值。若中继不可用且任务允许直连，可为新的终端请求选择可用路由。

## 权限检查与认证

先确认用户授权的目标和动作，再检查当前账号是否具备执行该动作所需的权限。优先复用获准的客户端会话、密钥或服务凭据。各工具使用自己支持的认证方式。能力未知时用支持的 `probe`、服务握手或最小只读检查定位，已有有效证据时可直接使用。连通端口或登录成功后，仍按任务确认终端、资源或文件的访问权限。

| 检查结果 | 只询问当前需要的信息 |
| --- | --- |
| 不清楚目标使用哪个账号 | 询问对应设备或服务的登录账号。确认已有登录方式或授权凭据是否可复用。 |
| 账号已知，登录缺少凭据或原凭据被拒绝 | 先核实目标身份和认证方式，再询问对应的登录凭据。能用现有密钥、agent 或用户登录时按该方式继续。确需密码或验证码时，请用户在该工具的无回显提示或其他获准的安全入口输入。 |
| 账号已通过认证，当前操作被拒绝 | 说明哪台设备、哪个服务、什么操作需要授权。请用户授予最小权限，或提供已获准执行该动作的目标账号。需要管理员身份时先确认操作确实需要且已获授权。 |
| 连接失败、主机身份异常或功能未开放 | 先定位网络、身份或功能原因。已确认缺少权限时，再询问对应的授权。 |

询问时指出卡在哪台设备、哪一层认证以及需要账号、凭据还是权限。中转场景分别检查中转机接入和最终服务登录，只询问缺失的一段。需要用户输入凭据期间，可继续独立且已获授权的工作。没有安全输入渠道时，请用户先完成登录或配置获准的凭据，再继续依赖该权限的操作。

| 验证层 | 可用方式 |
| --- | --- |
| GeodomDesk 账号 | 支持 `account login --user USER` 的独立 CLI 可复用已保存的 CLI 登录，也可使用当前系统用户已有的客户端登录。单次登录用 `--account-user`。密码通过无回显提示或受控的 `GEODOMDESK_ACCOUNT_PASSWORD` 提供。需二次验证时先完成客户端登录。 |
| 设备接入 | 使用客户端已有凭据，或通过 `--ask-device-password`、`GEODOMDESK_DEVICE_PASSWORD`、`GEODOMDESK_DEVICE_CODE` 提供获准的设备凭据。 |
| 独立 CLI 的内置 SSH | 用默认私钥、`--ssh-identity PATH` 或 `--ask-ssh-password`。私钥口令按提示输入，或在获准的自动化环境中用 `GEODOMDESK_SSH_KEY_PASSPHRASE`。SSH 密码可用受控的 `GEODOMDESK_SSH_PASSWORD`。 |
| 外部 OpenSSH | 按该客户端的配置使用密钥、agent 或交互认证。 |
| WinRM 或其他目标服务 | 使用对应客户端支持的登录方式、已授权的凭据和服务身份验证。 |
| Windows 管理员终端 | 仅在任务授权提权且目标客户端后台服务可用时，用 `--admin-user "DOMAIN\Administrator"`，管理员密码用交互输入或 `GEODOMDESK_ADMIN_PASSWORD`。设备接入仍需独立验证。 |

把 `--non-interactive` 用于已有凭据或安全注入凭据的自动化。需要人工输入时切换到该工具支持的无回显提示。凭据只交给对应认证，避免放入聊天明文、命令参数、脚本、技能或日志。SSH 主机信任按设备 ID、目的地址和端口管理。受管 SSH 首次信任记录存入专用文件。已有预先核验的记录时，用 `--ssh-known-hosts PATH --ssh-strict-host-key`。密钥不符先核实变更原因。

## 命令输入与会话控制

- `exec` 每次新建连接。简单命令用 `--command`，复杂脚本用 `--script PATH` 传发起端 UTF-8 普通文件，或用 `--script -` 从标准输入读取。每次选择一种输入方式。`session` 中的 `script_file` 也是发起端普通文件路径，`script` 是文本，stdin 专供 JSON Lines。
- 分别为连接认证和命令执行设置时限。参数、默认值、脚本大小、输出与排队限制以当前帮助及会话返回为准。为建连、上传、执行和清理留总等待余量。超大结果在远端汇总，或使用获准的文件传输通道。
- `session` 保持单个进程及三个管道。等 `ready`，确认目标身份、通道和协议后，再用 JSON 序列化写入并 flush。持续读取 stderr 和 stdout，按请求 `id` 等待结果，同时处理无 `id` 的 `invalid_request` 或含 `rejected_id` 的拒绝事件。默认逐条确认再提交。仅对相互独立的命令考虑 `--continue-on-error`。
- 每次执行使用独立 Shell 进程，变量和工作目录不在请求间继承。需要共享 Shell 状态时把操作写在同一脚本。活跃及排队的非空 `id` 要唯一。控制操作只包含 `op` 和可选的新 `id`。`id` 只用于关联结果。

```jsonl
{"id":"inspect","script":"pwd"}
{"id":"new-connection","connection":"fresh","script":"pwd"}
{"id":"progress","op":"status"}
{"id":"reset","op":"reconnect"}
{"id":"stop","op":"cancel"}
{"id":"release","op":"close"}
```

`fresh` 和 `op` 示例需要发起端报告支持相应会话控制，例如 `ready` 含 `session_protocol >= 2` 及所需控制。缺少这些能力时，只发送已支持的请求，或正常结束会话后用独立 `exec` 或新会话。

| 动作 | 何时用 | 结果 |
| --- | --- | --- |
| 默认复用 | 下一条请求能在健康的同目标连接运行 | 检查 `connection_id` 与 `reused_connection`。 |
| `connection: fresh` | 该新请求需要新连接 | 轮到它时替换旧连接，当前在执行的请求继续处理。 |
| `op: reconnect` | 当前连接卡住，且接下来仍要执行 | 结束本地等待、丢弃旧连接及未发送队列，重新认证。等相应 `ready`。 |
| `op: cancel` / `op: close` | 立即中止等待或释放连接 | 丢弃连接及未发送队列，保留 CLI 进程。下一条新请求可重新连接。 |
| 关闭 stdin | 整个会话结束 | 处理已接受的队列后退出。需要先取消未发送工作时先用控制操作。全成功返回 0，曾有失败或拒绝时也要读取逐条结果。 |

## 结果判断与恢复

`exec --json` 或 `session` 的输出字段是 Base64 字节。文本按正确编码解码，二进制按原字节处理。结合 `ok`、`exit_code`、`code`、`phase`、`execution_state` 判断结果。`fatal: false` 表示进程可继续处理输入，`connection_lost: true` 则表示原连接已丢弃。

| 状态 | 行动 |
| --- | --- |
| `not_started` | 修正参数或连接问题，按授权判断是否重试。`retry_safe` 只表明这一请求没有开始。 |
| `finished` | 按退出码及输出判断成功。非零退出仍可能已经完成部分操作。 |
| `unknown` | 超时、断线或取消时先只读核对业务状态。收到取消确认后，仍需核对远端任务状态及已产生的结果。 |

建连时 CLI 已在总预算内对临时故障有限重建，原始原因可见 `setup_errors`。明确的临时连接失败且状态为 `not_started` 时可有限重连。登录拒绝、权限不足、目标不符或 SSH 主机密钥变化时先解决原因。显式设备密码、管理员凭据或验证码使用单次完整登录预算。SSH 失败时由调用方为新的请求选择通道。上传或执行中断按结果状态核对。调用方设总等待上限，先尝试正常控制。仍卡死时只处理本次启动的进程树及其 UUID。
