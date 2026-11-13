# 发起端 CLI 查找流程

在实际发起端查找 CLI，并在同一宿主机的非沙箱会话中验证和运行它。使用支持 `sandbox_permissions` 的 Codex `exec_command` 时，以 `require_escalated` 请求在沙箱外执行。其他运行器使用各自的宿主机权限流程。只有受限执行环境时，将网络验证标记为待宿主机执行。

1. **检查已知路径。** 优先使用用户指定的可执行文件或安装目录。也可复用本次任务已验证的路径。
2. **检查 PATH。** Windows 用 `Get-Command geodomdesk-cli.exe -CommandType Application -All`，Linux 和 macOS 用 `command -v geodomdesk-cli`。取返回的可执行文件路径。
3. **按系统查安装位置。**
   - **Windows**：查 Windows 已安装应用的卸载注册表记录，覆盖当前用户和系统的 32 位、64 位安装。先用 `InstallLocation` 定位 `geodomdesk-cli.exe`。字段缺失或路径失效时，从 `DisplayIcon` 或开始菜单、桌面快捷方式的目标定位安装目录。图标可能指向资源子目录，可沿父目录找到主程序，再检查同目录的 CLI。也可按安装线索检查 `ProgramFiles`、`ProgramFiles(x86)` 或 `LOCALAPPDATA\Programs` 下的对应目录。
   - **Linux 和 macOS**：按实际安装方式查已装软件包的文件清单、应用目录或便携包解压位置。用清单或目录中的实际可执行文件名和路径。
4. **核对候选。** 用可执行文件的完整路径运行 `--version`，确认成功退出且返回 GeodomDesk 版本。Windows 用 `& $cli --version`，其中 `$cli` 是候选路径。按任务需要再用 `--help` 查看支持的命令和参数。帮助包含 `embedded SSH over TCP` 时可使用独立内置 SSH。存在多个候选时，优先选择来源已知且具备任务所需能力的独立 CLI，保留实际使用路径。

仍找不到可用的 CLI 时，询问安装目录或便携包位置。需要独立 CLI 时，从项目发布页或管理员取得适用平台的软件包，核对随包的 SHA-256，再用完整路径启动。

## Windows 安装记录示例

在 64 位 Windows 上使用 64 位 PowerShell 读取两个系统安装视图。下例列出匹配记录及按 `InstallLocation` 找到的 CLI 候选。

```powershell
$uninstallKeys = @(
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)

Get-ItemProperty -Path $uninstallKeys -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like 'GeodomDesk*' } |
    Select-Object DisplayName, DisplayVersion, InstallLocation, DisplayIcon, @{
        Name = 'CliPath'
        Expression = {
            if ($_.InstallLocation) {
                $directory = [Environment]::ExpandEnvironmentVariables($_.InstallLocation.Trim().Trim('"'))
                $candidate = Join-Path $directory 'geodomdesk-cli.exe'
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    $candidate
                }
            }
        }
    }
```
