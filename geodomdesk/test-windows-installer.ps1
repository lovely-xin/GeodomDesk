$ErrorActionPreference='Stop'
$template=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'install-windows.ps1.in'))
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($template,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Installer syntax is invalid'}
$quoteFunction=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Quote-ClientArgument'},$true)[0]
Invoke-Expression $quoteFunction.Extent.Text
if((Quote-ClientArgument 'A B"C\') -cne '"A B\"C\\"'){throw 'Windows command-line escaping failed'}
$start=$template.IndexOf('if($PermanentPassword) {')
$end=$template.IndexOf("Write-Output 'GeodomDesk installation and Geodom server configuration verified.'",$start)
if($start -lt 0 -or $end -lt 0){throw 'Password configuration block missing'}
$block=[scriptblock]::Create($template.Substring($start,$end-$start))
$script:passwordForTest='O''Brien $x;&!@'
function Start-Sleep {param($Seconds)}
function Invoke-GeodomDesk([string[]]$ClientArgs) {
  [void]$script:calls.Add(($ClientArgs -join '|'))
  if($ClientArgs[0] -eq '--password') {
    if($ClientArgs.Count -ne 2 -or $ClientArgs[1] -cne $script:passwordForTest){throw 'Password argument changed'}
    if($script:scenario -eq 'password-failure'){return 'Settings are disabled!'}
    return 'Done!'
  }
  if($ClientArgs[0] -eq '--option' -and $ClientArgs[1] -eq 'verification-method') {
    if($ClientArgs.Count -eq 3){$script:method=$ClientArgs[2];return ''}
    if($script:scenario -eq 'mode-failure'){return 'use-temporary-password'}
    return $script:method
  }
  throw 'Unexpected client command'
}
foreach($script:scenario in @('success','password-failure','mode-failure','preserve-existing')) {
  $script:calls=New-Object 'Collections.Generic.List[string]'
  $script:method=''
  $PermanentPassword=$script:passwordForTest
  $PreserveExistingPassword=$script:scenario -eq 'preserve-existing'
  $failed=$false
  try {$output=& $block} catch {$failed=$true}
  if($script:scenario -eq 'success') {
    if($failed -or $script:method -cne 'use-permanent-password'){throw 'Successful password configuration was rejected'}
    if(($output -join '') -match [regex]::Escape($PermanentPassword)){throw 'Password leaked to installer output'}
  } elseif($script:scenario -eq 'preserve-existing') {
    if($failed -or $script:calls.Count -ne 0 -or $script:method -ne ''){throw 'An existing password or verification mode was modified'}
    if(($output -join '') -notmatch 'preserved'){throw 'Existing installation was not acknowledged'}
  } elseif(-not $failed){throw 'Failed password configuration was reported as successful'}
}
Write-Output 'Windows password initialization, preservation, rejection, mode failure, and argument escaping checks passed.'

$start=$template.IndexOf('$verified=$false')
$end=$template.IndexOf('if($PermanentPassword)', $start)
if($start -lt 0 -or $end -lt 0){throw 'Server configuration verification block missing'}
$configBlock=[scriptblock]::Create($template.Substring($start,$end-$start))
$ExpectedHost='www.qcjcpt.com';$ExpectedKey='isolated-test-key';$ExpectedRelay=''
$ExpectedApi='https://www.qcjcpt.com/rustdesk';$Config='isolated-config'
function Invoke-GeodomDesk([string[]]$ClientArgs) {
  if($ClientArgs[0] -eq '--config'){
    if($ClientArgs[1] -cne $Config){throw 'Configuration argument changed'}
    if($script:apiScenario -ne 'stale-api'){$script:storedApi=$ExpectedApi}
    return ''
  }
  switch($ClientArgs[1]) {
    'websocket-port' {
      if($ClientArgs.Count -eq 3) {
        if($ClientArgs[2] -cne $ExpectedWsPort){throw 'WebSocket port argument changed'}
        if($script:apiScenario -ne 'ws-port-failure'){$script:storedWsPort=$ClientArgs[2]}
        return ''
      }
      return $script:storedWsPort
    }
    'allow-auto-update' {return 'Y'}
    'custom-rendezvous-server' {return $ExpectedHost}
    'key' {return $ExpectedKey}
    'relay-server' {return $ExpectedRelay}
    'api-server' {if($script:apiScenario -eq 'wrong-api'){return ''};return $script:storedApi}
    default {throw 'Unexpected option'}
  }
}
foreach($script:apiScenario in @('success','wrong-api','clear-old-api','stale-api','ws-port-success','ws-port-failure')) {
  $script:storedApi='https://legacy.example/api'
  $script:storedWsPort='80'
  $ExpectedWsPort=if($script:apiScenario -in @('ws-port-success','ws-port-failure')){'21114'}else{''}
  $ExpectedApi=if($script:apiScenario -in @('clear-old-api','stale-api','ws-port-success','ws-port-failure')){''}else{'https://www.qcjcpt.com/rustdesk'}
  $failed=$false
  try {$null=& $configBlock} catch {$failed=$true}
  if(($script:apiScenario -in @('wrong-api','stale-api','ws-port-failure')) -ne $failed){throw 'Server configuration verification gave the wrong result'}
  if($script:apiScenario -eq 'ws-port-success' -and $script:storedWsPort -cne '21114'){throw 'WebSocket port was not applied'}
}
Write-Output 'Windows API cleanup and WebSocket port success/failure checks passed.'

$processFunction=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-GeodomDesk'},$true)[0]
Invoke-Expression $processFunction.Extent.Text
$testRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot ('native-process-'+[Guid]::NewGuid().ToString('N'))))
$allowedRoot=[IO.Path]::GetFullPath($PSScriptRoot)+[IO.Path]::DirectorySeparatorChar
if(-not $testRoot.StartsWith($allowedRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Invalid test workspace'}
$null=New-Item -ItemType Directory -Path $testRoot
try {
  $Installed=Join-Path $testRoot 'fake-client.exe'
  $source=@'
using System;
using System.Threading;
public class Client {
  public static int Main(string[] args) {
    switch(args[0]) {
      case "--ok": Console.WriteLine("Done!");return 0;
      case "--empty": return 0;
      case "--fail": Console.Error.WriteLine("private-error-sentinel");return 7;
      case "--args": Console.OutputEncoding=new System.Text.UTF8Encoding(false);Console.Write(args[1]);return 0;
      case "--pipes": Console.Write(new string('x',2097152));Console.Error.Write(new string('y',2097152));return 0;
      case "--inherit-pipes":
        var child=System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(System.Reflection.Assembly.GetExecutingAssembly().Location,"--hold-pipes"){UseShellExecute=false,CreateNoWindow=true});
        System.IO.File.WriteAllText(args[1],child.Id.ToString());return 0;
      case "--hold-pipes": Thread.Sleep(4000);return 0;
      case "--pause": Thread.Sleep(30000);return 0;
      default:return 9;
    }
  }
}
'@
  Add-Type -TypeDefinition $source -Language CSharp -OutputType ConsoleApplication -OutputAssembly $Installed
  if((Invoke-GeodomDesk @('--ok')) -cne 'Done!'){throw 'Zero exit status was rejected'}
  if((Invoke-GeodomDesk @('--empty'))){throw 'Empty successful output changed'}
  $failed=$false
  try {$null=Invoke-GeodomDesk @('--fail')} catch {
    $failed=$true
    if($_.Exception.Message -notmatch 'exit code 7' -or $_.Exception.Message -match 'private-error-sentinel'){throw 'Exit code or private error handling failed'}
  }
  if(-not $failed){throw 'Nonzero exit status was accepted'}
  $argument='A B"C\'
  if((Invoke-GeodomDesk @('--args',$argument)) -cne $argument){throw 'Native argument round trip failed'}
  if([string](Invoke-GeodomDesk @('--args','')) -cne ''){throw 'Empty argument round trip failed'}
  $argument=[string][char]0x4E2D+[char]0x6587+' O''Brien $env:USERPROFILE;"'
  if((Invoke-GeodomDesk @('--args',$argument)) -cne $argument){throw 'Unicode argument round trip failed'}
  $large=Invoke-GeodomDesk @('--pipes')
  if($large.Length -ne 2097152){throw 'Concurrent output pipes were truncated'}
  Invoke-Expression ($processFunction.Extent.Text.Replace('@($output,$errors),3000)','@($output,$errors),200)'))
  $pidFile=Join-Path $testRoot 'child.pid'
  $watch=[Diagnostics.Stopwatch]::StartNew()
  $failed=$false
  try {$null=Invoke-GeodomDesk @('--inherit-pipes',$pidFile)} catch {$failed=$_.Exception.Message -match 'output timed out'}
  finally {
    if(Test-Path -LiteralPath $pidFile) {
      $fixtureProcess=Get-Process -Id ([int][IO.File]::ReadAllText($pidFile)) -ErrorAction SilentlyContinue
      if($fixtureProcess -and $fixtureProcess.Path -eq $Installed){Stop-Process -InputObject $fixtureProcess -Force;$null=$fixtureProcess.WaitForExit(3000)}
    }
  }
  if(-not $failed -or $watch.ElapsedMilliseconds -gt 2500){throw 'Inherited output handles bypassed the process timeout'}
  Invoke-Expression ($processFunction.Extent.Text.Replace('WaitForExit(15000)','WaitForExit(200)'))
  $failed=$false
  try {$null=Invoke-GeodomDesk @('--pause')} catch {$failed=$_.Exception.Message -match 'timed out'}
  if(-not $failed){throw 'A stuck child was not terminated'}
  Write-Output 'Windows native process: 9 real execution scenarios passed.'
} finally {
  $resolved=[IO.Path]::GetFullPath($testRoot)
  if($resolved -ne $testRoot -or -not $resolved.StartsWith($allowedRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Invalid test cleanup target'}
  Remove-Item -LiteralPath $resolved -Recurse -Force
}
