# 安装和使用 GeodomDesk CLI 技能

## 安装

1. 解压 ZIP，找到 `geodomdesk-cli` 文件夹，确认里面有 `SKILL.md`、`agents` 和 `references`。
2. 将整个文件夹放进 Codex 的技能目录。个人使用放在用户主目录的 `.agents/skills/` 下。仅在一个项目使用时放在项目根目录的 `.agents/skills/` 下。已有同名技能时更新原目录，避免重复安装。
3. 在 Codex 的新会话里输入 `$geodomdesk-cli`。技能未显示时重启 Codex。 [Codex 技能安装说明](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)

## 首次使用

发起端需有可用的 GeodomDesk CLI。可使用随客户端提供且具备所需功能的 CLI，或从项目发布页选取适合发起端系统的独立 CLI，并核对随包的 SHA-256。运行前准备已获授权的设备 ID、预期主机名和平台，按需准备目标服务凭据。CLI 在具备网络访问权限的发起端宿主机上运行。 [GeodomDesk 发布页](https://github.com/lovely-xin/GeodomDesk/releases)

把下文的设备 ID 和主机名换成实际值，再发送给 Codex。

> 使用 $geodomdesk-cli 在发起端宿主机验证我已授权的设备 DEVICE_ID，预期主机名 EXPECTED_HOST。优先通过 TCP 隧道运行只读的主机名检查，报告实际通道和结果，然后关闭本次临时连接。

共享包包含技能说明和参考文件。接收者使用自己的客户端、账号、设备凭据和主机信任记录。
