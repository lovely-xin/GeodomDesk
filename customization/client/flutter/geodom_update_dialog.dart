import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/desktop/widgets/update_progress.dart';
import 'package:flutter_hbb/models/platform_model.dart';
import 'package:flutter_hbb/models/state_model.dart';
import 'package:url_launcher/url_launcher.dart';

const String _kGeodomUpdateDialogTag = 'geodom-software-update-dialog';
const String _kGeodomManualCheckLoadingTag = 'geodom-manual-update-check-loading';
const String _kSkippedUpdateReleaseOption = 'geodom-skipped-update-release';
const String _kFallbackChangelogKey =
    'This update includes stability improvements, performance optimizations, and bug fixes.';

String _lastAutoPromptedRelease = '';
bool _updateDialogOpen = false;
int _manualCheckSeq = 0;
int _activeManualCheckSeq = 0;

String _releaseIdFromUrl(String url) {
  final idx = url.indexOf('/releases/');
  if (idx < 0) return '';
  final rest = url.substring(idx + '/releases/'.length);
  final seg = rest.split('/').first.split('?').first.trim();
  return seg;
}

class GeodomUpdateInfo {
  final String url;
  final String releaseId;
  final String version;
  final String changelog;
  final String publishedAt;

  const GeodomUpdateInfo({
    required this.url,
    required this.releaseId,
    required this.version,
    required this.changelog,
    required this.publishedAt,
  });

  String get displayVersion => releaseId.isNotEmpty ? releaseId : version;

  bool get canInAppInstall => (isWindows || isMacOS) && bind.mainIsInstalled();

  factory GeodomUpdateInfo.fromMap(
    Map<String, dynamic> map, {
    String fallbackUrl = '',
    String fallbackReleaseId = '',
  }) {
    final rawUrl = map['url'];
    final rawReleaseId = map['release_id'];
    final rawVersion = map['version'];
    final rawChangelog = map['changelog'];
    final rawPublishedAt = map['published_at'];

    final url =
        rawUrl is String && rawUrl.isNotEmpty ? rawUrl : fallbackUrl;
    final releaseId = rawReleaseId is String && rawReleaseId.isNotEmpty
        ? rawReleaseId
        : (fallbackReleaseId.isNotEmpty
            ? fallbackReleaseId
            : _releaseIdFromUrl(url));
    final version = rawVersion is String && rawVersion.isNotEmpty
        ? rawVersion
        : releaseId;
    final changelog = rawChangelog is String ? rawChangelog.trim() : '';
    final publishedAt = rawPublishedAt is String ? rawPublishedAt.trim() : '';
    return GeodomUpdateInfo(
      url: url,
      releaseId: releaseId,
      version: version,
      changelog: changelog,
      publishedAt: publishedAt,
    );
  }
}

GeodomUpdateInfo? loadCachedGeodomUpdateInfo({String fallbackUrl = ''}) {
  final effectiveFallbackUrl =
      fallbackUrl.isNotEmpty ? fallbackUrl : stateGlobal.updateUrl.value;
  final newVer = bind.mainGetNewVersion();
  final fallbackReleaseId =
      newVer.isNotEmpty ? newVer : _releaseIdFromUrl(effectiveFallbackUrl);
  final raw = bind.mainGetCommonSync(key: 'software-update-info');
  if (raw.isNotEmpty) {
    try {
      final decoded = json.decode(raw);
      if (decoded is Map<String, dynamic>) {
        final info = GeodomUpdateInfo.fromMap(
          decoded,
          fallbackUrl: effectiveFallbackUrl,
          fallbackReleaseId: fallbackReleaseId,
        );
        if (info.url.isNotEmpty) {
          return info;
        }
      }
    } catch (_) {}
  }
  if (effectiveFallbackUrl.isEmpty) {
    return null;
  }
  return GeodomUpdateInfo(
    url: effectiveFallbackUrl,
    releaseId: fallbackReleaseId,
    version: fallbackReleaseId,
    changelog: '',
    publishedAt: '',
  );
}

