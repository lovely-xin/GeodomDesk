use super::MAX_SCRIPT;
use hbb_common::{anyhow::Context, bail, ResultType};
use std::io::Read;

pub(super) fn read_script(path: &str, stdin: bool) -> ResultType<Vec<u8>> {
    let input: Box<dyn Read> = if path == "-" {
        if !stdin {
            bail!("Session script_file cannot read stdin; send script text or a local file path");
        }
        Box::new(std::io::stdin())
    } else {
        Box::new(std::fs::File::open(path).with_context(|| format!("Cannot open script: {path}"))?)
    };
    let mut script = Vec::new();
    input.take(MAX_SCRIPT as u64 + 1).read_to_end(&mut script)?;
    if script.len() > MAX_SCRIPT {
        bail!("Script exceeds {MAX_SCRIPT} bytes");
    }
    Ok(script)
}

pub(super) fn wants_json(args: &[String]) -> bool {
    let mut args = args.iter().skip(1);
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--json" => return true,
            "--relay" | "--ask-device-password" | "--non-interactive" | "--help" | "-h" => {}
            _ => {
                args.next();
            }
        }
    }
    false
}
