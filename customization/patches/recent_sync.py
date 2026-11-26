"""Integrate account-scoped recent device sync using dedicated storage separate from My Address Book."""

from .edits import replace_once


def apply(changes, root):
    def edit(path, before, after):
        changes[path] = replace_once(changes[path], before, after)

    for destination, asset in (
        ("src/geodomdesk_recent.rs", "rust/geodomdesk_recent.rs"),
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
        "impl Ab {\n    fn path() -> PathBuf {",
        """impl Ab {
    pub fn path() -> PathBuf {""",
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
        """    pub fn recent_path() -> PathBuf {
        let filename = format!("{}_recent", APP_NAME.read().unwrap().clone());
        Config::path(filename)
    }

    fn store_encrypted_atomic(path: PathBuf, json: &str, label: &str) {
        let data = compress(json.as_bytes());
        let max_len = 64 * 1024 * 1024;
        if data.len() > max_len {
            log::error!("{label} data too large, {} > {}", data.len(), max_len);
            return;
        }
        let Ok(data) = symmetric_crypt(&data, true) else { return; };
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
            let committed = written.and_then(|_| {
                for attempt in 0..3 {
                    match fs::rename(&temporary, &path) {
                        Ok(()) => return Ok(()),
                        Err(error) => {
                            if !cfg!(windows) || attempt == 2 {
                                return Err(error);
                            }
                            std::thread::sleep(std::time::Duration::from_millis(25));
                        }
                    }
                }
                unreachable!()
            });
            if committed.is_err() {
                let _ = fs::remove_file(&temporary);
            }
            committed
        })();
        if let Err(error) = result {
            log::error!("Failed to store {label} cache: {error}");
        }
    }

    pub fn store_recent(json: String) {
        let path = Self::recent_path();
        if json.is_empty() {
            let _ = fs::remove_file(&path);
            return;
        }
        Self::store_encrypted_atomic(path, &json, "recent");
    }

    pub fn load_recent() -> String {
        if let Ok(mut file) = std::fs::File::open(Self::recent_path()) {
            let mut data = vec![];
            if file.read_to_end(&mut data).is_ok() {
                if let Ok(data) = symmetric_crypt(&data, false) {
                    let data = decompress(&data);
                    return String::from_utf8_lossy(&data).into_owned();
                }
            }
        }
        String::new()
    }

    pub fn store(json: String) {
        Self::store_encrypted_atomic(Self::path(), &json, "address book");
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
    edit(
        "src/client.rs",
        "        config.info = serde;\n        let password = self.password.clone();",
        "        crate::geodomdesk_recent::mark_new_peer(&mut config);\n"
        "        config.info = serde;\n        let password = self.password.clone();",
    )
    edit(
        "src/ui_interface.rs",
        "pub fn peer_has_password(id: String) -> bool {\n    !PeerConfig::load(&id).password.is_empty()\n}",
        "pub fn peer_has_password(id: String) -> bool {\n"
        "    let mut c = PeerConfig::load(&id);\n"
        "    crate::geodomdesk_recent::apply_settings(&id, &mut c);\n"
        "    !c.password.is_empty()\n}",
    )
    edit(
        "src/ui_interface.rs",
        "pub fn set_peer_option(id: String, name: String, value: String) {\n    let mut c = PeerConfig::load(&id);",
        "pub fn set_peer_option(id: String, name: String, value: String) {\n"
        "    let mut c = PeerConfig::load(&id);\n"
        "    crate::geodomdesk_recent::hydrate_for_edit(&id, &mut c);",
    )
    ffi = "src/flutter_ffi.rs"
    edit(
        ffi,
        "pub fn main_set_common(_key: String, _value: String) {\n",
        """pub fn main_set_common(_key: String, _value: String) {
    if _key == "geodom-recent-cache" {
        crate::geodomdesk_recent::save_cache(&_value);
        return;
    }
""",
    )
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
                let profile = crate::geodomdesk_recent::export_record(&id, time, &peer);
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
    edit(book, "    if (desktopType == DesktopType.main) {\n      Timer.periodic",
         "    if (!kIsWeb && (isMobile || desktopType == DesktopType.main)) {\n      Timer.periodic")
    edit(book, "          if (!current.initialized || !current.canWrite()) return;",
         "          recentDevices.schedule();\n"
         "          if (desktopType != DesktopType.main || !current.initialized || !current.canWrite()) return;")
    edit(book, "    _pulledOnce = true;\n  }", "    _pulledOnce = true;\n    recentDevices.schedule();\n  }")
    edit(
        book,
        """      if (!shouldSyncAb()) return;
      final recents = await getRecentPeers();
      if (recents.isEmpty) return;""",
        """      if (recentDevices.supported.value || !shouldSyncAb()) return;
      final recents = await getRecentPeers();
      if (recentDevices.supported.value || recents.isEmpty) return;""",
    )
    edit(book, "        if (!legacyMode.value && _maxPeerOneAb == 0) {\n          await _getAbSettings(quiet: quiet);\n        }",
         "        if (legacyMode.value) {\n          recentDevices.resetSupport();\n        } else if (_maxPeerOneAb == 0) {\n          await _getAbSettings(quiet: quiet);\n        }")
    edit(
        book,
        "    _pulledOnce = false;\n    await bind.mainClearAb();",
        "    _pulledOnce = false;\n    _maxPeerOneAb = 0;\n    recentDevices.resetSupport();\n    await bind.mainSetCommon(key: _recentCacheKey, value: '');\n    await bind.mainClearAb();\n    await bind.mainLoadRecentPeers();",
    )
    edit(book, "  Future<bool> _getAbSettings({required bool quiet}) async {\n    int? statusCode;",
         "  Future<bool> _getAbSettings({required bool quiet}) async {\n"
         "    final token = bind.mainGetLocalOption(key: 'access_token');\n    int? statusCode;")
    edit(book, "        debugPrint(\"HTTP 404, api server doesn't support shared address book\");",
         "        await recentDevices.setSupport(false, token);\n"
         "        debugPrint(\"HTTP 404, api server doesn't support shared address book\");")
    edit(book, "      _maxPeerOneAb = json['max_peer_one_ab'] ?? 0;",
         "      if (token != bind.mainGetLocalOption(key: 'access_token')) return false;\n"
         "      _maxPeerOneAb = json['max_peer_one_ab'] ?? 0;\n"
         "      await recentDevices.setSupport(\n"
         "          !kIsWeb && Uri.parse(api).scheme == 'https' && (json['geodom_recent_store'] == 1 || json['geodom_recent_sync'] == 2), token);\n"
         "      if (token != bind.mainGetLocalOption(key: 'access_token')) return false;")

    # Hide upstream's "Sync with recent sessions" inside Address Book when dedicated recent sync is active,
    # so recent sessions never get copied back into My Address Book.
    widget = "flutter/lib/common/widgets/address_book.dart"
    edit(
        widget,
        "      if (canWrite) syncMenuItem(),",
        "      if (canWrite && !isMobile && !gFFI.abModel.recentDevices.supported.value)\n"
        "        syncMenuItem(),",
    )

    # Desktop settings: dedicated Recent sessions sync card in General settings (separate from Address Book).
    desktop_settings = "flutter/lib/desktop/pages/desktop_setting_page.dart"
    edit(
        desktop_settings,
        "        if (!isWeb) importOfficialUserData(),",
        "        if (!isWeb)\n"
        "          Obx(() => gFFI.abModel.recentDevices.supported.value\n"
        "              ? recentSessionsSync()\n"
        "              : const SizedBox.shrink()),\n"
        "        if (!isWeb) importOfficialUserData(),",
    )
    edit(
        desktop_settings,
        "  Widget importOfficialUserData() {",
        """  Widget recentSessionsSync() {
    final recent = gFFI.abModel.recentDevices;
    final value = recent.enabled.obs;
    return _Card(title: 'Recent sessions', children: [
      Obx(() => GestureDetector(
            onTap: () async {
              final next = !value.value;
              value.value = next;
              await recent.setEnabled(next);
              value.value = recent.enabled;
            },
            child: Row(
              children: [
                Checkbox(
                  value: value.value,
                  onChanged: (next) async {
                    if (next == null) return;
                    value.value = next;
                    await recent.setEnabled(next);
                    value.value = recent.enabled;
                  },
                ).marginOnly(right: 5),
                Expanded(
                  child: Text(translate('Sync recent devices and credentials')),
                ),
              ],
            ).marginOnly(left: _kCheckBoxLeftMargin),
          )),
      Align(
        alignment: Alignment.centerLeft,
        child: Text(
          translate('Share recent sessions, connection preferences, and remembered credentials across signed-in devices.'),
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ).marginOnly(left: _kContentHMargin, top: 4, bottom: 4),
    ]);
  }

  Widget importOfficialUserData() {""",
    )

    # Mobile settings: dedicated Recent sessions sync toggle (separate from Address Book).
    mobile_settings = "flutter/lib/mobile/pages/settings_page.dart"
    edit(
        mobile_settings,
        """        SettingsSection(
          title: Text(translate("About")),""",
        """        if (!isWeb && gFFI.abModel.recentDevices.supported.value)
          SettingsSection(
            title: Text(translate("Recent sessions")),
            tiles: [
              SettingsTile.switchTile(
                title: Text(translate('Sync recent devices and credentials')),
                initialValue: gFFI.abModel.recentDevices.enabled,
                onToggle: (v) async {
                  await gFFI.abModel.recentDevices.setEnabled(v);
                  if (mounted) setState(() {});
                },
              ),
            ],
          ),
        SettingsSection(
          title: Text(translate("About")),""",
    )

    # Recent sessions tab: trigger dedicated recent sync when opened/refreshed and sync removals.
    edit(
        "flutter/lib/common/widgets/peer_tab_page.dart",
        """    _TabEntry(RecentPeersView(
      menuPadding: _menuPadding(),
    )),""",
        """    _TabEntry(
        RecentPeersView(
          menuPadding: _menuPadding(),
        ),
        ({dynamic hint}) =>
            gFFI.abModel.recentDevices.schedule(force: hint == null)),""",
    )
    edit(
        "flutter/lib/common/widgets/peer_tab_page.dart",
        """              case 0:
                for (var p in peers) {
                  await bind.mainRemovePeer(id: p.id);
                }
                bind.mainLoadRecentPeers();
                break;""",
        """              case 0:
                for (var p in peers) {
                  await bind.mainRemovePeer(id: p.id);
                }
                await gFFI.abModel.recentDevices
                    .removeMany(peers.map((p) => p.id));
                await bind.mainLoadRecentPeers();
                break;""",
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
        final dedicated = recent.supported.value;
        final useAddressBook = !dedicated || tab == PeerTabIndex.ab;
        final succ = !useAddressBook ||
            await gFFI.abModel.changePersonalHashPassword(id, '');
        await bind.mainForgetPassword(id: id);
        if (dedicated && !useAddressBook && recent.enabled) {
          await recent.forgetCredential(id);
        }""",
    )
    http_service = "flutter/lib/utils/http_service.dart"
    edit(
        http_service,
        "class HttpService {\n",
        """class HttpService {
  HttpService({bool reuseConnection = false})
      : _reuseConnection = reuseConnection;

  final bool _reuseConnection;
  http.Client? _client;

  void close() {
    _client?.close();
    _client = null;
  }

""",
    )
    edit(
        http_service,
        "    final client = http.Client();",
        """    final client =
        _reuseConnection ? (_client ??= http.Client()) : http.Client();""",
    )
    edit(
        http_service,
        """      return response;
    } finally {
      client.close();
    }""",
        """      return response;
    } catch (_) {
      if (_reuseConnection) close();
      rethrow;
    } finally {
      if (!_reuseConnection) client.close();
    }""",
    )
    edit(
        http_service,
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
        '        ("Sync recent devices and credentials", "同步最近访问的设备与凭据"),\n'
        '        ("Share recent sessions, connection preferences, and remembered credentials across signed-in devices.", "在同一账户开启同步的设备间共享最近访问、连接设置和已记住的设备凭据。"),',
    )
