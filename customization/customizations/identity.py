"""Application identity, local listeners and process/file isolation."""


def apply(changes):
    change = changes.replace
    change(
        "libs/hbb_common/src/config.rs",
        'RwLock::new("RustDesk".to_owned())',
        'RwLock::new("GeodomDesk".to_owned())',
    )
    change(
        "libs/hbb_common/src/config.rs",
        'RwLock::new("com.carriez".to_owned())',
        'RwLock::new("cn.geodom".to_owned())',
    )
    change(
        "src/platform/windows.rs",
        '.join("RustDesk")\n        .join("RustDeskCustomClientStaging")',
        '.join("GeodomDesk")\n        .join("GeodomDeskCustomClientStaging")',
    )
    change(
        "libs/portable/src/main.rs",
        'const APP_PREFIX: &str = "rustdesk";',
        'const APP_PREFIX: &str = "geodomdesk";',
    )
    # Isolate both the extraction directory and the actual Windows process image.
    change(
        "libs/portable/src/main.rs", "dir.join(app_dir_name(&reader.exe))", "dir.join(APP_PREFIX)"
    )
    for name in ["libs/portable/src/main.rs", "src/privacy_mode/win_topmost_window.rs"]:
        changes[name] = changes[name].replace(
            "RuntimeBroker_rustdesk.exe", "RuntimeBroker_geodomdesk.exe"
        )
    for name in [
        "src/platform/windows.rs",
        "libs/portable/src/main.rs",
        "src/platform/windows/verified_update.rs",
    ]:
        changes[name] = changes[name].replace("rustdesk-verified-", "geodomdesk-verified-")
    changes["src/platform/windows.rs"] = (
        changes["src/platform/windows.rs"]
        .replace('file_name.starts_with("rustdesk-")', 'file_name.starts_with("geodomdesk-")')
        .replace(
            'file_name.starts_with("rustdesk_install_")',
            'file_name.starts_with("geodomdesk_install_")',
        )
    )
    change(
        "src/platform/windows/installer_handoff/directory.rs",
        '"rustdesk-install-{}"',
        '"geodomdesk-install-{}"',
    )
    changes["src/server/terminal_service.rs"] = changes["src/server/terminal_service.rs"].replace(
        "pipe\\rustdesk_term_", "pipe\\geodomdesk_term_"
    )
    change("src/lan.rs", "(RENDEZVOUS_PORT + 3) as _", "21129")
    change("src/rendezvous_mediator.rs", "port = RENDEZVOUS_PORT + 2;", "port = 21128;")
    changes["libs/portable/Cargo.toml"] = (
        changes["libs/portable/Cargo.toml"]
        .replace('"RustDesk Remote Desktop"', '"GeodomDesk Remote Desktop"')
        .replace('ProductName = "RustDesk"', 'ProductName = "GeodomDesk"')
        .replace('OriginalFilename = "rustdesk.exe"', 'OriginalFilename = "GeodomDesk.exe"')
    )
    change(
        "flutter/linux/CMakeLists.txt",
        'set(APPLICATION_ID "com.carriez.flutter_hbb")',
        'set(APPLICATION_ID "cn.geodom.GeodomDesk")',
    )
    change(
        "flutter/windows/CMakeLists.txt",
        'set(BINARY_NAME "rustdesk")',
        'set(BINARY_NAME "GeodomDesk")',
    )
    change(
        "build.py", "/{flutter_build_dir_2}/rustdesk.exe", "/{flutter_build_dir_2}/GeodomDesk.exe"
    )
    change(
        "flutter/windows/runner/win32_window.cpp",
        'L"FLUTTER_RUNNER_WIN32_WINDOW"',
        'L"GEODOMDESK_RUNNER_WIN32_WINDOW"',
    )
    change(
        "flutter/windows/runner/main.cpp",
        'std::wstring app_name = L"RustDesk";',
        'std::wstring app_name = L"GeodomDesk";',
    )
    changes["flutter/windows/runner/Runner.rc"] = (
        changes["flutter/windows/runner/Runner.rc"]
        .replace("RustDesk", "GeodomDesk")
        .replace('"rustdesk"', '"geodomdesk"')
        .replace('"rustdesk.exe"', '"GeodomDesk.exe"')
    )
    change(
        "flutter/lib/desktop/pages/desktop_setting_page.dart",
        "hintText: '21118'",
        "hintText: '21128'",
    )
