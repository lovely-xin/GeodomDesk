use super::{validate, MAX_OUTPUT, MAX_SCRIPT};
use crate::server::terminal_helper::{
    create_named_pipe_server, launch_exec_helper_with_token, open_pipe, HelperProcessGuard,
    OwnedHandle, UserToken, PIPE_CONNECTION_TIMEOUT_MS,
};
use base::message_proto::{ExecShell, TerminalExecRequest, TerminalExecResult};
use hbb_common::{
    anyhow::Context,
    bail,
    protobuf::Message as _,
    tokio::{
        self,
        io::{AsyncReadExt, AsyncWriteExt},
        net::windows::named_pipe::NamedPipeServer,
    },
    ResultType,
};
use std::{
    io::{Read, Write},
    os::windows::{io::AsRawHandle, process::CommandExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};
use windows::{
    core::PCWSTR,
    Win32::{
        Foundation::{HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT},
        Security::TOKEN_QUERY,
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            SystemInformation::GetSystemDirectoryW,
            Threading::{
                GetCurrentProcess, OpenProcessToken, WaitForSingleObject, CREATE_NO_WINDOW,
            },
        },
    },
};

fn packet(mut input: impl Read, max: usize) -> ResultType<Vec<u8>> {
    let mut size = [0; 4];
    input.read_exact(&mut size)?;
    let size = u32::from_le_bytes(size) as usize;
    if size > max {
        bail!("Terminal execution packet too large");
    }
    let mut bytes = vec![0; size];
    input.read_exact(&mut bytes)?;
    Ok(bytes)
}

fn write_packet(mut output: impl Write, bytes: &[u8]) -> ResultType<()> {
    output.write_all(&(bytes.len() as u32).to_le_bytes())?;
    output.write_all(bytes)?;
    output.flush()?;
    Ok(())
}

fn kill_on_close_job() -> ResultType<OwnedHandle> {
    let job = OwnedHandle::new(unsafe { CreateJobObjectW(None, PCWSTR::null())? });
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    unsafe {
        SetInformationJobObject(
            job.as_raw(),
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            std::mem::size_of_val(&limits) as u32,
        )?;
    }
    Ok(job)
}

pub(super) fn execute(
    request: TerminalExecRequest,
    user: Option<UserToken>,
    cancelled: &AtomicBool,
) -> ResultType<TerminalExecResult> {
    if let Some(user) = user {
        execute_with_helper(request, Some(user), cancelled, &std::env::current_exe()?)
    } else {
        execute_direct(request, cancelled)
    }
}

fn execute_direct(
    request: TerminalExecRequest,
    cancelled: &AtomicBool,
) -> ResultType<TerminalExecResult> {
    if request.shell.enum_value() == Ok(ExecShell::BASH) {
        bail!("This Windows device supports PowerShell execution");
    }
    let job = kill_on_close_job()?;
    let timeout = Duration::from_secs(request.timeout_seconds.into());
    run_script_bounded(request, Some((&job, timeout, cancelled)))
}

