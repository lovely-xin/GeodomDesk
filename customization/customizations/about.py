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
        "              Image.asset('assets/geodom-logo.png', height: 28,\n"
        + "                  fit: BoxFit.contain, semanticLabel: 'Geodom',\n"
        + "                  color: Theme.of(context).brightness == Brightness.dark\n"
        + "                      ? Colors.white : null)\n"
        + "                      .marginSymmetric(vertical: 8.0),\n"
        + "              SelectionArea(\n"
        + "                  child: Text(translate('"
        + attribution
        + "').replaceAll('%upstream%', 'RustDesk'))\n"
        + "                      .marginSymmetric(vertical: 4.0)),\n"
        + "              SelectionArea(\n                  child: Text('${translate('Version')}: $version')",
    )
    change(
        desktop,
        "launchUrlString('https://rustdesk.com');",
        "launchUrlString('https://www.geodom.cn');",
    )
    change(
        desktop,
        "                    translate('Website'),",
        "                    '${translate('Website')}: www.geodom.cn',",
    )
    start = changes[desktop].index(
        "              Container(\n"
        "                decoration: const BoxDecoration(color: Color(0xFF2c8cff)),"
    )
    end_marker = "              ).marginSymmetric(vertical: 4.0)"
    end = changes[desktop].index(end_marker, start) + len(end_marker)
    change(
        desktop,
        changes[desktop][start:end],
        """              InkWell(
                  onTap: () {
                    launchUrlString('"""
        + source_url
        + """');
                  },
                  child: Text(
                    translate('Source code'),
                    style: linkStyle,
                  ).marginSymmetric(vertical: 4.0)),""",
    )
    for unused in [
        "      final license = await bind.mainGetLicense();\n",
        "        'license': license,\n",
        "      final license = data['license'].toString();\n",
    ]:
        change(desktop, unused, "")
    change(
        mobile,
        "      content: Wrap(direction: Axis.vertical, spacing: 12, children: [\n        Text('Version: $version'),",
        """      content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
        Image.asset('assets/geodom-logo.png', height: 28,
            fit: BoxFit.contain, semanticLabel: 'Geodom',
            color: Theme.of(context).brightness == Brightness.dark
                ? Colors.white : null),
        const SizedBox(height: 12),
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
            indent + "const url = 'https://www.geodom.cn';",
        )
    for indent in ["                  ", "              "]:
        change(
            mobile, indent + "child: Text('rustdesk.com',", indent + "child: Text('www.geodom.cn',"
        )
    change(
        mobile,
        """          title: Text(translate("About")),
          tiles: [""",
        """          title: Text(translate("About")),
          tiles: [
            SettingsTile(
                title: Image.asset('assets/geodom-logo.png', height: 28,
                    fit: BoxFit.contain, alignment: Alignment.centerLeft,
                    semanticLabel: 'Geodom',
                    color: Theme.of(context).brightness == Brightness.dark
                        ? Colors.white : null)),""",
    )
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
                title: Text(translate('Source code')),
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
              child: Text(translate('Source code'),
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
        "Source code": "源代码",
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
