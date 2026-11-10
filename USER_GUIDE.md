# GeodomDesk 使用说明

GeodomDesk 基于 RustDesk 定制。连接服务和更新通道由安装包固定配置，普通使用无需填写服务器、公钥或管理员 API 令牌。

## 连接与账号

启动客户端后，使用设备 ID 发起连接。被控端需要按其权限设置确认连接，或由控制端输入被控端的远程访问密码。客户端不预置统一访问密码。

服务端要求登录时，使用自己的账号密码登录。登录状态由客户端管理；不要把会话令牌当作远程访问密码，也不要向其他人提供令牌。

## 命令行远程终端

Windows 安装目录和 Ubuntu 安装包都提供 `geodomdesk-cli`。在目标设备开启终端权限后，用设备 ID 执行脚本。

```text
geodomdesk-cli exec --id DEVICE_ID --shell bash --script check.sh
geodomdesk-cli.exe exec --id DEVICE_ID --shell powershell --script check.ps1
geodomdesk-cli.exe exec --id DEVICE_ID --shell pwsh --script check.ps1 --admin-user "DOMAIN\Administrator" --ask-device-password
```

根据目标系统选用 Bash、Windows PowerShell 或 PowerShell 7。`--script -` 从标准输入读取脚本，`--relay` 指定中继，`--timeout` 设置远端执行时限（默认 15 秒）。`exec --help` 显示完整参数。新协议分别返回标准输出和错误输出，进程返回远端退出码。

- GeodomDesk 账号默认沿用发起端的登录状态。无界面时可用 `--account-user` 登录，令牌仅供本次调用使用。账号若需要二次验证，请先在客户端完成登录。
- 设备密码默认沿用已保存的连接密码。可用 `--ask-device-password` 无回显输入，或者设置 `GEODOMDESK_DEVICE_PASSWORD`。目标设备要求二次验证时，使用 `GEODOMDESK_DEVICE_CODE` 提供当次验证码。
- Windows 管理员终端使用 `--admin-user` 指定目标机的本地或域管理员账号。目标机需安装客户端并运行后台服务，同时通过设备密码和 Windows 管理员验证。密码可在终端无回显输入。
- 自动化可使用 `--non-interactive`，由运行环境注入 `GEODOMDESK_ACCOUNT_PASSWORD` 和 `GEODOMDESK_ADMIN_PASSWORD` 等对应凭据。脚本从标准输入读取时，凭据仍通过终端或环境变量提供。

遇到只支持旧终端协议的 Windows 或 Linux 设备，CLI 会在认证后使用独立的加密终端会话执行命令。这种模式会将远端的标准输出和错误输出合并返回，并报告命令退出码。

连续命令可使用常驻会话，共用一次账号验证和一条加密连接，兼容只支持旧终端的设备：

```text
geodomdesk-cli session --id DEVICE_ID --non-interactive --timeout 120
```

保持这个进程的标准输入打开，每行发送一条 JSON：

```json
{"shell":"bash","script":"printf '你好\\n'","timeout":60}
{"shell":"powershell","script":"Get-Process | Select-Object -First 3"}
```

先返回 `event: ready`，之后每条命令返回一行 `event: result`。`stdout`、`stderr` 使用 Base64 编码，`exit_code` 是远端退出码；旧终端的错误输出合并到 `stdout`。逐条提交并等待结果即可复用连接，命令使用独立进程，不保留上一条命令的工作目录和变量。关闭标准输入会在已提交命令执行完成后退出。分别启动的 `exec` 进程仍各自建立连接。

会话协议 `session_protocol: 2` 将连接与 CLI 进程分开管理：参数错误可修正后继续；连接、传输或执行等待失败时丢弃旧连接、取消尚未发送的队列，返回 `fatal: false`，下一条新请求可重新建连。执行结果未知时先核对远端状态，不自动重跑命令。只有致命输入错误等无法继续处理时返回 `fatal: true` 并退出进程。启动、分批上传和执行分别计时；旧终端上传每批最多等待 15 秒进展，`timeout` 仅从脚本开始执行后计时，范围为 1–300 秒。脚本上限为 64 KiB，单个输出通道上限为 256 KiB；旧终端合并输出上限为 256 KiB。

### Agent 常用增强参数

```text
geodomdesk-cli.exe probe --id DEVICE_ID --non-interactive --connect-timeout 30
geodomdesk-cli.exe exec --id DEVICE_ID --shell powershell --command "Get-Location" --json --non-interactive
geodomdesk-cli session --id DEVICE_ID --shell bash --timeout 120 --non-interactive
```

`probe` 只连接和认证，不执行命令；返回设备平台、版本、加密状态、直连/中继、传输类型及新旧终端能力。`--connect-timeout` 单独设置连接及设备认证时限（默认 60 秒，1–300 秒），与脚本执行时限分开。`--command` 支持内联脚本，与 `--script` 二选一。旧用法保持可用。

`exec --json` 返回单行 JSON，包括成功或失败、远端退出码、Base64 编码的 stdout/stderr、耗时和连接信息；连接或参数错误也返回 JSON，诊断信息仍可能写入 stderr。`session` 始终使用 JSON Lines，无需 `--json`。会话的 `--shell` 为后续命令提供默认 Shell，单条命令仍可覆盖：

