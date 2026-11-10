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
    if env::var("GEODOMDESK_CLI_SSH_ASKPASS").as_deref()==Ok("1") {
        use std::io::Write;
        let prompt=env::args().nth(1).unwrap_or_default().to_ascii_lowercase();
        if !prompt.contains("password:") {std::process::exit(1);}
        let password=env::var("GEODOMDESK_SSH_PASSWORD").unwrap_or_default();
        if password.contains(['\r','\n']) {std::process::exit(1);}
        std::process::exit(if writeln!(io::stdout().lock(),"{password}").is_ok(){0}else{1});
    }
    #[cfg(windows)]
    if let Err(error)=own_process_tree() {eprintln!("geodomdesk-cli: cannot manage child process lifetime: {error}");std::process::exit(1);}
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

// The OS closes this non-inherited job handle when the launcher exits or is killed.
// Assign before spawning, so a killed launcher cannot orphan the main process or SSH.
#[cfg(windows)]
fn own_process_tree()->io::Result<()> {
    use std::ffi::c_void;
    type Handle=*mut c_void;
    #[repr(C)] #[derive(Default)]
    struct Basic {process_time:i64,job_time:i64,flags:u32,min_working:usize,max_working:usize,active:u32,affinity:usize,priority:u32,scheduling:u32}
    #[repr(C)] #[derive(Default)]
    struct Extended {basic:Basic,io:[u64;6],process_memory:usize,job_memory:usize,peak_process:usize,peak_job:usize}
    #[link(name="kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes:*const c_void,name:*const u16)->Handle;
        fn SetInformationJobObject(job:Handle,class:i32,data:*const c_void,size:u32)->i32;
        fn AssignProcessToJobObject(job:Handle,process:Handle)->i32;
        fn GetCurrentProcess()->Handle;
        fn CloseHandle(handle:Handle)->i32;
    }
    unsafe {
        let job=CreateJobObjectW(std::ptr::null(),std::ptr::null());
        if job.is_null(){return Err(io::Error::last_os_error());}
        let mut limits=Extended::default();limits.basic.flags=0x2000;
        if SetInformationJobObject(job,9,(&limits as *const Extended).cast(),std::mem::size_of::<Extended>() as u32)==0 || AssignProcessToJobObject(job,GetCurrentProcess())==0 {
            let error=io::Error::last_os_error();CloseHandle(job);return Err(error);
        }
    }
    Ok(())
}
