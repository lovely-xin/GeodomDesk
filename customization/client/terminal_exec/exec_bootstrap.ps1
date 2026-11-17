$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
try {
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
    if ($geodomdeskScriptChunks) {
        $encoded = ''
        for ($i = 0; $i -lt $geodomdeskScriptChunks; $i++) {
            $name = "GEODOMDESK_EXEC_SCRIPT_$i"
            $chunk = [Environment]::GetEnvironmentVariable($name)
            [Environment]::SetEnvironmentVariable($name, $null)
            if (!$chunk) { throw 'Incomplete PowerShell script' }
            $encoded += $chunk
        }
        $source = $utf8.GetString([Convert]::FromBase64String($encoded))
    } else {
        [Console]::InputEncoding = $utf8
        $source = [Console]::In.ReadToEnd()
    }
    if ($null -ne $geodomdeskExpectedBytes -and $utf8.GetByteCount($source) -ne $geodomdeskExpectedBytes) {
        [Console]::Error.WriteLine('Incomplete PowerShell script')
        exit 1
    }
    $remoteScript = [ScriptBlock]::Create($source)
    $global:LASTEXITCODE = $null
    $script:commandHadError = $false
    if ($geodomdeskScriptChunks) {
        [Console]::Out.Write('GEODOMDESK_EXEC_READY')
        [Console]::Out.Flush()
    }
    & $remoteScript 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            [Console]::Error.WriteLine($_.ToString())
            if ($_.FullyQualifiedErrorId -notlike 'NativeCommandError*') {
                $script:commandHadError = $true
            }
        } else {
            $_
        }
    }
    if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }
    if ($commandHadError) { exit 1 }
    exit 0
} catch {
    [Console]::Error.WriteLine($_.ToString())
    exit 1
}
