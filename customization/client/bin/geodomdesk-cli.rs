#[cfg(any(windows, target_os = "linux"))]
use std::{
    env, io,
    process::{Command, Stdio},
};

#[cfg(any(windows, target_os = "linux"))]
fn run() -> io::Result<i32> {
    let mut executable = env::current_exe()?;
    executable.set_file_name(if cfg!(windows) {
        "GeodomDesk.exe"
    } else {
        "geodomdesk"
    });
    let mut command = Command::new(executable);
    command
        .arg("--terminal-cli")
        .args(env::args_os().skip(1))
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        Err(command.exec())
    }
    #[cfg(windows)]
    {
        Ok(command.status()?.code().unwrap_or(1))
    }
}

#[cfg(any(windows, target_os = "linux"))]
fn main() {
    std::process::exit(match run() {
        Ok(code) => code,
        Err(error) => {
            eprintln!("geodomdesk-cli: cannot run GeodomDesk: {error}");
            1
        }
    });
}

#[cfg(not(any(windows, target_os = "linux")))]
fn main() {
    eprintln!("GeodomDesk CLI requires Windows or Linux");
    std::process::exit(1);
}
