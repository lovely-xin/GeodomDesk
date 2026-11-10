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
        "elapsed_ms": elapsed.as_millis()});
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
    let mut value = json!({"event": "result", "error": format!("{error:#}"),
        "fatal": fatal, "ok": false, "elapsed_ms": elapsed.as_millis()});
    if let Some(id) = id {
        value["id"] = id;
    }
    value
}
