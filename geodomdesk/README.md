# 开发与维护说明

GeodomDesk 基于官方 RustDesk 稳定版，支持 Windows x64、Ubuntu amd64、Android ARM64。此目录包含客户端定制、构建、打包与更新发布代码。

## 目录职责

| 路径 | 职责 |
| --- | --- |
| `prepare_client.py` | 源码定制命令行入口 |
| `customizations/identity.py` | 应用、服务、配置、IPC、进程与本机监听隔离 |
| `customizations/account_login.py` | 账号登录错误处理及重试控制 |
| `customizations/update_channel.py` | 更新检查、公钥及更新界面适配 |
| `customizations/android.py` | Android 应用标识、公开服务器默认值及移动更新入口 |
| `customizations/clone_guard.py` | Ubuntu 克隆配置的硬件绑定与 ID 恢复 |
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

## Ubuntu 系统克隆

沿用官方初始 ID 算法及手动修改 ID 功能。在主配置中单独记录永久物理网卡绑定：首次没有记录时只建立绑定，不改变现有 ID；同一硬件保留现有 ID，包括手动修改的 ID。克隆镜像在不同网卡上启动时，重新分配副本 ID 并保存新绑定，后续正常启动不重复修改。

虚拟桥接、VPN 与随机化地址不作为绑定依据；硬件信息读取失败时不重置。保留服务器、密码和其他设置，不修改操作系统的机器标识。若副本连永久网卡标识也完全相同，或旧配置尚未建立绑定记录，不能仅凭复制的配置可靠判断来源。

## 可选数据导入

入口位于「设置 → 通用 → 数据迁移」。操作前确认范围，完成后显示新增、已存在、跳过和收藏数量。默认不导入，现有记录不覆盖，重复操作不重复添加。

只读取当前用户的官方客户端资料，导入最近连接、收藏、设备名称与备注；不复制配置文件，不导入服务器设置、本机身份、密码、登录令牌、信任凭据或画质设置，也不更改官方客户端。

## 上游同步与版本

工作流每 6 小时检查官方稳定 Release；源码或定制改变后重新构建，也支持手动运行。三方合并保留无冲突的官方改动及本地定制。遇到冲突、文件移除、构建失败或校验失败时停止发布，审查后再构建。构建依据实际定制内容的摘要判断变化，纯说明修改不触发重编译。

显示版本和 Release 标签与官方一致，例如 `1.5.1`；内部构建序号独立递增，例如 `1.5.1-r101`。Ubuntu 包版本使用 `1.5.1+geodom.101`，应用显示仍为 `1.5.1`。同一官方版本的定制修复因此可以单独更新。

```sh
python3 prepare_client.py --source /path/to/source --version 1.5.1 --build-number 101
```

## 打包与更新

三种平台原生编译，不使用 Docker。每个内部构建使用独立的不可变目录，完整校验所有产物后才切换更新指针，拒绝回退到旧版本或旧构建。

产物包含 Windows 原生更新 EXE、配置安装包、Ubuntu DEB、RUN 安装包、Android ARM64 APK、对应源码、更新清单及签名。

Windows 原生更新使用单文件客户端 EXE 的 `--update`；外层 NSIS 用于安装和预置配置。Ubuntu 提供更新提示与下载，由管理员运行新 RUN 安装包，不支持静默自动安装。

保留官方签名、哈希、大小、临时文件复核和提升权限流程。应用标识、更新信任公钥及签名上下文独立。签名私钥和正式访问密码只保存在部署环境的受限配置中。已有安装保留原永久密码及验证模式。

Android 资源修复可在手动构建中填写 `reuse_desktop_run`，复用同一失败工作流尚未发布的桌面产物。复用前核对实际生成的桌面源码、构建配置、工具链和桌面构建步骤；任一变化均拒绝复用。

## Android

使用独立应用标识与固定的正式签名证书，可与官方客户端共存。保留原生 JNI/Kotlin 命名空间，设备数据由 Android 应用沙箱隔离。预置公开服务器参数，账号令牌和远程访问密码不进入客户端源码。

APK 显示版本跟随官方，版本代码使用递增的内部构建序号。更新提示指向独立下载页，安装由 Android 系统确认；屏幕录制、无障碍和后台权限保留官方授权流程。构建后核对应用标识、版本、ARM64 库和签名证书。签名私钥只存放在受限部署目录与 Actions Secrets。

## 验证

```sh
python3 -m unittest test_channel test_packaging test_plan test_android -v
python3 verify_rust.py
```

Windows 另运行 `test-windows-installer.ps1`。

验证覆盖上游改动保留、冲突不写入、监听隔离、账号错误不重试、签名篡改、同版本构建更新、回退保护、打包失败不切换及源码归档完整性。

## 许可

沿用 RustDesk 的 AGPL-3.0 许可，每次分发提供包含修改及子模块的对应源码。图标资源位于 `assets/`。
