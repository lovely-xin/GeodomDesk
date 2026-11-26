# 开发与维护说明

GeodomDesk 基于官方 RustDesk 稳定版，支持 Windows x64、Ubuntu amd64、Android ARM64。此目录包含客户端定制、构建、打包与更新发布代码。

## 目录职责

| 路径 | 职责 |
| --- | --- |
| `packaging/prepare_client.py` | 源码定制命令行入口 |
| `packaging/patch_flutter_sdk.py` | 修补 pinned Flutter SDK（及 legacy 引擎校验） |
| `packaging/repack_deb.py` | Ubuntu 软件包和服务隔离 |
| `packaging/prepare_printer.py`、`config/printer-assets.json` | 校验并打包官方签名打印驱动及适配器；缺失时阻止发布 |
| `packaging/android_release.py` | 准备 Android 签名输入并校验正式 APK |
| `packaging/archive_source.py` | 生成对应发行版的完整源码归档 |
| `packaging/verify_rust.py` | 在独立测试 crate 中验证定制 Rust 模块与更新契约 |
| `release/channel.py` | 版本、产物与签名契约 |
| `release/plan_upstream.py` | 官方版本检查与构建计划 |
| `release/reuse_desktop.py` | 校验并复用未发布的已验证桌面产物 |
| `release/release_bundle.py` | 写入多平台发布清单与更新日志摘要 |
| `release/publisher.py` | 原生安装文件校验、签名和发布 |
| `release/activate.py` | 激活独立下载与更新通道 |
| `patches/` | 按功能拆分的 Rust/Dart 三方合并与定制模块 |
| `patches/identity.py` | 应用、服务、配置、IPC、进程与本机监听隔离 |
| `patches/account_login.py` | 账号登录错误处理及重试控制 |
| `patches/update_channel.py` | 更新检查、公钥及更新界面适配 |
| `patches/android.py` | Android 应用标识及移动更新入口 |
| `patches/server_config.py` | 三平台固定连接参数，忽略外部注入并隐藏修改入口 |
| `patches/about.py` | 关于页面的公司官网、开源来源说明与对应源码入口 |
| `patches/printer.py` | 原生打印界面文案、对齐和安装回调生命周期 |
| `patches/clone_guard.py` | Ubuntu 克隆配置的硬件绑定与 ID 恢复 |
| `patches/user_data.py` | 首次启动与手动导入，统一按服务器公钥筛选 |
| `patches/branding.py` | 独立应用图标，公司标识仅出现在关于页面 |
| `client/rust/` | 编译进客户端的独立 Rust 模块、`geodomdesk-cli` 与 `hbb_common` 策略 |
| `client/flutter/` | Flutter 页面组件与设置页注入模板 |
| `client/windows/geodomdesk_window_icons.h` | Windows 窗口与任务栏的 DPI 图标加载和句柄释放 |
| `client/standalone/` | 独立 CLI 入口与内置 SSH 模块 |
| `reference/`、`reference/source-lock.json` | 已审查的官方基线与 SHA-256 |
| `tests/`、`tests/windows/` | 定制回归、Windows 安装验证和 Rust 测试锁文件 |
| `tools/` | 图标生成等本地维护脚本 |
| `config/` | 编译连接参数、Android 图标设置和发布配置示例 |
| `config/systemd/` | 云端发布任务的服务及定时器 |

正式主线构建工作流位于仓库 `.github/workflows/build-client.yml`（构建 Windows 10/11/Server 2016+ 标准版、Ubuntu 与 Android 并自动发布更新）。针对 Win7 SP1 / Win8 / Server 2008 R2–2012 R2 的旧系统兼容版（`windows-legacy`）已分离到独立手动工作流 `.github/workflows/build-windows-legacy.yml`，不参与主线同步发布，且已冻结自动更新。新增定制归入对应模块，基线变更同步审查 `reference/source-lock.json`。

## 执行链路

| 链路 | 入口与产物 | 检查项 |
| --- | --- | --- |
| 上游构建计划 | `release/plan_upstream.py` → 官方稳定版本、提交与内部构建号 | 核对已审查提交与官方标签，按源码和定制摘要决定是否构建 |
| 源码定制 | `packaging/prepare_client.py` → `patches/build.py` → 定制源码 | 先校验基线并完成三方合并，冲突时停止写入 |
| 原生构建 | Actions 的 `bridge` → `windows` / `ubuntu` / `android` | UI 分析与原生检查通过，三平台成功后才生成 Release |
| 产物契约 | `release/release_bundle.py` → `geodomdesk-build.json` 与对应源码 | 三平台版本、构建号和配置必须一致；逐文件记录大小与 SHA-256 |
| 云端发布 | `release/publisher.py` → 不可变发布目录、签名更新清单、`latest.json` | 复核来源和摘要，使用同目录原子替换；构建或文件校验失败不切换指针 |
| 下载入口 | `release/publisher.py` 的 `portal_update` → 导航页下载区域 | 只替换标记区域，保留现有布局；旧生产任务必须停用 |
| 客户端更新 | `client/rust/geodomdesk_update.rs` → 官方 updater | 限定独立通道；下载、签名、摘要与提升权限继续使用官方流程 |

