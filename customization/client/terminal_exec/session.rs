//! A replaceable transport with one in-flight command and responsive control requests.
use super::{cli, input, legacy, output, stage, validate, MAX_SCRIPT};
use base::message_proto::{message, ExecShell, TerminalExecRequest, TerminalExecResult};
use hbb_common::{anyhow::Context, bail, tokio, ResultType};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    future::pending,
    io::{BufRead, Read},
    time::{Duration, Instant},
};

const MAX_LINE: usize = MAX_SCRIPT * 6 + 1024;
const MAX_QUEUED: usize = 16;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Command {
    id: Option<Value>,
    op: Option<String>,
    connection: Option<String>,
    shell: Option<String>,
    script: Option<String>,
    script_file: Option<String>,
    timeout: Option<u32>,
}

impl Command {
    fn request(
        self,
        admin: bool,
        timeout: u32,
        shell: Option<ExecShell>,
    ) -> ResultType<TerminalExecRequest> {
        if !matches!(self.op.as_deref(), None | Some("exec")) {
            bail!("Unknown operation");
        }
        if !matches!(self.connection.as_deref(), None | Some("reuse" | "fresh")) {
            bail!("connection must be reuse or fresh");
        }
        let shell = match self.shell.as_deref() {
            Some("bash") => ExecShell::BASH,
            Some("powershell") => ExecShell::WINDOWS_POWERSHELL,
            Some("pwsh") => ExecShell::POWERSHELL_CORE,
            Some(shell) => bail!("Unknown shell: {shell}"),
            None => shell.context("Missing shell; specify it in the command or session --shell")?,
        };
        if admin && shell == ExecShell::BASH {
            bail!("--admin-user requires powershell or pwsh");
        }
        let script = match (self.script, self.script_file) {
            (Some(script), None) => script.into_bytes(),
            (None, Some(path)) => input::read_script(&path, false)?,
            _ => bail!("Specify exactly one of script or script_file"),
        };
        let request = TerminalExecRequest {
            shell: shell.into(),
            script: script.into(),
            timeout_seconds: self.timeout.unwrap_or(timeout),
            ..Default::default()
        };
        validate(&request)?;
        Ok(request)
    }
    fn control(&self) -> ResultType<()> {
        if self.connection.is_some()
            || self.shell.is_some()
            || self.script.is_some()
            || self.script_file.is_some()
            || self.timeout.is_some()
        {
            bail!("Control operations accept only op and id");
        }
        Ok(())
    }
}

enum Completed {
    Connected(cli::Connection, Duration),
    Executed(cli::Connection, TerminalExecResult, Duration, bool),
}

fn rejected(id: Option<Value>, code: &str, message: &str) -> Value {
    let mut value = json!({"event":"result","ok":false,"fatal":false,"code":code,
        "phase":"request","execution_state":"not_started","retry_safe":true,"error":message});
    if let Some(id) = id {
        value["id"] = id;
    }
    value
}

pub(super) async fn execute(
    connection: &mut cli::Connection,
    request: TerminalExecRequest,
    terminal_id: i32,
) -> ResultType<TerminalExecResult> {
    if connection.peer.features.terminal_exec {
        tokio::time::timeout(
            Duration::from_secs(u64::from(request.timeout_seconds) + 25),
            cli::exchange(&mut connection.stream, request),
        )
        .await
        .context("Timed out waiting for remote execution result")
        .and_then(|result| result)
        .map_err(|error| stage::wrap(error, "execution", "unknown"))
    } else if connection.peer.features.terminal {
        legacy::exchange(
            &mut connection.stream,
            &connection.peer.platform,
            request,
            terminal_id,
        )
        .await
    } else {
        Err(stage::wrap(
            hbb_common::anyhow::anyhow!("Remote device does not support terminal commands"),
            "capability",
            "not_started",
        ))
    }
}

