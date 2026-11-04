# GeodomDesk

Geodom 远程工具，基于 RustDesk 官方稳定版，支持 Windows x64、Ubuntu amd64。

- 独立应用、服务、目录、配置、IPC、辅助进程和签名更新通道。
- 保留官方 ID、加密、打洞、中继及 WebSocket 协议，不修改服务器端口。
- 本机直接 IP 监听 TCP 21128，局域网发现 UDP 21129，与官方客户端隔离。
- 服务端要求账号登录时打开登录窗口，暂停自动重连。
- 每 6 小时跟踪官方稳定版，三方合并后原生构建；冲突或失败保留已发布版本。
- 显示版本与官方一致，内部构建序号独立递增，支持同版本修复更新。
- qcjcpt HTTPS 更新通道，保留 Ed25519、SHA-256 验证。

在 Actions 中运行 **Build GeodomDesk** 可手动构建。Windows、Ubuntu 成功后才发布 Release，供云端生成安装包和签名更新文件。签名私钥与正式访问密码不进入本仓库。

详见 [geodomdesk/README.md](geodomdesk/README.md)。完整二进制共存和互通需构建后实机验证。对应源码随版本分发，沿用 AGPL-3.0。