客户端从 `config/client-channel.json` 编译连接参数。云端使用 `config/publisher.example.json` 配置发布任务、签名密钥位置和输出目录。

配置中的 `github_asset_accelerators` 按顺序填写 HTTPS 加速前缀，未配置该字段时默认使用 `["https://gh-proxy.org/", "https://ghfast.top/"]`（显式设为空列表 `[]` 则禁用加速，最多支持 5 个 HTTPS 入口）。`github_prefer_accelerators` 默认 `true`，优先按健康记忆顺序走加速入口下载并校验 Release 文件，加速线路均失败时自动回退官方直连；若设为 `false`，则先尝试官方直连（约 25 秒时限）再依次回退加速入口。Release 元数据（`api.github.com`）与 GitHub 令牌始终只发往官方 API，不经第三方镜像；加速入口仅接收公开 Release 文件链接，所有下载内容均严格核对官方 `size` 与 SHA-256 后才进入缓存与签名发布流程。

账号由服务端验证。`account_login.py` 和 `client/rust/geodomdesk_auth.rs` 识别服务端登录错误，取消重试并打开登录窗口。

构建错误从 Actions 中对应的任务和步骤查看。使用 `journalctl -u geodomdesk-publisher.service --since today` 查看云端日志，最近一次结果位于 `/var/lib/geodomdesk-publisher/status.json`。

## 连接协议与登录

设备 ID、消息协议、密钥交换、加密、打洞、中继和 WebSocket 沿用官方实现。通过设备 ID 互连时，两端使用相同服务端及公钥。

客户端本机直接 IP 监听使用 TCP **21128**，局域网发现使用 UDP **21129**，服务端端口不变。手动直接 IP 连接时需要指定被控端的监听端口；通过设备 ID 建立的点对点连接沿用官方流程。局域网自动发现与官方客户端隔离。

通过设备 ID 发起的远程控制、文件传输、TCP 隧道、RDP、摄像头和终端共用服务端账号验证。服务端要求登录时取消重连计时器，并去重打开原有登录窗口；中继回退包装的登录错误同样处理。普通会话登录成功后只重连一次，取消后不继续重试。TCP 隧道和 RDP 保留原监听，每次新的本地连接读取当前账号令牌；被拒绝的本地应用连接需要重新发起，不重建监听或套用桌面画面等待流程。被控端访问密码和临时网络错误沿用官方流程。此界面修复只在 GeodomDesk 生效。

## Ubuntu 系统克隆

沿用官方初始 ID 算法及手动修改 ID 功能。在主配置中单独记录永久物理网卡绑定：首次没有记录时只建立绑定，不改变现有 ID；同一硬件保留现有 ID，包括手动修改的 ID。克隆镜像在不同网卡上启动时，重新分配副本 ID 并保存新绑定，后续正常启动不重复修改。

绑定使用永久物理网卡标识，过滤虚拟桥接、VPN 与随机化地址。硬件信息读取失败时沿用现有 ID。重新分配 ID 时保留服务器、密码、个人设置和系统机器标识。

## 数据导入

Windows、Ubuntu 新安装首次启动时自动导入一次，中断后下次启动继续；已有设备记录或收藏的升级安装保留原有选择。入口仍位于「设置 → 常规 → 数据迁移」，手动操作前确认范围，完成后显示新增、已存在、跳过和收藏数量。现有记录不覆盖，重复操作不重复添加。

自动和手动导入使用相同规则：普通记录匹配原客户端当前 ID 服务器公钥；跨服务器记录匹配其独立公钥，缺少或不匹配则跳过。导入通过筛选的设备资料及保存的连接密码，已有记录只补齐空密码；密码经官方加解密接口按目标规则保存，无法解密则跳过并计数。本机身份、无人值守密码、登录令牌、信任凭据及画质设置不迁移。跨服务器记录保留已验证的公钥上下文，不更改官方客户端。

## 最近访问同步

