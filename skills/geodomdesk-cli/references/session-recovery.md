# 会话控制和故障恢复

单次用 `exec --json`。多条相关命令用 `--script PATH` 或 `--script -` 合批，连续操作用 `session` 复用健康连接。独立请求使用各自的 Shell 进程，需要共享工作目录或环境变量的步骤写进同一脚本。大脚本、大输出或长任务改用 [显式 TCP 映射](tcp-tunnels.md)。

## 交互和退出

由运行器持有子进程并保持 stdin 打开。等 `ready` 后，每次发送一行带换行的 JSON，再读对应 `id` 的结果。

```jsonl
{"id":"check","script":"hostname"}
{"id":"fresh","connection":"fresh","script":"hostname"}
{"id":"status","op":"status"}
{"id":"reconnect","op":"reconnect"}
{"id":"cancel","op":"cancel"}
{"id":"exit","op":"exit"}
```

- `connection: "fresh"` 给下一条待执行请求换连接。需要立即放弃卡住的连接时用 `reconnect`，等待新的 `ready`。`cancel` 取消当前和未开始请求，保留 CLI 进程。
- `close` 释放连接，进程仍可接收新请求。结束会话用 `exit` 或关闭 stdin 并等待进程退出。旧版若不识别 `exit`，使用 stdin EOF。收尾时核对退出状态并释放该进程持有的隧道。
- 解码 Base64 的 `stdout`/`stderr`，同时检查 `ok`、`exit_code` 和 `execution_state`。`not_started` 表示尚未开始执行。`unknown` 时先只读核对远端状态，再判断是否重试。

## 建连失败

| 失败环节 | 核对方式 |
| --- | --- |
| 发起端与账号 | 核对 CLI 所在系统和配置、网络权限与登录期限。独立版支持 `account status` 时可查看会话来源，否则用只读 `probe` 检查当前登录。 |
| 设备接入与功能权限 | 核对 ID、在线状态、预期主机、设备密码或安全码，以及 TCP 转发或终端权限。必要时提示用户安全输入设备密码，例如 `--ask-device-password`。 |
| 网络与转发 | 可对比一次默认路由和 `--relay`。如果仍无法建立，查目标设备的监听地址、端口、服务状态和防火墙。 |
| SSH 或下游服务 | 区分转发失败、应用握手失败、身份校验异常和系统账号拒绝。经 A 中转时分开检查发起端到 A、A 到 B 和 B 的服务认证。 |

若需要检查服务或端口，可先通过已经授权的其他路径做只读诊断。用证据确认无法建立 TCP 隧道后，再按授权选用替代通道。失败报告保留实际路径、`phase`、`code` 或 `last_error` 及脱敏原因。
