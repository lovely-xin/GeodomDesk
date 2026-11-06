param([Parameter(Mandatory=$true)][string]$Client)
$ErrorActionPreference='Stop'

function Test-PowerShellExec([byte]$Shell) {
  $id=[Guid]::NewGuid().ToString('N')
  $inputName="geodomdesk_test_in_$id"
  $outputName="geodomdesk_test_out_$id"
  $inputPipe=[IO.Pipes.NamedPipeServerStream]::new($inputName,[IO.Pipes.PipeDirection]::Out,1,[IO.Pipes.PipeTransmissionMode]::Byte,[IO.Pipes.PipeOptions]::Asynchronous)
  $outputPipe=[IO.Pipes.NamedPipeServerStream]::new($outputName,[IO.Pipes.PipeDirection]::In,1,[IO.Pipes.PipeTransmissionMode]::Byte,[IO.Pipes.PipeOptions]::Asynchronous)
  $helper=$null
  try {
    $waits=[Threading.Tasks.Task[]]@($inputPipe.WaitForConnectionAsync(),$outputPipe.WaitForConnectionAsync())
    $helper=Start-Process -FilePath $Client -ArgumentList @('--terminal-exec-helper',"\\.\pipe\$inputName","\\.\pipe\$outputName") -WindowStyle Hidden -PassThru
    if(-not [Threading.Tasks.Task]::WaitAll($waits,15000)){throw 'Native terminal helper did not connect'}
    $scriptBytes=[Text.Encoding]::UTF8.GetBytes("[Console]::Write('输出'); [Console]::Error.Write('错误'); exit 7")
    # TerminalExecRequest fields: shell (1), script (2), timeout_seconds (3).
    [byte[]]$request=@(8,$Shell,18,$scriptBytes.Length)+$scriptBytes+@(24,10)
    $writer=[IO.BinaryWriter]::new($inputPipe)
    $writer.Write([int]$request.Length)
    $writer.Write($request)
    $writer.Flush()
    if(-not $helper.WaitForExit(15000) -or $helper.ExitCode -ne 0){throw 'Native terminal helper did not exit successfully'}
    $reader=[IO.BinaryReader]::new($outputPipe)
    $size=$reader.ReadInt32()
    $stdout=[Text.Encoding]::UTF8.GetBytes('输出')
    $stderr=[Text.Encoding]::UTF8.GetBytes('错误')
    # TerminalExecResult fields: stdout (1), stderr (2), exit_code (3).
    [byte[]]$expected=@(10,$stdout.Length)+$stdout+@(18,$stderr.Length)+$stderr+@(24,7)
    if($size -ne $expected.Length -or [Convert]::ToHexString($reader.ReadBytes($size)) -cne [Convert]::ToHexString($expected)){
      throw "Native terminal helper returned incorrect PowerShell output or exit status (shell $Shell)"
    }
  } finally {
    if($helper -and -not $helper.HasExited){$helper.Kill($true)}
    if($helper){$helper.Dispose()}
    $inputPipe.Dispose()
    $outputPipe.Dispose()
  }
}

Test-PowerShellExec 1
Test-PowerShellExec 2
Write-Output 'Installed terminal helper ran Windows PowerShell and PowerShell 7 with UTF-8 output, stderr and exit code.'