fn execute_with_helper(
    request: TerminalExecRequest,
    user: Option<UserToken>,
    cancelled: &AtomicBool,
    executable: &Path,
) -> ResultType<TerminalExecResult> {
    if request.shell.enum_value() == Ok(ExecShell::BASH) {
        bail!("This Windows device supports PowerShell execution");
    }
    let local_token;
    let token = if let Some(token) = user {
        token
    } else {
        let mut raw = HANDLE::default();
        unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw)? };
        local_token = OwnedHandle::new(raw);
        UserToken::new(local_token.as_raw().0 as usize)
    };
    let pipe_id = uuid::Uuid::new_v4();
    let input_name = format!(r"\\.\pipe\geodomdesk_exec_in_{pipe_id}");
    let output_name = format!(r"\\.\pipe\geodomdesk_exec_out_{pipe_id}");
    let input = OwnedHandle::new(create_named_pipe_server(&input_name, false, token)?);
    let output = OwnedHandle::new(create_named_pipe_server(&output_name, true, token)?);
    // These handles are opened with FILE_FLAG_OVERLAPPED.
    let mut input = unsafe { NamedPipeServer::from_raw_handle(input.into_raw().0)? };
    let output = unsafe { NamedPipeServer::from_raw_handle(output.into_raw().0)? };
    let job = kill_on_close_job()?;
    let process;
    let local_child;
    let handle = if user.is_some() {
        let helper = launch_exec_helper_with_token(token, &input_name, &output_name)?;
        let guard = HelperProcessGuard::new(helper.handle, helper.pid);
        unsafe { AssignProcessToJobObject(job.as_raw(), guard.as_raw())? };
        process = OwnedHandle::new(guard.disarm());
        process.as_raw()
    } else {
        let mut child = Command::new(executable)
            .args(["--terminal-exec-helper", &input_name, &output_name])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW.0)
            .spawn()?;
        let handle = HANDLE(child.as_raw_handle());
        if let Err(error) = unsafe { AssignProcessToJobObject(job.as_raw(), handle) } {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error.into());
        }
        local_child = child;
        HANDLE(local_child.as_raw_handle())
    };
    // The helper cannot start the script until it receives the request.
    let runtime = tokio::runtime::Handle::current();
    let request_bytes = request.write_to_bytes()?;
    let mut output = runtime.block_on(async move {
        if cancelled.load(Ordering::Relaxed) {
            bail!("Remote command cancelled");
        }
        tokio::time::timeout(
            Duration::from_millis(PIPE_CONNECTION_TIMEOUT_MS.into()),
            async {
                tokio::try_join!(input.connect(), output.connect())?;
                input.write_u32_le(request_bytes.len() as u32).await?;
                input.write_all(&request_bytes).await
            },
        )
        .await
        .context("PowerShell helper did not accept the command")??;
        Ok::<_, hbb_common::anyhow::Error>(output)
    })?;
    let reader = runtime.spawn(async move {
        let size = output
            .read_u32_le()
            .await
            .context("PowerShell helper returned no result")? as usize;
        if size > 2 * MAX_OUTPUT + 4096 {
            bail!("Terminal execution packet too large");
        }
        let mut response = vec![0; size];
        output.read_exact(&mut response).await?;
        Ok(TerminalExecResult::parse_from_bytes(&response)?)
    });
    let deadline = Instant::now() + Duration::from_secs(request.timeout_seconds.into());
    let wait = loop {
        if cancelled.load(Ordering::Relaxed) {
            break Err(hbb_common::anyhow::anyhow!("Remote command cancelled"));
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break Err(hbb_common::anyhow::anyhow!("Remote command timed out"));
        }
        let status =
            unsafe { WaitForSingleObject(handle, remaining.as_millis().clamp(1, 100) as u32) };
        if status == WAIT_OBJECT_0 {
            break Ok(());
        }
        if status != WAIT_TIMEOUT {
            break Err(hbb_common::anyhow::anyhow!(
                "Failed to wait for PowerShell execution"
            ));
        }
    };
    drop(job);
    if let Err(error) = wait {
        reader.abort();
        return Err(error);
    }
    runtime.block_on(reader)?
}

