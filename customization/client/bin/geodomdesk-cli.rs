#[cfg(any(windows, target_os = "linux"))]
fn main() {
    use hbb_common::tokio::runtime::Builder;
    if !librustdesk::common::global_init() {
        eprintln!("GeodomDesk initialization failed");
        std::process::exit(1);
    }
    let result = Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(hbb_common::anyhow::Error::from)
        .and_then(|runtime| runtime.block_on(librustdesk::terminal_exec::run_cli()));
    librustdesk::common::global_clean();
    std::process::exit(match result {
        Ok(code) => code,
        Err(error) => {
            eprintln!("geodomdesk-cli: {error:#}");
            1
        }
    });
}

#[cfg(not(any(windows, target_os = "linux")))]
fn main() {
    eprintln!("GeodomDesk CLI requires Windows or Linux");
    std::process::exit(1);
}
