$marker = '__MARKER__'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$shell = if (__CORE__) {
    $installed = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'PowerShell\7\pwsh.exe'
    if (Test-Path -LiteralPath $installed -PathType Leaf) {
        $installed
    } else {
        Get-Command pwsh.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty Source
    }
} else {
    (Get-Process -Id $PID).Path
}
if (!$shell -or !(Test-Path -LiteralPath $shell -PathType Leaf)) {
    [Console]::WriteLine("`n${marker}:ERROR:Requested PowerShell is unavailable")
    exit 1
}
[Console]::WriteLine("`n${marker}:READY")
$payload = [Text.StringBuilder]::new()
for ($i = 1; $i -le __LINES__; $i++) {
    $line = [Console]::ReadLine()
    if ($null -eq $line) { exit 1 }
    [void]$payload.Append($line)
    if ($i % __BATCH__ -eq 0) { [Console]::WriteLine("`n${marker}:ACK") }
}
$payload = $payload.ToString()
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $digest = [BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes($payload))).Replace('-', '')
} finally {
    $sha.Dispose()
}
if ($digest -ne '__DIGEST__') {
    [Console]::WriteLine("`n${marker}:ERROR:Legacy terminal script checksum mismatch")
    exit 1
}
$parts = $payload.Split(':', 2)
$script = $utf8.GetString([Convert]::FromBase64String($parts[1]))
[Console]::WriteLine("`n${marker}:START")
$script | & $shell -NoLogo -NoProfile -NonInteractive -EncodedCommand $parts[0] 2>&1 | ForEach-Object {
    $bytes = $utf8.GetBytes($_.ToString() + "`n")
    for ($offset = 0; $offset -lt $bytes.Length; $offset += 48) {
        $record = [Convert]::ToBase64String($bytes, $offset, [Math]::Min(48, $bytes.Length - $offset))
        [Console]::WriteLine("${marker}:DATA:$record")
    }
}
$code = $LASTEXITCODE
[Console]::WriteLine("`n${marker}:END:$code")
