# 开发与维护说明

GeodomDesk 基于官方 RustDesk 稳定版，支持 Windows x64、Ubuntu amd64、Android ARM64。此目录包含客户端定制、构建、打包与更新发布代码。

## 目录职责

| 路径 | 职责 |
| --- | --- |
| `prepare_client.py` | 源码定制命令行入口 |
| `customizations/identity.py` | 应用、服务、配置、IPC、进程与本机监听隔离 |
| `customizations/account_login.py` | 账号登录错误处理及重试控制 |
| `customizations/update_channel.py` | 更新检查、公钥及更新界面适配 |
| `customizations/android.py` | Android 应用标识及移动更新入口 |
| `customizations/server_config.py` | 三平台固定连接参数，忽略外部注入并隐藏修改入口 |
| `customizations/about.py` | 关于页面的公司官网、开源来源说明与对应源码入口 |
| `customizations/printer.py` | 原生打印界面文案、对齐和安装回调生命周期 |
| `customizations/clone_guard.py` | Ubuntu 克隆配置的硬件绑定与 ID 恢复 |
| `customizations/user_data.py` | 首次启动与手动导入，统一按服务器公钥筛选 |
| `customizations/branding.py` | 独立应用图标，公司标识仅出现在关于页面 |
| `client/geodomdesk_window_icons.h` | Windows 窗口与任务栏的 DPI 图标加载和句柄释放 |
| `customizations/upstream.py` | 基线校验与三方合并 |
| `customizations/build.py` | 组合定制、版本记录和统一写入 |
| `client/` | 编译进客户端的独立 Rust 模块 |
| `reference/`、`reference/source-lock.json` | 已审查的官方基线与 SHA-256 |
| `plan_upstream.py` | 官方版本检查与构建计划 |
| `repack_deb.py` | Ubuntu 软件包和服务隔离 |
| `prepare_printer.py`、`config/printer-assets.json` | 校验并打包官方签名打印驱动及适配器；缺失时阻止发布 |
| `archive_source.py`、`release_bundle.py` | 对应源码及发布清单 |
| `channel.py` | 版本、产物与签名契约 |
| `publisher.py` | 原生安装文件校验、签名和发布 |
| `tests/` | 定制回归、Windows 安装验证和 Rust 测试锁文件 |
| `config/` | 编译连接参数、Android 图标设置和发布配置示例 |
| `config/systemd/` | 云端发布任务的服务及定时器 |

正式构建工作流位于仓库 `.github/workflows/build.yml`。新增定制应归入对应模块，不直接编辑临时 checkout；变更基线时同步审查 `reference/source-lock.json`。

## 执行链路

| 链路 | 入口与产物 | 边界与失败处理 |
| --- | --- | --- |
| 官方同步 | `plan_upstream.py` → 官方稳定版本、提交与内部构建号 | 定时检查无变化时跳过；不直接修改已发布客户端 |
| 源码定制 | `prepare_client.py` → `customizations/build.py` → 定制源码 | 先校验基线并完成三方合并，冲突时停止写入 |
| 原生构建 | Actions 的 `bridge` → `windows` / `ubuntu` / `android` | UI 分析与原生检查通过，三平台成功后才生成 Release |
| 产物契约 | `release_bundle.py` → `geodomdesk-build.json` 与对应源码 | 三平台版本、构建号和配置必须一致；逐文件记录大小与 SHA-256 |
| 云端发布 | `publisher.py` → 不可变发布目录、签名更新清单、`latest.json` | 复核来源和摘要，使用同目录原子替换；构建或文件校验失败不切换指针 |
| 下载入口 | `publisher.py` 的 `portal_update` → 导航页下载区域 | 只替换标记区域，保留现有布局；旧生产任务必须停用 |
| 客户端更新 | `client/geodomdesk_update.rs` → 官方 updater | 限定独立通道；下载、签名、摘要与提升权限继续使用官方流程 |

连接参数的编译来源只有 `config/client-channel.json`；云端 `config/publisher.example.json` 只描述发布任务、签名密钥位置和输出目录。客户端不读取云端发布配置，发布端不生成访问密码或客户端配置脚本。

账号登录由 `account_login.py` 和 `client/geodomdesk_auth.rs` 适配官方客户端界面：识别服务端登录错误、取消重试、打开已有登录窗口；不在此项目实现或修改服务端的账号验证。Ubuntu 克隆恢复与用户资料导入分别由独立模块处理，不进入发布程序。

构建错误先查看 Actions 中失败的 job / step。云端日志统一由 systemd journal 管理，使用 `journalctl -u geodomdesk-builder.service --since today` 查看；最近一次结果保存在 `/var/lib/geodomdesk-builder/status.json`。发布程序不常驻进程，不另建无限增长的日志文件。

## 兼容边界

保留官方设备 ID、消息协议、密钥交换、加密、打洞、中继和 WebSocket 行为。双方使用相同服务端及公钥时，设备 ID 连接保持兼容；完整二进制互通仍需实机验证。

客户端本机直接 IP 监听使用 TCP **21128**，局域网发现使用 UDP **21129**，服务端端口不变。手动直接 IP 连接时需要指定被控端的监听端口；通过设备 ID 建立的点对点连接沿用官方流程。局域网自动发现与官方客户端隔离。

通过设备 ID 发起的远程控制、文件传输、TCP 隧道、RDP、摄像头和终端共用服务端账号验证。服务端要求登录时取消重连计时器，并去重打开原有登录窗口；中继回退包装的登录错误同样处理。普通会话登录成功后只重连一次，取消后不继续重试。TCP 隧道和 RDP 保留原监听，每次新的本地连接读取当前账号令牌；被拒绝的本地应用连接需要重新发起，不重建监听或套用桌面画面等待流程。被控端访问密码和临时网络错误沿用官方流程。此界面修复只在 GeodomDesk 生效。

