"""Company website and upstream attribution in the desktop and mobile About UI."""

import json


def apply(changes):
    change = changes.replace
    desktop = "flutter/lib/desktop/pages/desktop_setting_page.dart"
    mobile = "flutter/lib/mobile/pages/settings_page.dart"
    # The upstream translator rebrands literal RustDesk; insert the credit afterwards.
    attribution = "GeodomDesk is customized from the %upstream% open-source project."
    source_url = "https://github.com/lovely-xin/GeodomDesk"
    for name in [desktop, mobile]:
        change(name, "translate('About RustDesk')", "translate('About GeodomDesk')")
    change(
        desktop,
        "              SelectionArea(\n                  child: Text('${translate('Version')}: $version')",
        "              SelectionArea(\n"
        + "                  child: Text(translate('"
        + attribution
        + "').replaceAll('%upstream%', 'RustDesk'))\n"
        + "                      .marginSymmetric(vertical: 4.0)),\n"
        + "              SelectionArea(\n                  child: Text('${translate('Version')}: $version')",
    )
    change(
        desktop, "launchUrlString('https://rustdesk.com');", "launchUrlString('https://geodom.cn');"
    )
    change(
        desktop,
        "                    translate('Website'),",
        "                    '${translate('Website')}: geodom.cn',",
    )
    change(
        desktop,
        "              Container(\n                decoration: const BoxDecoration(color: Color(0xFF2c8cff)),",
        """              InkWell(
                  onTap: () {
                    launchUrlString('"""
        + source_url
        + """');
                  },
                  child: Text(
                    translate('GeodomDesk source code'),
                    style: linkStyle,
                  ).marginSymmetric(vertical: 4.0)),
              Container(
                decoration: const BoxDecoration(color: Color(0xFF2c8cff)),""",
    )
    change(
        mobile,
        "      content: Wrap(direction: Axis.vertical, spacing: 12, children: [\n        Text('Version: $version'),",
        """      content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
        Text('${translate('Version')}: $version'),
        const SizedBox(height: 12),
        Text(translate('"""
        + attribution
        + """').replaceAll('%upstream%', 'RustDesk')),
        const SizedBox(height: 12),""",
    )
    for indent in ["\n", "              "]:
        change(
            mobile,
            indent + "const url = 'https://rustdesk.com/';",
            indent + "const url = 'https://geodom.cn';",
        )
    for indent in ["                  ", "              "]:
        change(mobile, indent + "child: Text('rustdesk.com',", indent + "child: Text('geodom.cn',")
    change(
        mobile,
        """            SettingsTile(
                title: Text(translate("Build Date")),""",
        """            SettingsTile(
                title: Text(translate('"""
        + attribution
        + """').replaceAll('%upstream%', 'RustDesk')),
                leading: Icon(Icons.info_outline)),
            SettingsTile(
                onPressed: (context) async {
                  await launchUrl(Uri.parse('"""
        + source_url
        + """'));
                },
                title: Text(translate('GeodomDesk source code')),
                leading: Icon(Icons.code)),
            SettingsTile(
                title: Text(translate("Build Date")),""",
    )
    change(
        mobile,
        """      ]),
      actions: [],
    );
  }, clickMaskDismiss: true, backDismiss: true);
}

class ScanButton""",
        """        InkWell(
            onTap: () async {
              await launchUrl(Uri.parse('"""
        + source_url
        + """'));
            },
            child: Padding(
              padding: EdgeInsets.symmetric(vertical: 8),
              child: Text(translate('GeodomDesk source code'),
                  style: TextStyle(decoration: TextDecoration.underline)),
            )),
      ]),
      actions: [],
    );
  }, clickMaskDismiss: true, backDismiss: true);
}

class ScanButton""",
    )
    translations = {
        "About GeodomDesk": "关于 GeodomDesk",
        attribution: "GeodomDesk 基于 %upstream% 开源项目定制。",
        "GeodomDesk source code": "GeodomDesk 源代码",
    }
    entries = "".join(
        "        ("
        + json.dumps(key, ensure_ascii=False)
        + ", "
        + json.dumps(value, ensure_ascii=False)
        + "),\n"
        for key, value in translations.items()
    )
    change("src/lang/cn.rs", "    [\n", "    [\n" + entries)