pub(super) async fn run(
    connection: cli::Connection,
    options: cli::ConnectOptions,
    admin: bool,
    timeout: u32,
    shell: Option<ExecShell>,
    info: Value,
    continue_on_error: bool,
) -> ResultType<i32> {
    let (tx, mut rx) = tokio::sync::mpsc::channel(1);
    std::thread::Builder::new()
        .name("terminal-cli-input".into())
        .spawn(move || {
            let mut input = std::io::stdin().lock();
            loop {
                let mut line = Vec::new();
                let result = input
                    .by_ref()
                    .take(MAX_LINE as u64 + 1)
                    .read_until(b'\n', &mut line);
                if matches!(result, Ok(0)) {
                    break;
                }
                let result = result
                    .map_err(hbb_common::anyhow::Error::from)
                    .and_then(|_| {
                        if line.len() > MAX_LINE {
                            bail!("Session command exceeds {MAX_LINE} bytes");
                        }
                        Ok(line)
                    });
                let failed = result.is_err();
                if tx.blocking_send(result).is_err() || failed {
                    break;
                }
            }
        })?;
    output::write(info)?;
    let mut connection = Some(connection);
    let mut worker: Option<tokio::task::JoinHandle<ResultType<Completed>>> = None;
    let mut active_id: Option<Value> = None;
    let mut active_op = "";
    let mut active_connection: Option<String> = None;
    let mut terminal_id = 0i32;
    let mut queue: VecDeque<(Option<Value>, TerminalExecRequest, bool)> = VecDeque::new();
    let mut eof = false;
    let mut had_failure = false;
    let mut started = Instant::now();
    let result: ResultType<i32> = async {
        loop {
            if worker.is_none() {
                if let Some((id,request,fresh))=queue.pop_front() {
                if fresh {connection=None;}
                let saved=connection.take();
                active_connection=saved.as_ref().map(|c|c.id.clone());
                active_id=id;active_op="exec";started=Instant::now();
                terminal_id=terminal_id.checked_add(1).context("Session command limit reached")?;
                let terminal_id=terminal_id;
                let options=options.clone();
                worker=Some(tokio::spawn(async move {
                    let start=Instant::now();let reused=saved.is_some();
                    let mut connection=match saved {Some(connection)=>connection,None=>options.open().await?};
                    let result=execute(&mut connection,request,terminal_id).await.map_err(|error|error.context(stage::ConnectionId(connection.id.clone())))?;
                    Ok(Completed::Executed(connection,result,start.elapsed(),reused))
                }));
                }
            }
            if eof && worker.is_none() { return Ok(if had_failure{1}else{0}); }
            tokio::select! {
                line=async { if eof { pending().await } else { rx.recv().await } } => {
                    let Some(line)=line else { eof=true;continue; };
                    let line=line?;
                    if line.iter().all(u8::is_ascii_whitespace) { continue; }
                    let mut command: Command=match serde_json::from_slice(&line) {
                        Ok(command)=>command,
                        Err(error)=> {had_failure=true;output::write(rejected(None,"invalid_request",&format!("Invalid session command: {error}")))?;continue;}
                    };
                    let id=command.id.take();
                    let op=command.op.as_deref().unwrap_or("exec").to_owned();
                    if id.as_ref().is_some_and(|id| (worker.is_some() && active_id.as_ref()==Some(id)) || queue.iter().any(|(queued,_,_)|queued.as_ref()==Some(id))) {
                        had_failure=true;
                        let mut value=rejected(None,"duplicate_id","id labels this request; choose a unique id, or omit id on control operations");
                        value["event"]=json!("rejected");value["rejected_id"]=json!(id);
                        output::write(value)?;continue;
                    }
                    if op!="exec" {
                        if let Err(error)=command.control() {had_failure=true;output::write(rejected(id,"invalid_request",&error.to_string()))?;continue;}
                        if op=="status" {
                            let mut value=json!({"event":"status","state":if worker.is_some(){"busy"}else if connection.is_some(){"ready"}else{"disconnected"},
                                "connection_id":connection.as_ref().map(|c|c.id.clone()).or_else(||active_connection.clone()),
                                "had_failure":had_failure,"queued_requests":queue.len(),"max_queued_requests":MAX_QUEUED,"active_request_id":active_id,"active_operation":active_op,"session_protocol":2});
                            if let Some(id)=id {value["id"]=id;}
                            output::write(value)?;continue;
                        }
                        if !matches!(op.as_str(),"reconnect"|"cancel"|"close") {
                            had_failure=true;output::write(rejected(id,"invalid_request","Unknown operation; use exec, status, reconnect, cancel or close"))?;continue;
                        }
                        if let Some(task)=worker.take() {
                            had_failure=true;
                            task.abort();
                            let _=tokio::time::timeout(Duration::from_secs(1),task).await;
                            let mut interrupted=rejected(active_id.take(),"cancelled","Request cancelled; it was not replayed. Verify remote state before retrying.");
                            interrupted["phase"]=json!(if active_op=="exec"{"execution"}else{"connect"});
                            if active_op=="exec" {
                                interrupted["execution_state"]=json!("unknown");interrupted["retry_safe"]=json!(false);
                            }
                            interrupted["connection_id"]=json!(active_connection.take());
                            interrupted["connection_lost"]=json!(true);
                            output::write(interrupted)?;
                        }
                        for (queued_id,_,_) in queue.drain(..) {
                            let mut cancelled=rejected(queued_id,"cancelled","Queued request cancelled before execution");
                            cancelled["phase"]=json!("queue");output::write(cancelled)?;
                        }
                        connection=None;active_connection=None;active_op="";
                        if op=="reconnect" {
                            let options=options.clone();active_id=id;active_op="reconnect";started=Instant::now();
                            worker=Some(tokio::spawn(async move {
                                let start=Instant::now();
                                Ok(Completed::Connected(options.open().await?,start.elapsed()))
                            }));
                        } else {
                            let mut value=json!({"event":if op=="cancel"{"cancelled"}else{"closed"},"ok":true,"state":"disconnected"});
                            if let Some(id)=id {value["id"]=id;}
                            output::write(value)?;
                        }
                        continue;
                    }
                    let fresh=command.connection.as_deref()==Some("fresh");
                    let request=match command.request(admin,timeout,shell) {
                        Ok(request)=>request,
                        Err(error)=> {had_failure=true;output::write(rejected(id,"invalid_request",&format!("{error:#}")))?;continue;}
                    };
                    if queue.len()>=MAX_QUEUED {
                        had_failure=true;output::write(rejected(id,"busy","The bounded request queue is full; wait for a result before submitting more commands"))?;continue;
                    }
                    queue.push_back((id,request,fresh));
                },
                completed=async { match worker.as_mut(){Some(worker)=>worker.await,None=>pending().await} } => {
                    worker=None;active_op="";active_connection=None;
                    let outcome=completed.context("Session worker failed").map_err(|error|stage::wrap(error,"worker","unknown")).and_then(|result|result);
                    match outcome {
                        Ok(Completed::Connected(next,elapsed))=> {
                            let mut value=cli::connection_info(&next,timeout,elapsed,elapsed);
                            if let Some(id)=active_id.take(){value["id"]=id;}
                            output::write(value)?;connection=Some(next);
                        },
                        Ok(Completed::Executed(next,result,elapsed,reused))=> {
                            let failed=!result.error.is_empty() || result.exit_code!=0;
                            had_failure|=failed;
                            let mut value=output::result(active_id.take(),&result,elapsed);
                            value["connection_id"]=json!(next.id);value["reused_connection"]=json!(reused);
                            output::write(value)?;connection=Some(next);
                            if failed && !continue_on_error {
                                for (id,_,_) in queue.drain(..) {
                                    let mut cancelled=rejected(id,"cancelled","Queued request was not started because the preceding command failed; use --continue-on-error only for independent commands");
                                    cancelled["phase"]=json!("queue");output::write(cancelled)?;
                                }
                            }
                        },
                        Err(error)=> {
                            had_failure=true;
                            let mut value=output::failure(active_id.take(),&error,false,started.elapsed());
                            value["connection_lost"]=json!(true);
                            output::write(value)?;connection=None;
                            for (id,_,_) in queue.drain(..) {
                                let mut cancelled=rejected(id,"cancelled","Queued request was not started because the preceding connection failed");
                                cancelled["phase"]=json!("queue");output::write(cancelled)?;
                            }
                        }
                    }
                },
                message=async { match connection.as_mut(){Some(connection)=>cli::receive(&mut connection.stream).await,None=>pending().await} } => {
                    match message {
                        Ok(message)=>if let Some(message::Union::TestDelay(delay))=message.union {
                            if let Some(connection)=connection.as_mut(){crate::client::handle_test_delay(delay,&mut connection.stream).await;}
                        },
                        Err(error)=> {
                            had_failure=true;
                            output::write(json!({"event":"connection_lost","error":format!("{error:#}"),"fatal":false,
                                "connection_id":connection.as_ref().map(|c|c.id.clone()),"state":"disconnected"}))?;
                            connection=None;
                            for (id,_,_) in queue.drain(..) {
                                let mut cancelled=rejected(id,"cancelled","Queued request was not started because the connection was lost");
                                cancelled["phase"]=json!("queue");output::write(cancelled)?;
                            }
                        }
                    }
                }
            }
        }
    }.await;
    let interrupted_execution = worker.is_some() && active_op == "exec";
    if let Some(worker) = worker {
        worker.abort();
        let _ = tokio::time::timeout(Duration::from_secs(1), worker).await;
    }
    match result {
        Ok(code) => Ok(code),
        Err(error) => {
            for (id, _, _) in queue.drain(..) {
                let mut value = rejected(
                    id,
                    "cancelled",
                    "Queued request cancelled because the session input failed",
                );
                value["phase"] = json!("queue");
                output::write(value)?;
            }
            let error = stage::wrap(
                error,
                "input",
                if interrupted_execution {
                    "unknown"
                } else {
                    "not_started"
                },
            );
            output::write(output::failure(active_id, &error, true, started.elapsed()))?;
            Ok(1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn session_commands_preserve_limits_and_admin_scope() {
        let parse = |line: &[u8], admin, timeout| -> ResultType<TerminalExecRequest> {
            serde_json::from_slice::<Command>(line)?.request(admin, timeout, None)
        };
        let line = br#"{"shell":"bash","script":"printf ok"}"#;
        assert_eq!(parse(line, false, 120).unwrap().timeout_seconds, 120);
        assert!(parse(line, true, 30).is_err());
        for line in [
            br#"{"shell":"bash","script":"exit","timeout":301}"#.as_slice(),
            br#"{"shell":"bash","script":"exit","timeout":0}"#,
            br#"{"shell":"bash","script":"exit","typo":1}"#,
            br#"{"shell":"bash","script":""}"#,
        ] {
            assert!(parse(line, false, 30).is_err());
        }
        let line =
            serde_json::to_vec(&json!({"shell": "pwsh", "script": "x".repeat(MAX_SCRIPT + 1)}))
                .unwrap();
        assert!(parse(&line, true, 30).is_err());
        assert!(parse(
            r#"{"shell":"powershell","script":"Write-Output '你好'","timeout":5}"#.as_bytes(),
            true,
            30
        )
        .is_ok());
    }

    #[test]
    fn local_script_and_default_shell_are_validated_without_consuming_stdin() {
        let file = std::env::temp_dir().join(format!("geodomdesk-cli-{}.sh", uuid::Uuid::new_v4()));
        std::fs::write(&file, "printf '你好'").unwrap();
        let command: Command =
            serde_json::from_value(json!({"id": "check", "script_file": &file})).unwrap();
        assert_eq!(command.id.as_ref().unwrap(), "check");
        let request = command.request(false, 15, Some(ExecShell::BASH)).unwrap();
        assert_eq!(request.script.as_ref(), "printf '你好'".as_bytes());
        for value in [
            json!({"script_file": "-"}),
            json!({"script": "exit", "script_file": &file}),
        ] {
            let command: Command = serde_json::from_value(value).unwrap();
            assert!(command.request(false, 15, Some(ExecShell::BASH)).is_err());
        }
        std::fs::remove_file(&file).unwrap();
        assert!(!input::wants_json(
            &["exec", "--command", "--json"].map(String::from)
        ));
        assert!(input::wants_json(
            &["exec", "--command", "pwd", "--json"].map(String::from)
        ));
        let mut command: Command =
            serde_json::from_value(json!({"id": 7, "script": "exit"})).unwrap();
        let id = command.id.take();
        let result = output::result(id, &Default::default(), Duration::from_millis(23));
        assert_eq!(result["id"], 7);
        assert_eq!(result["elapsed_ms"], 23);
    }
    #[test]
    fn controls_cannot_silently_execute_or_ignore_scripts() {
        let command: Command =
            serde_json::from_value(json!({"op":"reconnect","id":"reset"})).unwrap();
        assert!(command.control().is_ok());
        for value in [
            json!({"op":"cancel","script":"exit"}),
            json!({"op":"status","timeout":1}),
        ] {
            let command: Command = serde_json::from_value(value).unwrap();
            assert!(command.control().is_err());
        }
        let command: Command =
            serde_json::from_value(json!({"script":"pwd","connection":"invalid"})).unwrap();
        assert!(command.request(false, 15, Some(ExecShell::BASH)).is_err());
    }
}
