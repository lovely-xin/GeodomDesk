"""Android coexist identity, public server defaults and the existing mobile update UI."""

import json


def apply(changes, config):
    change = changes.replace
    package = config["android_package_id"]
    change(
        "flutter/android/app/build.gradle",
        'applicationId "com.carriez.flutter_hbb"',
        'applicationId "' + package + '"',
    )
    # Keep the Kotlin namespace/JNI names: applicationId owns the Android sandbox.
    name = "flutter/android/app/src/main/AndroidManifest.xml"
    changes[name] = (
        changes[name]
        .replace('android:label="RustDesk', 'android:label="GeodomDesk')
        .replace('android:scheme="rustdesk"', 'android:scheme="geodomdesk"')
        .replace("com.carriez.flutter_hbb.DEBUG_BOOT_COMPLETED", package + ".DEBUG_BOOT_COMPLETED")
    )
    prefix = "flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/"
    for name in ["MainService.kt", "BootReceiver.kt", "FloatingWindowService.kt"]:
        changes[prefix + name] = changes[prefix + name].replace("RustDesk", "GeodomDesk")
    change(
        prefix + "BootReceiver.kt",
        "com.carriez.flutter_hbb.DEBUG_BOOT_COMPLETED",
        package + ".DEBUG_BOOT_COMPLETED",
    )
    for name in ["DocumentLogs.kt", "DocumentTree.kt", "MainActivity.kt"]:
        changes[prefix + name] = changes[prefix + name].replace("rustdesk-", "geodomdesk-")
    name = "flutter/android/app/src/main/res/values/strings.xml"
    changes[name] = changes[name].replace("RustDesk", "GeodomDesk")
    defaults = {
        "custom-rendezvous-server": config["server"]["host"],
        "key": config["server"]["key"],
        "websocket-port": config["server"]["websocket_port"],
    }
    entries = ",\n".join(
        "            (" + json.dumps(key) + ".to_owned(), " + json.dumps(value) + ".to_owned())"
        for key, value in defaults.items()
    )
    change(
        "libs/hbb_common/src/config.rs",
        "pub static ref DEFAULT_SETTINGS: RwLock<HashMap<String, String>> = Default::default();",
        """pub static ref DEFAULT_SETTINGS: RwLock<HashMap<String, String>> = RwLock::new({
        #[cfg(target_os = "android")]
        { HashMap::from([
"""
        + entries
        + """
        ]) }
        #[cfg(not(target_os = "android"))]
        { HashMap::new() }
    });""",
    )
    change(
        "flutter/lib/mobile/pages/connection_page.dart",
        "final url = 'https://rustdesk.com/download';",
        "final url = updateUrl;",
    )
