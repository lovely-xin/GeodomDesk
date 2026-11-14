"""Integrate account-scoped recent device sync with the personal address book."""

from .edits import replace_once


def apply(changes, root):
    def edit(path, before, after):
        changes[path] = replace_once(changes[path], before, after)

    for destination, asset in (
        ("src/geodomdesk_recent.rs", "geodomdesk_recent.rs"),
        ("flutter/lib/models/geodom_recent_sync.dart", "flutter/geodom_recent_sync.dart"),
    ):
        changes[destination] = (root / "client" / asset).read_text(encoding="utf-8")

    edit("src/lib.rs", "pub mod terminal_exec;\n", "pub mod terminal_exec;\nmod geodomdesk_recent;\n")
    edit(
        "libs/hbb_common/src/config.rs",
        "pub struct AbPeer {\n",
        """pub struct AbPeer {
    #[serde(default, deserialize_with = "deserialize_string", skip_serializing_if = "String::is_empty")]
    pub geodom_recent: String,
    #[serde(default)]
    pub geodom_recent_hash_present: bool,
""",
    )
    edit(
        "libs/hbb_common/src/config.rs",
        "impl PeerConfig {\n    pub fn load(id: &str)",
        """impl PeerConfig {
    pub fn last_modified(id: &str) -> SystemTime {
        std::fs::metadata(Self::path(id))
            .and_then(|meta| meta.modified())
            .unwrap_or(std::time::UNIX_EPOCH)
    }

    pub fn load(id: &str)""",
    )
    edit(
        "libs/hbb_common/src/config.rs",
        """    pub fn store(json: String) {
        if let Ok(mut file) = std::fs::File::create(Self::path()) {
            let data = compress(json.as_bytes());
            let max_len = 64 * 1024 * 1024;
            if data.len() > max_len {
                // maxlen of function decompress
                log::error!("ab data too large, {} > {}", data.len(), max_len);
                return;
            }
            if let Ok(data) = symmetric_crypt(&data, true) {
                file.write_all(&data).ok();
            }
        };
    }""",
        """    pub fn store(json: String) {
        let data = compress(json.as_bytes());
        let max_len = 64 * 1024 * 1024;
        if data.len() > max_len {
            log::error!("ab data too large, {} > {}", data.len(), max_len);
            return;
        }
        let Ok(data) = symmetric_crypt(&data, true) else { return; };
        let path = Self::path();
        let temporary = path.with_extension(format!("tmp-{}-{}", std::process::id(), rand::random::<u64>()));
        let result = (|| -> std::io::Result<()> {
            let mut options = fs::OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let mut file = options.open(&temporary)?;
            let written = file.write_all(&data).and_then(|_| file.sync_all());
            drop(file);
            let committed = written.and_then(|_| fs::rename(&temporary, &path));
            if committed.is_err() {
                let _ = fs::remove_file(&temporary);
            }
            committed
        })();
        if let Err(error) = result {
            log::error!("Failed to store address book cache: {error}");
        }
    }""",
    )
    edit(
        "libs/hbb_common/src/config.rs",
        "        Self::remove();\n        Ab::default()",
        "        Ab::default()",
    )
    edit(
        "src/client.rs",
        "        let config = self.load_config();\n        self.remember",
        "        let mut config = self.load_config();\n"
        "        crate::geodomdesk_recent::apply_settings(&self.id, &mut config);\n"
        "        self.remember",
    )
    ffi = "src/flutter_ffi.rs"
    edit(
        ffi,
        """        let vec_id_modified_time_path = PeerConfig::get_vec_id_modified_time_path(&None);
        if vec_id_modified_time_path.is_empty() {""",
        """        let vec_id_modified_time_path = PeerConfig::get_vec_id_modified_time_path(&None);
        let cloud = crate::geodomdesk_recent::account_peers();
        if vec_id_modified_time_path.is_empty() && cloud.is_empty() {""",
    )
    edit(
        ffi,
        "let load_two_times = vec_id_modified_time_path.len()",
        "let load_two_times = cloud.is_empty() && vec_id_modified_time_path.len()",
    )
    edit(
        ffi,
        "        // Don't check if `all_peers` is empty,",
        "        let all_peers = crate::geodomdesk_recent::merge_list(all_peers, &vec_id_modified_time_path, cloud);\n"
        "        // Don't check if `all_peers` is empty,",
    )
    edit(
        ffi,
        """        let peers: Vec<HashMap<&str, String>> = PeerConfig::peers(id_filters)
            .drain(..)
            .map(|(id, _, p)| peer_to_map(id, p))
            .collect();""",
        """        let peers: Vec<HashMap<&str, String>> = PeerConfig::peers(id_filters)
            .into_iter()
            .map(|(id, time, peer)| {
                let profile = crate::geodomdesk_recent::record(&id, time, &peer);
                let mut entry = peer_to_map(id, peer);
                if let Some(profile) = profile {
                    entry.insert(crate::geodomdesk_recent::FIELD, profile);
                }
                entry
            })
            .collect();""",
    )
    peer = "flutter/lib/models/peer_model.dart"
    edit(peer, "  String hash; // personal ab hash password", "  String geodomRecent;\n  bool geodomRecentHashPresent;\n  String hash; // personal ab hash password")
    edit(peer, "        hash = json['hash'] ?? '',", "        geodomRecent = json['geodom_recent'] is String ? json['geodom_recent'] : '',\n        geodomRecentHashPresent = json['geodom_recent_hash_present'] as bool? ?? json.containsKey('hash'),\n        hash = json['hash'] ?? '',")
    edit(peer, "      res['hash'] = hash;", "      res['hash'] = hash;\n      if (geodomRecent.isNotEmpty) res['geodom_recent'] = geodomRecent;")
    edit(peer, "    required this.hash,", "    required this.hash,\n    this.geodomRecent = '',\n    this.geodomRecentHashPresent = false,")
    edit(peer, "        hash == other.hash &&", "        hash == other.hash &&\n        geodomRecent == other.geodomRecent &&\n        geodomRecentHashPresent == other.geodomRecentHashPresent &&")
    edit(peer, "        hash: other.hash,", "        hash: other.hash,\n        geodomRecent: other.geodomRecent,\n        geodomRecentHashPresent: other.geodomRecentHashPresent,")

    book = "flutter/lib/models/ab_model.dart"
    edit(book, "import 'package:flutter/material.dart';", "import 'package:flutter/foundation.dart' show kIsWeb;\nimport 'package:flutter/material.dart';")
    edit(book, "final syncAbOption", "part 'geodom_recent_sync.dart';\n\nfinal syncAbOption")
    edit(book, "class AbModel {\n", "class AbModel {\n  late final recentDevices = GeodomRecentSync(this);\n")
    edit(book, "          _syncFromRecent();\n", "          recentDevices.schedule();\n          _syncFromRecent();\n")
    edit(book, "          if (!current.initialized || !current.canWrite()) return;\n          recentDevices.schedule();", "          recentDevices.schedule();\n          if (!current.initialized || !current.canWrite()) return;")
    edit(
        book,
        ".map((e) => e.toCustomJson(includingHash: value.isPersonal()))",
        """.map((e) => {
              ...e.toCustomJson(includingHash: value.isPersonal()),
              if (value.isPersonal())
                'geodom_recent_hash_present': e.geodomRecentHashPresent,
            })""",
    )
    edit(
        book,
        "if (personal && r.hash.isNotEmpty && p.hash != r.hash) {",
        "if (personal && !gFFI.abModel.recentDevices.enabled && r.hash.isNotEmpty && p.hash != r.hash) {",
    )
    edit(book, "    _pulledOnce = false;\n    await bind.mainClearAb();", "    _pulledOnce = false;\n    _maxPeerOneAb = 0;\n    recentDevices.supported.value = false;\n    await bind.mainClearAb();")
    edit(book, "      _maxPeerOneAb = json['max_peer_one_ab'] ?? 0;", "      _maxPeerOneAb = json['max_peer_one_ab'] ?? 0;\n      recentDevices.supported.value = !kIsWeb && json['geodom_recent_sync'] == 1;")
    fetch_start = "  Future<bool> _fetchPeers(List<Peer> tmpPeers, {quiet = false}) async {"
    before, fetch = changes[book].split(fetch_start, 1)
    fetch, after = fetch.split("  Future<bool> _fetchTags(", 1)
    fetch = replace_once(fetch, "        current += 1;", "        if (active != null && !active()) return false;\n        current += 1;")
    fetch = replace_once(fetch, "      if (pullError.isNotEmpty) {", "      if (pullError.isNotEmpty && (active?.call() ?? true)) {")
    changes[book] = before + (
        "  Future<bool> _fetchPeers(List<Peer> tmpPeers,\n"
        "      {quiet = false, bool Function()? active}) async {"
    ) + fetch + "  Future<bool> _fetchTags(" + after

    widget = "flutter/lib/common/widgets/address_book.dart"
    edit(
        widget,
        "      if (canWrite) syncMenuItem(),",
        """      if (canWrite) syncMenuItem(),
      if (gFFI.abModel.recentDevices.supported.value)
        MenuEntrySwitch<String>(
          switchType: SwitchType.scheckbox,
          text: translate('Sync recent devices and credentials'),
          getter: () async => gFFI.abModel.recentDevices.enabled,
          setter: gFFI.abModel.recentDevices.setEnabled,
          dismissOnClicked: true,
          enabled: gFFI.abModel.recentDevices.supported,
        ),""",
    )
    edit(
        "flutter/lib/common/widgets/peer_card.dart",
        "              await bind.mainRemovePeer(id: id);",
        "              await bind.mainRemovePeer(id: id);\n              await gFFI.abModel.recentDevices.remove(id);",
    )
    edit(
        "flutter/lib/common/widgets/peer_card.dart",
        """        bool succ = await gFFI.abModel.changePersonalHashPassword(id, '');
        await bind.mainForgetPassword(id: id);""",
        """        final recent = gFFI.abModel.recentDevices;
        final sync = recent.enabled;
        final succ = sync || await gFFI.abModel.changePersonalHashPassword(id, '');
        await bind.mainForgetPassword(id: id);
        if (sync) await recent.forgetCredential(id);""",
    )
    edit(
        "flutter/lib/utils/http_service.dart",
        """    } catch (e) {
      print('Failed to parse response\\n$responseJson\\nError:\\n$e');
      throw Exception('Failed to parse response.\\n$responseJson');
    }""",
        """    } catch (_) {
      throw const FormatException('Failed to parse the API response');
    }""",
    )
    edit(
        "src/lang/cn.rs",
        '        ("Recent sessions", "最近访问过"),',
        '        ("Recent sessions", "最近访问过"),\n'
        '        ("Sync recent devices and credentials", "同步最近访问的设备与凭据"),',
    )