## Ubuntu 系统克隆

沿用官方初始 ID 算法及手动修改 ID 功能。在主配置中单独记录永久物理网卡绑定：首次没有记录时只建立绑定，不改变现有 ID；同一硬件保留现有 ID，包括手动修改的 ID。克隆镜像在不同网卡上启动时，重新分配副本 ID 并保存新绑定，后续正常启动不重复修改。

虚拟桥接、VPN 与随机化地址不作为绑定依据；硬件信息读取失败时不重置。保留服务器、密码和其他设置，不修改操作系统的机器标识。若副本连永久网卡标识也完全相同，或旧配置尚未建立绑定记录，不能仅凭复制的配置可靠判断来源。

## 数据导入

Windows、Ubuntu 新安装首次启动时自动导入一次，中断后下次启动继续；已有设备记录或收藏的升级安装保留原有选择。入口仍位于「设置 → 常规 → 数据迁移」，手动操作前确认范围，完成后显示新增、已存在、跳过和收藏数量。现有记录不覆盖，重复操作不重复添加。

自动和手动导入使用相同规则：普通记录匹配原客户端当前 ID 服务器公钥；跨服务器记录匹配其独立公钥，缺少或不匹配则跳过。导入通过筛选的设备资料及保存的连接密码，已有记录只补齐空密码；密码经官方加解密接口按目标规则保存，无法解密则跳过并计数。本机身份、无人值守密码、登录令牌、信任凭据及画质设置不迁移。跨服务器记录保留已验证的公钥上下文，不更改官方客户端。

## 上游同步与版本

工作流每 6 小时检查官方稳定 Release；源码或定制改变后重新构建，也支持手动运行。三方合并保留无冲突的官方改动及本地定制。遇到冲突、文件移除、构建失败或校验失败时停止发布，审查后再构建。构建依据实际定制内容的摘要判断变化，纯说明修改不触发重编译。

显示版本和 Release 标签与官方一致，例如 `1.5.1`；内部构建序号独立递增，例如 `1.5.1-r101`。Ubuntu 包版本使用 `1.5.1+geodom.101`，应用显示仍为 `1.5.1`。同一官方版本的定制修复因此可以单独更新。

```sh
python3 prepare_client.py --source /path/to/source --version 1.5.1 --build-number 101
```

## 打包与更新

三种平台原生编译，不使用 Docker。每个内部构建使用独立的不可变目录，完整校验所有产物后才切换更新指针，拒绝回退到旧版本或旧构建。

产物包含 Windows 原生 EXE、Ubuntu DEB、Android ARM64 APK、对应源码、更新清单及签名。公开连接参数在编译时写入固定配置；界面和配置文件均不能覆盖这些连接参数；API 可留空，不预置永久密码，不覆盖已有密码与验证模式。

连接参数只在 `config/client-channel.json` 维护，由 `customizations/server_config.py` 写入官方配置优先级；未新增运行时配置文件或轮询。桌面与移动设置页隐藏服务器入口，公共编辑窗口同时拦截扫码等内部调用；外部配置加载和文件名注入入口不读取参数。设备 ID、访问密码和个人记录继续使用独立的正常配置。

Windows 原生 EXE 自带安装入口，更新使用同一程序的 `--update`。Ubuntu 提供更新提示与下载，由管理员安装 DEB，不支持静默自动安装。云端只校验、复制已构建的文件并生成签名更新清单，不重新封装或注入配置。

保留官方签名、哈希、大小、临时文件复核和提升权限流程。应用标识、更新信任公钥及签名上下文独立。签名私钥只保存在受限部署环境与 Actions Secrets；账号、令牌和远程访问密码不进入源码或安装包默认值。

Android 资源修复可在手动构建中填写 `reuse_desktop_run`，复用同一失败工作流尚未发布的桌面产物。复用前核对实际生成的桌面源码、构建配置、工具链和桌面构建步骤；任一变化均拒绝复用。

## Android

使用独立应用标识与固定的正式签名证书，可与官方客户端共存。保留原生 JNI/Kotlin 命名空间，设备数据由 Android 应用沙箱隔离。预置公开服务器参数，账号令牌和远程访问密码不进入客户端源码。

APK 显示版本跟随官方，版本代码使用递增的内部构建序号。更新提示指向独立下载页，安装由 Android 系统确认；屏幕录制、无障碍和后台权限保留官方授权流程。构建后核对应用标识、版本、ARM64 库和签名证书。签名私钥只存放在受限部署目录与 Actions Secrets。

## 验证

```sh
python3 -m unittest discover -s tests -v
python3 verify_rust.py
```

Windows 在临时 CI 环境另运行 `tests/test-windows-native.ps1`，核对实际安装与固定连接参数。

`customizations/id_controls.py` 维护默认 ID 及原生恢复入口；`customizations/settings_cleanup.py` 维护设置项可见性和用户链接，保留网络兼容、权限与打印功能。用户说明位于 `USER_GUIDE.md`。

验证覆盖上游改动保留、冲突不写入、监听隔离、账号错误不重试、签名篡改、同版本构建更新、回退保护、打包失败不切换及源码归档完整性。

## 许可

沿用 RustDesk 的 AGPL-3.0 许可，每次分发提供包含修改及子模块的对应源码。图标资源位于 `assets/`。
