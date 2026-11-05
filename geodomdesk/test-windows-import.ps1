param(
  [Parameter(Mandatory=$true)][string]$Client,
  [Parameter(Mandatory=$true)][string]$BuildConfig
)
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -cne 'true'){throw 'Run import fixtures only on a disposable GitHub Actions runner'}
$config=Get-Content -LiteralPath $BuildConfig -Raw | ConvertFrom-Json
$source=Join-Path $env:APPDATA 'RustDesk\config'
$target=Join-Path $env:APPDATA 'GeodomDesk\config'
$null=New-Item -ItemType Directory -Path (Join-Path $source 'peers') -Force
Set-Content -LiteralPath (Join-Path $source 'RustDesk2.toml') -Encoding utf8 -Value ('[options]'+"`n"+'key = "'+$config.server.key+'"')
Set-Content -LiteralPath (Join-Path $source 'RustDesk_local.toml') -Encoding utf8 -Value 'fav = ["111333555", "111333556@different-host"]'
Set-Content -LiteralPath (Join-Path $source 'peers\111333555.toml') -Encoding utf8 -Value @'
password = [1, 2, 3]
[info]
hostname = "ImportFixture"
platform = "Windows"
[options]
alias = "Matching server"
'@
Set-Content -LiteralPath (Join-Path $source 'peers\111333556@different-host.toml') -Encoding utf8 -Value @'
[options]
other-server-key = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="
'@
$local=Join-Path $target 'GeodomDesk_local.toml'
$matched=Join-Path $target 'peers\111333555.toml'
$unmatched=Join-Path $target 'peers\111333556@different-host.toml'
function Start-ImportFixture {
  $process=Start-Process -FilePath $Client -WindowStyle Hidden -PassThru
  try {
    for($i=0;$i -lt 45;$i++) {
      if((Test-Path -LiteralPath $local) -and (Get-Content -LiteralPath $local -Raw) -match 'geodomdesk-initial-import\s*=\s*"Y"'){return}
      if($process.HasExited){throw 'Client exited before first-run initialization'}
      Start-Sleep -Seconds 1
    }
    throw 'First-run import did not complete'
  } finally {
    if(-not $process.HasExited){Stop-Process -Id $process.Id -Force}
  }
}
Start-ImportFixture
if(-not (Test-Path -LiteralPath $matched) -or (Test-Path -LiteralPath $unmatched)){throw 'First-run import did not enforce the ID-server public key'}
$peer=Get-Content -LiteralPath $matched -Raw
if($peer -notmatch 'password\s*=\s*\[\s*\]' -or $peer -notmatch 'Matching server'){throw 'Imported record contains credentials or lost its alias'}
$settings=Get-Content -LiteralPath $local -Raw
if($settings -notmatch '111333555' -or $settings -match '111333556'){throw 'Favorites were not filtered with device records'}
# Later source changes must not silently import again on an ordinary restart.
Set-Content -LiteralPath (Join-Path $source 'peers\111333557.toml') -Encoding utf8 -Value '[info]'
$process=Start-Process -FilePath $Client -WindowStyle Hidden -PassThru
try {
  Start-Sleep -Seconds 8
  if(Test-Path -LiteralPath (Join-Path $target 'peers\111333557.toml')){throw 'First-run import repeated on restart'}
} finally {
  if(-not $process.HasExited){Stop-Process -Id $process.Id -Force}
}
Write-Output 'First-run same-server import, credential exclusion, favorites and one-time initialization verified.'
# A pending migration resumes even after some device files were already written.
Set-Content -LiteralPath $matched -Encoding utf8 -Value ((Get-Content -LiteralPath $matched -Raw).Replace('Matching server','Retained alias'))
Set-Content -LiteralPath $local -Encoding utf8 -Value ((Get-Content -LiteralPath $local -Raw) -replace '(geodomdesk-initial-import\s*=\s*)"Y"','$1"pending"')
Start-ImportFixture
if(-not (Test-Path -LiteralPath (Join-Path $target 'peers\111333557.toml'))){throw 'Interrupted migration did not resume'}
if((Get-Content -LiteralPath $matched -Raw) -notmatch 'Retained alias'){throw 'Resumed migration overwrote an existing record'}
Write-Output 'Interrupted first-run import resumes without overwriting existing user data.'
