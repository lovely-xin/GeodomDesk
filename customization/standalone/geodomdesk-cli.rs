fn main() -> std::process::ExitCode {
    match librustdesk::terminal_exec::run_standalone_cli() {
        Ok(code) if (0..=255).contains(&code) => std::process::ExitCode::from(code as u8),
        Ok(_) => std::process::ExitCode::FAILURE,
        Err(error) => {
            eprintln!("geodomdesk-cli: {error:#}");
            std::process::ExitCode::FAILURE
        }
    }
}
