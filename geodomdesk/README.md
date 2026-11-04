# 开发与维护说明

GeodomDesk 基于官方 RustDesk 稳定版，支持 Windows x64、Ubuntu amd64。此目录包含客户端定制、构建、打包与更新发布代码。

## 目录职责

| 路径 | 职责 |
| --- | --- |
| `prepare_client.py` | 源码定制命令行入口 |
| `customizations/identity.py` | 应用、服务、配置、IPC、进程与本机监听隔离 |
| `customizations/account_login.py` | 账号登录错误处理及重试控制 |
| `customizations/update_channel.py` | 更新检查、公钥及更新界面适配 |
| `customizations/user_data.py` | 设置页的可选设备记录导入入口 |
| `customizations/branding.py` | 图标与 Logo 资源 |
| `customizations/upstream.py` | 基线校验与三方合并 |
| `customizations/build.py` | 组合定制、版本记录和统一写入 |
| `client/` | 编译进客户端的独立 Rust 模块 |
| `reference/`、`source-lock.json` | 已审查的官方基线与 SHA-256 |
| `plan_upstream.py` | 官方版本检查与构建计划 |
| `repack_deb.py` | Ubuntu 软件包和服务隔离 |
| `archive_source.py`、`release_bundle.py` | 对应源码及发布清单 |
| `channel.py` | 版本、产物与签名契约 |
| `publisher.py`、`installer.py`、安装模板 | 云端打包与更新发布 |

正式构建工作流位于仓库 `.github/workflows/build.yml`。新增定制应归入对应模块，不直接编辑临时 checkout；变更基线时同步审查 `source-lock.json`。

## 兼容边界

保留官方设备 ID、消息协议、密钥交换、加密、打洞、中继和 WebSocket 行为。双方使用相同服务端及公钥时，设备 ID 连接保持兼容；完整二进制互通仍需实机验证。

客户端本机直接 IP 监听使用 TCP **21128**，局域网发现使用 UDP **21129**，服务端端口不变。手动直接 IP 连接时需要指定被控端的监听端口；通过设备 ID 建立的点对点连接沿用官方流程。局域网自动发现与官方客户端隔离。

服务端要求账号登录时取消重连计时器，并去重打开原有登录窗口。登录成功后只重连一次，取消后不继续重试。被控端访问密码和临时网络错误沿用官方流程。此界面修复只在 GeodomDesk 生效。

## 可选数据导入

入口位于「设置 → 通用 → 数据迁移」。操作前确认范围，完成后显示新增、已存在、跳过和收藏数量。默认不导入，现有记录不覆盖，重复操作不重复添加。

只读取当前用户的官方客户端资料，导入最近连接、收藏、设备名称与备注；不复制配置文件，不导入服务器设置、本机身份、密码、登录令牌、信任凭据或画质设置，也不更改官方客户端。

## 上游同步与版本

工作流每 6 小时检查官方稳定 Release；源码或定制改变后重新构建，也支持手动运行。三方合并保留无冲突的官方改动及本地定制。遇到冲突、文件移除、构建失败或校验失败时停止发布，审查后再构建。

显示版本和 Release 标签与官方一致，例如 `1.5.1`；内部构建序号独立递增，例如 `1.5.1-r101`。Ubuntu 包版本使用 `1.5.1+geodom.101`，应用显示仍为 `1.5.1`。同一官方版本的定制修复因此可以单独更新。

```sh
python3 prepare_client.py --source /path/to/source --version 1.5.1 --build-number 101
```

## 打包与更新

两种平台原生编译，不使用 Docker。每个内部构建使用独立的不可变目录，完整校验所有产物后才切换更新指针，拒绝回退到旧版本或旧构建。

产物包含 Windows 原生更新 EXE、配置安装包、Ubuntu DEB、RUN 安装包、对应源码、更新清单及签名。

Windows 原生更新使用单文件客户端 EXE 的 `--update`；外层 NSIS 用于安装和预置配置。Ubuntu 提供更新提示与下载，由管理员运行新 RUN 安装包，不支持静默自动安装。

保留官方签名、哈希、大小、临时文件复核和提升权限流程。应用标识、更新信任公钥及签名上下文独立。签名私钥和正式访问密码只保存在部署环境的受限配置中。已有安装保留原永久密码及验证模式。

## 验证

```sh
python3 -m unittest test_channel test_packaging test_plan -v
python3 verify_rust.py
```

Windows 另运行 `test-windows-installer.ps1`。

验证覆盖上游改动保留、冲突不写入、监听隔离、账号错误不重试、签名篡改、同版本构建更新、回退保护、打包失败不切换及源码归档完整性。

## 许可

沿用 RustDesk 的 AGPL-3.0 许可，每次分发提供包含修改及子模块的对应源码。图标资源位于 `assets/`。
