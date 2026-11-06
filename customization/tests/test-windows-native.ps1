param(
  [Parameter(Mandatory=$true)][string]$Package,
  [Parameter(Mandatory=$true)][string]$BuildConfig
)
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -cne 'true'){throw 'Run this installation check only on a disposable GitHub Actions runner'}
$config=Get-Content -LiteralPath $BuildConfig -Raw | ConvertFrom-Json
$installed=Join-Path $env:ProgramFiles 'GeodomDesk\GeodomDesk.exe'
$process=Start-Process -FilePath (Resolve-Path -LiteralPath $Package).Path -ArgumentList '--silent-install' -WindowStyle Hidden -PassThru
if(-not $process.WaitForExit(120000)){throw 'Native installer extraction timed out'}
try {
  for($i=0;$i -lt 120;$i++) {
    $service=Get-Service -Name 'GeodomDesk' -ErrorAction SilentlyContinue
    if((Test-Path -LiteralPath $installed) -and $service -and $service.Status -eq 'Running'){break}
    Start-Sleep -Seconds 1
  }
  if(-not (Test-Path -LiteralPath $installed) -or -not $service -or $service.Status -ne 'Running'){throw 'Native installation did not create the GeodomDesk application and running service'}
  $info=[Diagnostics.FileVersionInfo]::GetVersionInfo($installed)
  Write-Output ('Installed native client: {0} {1}' -f $info.ProductName,$info.ProductVersion)
  if($info.ProductName -cne 'GeodomDesk' -or $info.ProductVersion -notmatch ('^'+[regex]::Escape($config.version)+'(?:[.+]|$)')){throw 'Installed application identity or version mismatch'}
  function Invoke-Client([string[]]$ClientArgs) {
    $start=[Diagnostics.ProcessStartInfo]::new()
    $start.FileName=$installed
    $start.UseShellExecute=$false
    $start.CreateNoWindow=$true
    $start.RedirectStandardOutput=$true
    $start.RedirectStandardError=$true
    foreach($argument in $ClientArgs){$start.ArgumentList.Add($argument)}
    $client=[Diagnostics.Process]::Start($start)
    try {
      $output=$client.StandardOutput.ReadToEndAsync()
      $errors=$client.StandardError.ReadToEndAsync()
      if(-not $client.WaitForExit(15000)){$client.Kill();throw "Native CLI timed out: $($ClientArgs[0])"}
      $text=$output.GetAwaiter().GetResult().Trim()
      $null=$errors.GetAwaiter().GetResult()
      if($client.ExitCode -ne 0){throw "Native CLI failed: $($ClientArgs[0])"}
      return $text
    } finally {$client.Dispose()}
  }
  function Read-Option([string]$Name) {return Invoke-Client -ClientArgs @('--option',$Name)}
  $appRoot=Split-Path -Parent $installed
  $cli=Join-Path $appRoot 'geodomdesk-cli.exe'
  $helpText=& $cli --help
  if($LASTEXITCODE -ne 0 -or $helpText -notmatch 'geodomdesk-cli exec'){throw 'Installed terminal CLI did not start'}
  & (Join-Path $PSScriptRoot 'test-windows-terminal.ps1') -Client $installed
  $remoteConfigOption='allow-remote-config-modification'
  if((Read-Option $remoteConfigOption) -cne 'Y'){throw 'Remote configuration permission is not enabled by default'}
  $null=Invoke-Client -ClientArgs @('--option',$remoteConfigOption,'N')
  if((Read-Option $remoteConfigOption) -cne 'N'){throw 'A local user could not disable remote configuration'}
  $null=Invoke-Client -ClientArgs @('--option',$remoteConfigOption,'Y')
  foreach($name in @('printer_driver_adapter.dll','drivers\RustDeskPrinterDriver\RustDeskPrinterDriver.inf')) {
    if(-not (Test-Path -LiteralPath (Join-Path $appRoot $name))){throw "Printer component missing from installed package: $name"}
  }
  $null=Invoke-Client -ClientArgs @('--install-remote-printer')
  if(-not (Get-Printer -Name 'GeodomDesk Printer' -ErrorAction SilentlyContinue)){throw 'Native printer installation did not create the GeodomDesk queue'}
  $fixtureCreated=$false
  try {
    if(-not (Get-Printer -Name 'RustDesk Printer' -ErrorAction SilentlyContinue)) {
      if(-not (Get-PrinterPort -Name 'RustDesk Printer' -ErrorAction SilentlyContinue)){Add-PrinterPort -Name 'RustDesk Printer'}
      Add-Printer -Name 'RustDesk Printer' -DriverName 'RustDesk v4 Printer Driver' -PortName 'RustDesk Printer'
      $fixtureCreated=$true
    }
    $null=Invoke-Client -ClientArgs @('--uninstall-remote-printer')
    if(-not (Get-Printer -Name 'RustDesk Printer') -or -not (Get-PrinterDriver -Name 'RustDesk v4 Printer Driver')){throw 'Uninstalling the independent queue affected the existing printer'}
    $null=Invoke-Client -ClientArgs @('--install-remote-printer')
    if(-not (Get-Printer -Name 'GeodomDesk Printer') -or -not (Get-Printer -Name 'RustDesk Printer')){throw 'Printer coexistence failed'}
    Write-Output 'Native printer installation and shared-driver coexistence verified.'
  } finally {
    if($fixtureCreated){Remove-Printer -Name 'RustDesk Printer';Remove-PrinterPort -Name 'RustDesk Printer'}
  }
  $hostConfig=Join-Path $env:windir 'ServiceProfiles\LocalService\AppData\Roaming\GeodomDesk\config\GeodomDesk.toml'
  $defaultId=''
  for($i=0;$i -lt 15;$i++) {
    if(Test-Path -LiteralPath $hostConfig) {
      $content=Get-Content -LiteralPath $hostConfig -Raw
      if($content -match '(?m)^default_id\s*=\s*["\x27]([0-9]{6,10})["\x27]'){$defaultId=$Matches[1];break}
    }
    Start-Sleep -Seconds 1
  }
  if(-not $defaultId){throw 'The host service did not persist its default ID'}
  $currentId=Invoke-Client -ClientArgs @('--get-id')
  if($currentId -cne $defaultId){throw 'A fresh installation did not retain its assigned ID as the default'}
  Write-Output 'The host service retained its automatically assigned default ID.'
  $expected=@{
    'custom-rendezvous-server'=$config.server.host
    'key'=$config.server.key
    'websocket-port'=$config.server.websocket_port
    'api-server'=$config.server.api
    'relay-server'=$config.server.relay
    'allow-auto-update'='Y'
  }
  foreach($name in $expected.Keys) {
    if((Read-Option $name) -cne [string]($expected[$name])){throw "Fixed setting differs: $name"}
  }
  # Negative tests: command-line edits and the former wrapper's import must be ignored.
  foreach($name in @('custom-rendezvous-server','key','api-server','relay-server','websocket-port')) {
    $null=Invoke-Client -ClientArgs @('--option',$name,'blocked.invalid')
    if((Read-Option $name) -cne [string]($expected[$name])){throw "Fixed setting could be changed: $name"}
  }
  $injected=@{host='blocked.invalid';key='unused';api='https://blocked.invalid';relay='blocked.invalid'} | ConvertTo-Json -Compress
  $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($injected)).Replace('+','-').Replace('/','_')
  $characters=$encoded.ToCharArray();[Array]::Reverse($characters)
  $null=Invoke-Client -ClientArgs @('--config',(-join $characters))
  foreach($name in @('custom-rendezvous-server','key','api-server','relay-server','websocket-port')) {
    if((Read-Option $name) -cne [string]($expected[$name])){throw "Imported setting overrode the compiled value: $name"}
  }
  Write-Output 'Native installation and fixed connection settings verified; external configuration ignored and no password provisioned.'
  & (Join-Path $PSScriptRoot 'test-windows-import.ps1') -Client $installed -BuildConfig $BuildConfig
} finally {
  Stop-Service -Name 'GeodomDesk' -ErrorAction SilentlyContinue
}
