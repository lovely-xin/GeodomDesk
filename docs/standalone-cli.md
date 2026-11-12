# GeodomDesk 独立 CLI

将对应平台的 `geodomdesk-cli.exe` 或 `geodomdesk-cli` 解压到独立目录，运行 `geodomdesk-cli --help`。帮助中出现 `embedded SSH over TCP` 即为内置 SSH 版本。Linux 由系统提供程序依赖的运行库。被控设备或中转设备继续使用已授权的 GeodomDesk 服务。

## 执行命令

先从最近访问记录核对设备 ID、在线状态和设备身份。一次性执行用 `exec`，在同一目标上连续执行用 `session`。选择 `bash`、`powershell` 或 `pwsh`，脚本可用 `--command`、`--script FILE` 或 `--script -` 提供。

```text
geodomdesk-cli exec --id DEVICE_ID --expect-hostname HOSTNAME --shell bash --command 'hostname' --json
geodomdesk-cli exec --id DEVICE_ID --expect-platform Windows --shell powershell --command 'hostname' --json
```

默认模式在指定 shell 时先验证 GeodomDesk 账号，再通过 TCP 中继尝试设备的 SSH 服务。默认 SSH 端口未响应或没有可用的 SSH 凭据时，CLI 改用已授权的 GeodomDesk 终端，并按对端能力选用相应协议。服务拒绝登录或主机校验失败时直接返回原因。`--transport ssh` 要求使用 SSH，`--transport terminal` 直接使用 GeodomDesk 终端。无 `--shell` 的会话支持在每条请求中选择 shell，并使用 GeodomDesk 终端。

CLI 优先复用有效账号会话和已保存的设备凭据。缺少 GeodomDesk 登录时传入 `--account-user USER`，需要设备密码时加 `--ask-device-password`。SSH 服务按目标操作系统账号认证，支持 `--ssh-user USER`、`--ssh-identity FILE` 和 `--ask-ssh-password`。交互密码和私钥口令采用无回显输入。自动化可通过进程环境传入对应凭据并加 `--non-interactive`。将账号密码、设备密码、目标 SSH 密码和私钥口令分别保管。

## 中转到下游设备

`--id` 选择已授权的 GeodomDesk 中转设备，`--ssh-host` 和 `--ssh-port` 选择中转设备能够访问的下游 SSH 服务。中转设备负责 TCP 转发，下游服务负责操作系统账号认证和执行。命令的 `--expect-hostname` 和 `--expect-platform` 校验中转设备，SSH 主机密钥校验下游设备。

```text
geodomdesk-cli exec --id RELAY_DEVICE_ID --expect-hostname RELAY_HOSTNAME --transport ssh --ssh-host DESTINATION --ssh-port 22 --ssh-user OS_USER --ssh-host-fingerprint SHA256:TRUSTED_FINGERPRINT --ssh-identity KEY_FILE --shell bash --command 'hostname' --json
```

首次使用下游设备时，从管理员取得主机指纹并传给 `--ssh-host-fingerprint`，或提供已经配置的 `--ssh-known-hosts`。后续会话复用按中转设备、目的地址和端口保存的主机密钥。发现密钥变化时先通过可信渠道核实，再更新信任记录。

## 常驻会话和回收

```text
geodomdesk-cli session --id DEVICE_ID --shell powershell --expect-hostname HOSTNAME --non-interactive
```

保持标准输入打开，每行发送一个 JSON 请求。会话逐条执行并返回 JSON，`stdout` 和 `stderr` 为 Base64。

```json
{"id":"first","script":"hostname"}
{"id":"check","op":"status"}
{"id":"fresh","connection":"fresh","script":"hostname"}
{"id":"close","op":"close"}
```

健康连接优先复用。`connection: "fresh"` 或 `op: "reconnect"` 重新建立连接并校验账号。`op: "cancel"` 中断当前请求并清理本次连接。结果为 `execution_state: "unknown"` 时，先用只读操作核对远端状态，再决定是否重新提交。关闭输入或结束进程会释放会话和所属隧道。

`tunnel list --id DEVICE_ID` 显示内置 SSH 管理的隧道。`private_stream: true` 表示 SSH 流只在 CLI 进程内使用。`tunnel close --id DEVICE_ID --tunnel-id UUID` 可关闭对应隧道。访问 HTTP、HTTPS、RDP 等已知服务时，使用 `tunnel open` 管理本地 TCP 端口，并用服务实际握手核验。

## 构建

在新的已审核 RustDesk 源码目录中检出递归子模块。按平台安装项目的本地库依赖，使用 Rust 1.89.0 构建。GitHub 的 `Build standalone GeodomDesk CLI` 工作流分别运行 Windows 和 Ubuntu 测试，上传可独立解压使用的 CLI 和 SHA-256 清单。

```text
python customization/prepare_client.py --source UPSTREAM_DIRECTORY --version UPSTREAM_VERSION --build-number BUILD_NUMBER --standalone-cli
cd UPSTREAM_DIRECTORY
cargo +1.89.0 test --locked --release --features embedded-ssh --lib terminal_exec::
cargo +1.89.0 test --locked --release --features embedded-ssh --lib port_forward::
cargo +1.89.0 build --locked --release --features embedded-ssh --bin geodomdesk-cli
```
