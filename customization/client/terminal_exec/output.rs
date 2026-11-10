use base::message_proto::TerminalExecResult;
use hbb_common::{anyhow, ResultType};
use serde_json::{json, Value};
use std::{io::Write, time::Duration};

pub(super) fn write(value: Value) -> ResultType<()> {
    let mut data = serde_json::to_vec(&value)?;
    data.push(b'\n');
    let mut output = std::io::stdout().lock();
    output.write_all(&data)?;
    output.flush()?;
    Ok(())
}

pub(super) fn result(id: Option<Value>, result: &TerminalExecResult, elapsed: Duration) -> Value {
    let mut value = json!({"event": "result", "encoding": "base64",
        "stdout": crate::encode64(&result.stdout), "stderr": crate::encode64(&result.stderr),
        "exit_code": result.exit_code, "error": result.error, "fatal": false,
        "ok": result.error.is_empty() && result.exit_code == 0,
        "elapsed_ms": elapsed.as_millis(), "execution_state":"finished", "retry_safe":false, "code":if !result.error.is_empty(){"remote_error"}else if result.exit_code!=0{"remote_exit"}else{"ok"}});
    if let Some(id) = id {
        value["id"] = id;
    }
    value
}

pub(super) fn failure(
    id: Option<Value>,
    error: &anyhow::Error,
    fatal: bool,
    elapsed: Duration,
) -> Value {
    let (code, phase, state) = super::stage::describe(error);
    let mut value = json!({"event": "result", "error": format!("{error:#}"),
        "fatal": fatal, "ok": false, "elapsed_ms": elapsed.as_millis(),
        "code":code,"phase":phase,"execution_state":state,"retry_safe":state=="not_started"});
    value["connection_id"] = json!(error
        .downcast_ref::<super::stage::ConnectionId>()
        .map(|id| id.0.clone()));
    if let Some(id) = id {
        value["id"] = id;
    }
    value
}
