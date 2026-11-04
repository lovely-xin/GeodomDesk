# GeodomDesk 客户端定制与发布

支持 Windows x64、Ubuntu amd64，跟踪 RustDesk 官方稳定版。只修改客户端与下载安装包，不修改 RustDesk Server。

## 结构

| 路径 | 职责 |
| --- | --- |
| `prepare_client.py` | 源码定制命令行入口 |
| `customizations/identity.py` | 应用、服务、配置、IPC、进程、本机端口隔离 |
| `customizations/account_login.py` | 要求账号登录时停止重试，打开官方登录对话框 |
| `customizations/update_channel.py` | 独立更新通道、公钥与更新界面 |
| `customizations/branding.py` | 公司图标与 Logo |
| `customizations/upstream.py` | 基线校验、与官方新版三方合并 |
| `customizations/build.py` | 组合定制、统一写入、记录版本与提交 |
| `client/` | 随客户端编译的独立 Rust 模块 |
| `reference/`、`source-lock.json` | 已审查的官方基线及 SHA-256 |
| `plan_upstream.py`、`ci/build.yml` | 跟踪官方稳定版、原生构建及发布 |
| `repack_deb.py`、`archive_source.py`、`release_bundle.py` | Ubuntu 包隔离、对应源码和发布清单 |
| `channel.py`、`publisher.py`、`installer.py`、安装模板 | 云端打包、签名更新和下载发布 |

长期修改放在对应定制模块或 `client/` 中，不直接编辑临时 checkout。基线来自官方 1.5.1 提交 `23e5bb471a040d62f0a6ad52591fb30784265b73` 及对应子模块。

## 兼容与端口

设备 ID、消息协议、密钥交换、加密、打洞、中继及 WebSocket 沿用官方实现。GeodomDesk 与官方客户端使用相同 ID／中继服务器和公钥时，按设备 ID 互连不需要更改服务器端口。完整二进制互通和共存安装仍需构建后实机验证。

只更改客户端本机监听：直接 IP 访问 TCP **21128**，局域网发现 UDP **21129**。官方客户端直接连接 GeodomDesk 时填写 `设备IP:21128`。局域网自动发现与官方客户端隔离，设备 ID 连接不受影响。云端 ID／中继服务端口不变。

服务端返回 `Please log in before starting a remote connection` 或 `Invalid account or password` 时，取消重连计时器，去重打开已有账号登录窗口。登录成功只重连一次，取消后不继续重试。被控端访问密码及临时网络错误沿用官方流程。这个界面修复只对 GeodomDesk 生效。

## 官方同步与版本

Actions 每 6 小时检查官方最新稳定 Release。官方源码或定制代码改变后重新构建；手动运行和主分支定制修改也可构建。Windows、Ubuntu 原生编译，不使用 Docker。

显示版本与 GitHub Release 标签跟官方一致，例如 **1.5.1**。内部构建序号独立递增，例如 `1.5.1-r101`，所以同一官方版本的定制修复也能更新。Ubuntu 包版本使用 `1.5.1+geodom.101`，应用显示仍为 1.5.1。

三方合并保留无冲突的官方改动和本地定制。冲突、文件移除、构建或校验失败时停止发布，保留当前可用版本；审查冲突后再次构建。

```sh
python3 prepare_client.py --source /path/to/official-client --version 1.5.1 --build-number 101
```

## 下载和更新

更新检查 GET `https://www.qcjcpt.com/downloads/geodomdesk/latest.json`，保留官方响应的 `url` 字段，空字符串表示没有正式版本。

```text
releases/1.5.1-r101/
  geodomdesk-1.5.1-r101-windows-x64.exe
  geodomdesk-1.5.1-r101-windows-x64-setup.exe
  geodomdesk-1.5.1-r101-ubuntu-amd64.deb
  geodomdesk-1.5.1-r101-ubuntu-amd64.run
  geodomdesk-1.5.1-r101-source.tar.gz
  geodomdesk-update.json
  geodomdesk-update.json.sig
  publication.json
  index.html
```

每个构建目录不可变，完整校验后才切换更新指针。保留官方 Ed25519、SHA-256、下载大小、临时文件复核与提升权限流程。GeodomDesk 签名上下文、公钥、应用标识和目录独立。Windows 原生更新使用单文件客户端 EXE 的 `--update`；外层 NSIS 是带预置配置的新安装包。Ubuntu 提供更新提示和下载，管理员运行 RUN 安装，不声称支持静默自动安装。

签名私钥及预置访问密码只保存在云端受限配置中，不进入 GitHub。已有 GeodomDesk 安装保留原永久密码和验证模式。

## 云端

代码 `/home/geodom/script/geodomdesk-builder`，私有配置 `/etc/geodomdesk-builder/config.json`，状态 `/var/lib/geodomdesk-builder`。首次构建验证前保持 `enabled=false`，现有普通 RustDesk 下载继续工作。

```sh
sudo python3 /home/geodom/script/geodomdesk-builder/activate.py --repository lovely-xin/GeodomDesk
systemctl status geodomdesk-builder.timer
journalctl -u geodomdesk-builder.service -n 60 --no-pager
```

激活先打包并校验全部产物，再切换导航页和更新指针；后续定时检查 GitHub 清单，不重复打包相同构建，拒绝回退，不重启 ID／中继服务。

## 验证与许可

```sh
python3 -m unittest test_channel test_packaging -v
python3 verify_rust.py
```

Windows 另运行 `test-windows-installer.ps1`。覆盖合并冲突不写入、保留上游改动、本机端口与协议隔离、账号失败不重试、签名篡改、同版本更新、回退保护、打包失败不切换。

沿用 RustDesk AGPL-3.0。每次分发同时提供含修改及子模块的对应源码，公司图标见 `assets/`。