fn shell_path(shell: ExecShell) -> ResultType<PathBuf> {
    match shell {
        ExecShell::WINDOWS_POWERSHELL => {
            let mut buffer = [0u16; 512];
            let len = unsafe { GetSystemDirectoryW(Some(&mut buffer)) } as usize;
            if len == 0 || len >= buffer.len() {
                bail!("Cannot locate Windows system directory");
            }
            use std::os::windows::ffi::OsStringExt;
            let path = PathBuf::from(std::ffi::OsString::from_wide(&buffer[..len]))
                .join(r"WindowsPowerShell\v1.0\powershell.exe");
            if path.is_file() {
                return Ok(path);
            }
            bail!("Windows PowerShell is unavailable");
        }
        ExecShell::POWERSHELL_CORE => {
            for root in ["ProgramW6432", "ProgramFiles"] {
                if let Some(root) = std::env::var_os(root)
                    .map(PathBuf::from)
                    .filter(|path| path.is_absolute())
                {
                    let path = root.join(r"PowerShell\7\pwsh.exe");
                    if path.is_file() {
                        return Ok(path);
                    }
                }
            }
            bail!("PowerShell 7 is unavailable");
        }
        _ => bail!("Unsupported Windows shell"),
    }
}

fn read_output(input: impl Read, limit: usize, overflow: Arc<AtomicBool>) -> ResultType<Vec<u8>> {
    let mut bytes = Vec::new();
    input.take(limit as u64 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        overflow.store(true, Ordering::Release);
        bail!("Output exceeded {MAX_OUTPUT} bytes");
    }
    Ok(bytes)
}

fn run_script(request: TerminalExecRequest) -> ResultType<TerminalExecResult> {
    run_script_bounded(request, None)
}

fn run_script_bounded(
    request: TerminalExecRequest,
    guard: Option<(&OwnedHandle, Duration, &AtomicBool)>,
) -> ResultType<TerminalExecResult> {
    validate(&request)?;
    let shell = shell_path(
        request
            .shell
            .enum_value()
            .map_err(|_| hbb_common::anyhow::anyhow!("Unknown shell"))?,
    )?;
    const BOOTSTRAP: &str = include_str!("exec_bootstrap.ps1");
    const READY: &[u8] = b"GEODOMDESK_EXEC_READY";
    // Separate the script from ConsoleHost's stdin and the Windows command-line limit.
    const CHUNK_SIZE: usize = 24_000;
    let script = request
        .script
        .strip_prefix(b"\xef\xbb\xbf")
        .unwrap_or(&request.script);
    if script.is_empty() {
        bail!("PowerShell script is empty");
    }
    let encoded = crate::encode64(script);
    let chunks = encoded.as_bytes().chunks(CHUNK_SIZE);
    let chunk_count = chunks.len();
    let script_bytes = script.len();
    let startup = format!(
        "$geodomdeskScriptChunks = {chunk_count}\n$geodomdeskExpectedBytes = {script_bytes}\n{BOOTSTRAP}"
    );
    let bootstrap = crate::encode64(
        startup
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    let mut command = Command::new(shell);
    command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            &bootstrap,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW.0);
    for (index, chunk) in chunks.enumerate() {
        command.env(
            format!("GEODOMDESK_EXEC_SCRIPT_{index}"),
            std::str::from_utf8(chunk)?,
        );
    }
    let mut process = command.spawn()?;
    if let Some((job, _, _)) = guard {
        if let Err(error) =
            unsafe { AssignProcessToJobObject(job.as_raw(), HANDLE(process.as_raw_handle())) }
        {
            let _ = process.kill();
            let _ = process.wait();
            return Err(error.into());
        }
    }
    let stdout = process.stdout.take().context("Missing PowerShell output")?;
    let stderr = process
        .stderr
        .take()
        .context("Missing PowerShell error output")?;
    let overflow = Arc::new(AtomicBool::new(false));
    let out_flag = overflow.clone();
    let err_flag = overflow.clone();
    let stdout = thread::spawn(move || read_output(stdout, MAX_OUTPUT + READY.len(), out_flag));
    let stderr = thread::spawn(move || read_output(stderr, MAX_OUTPUT, err_flag));
    let started = Instant::now();
    let status = loop {
        if overflow.load(Ordering::Acquire) {
            let _ = process.kill();
            let _ = process.wait();
            bail!("Output exceeded {MAX_OUTPUT} bytes");
        }
        if let Some((_, timeout, cancelled)) = guard {
            if cancelled.load(Ordering::Relaxed) {
                let _ = process.kill();
                let _ = process.wait();
                bail!("Remote command cancelled");
            }
            if started.elapsed() >= timeout {
                let _ = process.kill();
                let _ = process.wait();
                bail!("Remote command timed out");
            }
        }
        if let Some(status) = process.try_wait()? {
            break status;
        }
        thread::sleep(Duration::from_millis(20));
    };
    let join = |thread: thread::JoinHandle<ResultType<Vec<u8>>>| {
        thread
            .join()
            .map_err(|_| hbb_common::anyhow::anyhow!("Output reader stopped"))?
    };
    let mut stdout = join(stdout)?;
    let stderr = join(stderr)?;
    let exit_code = status.code().context("Missing PowerShell exit code")?;
    if !stdout.starts_with(READY) {
        let diagnostic = String::from_utf8_lossy(&stderr);
        let diagnostic = diagnostic.trim().chars().take(512).collect::<String>();
        bail!("PowerShell did not start the script (exit {exit_code}) {diagnostic}");
    }
    stdout.drain(..READY.len());
    Ok(TerminalExecResult {
        stdout: stdout.into(),
        stderr: stderr.into(),
        exit_code,
        ..Default::default()
    })
}