Future<void> handleGeodomUpdateCheckEvent(Map<String, dynamic> evt) async {
  final url = evt['url'] is String ? evt['url'] as String : '';
  if (url.isEmpty || isWeb) {
    stateGlobal.updateUrl.value = url;
    return;
  }

  final cached = loadCachedGeodomUpdateInfo(fallbackUrl: url);
  final newVer = bind.mainGetNewVersion();
  final info = GeodomUpdateInfo.fromMap(
    evt,
    fallbackUrl: cached?.url ?? url,
    fallbackReleaseId: cached?.releaseId.isNotEmpty == true
        ? cached!.releaseId
        : (newVer.isNotEmpty ? newVer : _releaseIdFromUrl(url)),
  );
  final merged = GeodomUpdateInfo(
    url: info.url,
    releaseId: info.releaseId,
    version: info.version,
    changelog: info.changelog.isNotEmpty ? info.changelog : (cached?.changelog ?? ''),
    publishedAt: info.publishedAt.isNotEmpty ? info.publishedAt : (cached?.publishedAt ?? ''),
  );
  final dedupKey = merged.releaseId.isNotEmpty ? merged.releaseId : merged.url;
  final isSkipped = dedupKey.isNotEmpty &&
      bind.mainGetLocalOption(key: _kSkippedUpdateReleaseOption) == dedupKey;
  if (isSkipped) {
    stateGlobal.updateUrl.value = '';
    return;
  }
  stateGlobal.updateUrl.value = url;
  if (isDesktop && desktopType != DesktopType.main) {
    return;
  }
  if (_activeManualCheckSeq != 0) {
    return;
  }
  if (_lastAutoPromptedRelease == dedupKey) {
    return;
  }
  _lastAutoPromptedRelease = dedupKey;

  WidgetsBinding.instance.addPostFrameCallback((_) {
    _showUpdateDialogWhenReady(merged, dedupKey);
  });
}

Future<void> _showUpdateDialogWhenReady(
  GeodomUpdateInfo info,
  String dedupKey,
) async {
  for (var attempt = 0; attempt < 8; attempt++) {
    if (globalKey.currentState?.overlay != null) {
      break;
    }
    await Future.delayed(const Duration(milliseconds: 250));
  }
  if (_activeManualCheckSeq != 0 ||
      gFFI.dialogManager.hasAny ||
      (!isDesktop && !stateGlobal.isInMainPage) ||
      stateGlobal.updateUrl.value.isEmpty) {
    if (_lastAutoPromptedRelease == dedupKey) {
      _lastAutoPromptedRelease = '';
    }
    return;
  }
  final shown = await showGeodomUpdateDialog(info);
  if (!shown && _lastAutoPromptedRelease == dedupKey) {
    _lastAutoPromptedRelease = '';
  }
}

Future<void> showGeodomUpdateDialogFromCurrent([String fallbackUrl = '']) async {
  final info = loadCachedGeodomUpdateInfo(fallbackUrl: fallbackUrl);
  if (info == null) {
    return;
  }
  await showGeodomUpdateDialog(info, isManual: true);
}

String _formatPublishedDate(String publishedAt) {
  final trimmed = publishedAt.trim();
  if (trimmed.length >= 10) {
    return trimmed.substring(0, 10);
  }
  return trimmed;
}

Widget _buildChangelogContent(BuildContext context, String rawChangelog) {
  final effective = rawChangelog.trim().isNotEmpty
      ? rawChangelog.trim()
      : translate(_kFallbackChangelogKey);
  final textTheme = Theme.of(context).textTheme;
  final bodyStyle = textTheme.bodyMedium?.copyWith(height: 1.5) ??
      const TextStyle(fontSize: 13.5, height: 1.5);
  final headingStyle = bodyStyle.copyWith(fontWeight: FontWeight.w600);
  final lines = effective.split('\n');
  final widgets = <Widget>[];

  for (final rawLine in lines) {
    final line = rawLine.trimRight();
    final trimmed = line.trim();
    if (trimmed.isEmpty) {
      widgets.add(const SizedBox(height: 6));
      continue;
    }
    if (trimmed.startsWith('### ') || trimmed.startsWith('## ')) {
      final title = trimmed.replaceFirst(RegExp(r'^#{2,3}\s+'), '');
      widgets.add(Padding(
        padding: const EdgeInsets.only(top: 4, bottom: 4),
        child: Text(title, style: headingStyle),
      ));
      continue;
    }
    if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
      final item = trimmed.substring(2).trim();
      final isDark = Theme.of(context).brightness == Brightness.dark;
      final bulletColor = isDark ? const Color(0xFF58A6FF) : MyTheme.accent;
      widgets.add(Padding(
        padding: const EdgeInsets.only(bottom: 4),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('•  ', style: bodyStyle.copyWith(color: bulletColor)),
            Expanded(child: Text(item, style: bodyStyle)),
          ],
        ),
      ));
      continue;
    }
    widgets.add(Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Text(trimmed, style: bodyStyle),
    ));
  }

  return SelectionArea(
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: widgets,
    ),
  );
}

