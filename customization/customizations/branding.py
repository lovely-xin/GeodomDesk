"""Keep company branding in About; preserve the regular client layout."""


def apply(changes):
    # The tray's external asset takes precedence over its embedded fallback.
    changes.replace(
        "src/tray.rs",
        'let path = path.join(r"data\\flutter_assets\\assets\\icon.png");',
        'let path = path.join(r"data\\flutter_assets\\assets\\icon.ico");',
    )
    changes.replace(
        "flutter/lib/desktop/pages/desktop_home_page.dart",
        """      if (bind.isCustomClient())
        Align(
          alignment: Alignment.center,
          child: loadPowered(context),
        ),
      Align(
        alignment: Alignment.center,
        child: loadLogo(),
      ),
""",
        "",
    )
    name = "flutter/lib/mobile/pages/settings_page.dart"
    start = changes[name].index("    final customClientSection = CustomSettingsSection(")
    end = changes[name].index("    final List<AbstractSettingsTile> enhancementsTiles", start)
    changes.replace(name, changes[name][start:end], "")
    changes.replace(name, "        customClientSection,\n", "")


def assets(root):
    result = {}
    for name in [
        "flutter/windows/runner/resources/app_icon.ico",
        "res/icon.ico",
        "res/tray-icon.ico",
        "flutter/assets/icon.ico",
    ]:
        result[name] = (root / "assets/geodomdesk.ico").read_bytes()
    for name in ["res/128x128@2x.png", "flutter/assets/icon.png"]:
        result[name] = (root / "assets/geodomdesk.png").read_bytes()
    result["flutter/assets/geodom-logo.png"] = (root / "assets/geodom-logo.png").read_bytes()
    return result
