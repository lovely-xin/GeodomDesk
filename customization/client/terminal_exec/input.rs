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
        let metadata =
            std::fs::metadata(path).with_context(|| format!("Cannot inspect script: {path}"))?;
        if !metadata.is_file() {
            bail!("Script path must be a regular file; pipe to exec --script -, or send script text in session");
        }
        let mut options = std::fs::OpenOptions::new();
        options.read(true);
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::fs::OpenOptionsExt;
            // A path can change after metadata inspection; opening a FIFO must not block.
            options.custom_flags(hbb_common::libc::O_NONBLOCK);
        }
        let file = options
            .open(path)
            .with_context(|| format!("Cannot open script: {path}"))?;
        if !file.metadata()?.is_file() {
            bail!("Script path must be a regular file");
        }
        Box::new(file)
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
            "--relay"
            | "--ask-device-password"
            | "--non-interactive"
            | "--help"
            | "-h"
            | "--continue-on-error" => {}
            _ => {
                args.next();
            }
        }
    }
    false
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    #[test]
    fn regression_nonregular_script_is_rejected_without_waiting_for_a_writer() {
        use std::{io::Write, sync::mpsc, time::Duration};
        let path = std::env::temp_dir().join(format!("geodomdesk-fifo-{}", uuid::Uuid::new_v4()));
        assert!(std::process::Command::new("mkfifo")
            .arg(&path)
            .status()
            .unwrap()
            .success());
        let (tx, rx) = mpsc::channel();
        let source = path.clone();
        let reader = std::thread::spawn(move || {
            tx.send(read_script(&source.to_string_lossy(), false))
                .unwrap()
        });
        let mut result = rx.recv_timeout(Duration::from_secs(2));
        if matches!(result, Err(mpsc::RecvTimeoutError::Timeout)) {
            // Release an old blocking implementation so a regression fails without hanging CI.
            let mut writer = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
            writer.write_all(b"printf SHOULD_NOT_BE_ACCEPTED").unwrap();
            drop(writer);
            result = rx.recv_timeout(Duration::from_secs(2));
        }
        reader.join().unwrap();
        std::fs::remove_file(path).unwrap();
        assert!(
            result.unwrap().is_err(),
            "A FIFO must be refused, not read as a script"
        );
    }
}