class _GeodomChangelogBox extends StatefulWidget {
  final String changelog;
  final bool isDesktopLayout;

  const _GeodomChangelogBox({
    required this.changelog,
    required this.isDesktopLayout,
  });

  @override
  State<_GeodomChangelogBox> createState() => _GeodomChangelogBoxState();
}

class _GeodomChangelogBoxState extends State<_GeodomChangelogBox> {
  final ScrollController _scrollController = ScrollController();

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;
    final screenHeight = MediaQuery.of(context).size.height;
    final maxBoxHeight = (screenHeight * 0.35)
        .clamp(120.0, widget.isDesktopLayout ? 280.0 : 220.0)
        .toDouble();
    return Container(
      width: double.infinity,
      constraints: BoxConstraints(
        minHeight: 72,
        maxHeight: maxBoxHeight,
      ),
      decoration: BoxDecoration(
        color: isDark
            ? Colors.white.withOpacity(0.05)
            : Colors.black.withOpacity(0.03),
        border: Border.all(
          color: theme.dividerColor.withOpacity(0.35),
        ),
        borderRadius: BorderRadius.circular(8),
      ),
      padding: const EdgeInsets.all(12),
      child: Scrollbar(
        controller: _scrollController,
        thumbVisibility: true,
        child: SingleChildScrollView(
          controller: _scrollController,
          padding: const EdgeInsets.only(right: 10),
          child: _buildChangelogContent(context, widget.changelog),
        ),
      ),
    );
  }
}

Future<void> _confirmInstallUpdate(GeodomUpdateInfo info) async {
  if (info.canInAppInstall) {
    await handleUpdate(info.url);
  } else {
    final launched = await launchUrl(
      Uri.parse(info.url),
      mode: LaunchMode.externalApplication,
    );
    if (!launched) {
      showToast(translate('Unable to open download page'));
    }
  }
}

