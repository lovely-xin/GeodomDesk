"""Keep upstream allocation tests safe on Windows toolchains with dynamic TLS."""

from .edits import replace_once


def apply(source, merged):
    name = "src/audio_resampler/allocation_tests.rs"
    path = source / name
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    hook = "fn record_allocation() {\n    let _ = ALLOCATIONS.try_with"
    if hook not in text or "static ALLOCATOR: CountingAllocator" not in text:
        return
    text = replace_once(
        text,
        hook,
        """#[cfg(windows)]
mod allocation_scope {
    use std::sync::{
        atomic::{AtomicU32, Ordering},
        Mutex, MutexGuard,
    };
    use windows::Win32::System::Threading::GetCurrentThreadId;

    static TRACKING_THREAD: AtomicU32 = AtomicU32::new(0);
    static LOCK: Mutex<()> = Mutex::new(());

    pub(super) struct Scope {
        _lock: MutexGuard<'static, ()>,
    }

    impl Scope {
        pub(super) fn enter() -> Self {
            let thread = unsafe { GetCurrentThreadId() };
            assert_ne!(TRACKING_THREAD.load(Ordering::Relaxed), thread);
            let lock = LOCK.lock().unwrap_or_else(|error| error.into_inner());
            // Dynamic TLS may allocate during initialization. Enable the hook only
            // after the test thread's counter has been initialized.
            super::ALLOCATIONS.with(|_| {});
            TRACKING_THREAD.store(thread, Ordering::Relaxed);
            Self { _lock: lock }
        }
    }

    impl Drop for Scope {
        fn drop(&mut self) {
            TRACKING_THREAD.store(0, Ordering::Relaxed);
        }
    }

    pub(super) fn tracking() -> bool {
        let thread = TRACKING_THREAD.load(Ordering::Relaxed);
        thread != 0 && thread == unsafe { GetCurrentThreadId() }
    }
}

fn record_allocation() {
    #[cfg(windows)]
    if !allocation_scope::tracking() {
        return;
    }
    let _ = ALLOCATIONS.try_with""",
    )
    signature = "pub(crate) fn assert_no_allocations(process: impl FnOnce()) {\n"
    text = replace_once(
        text,
        signature,
        signature + "    #[cfg(windows)]\n    let _scope = allocation_scope::Scope::enter();\n",
    )
    merged[name] = text.encode("utf-8")
