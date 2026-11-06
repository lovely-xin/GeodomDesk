$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$ProgressPreference = 'SilentlyContinue'
try {
    $remoteScript = [ScriptBlock]::Create([Console]::In.ReadToEnd())
    $global:LASTEXITCODE = $null
    $script:commandHadError = $false
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