```json
{"id":"inspect","script":"pwd"}
{"id":2,"script_file":"./check.sh","timeout":60}
```

`script_file` 读取发起端的 UTF-8 文件，与 `script` 二选一，不能用 `-`，以免消耗命令输入流。结果原样返回 `id` 以便关联；`elapsed_ms` 是本次请求处理耗时（旧协议包含终端准备和上传），`ok` 表示无执行错误且远端退出码为 0。`fatal: true` 后应结束会话并核对远端状态，不能直接自动重试有副作用的命令。

新配置默认关闭 TCP 打洞。需要时可在设置中手动开启。TCP 中继仍可使用。

## 导入设备记录

Windows、Ubuntu 的新配置首次启动时，自动导入同一用户 RustDesk 中服务器公钥匹配的设备记录、收藏、备注及已保存的连接密码。设置 → 常规 → 数据迁移也可手动导入。

已有记录只补齐空的连接密码，已有密码和其他字段保留。本机无人值守密码、服务器设置、登录令牌及信任凭据不会导入。缺失或不匹配公钥的记录会跳过；无法解密的保存密码也会跳过并计数。导入中断后，下次启动会继续，完成后不再自动重复导入。

## 更改和恢复 ID

桌面端设置 → 安全 → ID 中，“更改 ID”用于指定自定义 ID，“恢复”用于切回本机自动分配的默认 ID。恢复前会显示目标 ID，由服务器确认可用后才切换。

新 ID 已被其他设备占用时，操作会被拒绝。密码和其他设置保留；切换完成后，请使用新的 ID 连接该设备。

<a id="ubuntu"></a>
## Ubuntu 权限与桌面

在实际需要被控的用户桌面会话中启动客户端，并按系统提示允许屏幕共享和远程输入。X11 与 Wayland 的权限和登录界面能力不同；若提示当前会话不支持捕获或控制，可尝试登录到 Ubuntu on Xorg 后重试。

优先保留硬件编解码；显卡驱动或渲染不正常时，再尝试设置中的软件渲染选项。调整图像质量、帧率和缩放可减少网络与图形负载。

## Android 权限

被控时按系统提示允许屏幕录制；远程输入还需要开启对应的无障碍服务。需要后台被控时，再按使用需要允许悬浮窗、自启动及后台运行。系统或设备厂商可能限制锁屏、重启后的自动接入。

## Windows 打印

支持的 Windows 系统安装客户端后，可在设置 → 打印机中安装打印机组件。接收打印任务时，可选择默认打印机、指定打印机或忽略任务；自动打印仅在确认设备和文档来源可信时开启。

Ubuntu 和 Android 不展示 Windows 专用的打印机驱动安装入口。

<a id="data-and-permissions"></a>
## 数据与权限

屏幕、键鼠、剪贴板、文件和打印权限由被控端设置控制。只向可信的人授予所需权限，远程访问密码应由设备使用者自行设置。

客户端会在本机保存设备记录、个人设置和登录状态。服务端的账号验证与连接记录按其配置管理。分享截图、日志或配置文件前，应检查其中的设备信息和凭据；不要公开密码或访问令牌。

## 源代码与许可

定制代码和构建流程在本仓库维护，软件沿用 RustDesk 的开源许可。参见 [LICENSE](LICENSE)。


### 新建连接与故障恢复

以下控制要求 `ready.session_protocol >= 2`。`connection: fresh` 在轮到该请求执行时替换连接；卡住时使用 `reconnect` 立即停止当前等待、取消未发送的队列并重新认证。每条控制使用新的 id 或省略 id，等待其确认后继续。

```json
{"id":"new-check","connection":"fresh","script":"pwd"}
{"id":"state","op":"status"}
{"id":"reset","op":"reconnect"}
{"id":"stop","op":"cancel"}
{"id":"release","op":"close"}
```

`cancel`、`close` 都释放连接并保留 CLI 进程，下一条新请求重新连接。关闭 stdin 才结束整个会话。控制操作只接受 op 和 id，附带脚本等字段会被拒绝。

默认一次执行一条，最多再排队 16 条。命令失败会取消未开始的队列，防止依赖步骤继续执行；只有相互独立的批处理才加 `--continue-on-error`。发生过失败或拒绝时会话最终返回 1，全成功返回 0。推荐逐条发送、等待结果，避免排队依赖步骤。

可使用 `--expect-platform Linux`、`--expect-hostname EXPECTED_HOST` 在每次认证后核对目标，不匹配则拒绝执行。连接编号 `connection_id` 可用于确认是否已经换新连接。重复指定目标和脚本参数、重复使用活跃/排队请求 id 都会被拒绝。

恢复判断应读取 `code`、`phase` 和 `execution_state`：`not_started` 表示没有执行；`finished` 表示收到结果但可能业务失败；`unknown` 表示无法确认远端状态，不能盲目重试。`retry_safe` 仅反映本次请求是否开始。取消或重连不保证远端业务进程终止，也不撤销已发生的修改。id 仅关联结果，不提供幂等去重。

完整的操作示例、超时预算、故障恢复步骤、兼容边界和设计依据见 [CLI 实践说明](docs/CLI_RELIABILITY.txt)。