各端使用同一 GeodomDesk 账号登录，检测到服务端支持后会自动同步最近访问的设备。设备别名、显示、键盘和中继偏好随最近一次修改同步，已保存的设备访问凭据通过个人地址簿复用。可在地址簿菜单调整「同步最近访问的设备与凭据」。客户端使用 `config/client-channel.json` 中的 HTTPS 账户地址访问登录与个人地址簿接口。

## 上游同步与版本

将 OpenAI 项目密钥保存到仓库 Actions Secret `OPENAI_API_KEY`，并启用 Actions 创建 PR 的仓库权限。

1. **Sync RustDesk Upstream** 每 12 小时解析官方稳定版对应的提交。已审查的提交直接结束本次同步。也可在 **Actions** 手动运行，输入带或省略 `v` 前缀的官方稳定版标签，留空选择最新稳定版。
2. 工作流先尝试三方合并，遇到定制冲突或上游移除定制文件时由 Codex 生成修复。工作流记录近 7 天的冲突修复尝试，同一主分支提交和上游提交组合只自动调用一次。勾选 `retry_codex` 可主动复查并重新尝试。
3. 独立任务核对定制补丁和官方基线，再运行 Windows、Ubuntu 原生构建及 CLI、TCP 隧道、安装校验。通过后创建并合并 PR。构建阶段遇到临时故障时，可重跑失败任务并沿用本次补丁。
4. 正式构建从合并提交生成 Windows、Ubuntu 和 Android Release 产物。云端发布程序验证摘要、签署更新清单并切换发布指针。

显示版本和 Release 标签与官方版本一致。内部发布标识采用 `VERSION-rBUILD_NUMBER`，Ubuntu 包版本采用 `VERSION+geodom.BUILD_NUMBER`。内部构建号递增，支持同一官方版本的定制更新。

```sh
python3 packaging/prepare_client.py --source "$UPSTREAM_SOURCE" --version "$VERSION" --build-number "$BUILD_NUMBER"
```

## 打包与更新

三种平台原生编译。每个内部构建使用独立的不可变目录，产物校验通过后按版本和构建号向前更新发布指针。

产物包含 Windows 原生 EXE、Ubuntu DEB、Android ARM64 APK、对应源码、更新清单及签名。公开连接参数以编译时配置为准，API 可留空。设备访问密码和验证模式由设备使用者管理。

连接参数在 `config/client-channel.json` 维护，由 `patches/server_config.py` 写入编译配置。桌面和移动客户端使用这份连接配置。设备 ID、访问密码和个人记录保存在客户端的独立配置目录。

Windows 原生 EXE 自带安装入口，更新使用同一程序的 `--update`。Ubuntu 提供更新提示和下载，由管理员安装 DEB。云端校验并复制原生构建文件，生成签名更新清单。

保留官方签名、哈希、大小、临时文件复核和提升权限流程。应用标识、更新信任公钥及签名上下文独立。签名私钥只保存在受限部署环境与 Actions Secrets；账号、令牌和远程访问密码不进入源码或安装包默认值。

上游同步会在合并 PR 后自动通过 `reuse_desktop_run` 复用候选分支已验证的 Windows / Ubuntu 桌面产物，避免二次全量编译；Android 资源修复也可在手动构建中填写 `reuse_desktop_run` 复用主分支失败运行中未发布的桌面产物。复用条件均为桌面源码、构建配置、工具链和构建步骤一致。

## Android

使用独立应用标识与固定的正式签名证书，可与官方客户端共存。保留原生 JNI/Kotlin 命名空间，设备数据由 Android 应用沙箱隔离。预置公开服务器参数，账号令牌和远程访问密码不进入客户端源码。

APK 显示版本跟随官方，版本代码使用递增的内部构建序号。更新提示指向独立下载页，安装由 Android 系统确认；屏幕录制、无障碍和后台权限保留官方授权流程。构建后核对应用标识、版本、ARM64 库和签名证书。签名私钥只存放在受限部署目录与 Actions Secrets。

## 验证

```sh
python3 -m unittest discover -s tests -v
python3 packaging/verify_rust.py
```

Windows 在临时 CI 环境另运行 `tests/windows/test-windows-native.ps1`，核对实际安装与固定连接参数。

`patches/id_controls.py` 维护默认 ID 及原生恢复入口；`patches/settings_cleanup.py` 维护设置项可见性和用户链接，保留网络兼容、权限与打印功能。用户说明位于 `USER_GUIDE.md`。

验证覆盖上游改动保留、冲突不写入、监听隔离、账号错误不重试、签名篡改、同版本构建更新、回退保护、打包失败不切换及源码归档完整性。

## 许可

沿用 RustDesk 的 AGPL-3.0 许可，每次分发提供包含修改及子模块的对应源码。图标资源位于 `assets/`。
