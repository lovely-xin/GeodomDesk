# GeodomDesk CLI 能力与验证

| 用途 | 工作流 | 验证 |
| --- | --- | --- |
| 查找设备 | `device list --recent --online --json`。优先使用同账号本地记录，无可用记录时读云端。 | 校验账号、过滤已删除记录，列表只输出设备元数据。 |
| 探测连接 | `doctor` 检查终端或指定 TCP 目标，返回故障阶段和建议。 | 核对设备平台、主机名和目标端口。经 A 访问 B 时另行校验 B 的服务身份。 |
| 使用 TCP 工具 | `tunnel run -- TOOL ...` 托管单个前台工具。`tunnel open/list/close` 支持复用映射。 | 核对应用握手、业务结果、退出码、活动记录和监听释放。 |
| 长会话和实时输出 | 经隧道使用 tmux 或已启用的 WinRM 会话。标准输出与错误由本机工具直接传递。 | 验证附加、断开后恢复、会话结束和隧道释放。 |

两种发行版的参数和示例见[独立 CLI 使用说明](standalone-cli.md)。操作技能见 [GeodomDesk CLI](../skills/geodomdesk-cli/SKILL.md)。
