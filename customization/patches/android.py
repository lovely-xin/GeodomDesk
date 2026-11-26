"""Android coexist identity and the existing mobile update UI."""


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
    # flutter_launcher_icons writes this color into colors.xml.
    change(
        "flutter/android/app/src/main/res/values/ic_launcher_background.xml",
        '    <color name="ic_launcher_background">#ffffff</color>\n',
        "",
    )
    change(
        "flutter/lib/mobile/pages/connection_page.dart",
        "final url = 'https://rustdesk.com/download';",
        "final url = updateUrl;",
    )
    name = "flutter/lib/mobile/pages/settings_page.dart"
    change(name, "import '../widgets/deploy_dialog.dart';\n", "")
    change(name, """          if (isAndroid && !bind.isOutgoingOnly())
            SettingsTile(
                title: Text(translate('Deploy')),
                leading: Icon(Icons.cloud_upload),
                onPressed: (context) {
                  showDeployDialog();
                }),
""", "")