Future<bool> showGeodomUpdateDialog(
  GeodomUpdateInfo info, {
  bool isManual = false,
}) async {
  if (_updateDialogOpen || info.url.isEmpty || isWeb) {
    return false;
  }
  if (gFFI.dialogManager.existing(_kGeodomUpdateDialogTag)) {
    return false;
  }
  _updateDialogOpen = true;
  try {
    String currentVersion = '';
    try {
      currentVersion = await bind.mainGetVersion();
    } catch (_) {}
    if (currentVersion.isEmpty) {
      currentVersion = version;
    }
    final targetVersion = info.displayVersion;
    final publishedDate = _formatPublishedDate(info.publishedAt);
    final dedupKey = info.releaseId.isNotEmpty ? info.releaseId : info.url;

    await gFFI.dialogManager.show<void>(
      (setState, close, context) {
        final theme = Theme.of(context);
        final isDark = theme.brightness == Brightness.dark;
        final accentColor = isDark ? const Color(0xFF58A6FF) : MyTheme.accent;

        void closeDialog() {
          close();
        }

        return CustomAlertDialog(
          contentBoxConstraints: BoxConstraints(
            maxWidth: isDesktop ? 520 : 380,
          ),
          title: Row(
            children: [
              Icon(
                Icons.system_update_alt_rounded,
                color: accentColor,
                size: 22,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  translate('New version available'),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (targetVersion.isNotEmpty)
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(
                    color: accentColor.withOpacity(0.14),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Text(
                    'v$targetVersion',
                    style: TextStyle(
                      color: accentColor,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
            ],
          ),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Wrap(
                spacing: 14,
                runSpacing: 4,
                children: [
                  if (currentVersion.isNotEmpty)
                    Text(
                      '${translate('Current version')}: $currentVersion',
                      style: theme.textTheme.bodySmall,
                    ),
                  if (targetVersion.isNotEmpty)
                    Text(
                      '${translate('New version')}: $targetVersion',
                      style: theme.textTheme.bodySmall?.copyWith(
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  if (publishedDate.isNotEmpty)
                    Text(
                      publishedDate,
                      style: theme.textTheme.bodySmall,
                    ),
                ],
              ),
              const SizedBox(height: 10),
              Text(
                translate('Changelog'),
                style: const TextStyle(
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 6),
              _GeodomChangelogBox(
                changelog: info.changelog,
                isDesktopLayout: isDesktop,
              ),
              const SizedBox(height: 8),
              Text(
                info.canInAppInstall
                    ? translate('Confirm to verify and install this update now?')
                    : translate('Confirm to open the download page for this version?'),
                style: theme.textTheme.bodySmall,
              ),
              if (!isManual && dedupKey.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: InkWell(
                    borderRadius: BorderRadius.circular(6),
                    onTap: () async {
                      await bind.mainSetLocalOption(
                        key: _kSkippedUpdateReleaseOption,
                        value: dedupKey,
                      );
                      stateGlobal.updateUrl.value = '';
                      closeDialog();
                    },
                    child: Padding(
                      padding: const EdgeInsets.symmetric(
                        vertical: 8,
                        horizontal: 4,
                      ),
                      child: Text(
                        translate('Skip this version'),
                        style: theme.textTheme.bodySmall?.copyWith(
                          decoration: TextDecoration.underline,
                        ),
                      ),
                    ),
                  ),
                ),
            ],
          ),
          actions: [
            dialogButton(
              'Later',
              onPressed: closeDialog,
              isOutline: true,
            ),
            dialogButton(
              info.canInAppInstall ? 'Install update' : 'Download',
              onPressed: () async {
                closeDialog();
                await _confirmInstallUpdate(info);
              },
              isOutline: false,
            ),
          ],
          onCancel: closeDialog,
        );
      },
      tag: _kGeodomUpdateDialogTag,
    );
    return true;
  } catch (error) {
    debugPrint('Failed to show update dialog: $error');
    return false;
  } finally {
    _updateDialogOpen = false;
  }
}

Future<void> checkGeodomUpdateManually() async {
  if (_activeManualCheckSeq != 0 || isWeb) {
    return;
  }
  final seq = ++_manualCheckSeq;
  _activeManualCheckSeq = seq;
  gFFI.dialogManager.showLoading(
    'Checking for updates...',
    onCancel: () {
      if (_activeManualCheckSeq == seq) {
        _activeManualCheckSeq = 0;
      }
    },
    tag: _kGeodomManualCheckLoadingTag,
  );
  try {
    final raw = await bind.mainGetCommon(key: 'check-software-update');
    if (_activeManualCheckSeq != seq) {
      return;
    }
    gFFI.dialogManager.dismissByTag(_kGeodomManualCheckLoadingTag);
    final decoded = json.decode(raw);
    if (decoded is! Map<String, dynamic>) {
      showToast(translate('Failed to check for updates'));
      return;
    }
    final status = decoded['status'];
    if (status == 'update_available') {
      final info = GeodomUpdateInfo.fromMap(decoded);
      if (info.url.isEmpty) {
        showToast(translate('Failed to check for updates'));
        return;
      }
      stateGlobal.updateUrl.value = info.url;
      await showGeodomUpdateDialog(info, isManual: true);
    } else if (status == 'up_to_date') {
      stateGlobal.updateUrl.value = '';
      showToast(translate('Already up to date'));
    } else {
      final errText = (decoded['error'] ?? '').toString().toLowerCase();
      final timedOut = decoded['timed_out'] == true || errText.contains('timed out');
      showToast(translate(timedOut
          ? 'Update check timed out, please try again later'
          : 'Failed to check for updates'));
    }
  } catch (_) {
    if (_activeManualCheckSeq == seq) {
      gFFI.dialogManager.dismissByTag(_kGeodomManualCheckLoadingTag);
      showToast(translate('Failed to check for updates'));
    }
  } finally {
    if (_activeManualCheckSeq == seq) {
      _activeManualCheckSeq = 0;
    }
  }
}
