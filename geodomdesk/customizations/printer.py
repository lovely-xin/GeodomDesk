"""Native printer controls with compact labels and owned installation callbacks."""

from .edits import SourceEdits


def apply(changes):
    name = "flutter/lib/desktop/pages/desktop_setting_page.dart"
    start = changes[name].index("class __PrinterState extends State<_Printer> {")
    end = changes[name].index("class _About extends StatefulWidget", start)
    original = changes[name][start:end]
    scope = SourceEdits({name: original})
    scope[name] = scope[name].replace("failedMsg.value", "_failedMsg.value")
    change = scope.replace
    change(
        name,
        "class __PrinterState extends State<_Printer> {",
        """class __PrinterState extends State<_Printer> {
  final _failedMsg = ''.obs;
  final _installing = false.obs;

  @override
  void initState() {
    super.initState();
    platformFFI.registerEventHandler(
        'install-printer-res', 'install-printer-res', (evt) async {
      if (!mounted) return;
      _installing.value = false;
      if (evt['success'] as bool) {
        setState(() {});
      } else {
        _failedMsg.value = evt['msg'] as String;
      }
    }, replace: true);
  }

  @override
  void dispose() {
    platformFFI.unregisterEventHandler('install-printer-res', 'install-printer-res');
    super.dispose();
  }
""",
    )
    begin = scope[name].index("      final failedMsg = ''.obs;")
    finish = scope[name].index("      return Column(children: [", begin)
    change(name, scope[name][begin:finish], "")
    # Both explanation and button use the card's single horizontal inset.
    change(
        name,
        ".marginOnly(left: _kCardLeftMargin, bottom: 2.0);",
        ".marginOnly(bottom: 2.0);",
    )
    scope[name] = scope[name].replace(
        ".marginOnly(bottom: 10.0)",
        ".marginOnly(left: _kContentHMargin, right: _kContentHMargin, bottom: 10.0)",
    )
    change(
        name,
        """        _Button('Install {$appName} Printer', () {
          _failedMsg.value = '';
          bind.mainSetCommon(key: 'install-printer', value: '');
        })""",
        """        Obx(() => _Button('Install printer', () {
          _failedMsg.value = '';
          _installing.value = true;
          bind.mainSetCommon(key: 'install-printer', value: '');
        }, enabled: !_installing.value))""",
    )
    scope[name] = scope[name].replace(
        "child: Text(translate('printer-{$appName}-not-installed-tip'))",
        "child: Text(translate('printer-{$appName}-not-installed-tip'),\n"
        "                      style: Theme.of(context).textTheme.bodySmall)",
    )
    changes.replace(name, original, scope[name])
    changes.replace(
        "src/lang/cn.rs", "    [\n", '    [\n        ("Install printer", "安装打印机"),\n'
    )
    for before, after in [
        ('("Outgoing Print Jobs", "传出的打印任务")', '("Outgoing Print Jobs", "发送打印任务")'),
        ('("Incoming Print Jobs", "传入的打印任务")', '("Incoming Print Jobs", "接收打印任务")'),
        (
            '("use-the-default-printer-tip", "使用默认的打印机执行")',
            '("use-the-default-printer-tip", "使用默认打印机")',
        ),
        (
            '("use-the-selected-printer-tip", "使用选择的打印机执行")',
            '("use-the-selected-printer-tip", "使用指定打印机")',
        ),
        (
            '("auto-print-tip", "使用选择的打印机自动执行")',
            '("auto-print-tip", "自动打印收到的任务")',
        ),
    ]:
        changes.replace("src/lang/cn.rs", before, after)
