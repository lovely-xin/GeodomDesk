# 安装 GeodomDesk CLI 技能

1. 解压发布包，按随包校验清单核对文件。
2. 将 `skills/geodomdesk-cli` 整个目录复制到运行器识别的技能目录。已有同名技能时更新原目录，避免重复加载。
3. 在新会话调用 `$geodomdesk-cli`。发起端可用随客户端安装的 CLI，或选用对应系统的独立 CLI。按 `--help` 核对所需能力。

调用示例

> 使用 $geodomdesk-cli 验证已授权的在线设备。优先经 TCP 隧道实际握手并运行只读检查，确认无法建立隧道后再回落。报告实际通道和清理结果。

[GeodomDesk 发布页](https://github.com/lovely-xin/GeodomDesk/releases)
