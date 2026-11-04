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
} finally {
  Stop-Service -Name 'GeodomDesk' -ErrorAction SilentlyContinue
}