pub(crate) fn run_helper(args: &[String]) -> ResultType<()> {
    let [input, output] = args else {
        bail!("Invalid exec helper arguments");
    };
    let input = open_pipe(input, true)?;
    let output = open_pipe(output, false)?;
    let request = TerminalExecRequest::parse_from_bytes(&packet(input, MAX_SCRIPT + 128)?)?;
    let result = run_script(request).unwrap_or_else(|error| TerminalExecResult {
        error: error.to_string(),
        ..Default::default()
    });
    write_packet(output, &result.write_to_bytes()?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    #[ignore = "requires the built Windows desktop runner and both PowerShell versions"]
    async fn native_powershell_helper_round_trip() {
        let executable = std::env::var_os("GEODOMDESK_TEST_CLIENT")
            .map(PathBuf::from)
            .expect("Set GEODOMDESK_TEST_CLIENT to the Windows runner");
        tokio::task::spawn_blocking(move || {
            let cancelled = AtomicBool::new(false);
            let execute = |shell: ExecShell, script: &str, timeout_seconds| {
                let request = TerminalExecRequest {
                    shell: shell.into(),
                    script: script.as_bytes().to_vec().into(),
                    timeout_seconds,
                    ..Default::default()
                };
                execute_with_helper(request, None, &cancelled, &executable)
            };
            for shell in [ExecShell::WINDOWS_POWERSHELL, ExecShell::POWERSHELL_CORE] {
                let result = execute(
                    shell,
                    "[Console]::Write('输出'); [Console]::Error.Write('错误'); exit 7",
                    10,
                )
                .unwrap();
                assert!(result.error.is_empty(), "{}", result.error);
                assert_eq!(&result.stdout[..], "输出".as_bytes());
                assert_eq!(&result.stderr[..], "错误".as_bytes());
                assert_eq!(result.exit_code, 7);
                let script = format!(
                    "#{}\nexit 19",
                    "中文".repeat((MAX_SCRIPT - 10) / "中文".len())
                );
                let result = execute(shell, &script, 10).unwrap();
                assert!(result.error.is_empty(), "{}", result.error);
                assert!(result.stdout.is_empty());
                assert!(result.stderr.is_empty());
                assert_eq!(result.exit_code, 19);
                let result = execute(shell, "Write-Output '", 10).unwrap();
                assert!(result.error.contains("PowerShell did not start the script"));
            }
            let error =
                execute(ExecShell::WINDOWS_POWERSHELL, "Start-Sleep -Seconds 30", 2).unwrap_err();
            assert!(error.to_string().contains("timed out"), "{error}");
        })
        .await
        .unwrap();
    }
}
