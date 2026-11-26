part of 'ab_model.dart';

const _recentSyncAccount = 'geodom-recent-sync-account';
const _recentSyncLocalOwner = 'geodom-recent-sync-local-owner';
const _recentSyncDisabled = 'geodom-recent-sync-disabled';
const _recentSyncRemovals = 'geodom-recent-sync-removals';
const _recentSyncForgotten = 'geodom-recent-sync-forgotten';
const _recentCacheKey = 'geodom-recent-cache';

class GeodomRecentSync with WidgetsBindingObserver {
  GeodomRecentSync(this.model);

  final AbModel model;
  final supported = false.obs;
  final syncing = false.obs;
  bool _running = false;
  bool _probing = false;
  bool _paused = false;
  bool _observerRegistered = false;
  int _consecutiveFailures = 0;
  String? _savedState;
  String? _savedToken;
  String? _supportToken;
  DateTime _next = DateTime.fromMillisecondsSinceEpoch(0);
  DateTime _nextProbe = DateTime.fromMillisecondsSinceEpoch(0);

  void _ensureObserver() {
    if (_observerRegistered) return;
    try {
      WidgetsBinding.instance.addObserver(this);
      _observerRegistered = true;
    } catch (_) {}
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final inactive = state == AppLifecycleState.paused ||
        state == AppLifecycleState.hidden ||
        state == AppLifecycleState.detached;
    final resumed = _paused && !inactive;
    _paused = inactive;
    if (resumed && enabled) {
      _next = DateTime.fromMillisecondsSinceEpoch(0);
      schedule();
    }
  }

  String get _account => gFFI.userModel.userName.value;
  String get _token => bind.mainGetLocalOption(key: 'access_token');

  Set<String> get _disabledAccounts {
    try {
      return (jsonDecode(bind.mainGetLocalOption(key: _recentSyncDisabled))
              as List)
          .whereType<String>()
          .toSet();
    } catch (_) {
      return {};
    }
  }

  bool get _canQueue =>
      !kIsWeb &&
      gFFI.userModel.isLogin &&
      _account.isNotEmpty &&
      _token.isNotEmpty &&
      !_disabledAccounts.contains(_account);

  bool get enabled =>
      supported.value &&
      _canQueue &&
      _supportToken != null &&
      _supportToken == _token;

  Future<void> setSupport(bool available, String token) async {
    final account = _account;
    bool current() => token.isNotEmpty && _token == token && _account == account;
    if (!current()) return;
    if (available && (account.isEmpty || !await _bindAccount(account, current))) return;
    if (available) _ensureObserver();
    if (available && (!supported.value || _supportToken != token)) {
      _consecutiveFailures = 0;
      _next = DateTime.fromMillisecondsSinceEpoch(0);
    }
    _supportToken = available ? token : null;
    supported.value = available;
  }

  void resetSupport() {
    _supportToken = null;
    supported.value = false;
    _savedState = null;
    _savedToken = null;
    _consecutiveFailures = 0;
    _next = DateTime.fromMillisecondsSinceEpoch(0);
    _nextProbe = DateTime.fromMillisecondsSinceEpoch(0);
  }

  Future<void> setEnabled(bool value) async {
    final account = _account;
    final token = _token;
    if (account.isEmpty || token.isEmpty || (value && _supportToken != token)) return;
    final disabled = _disabledAccounts;
    if (value) {
      disabled.remove(account);
    } else {
      disabled.add(account);
    }
    await bind.mainSetLocalOption(
        key: _recentSyncDisabled, value: jsonEncode(disabled.toList()));
    if (_account != account || _token != token) return;
    _savedState = null;
    _savedToken = null;
    _next = DateTime.fromMillisecondsSinceEpoch(0);
    if (!value) {
      await bind.mainSetCommon(key: _recentCacheKey, value: '');
    }
    await bind.mainLoadRecentPeers();
    schedule();
  }

  Future<bool> _bindAccount(String account, bool Function() current) async {
    final previousOwner = bind.mainGetLocalOption(key: _recentSyncAccount);
    if (bind.mainGetLocalOption(key: _recentSyncLocalOwner).isEmpty) {
      await bind.mainSetLocalOption(
          key: _recentSyncLocalOwner,
          value: previousOwner.isEmpty ? account : previousOwner);
    }
    if (previousOwner != account) {
      if (!current()) return false;
      await bind.mainSetLocalOption(key: _recentSyncRemovals, value: '');
      if (!current()) return false;
      await bind.mainSetLocalOption(key: _recentSyncForgotten, value: '');
      if (!current()) return false;
      await bind.mainSetCommon(key: _recentCacheKey, value: '');
      if (!current()) return false;
      await bind.mainSetLocalOption(key: _recentSyncAccount, value: account);
    }
    return current();
  }

  Map<String, int> _pending(String key) {
    try {
      final value = jsonDecode(bind.mainGetLocalOption(key: key));
      return Map<String, int>.from(value as Map);
    } catch (_) {
      return {};
    }
  }

  Future<void> remove(String id) => removeMany([id]);

  Future<void> removeMany(Iterable<String> ids) async {
    if (!_canQueue) return;
    final list = ids.where((id) => id.isNotEmpty).toList();
    if (list.isEmpty) return;
    final pending = _pending(_recentSyncRemovals);
    final now = DateTime.now().millisecondsSinceEpoch;
    for (final id in list) {
      pending[id] = now;
    }
    await bind.mainSetLocalOption(
        key: _recentSyncRemovals, value: jsonEncode(pending));
    _next = DateTime.fromMillisecondsSinceEpoch(0);
    schedule();
  }

  Future<void> forgetCredential(String id) => _queue(_recentSyncForgotten, id);

  Future<void> _queue(String key, String id) async {
    if (!_canQueue) return;
    final pending = _pending(key);
    pending[id] = DateTime.now().millisecondsSinceEpoch;
    await bind.mainSetLocalOption(key: key, value: jsonEncode(pending));
    _next = DateTime.fromMillisecondsSinceEpoch(0);
    schedule();
  }

  Future<void> _probeSupport() async {
    if (_probing || _paused || kIsWeb || !gFFI.userModel.isLogin) return;
    final token = _token;
    if (token.isEmpty || _account.isEmpty || _supportToken == token) return;
    _probing = true;
    _nextProbe = DateTime.now().add(const Duration(minutes: 5));
    try {
      final api = await bind.mainGetApiServer();
      final base = Uri.parse(api);
      if (base.scheme != 'https' || token != _token) return;
      final prefix = base.path.replaceFirst(RegExp(r'/$'), '');
      final uri = base.replace(path: '$prefix/api/ab/settings');
      final response = await http.post(uri, headers: getHttpHeaders());
      if (token != _token) return;
      if (response.statusCode >= 200 && response.statusCode < 300) {
        final decoded = jsonDecode(response.body);
        if (decoded is Map<String, dynamic>) {
          final available = decoded['geodom_recent_store'] == 1 ||
              decoded['geodom_recent_sync'] == 2;
          await setSupport(available, token);
          if (enabled) schedule(force: true);
        }
      }
    } catch (_) {
      // Retry on the next probe interval.
    } finally {
      _probing = false;
    }
  }

  void schedule({bool force = false}) {
    _ensureObserver();
    if (isDesktop && desktopType != DesktopType.main) return;
    if (_paused || _running) return;
    if (!enabled) {
      if (_canQueue &&
          _supportToken != _token &&
          (force || !DateTime.now().isBefore(_nextProbe))) {
        unawaited(_probeSupport());
      }
      return;
    }
    if (!force && DateTime.now().isBefore(_next)) {
      return;
    }
    _running = true;
    syncing.value = true;
    _next = DateTime.now().add(const Duration(seconds: 30));
    unawaited(_sync().whenComplete(() {
      _running = false;
      syncing.value = false;
    }));
  }

  int _timestamp(String profile) {
    try {
      return (jsonDecode(profile) as Map)['timestamp'] as int;
    } catch (_) {
      return 0;
    }
  }

  bool _isRemoved(String profile) {
    try {
      return (jsonDecode(profile) as Map)['removed'] == true;
    } catch (_) {
      return false;
    }
  }

  Future<List<Peer>?> _fetchRecentPeers(
      Uri base, Map<String, String> headers, bool Function() current) async {
    final prefix = base.path.replaceFirst(RegExp(r'/$'), '');
    final fetched = <Peer>[];
    for (var page = 1; page <= 100; page++) {
      if (!current()) return null;
      final uri = base.replace(
        path: '$prefix/api/recent/peers',
        queryParameters: {'current': '$page', 'pageSize': '100'},
      );
      final response = await http.post(uri, headers: headers, body: '{}');
      if (!current()) return null;
      if (response.statusCode < 200 || response.statusCode >= 300) {
        return null;
      }
      final decoded = jsonDecode(response.body);
      if (decoded is! Map<String, dynamic> || decoded['data'] is! List) {
        return null;
      }
      final rows = decoded['data'] as List;
      for (final item in rows) {
        if (item is Map<String, dynamic>) {
          fetched.add(Peer.fromJson(item));
        }
      }
      final total = decoded['total'];
      if (rows.length < 100 || (total is int && page * 100 >= total)) {
        return fetched;
      }
    }
    return fetched;
  }

  Future<void> _sync() async {
    final account = _account;
    final token = _token;
    bool current() => enabled && _account == account && _token == token;
    var synced = false;
    try {
      final base = Uri.parse(await bind.mainGetApiServer());
      if (token.isEmpty || !current() || base.scheme != 'https') return;
      final headers = getHttpHeaders();
      headers['Content-Type'] = 'application/json';
      final initialList = await _fetchRecentPeers(base, headers, current);
      if (initialList == null || !current()) return;

      final saved = {for (final peer in initialList) peer.id: peer};
      final pending = _pending(_recentSyncRemovals);
      final forgotten = _pending(_recentSyncForgotten);
      final acknowledged = <String, int>{};
      final verifyRemoved = <String>{};
      final cleared = <String, int>{};
      final verifyCleared = <String>{};
      var wrote = false;
      var failed = false;
      Future<bool> send(Map<String, dynamic> update) async {
        if (!current()) return false;
        final prefix = base.path.replaceFirst(RegExp(r'/$'), '');
        final uri = base.replace(path: '$prefix/api/recent/peer');
        final response =
            await http.put(uri, headers: headers, body: jsonEncode(update));
        if (response.statusCode < 200 || response.statusCode >= 300) {
          return false;
        }
        return _jsonDecodeActionResp(response).isEmpty;
      }

      final raw = await bind.mainLoadRecentPeersForAb(filter: '[]');
      final records = raw.isEmpty ? const [] : jsonDecode(raw) as List;
      final localTimes = <String, int>{};
      final localHasCred = <String, bool>{};
      for (final row in records) {
        if (row is! Map<String, dynamic>) continue;
        final peer = Peer.fromJson(row);
        final ts = _timestamp(peer.geodomRecent);
        if (ts > 0) localTimes[peer.id] = ts;
        if (_savedCredential(peer.hash)) localHasCred[peer.id] = true;
      }

      for (final entry in pending.entries.take(32)) {
        final remote = saved[entry.key];
        final localTime = localTimes[entry.key] ?? 0;
        if (localTime > entry.value) {
          acknowledged[entry.key] = entry.value;
          continue;
        }
        if (remote == null) {
          acknowledged[entry.key] = entry.value;
          continue;
        }
        final remoteRevision = _timestamp(remote.geodomRecent);
        if (_isRemoved(remote.geodomRecent) && remoteRevision >= entry.value) {
          acknowledged[entry.key] = entry.value;
          continue;
        }
        final now = DateTime.now().millisecondsSinceEpoch;
        if (remoteRevision >= now + 299000) continue;
        final revision = remoteRevision >= entry.value
            ? (remoteRevision >= now ? remoteRevision + 1 : now)
            : entry.value;
        if (await send({
          'id': entry.key,
          'hash': '',
          'geodom_recent':
              jsonEncode({'timestamp': revision, 'removed': true}),
        })) {
          wrote = true;
          acknowledged[entry.key] = entry.value;
          verifyRemoved.add(entry.key);
        } else {
          failed = true;
          break;
        }
      }

      if (!failed) {
        for (final entry in forgotten.entries.take(32)) {
          final localTime = localTimes[entry.key] ?? 0;
          if (localTime > entry.value && (localHasCred[entry.key] ?? false)) {
            cleared[entry.key] = entry.value;
            continue;
          }
          if (verifyRemoved.contains(entry.key)) {
            cleared[entry.key] = entry.value;
            verifyCleared.add(entry.key);
            continue;
          }
          if (pending.containsKey(entry.key) &&
              !acknowledged.containsKey(entry.key)) {
            continue;
          }
          final remote = saved[entry.key];
          if (remote == null) {
            cleared[entry.key] = entry.value;
            continue;
          }
          final remoteRevision = _timestamp(remote.geodomRecent);
          if (_isRemoved(remote.geodomRecent) &&
              remoteRevision >= entry.value &&
              remote.hash.isEmpty) {
            cleared[entry.key] = entry.value;
            continue;
          }
          final profile = <String, dynamic>{};
          if (remoteRevision > 0) {
            profile.addAll(jsonDecode(remote.geodomRecent) as Map<String, dynamic>);
          }
          final now = DateTime.now().millisecondsSinceEpoch;
          if (remoteRevision > now + 299000) continue;
          profile['timestamp'] =
              remoteRevision >= now ? remoteRevision + 1 : now;
          if (await send({
            'id': entry.key,
            'hash': '',
            'geodom_recent': jsonEncode(profile),
          })) {
            wrote = true;
            cleared[entry.key] = entry.value;
            verifyCleared.add(entry.key);
          } else {
            failed = true;
            break;
          }
        }
      }

      var changes = 0;
      if (!failed) {
        for (final row in records) {
          if (!current() || changes >= 32) break;
          if (row is! Map<String, dynamic>) continue;
          final peer = Peer.fromJson(row);
          final timestamp = _timestamp(peer.geodomRecent);
          if (timestamp == 0 ||
              (pending.containsKey(peer.id) && !acknowledged.containsKey(peer.id)) ||
              (forgotten.containsKey(peer.id) && !cleared.containsKey(peer.id))) continue;
          final previous = saved[peer.id];
          if (previous != null && timestamp <= _timestamp(previous.geodomRecent))
            continue;
          final update = <String, dynamic>{
            'id': peer.id,
            'username': peer.username,
            'hostname': peer.hostname,
            'platform': peer.platform,
            if (peer.alias.isNotEmpty ||
                (previous != null && previous.geodomRecent.isNotEmpty))
              'alias': peer.alias,
            'geodom_recent': peer.geodomRecent,
            if (_savedCredential(peer.hash)) 'hash': peer.hash,
          };
          changes++;
          if (await send(update)) {
            wrote = true;
          } else {
            failed = true;
            break;
          }
        }
      }

      if (!current()) return;
      final latestList = wrote
          ? await _fetchRecentPeers(base, headers, current)
          : initialList;
      if (latestList == null || !current()) return;
      final latest = {for (final peer in latestList) peer.id: peer};
      acknowledged.removeWhere((id, _) {
        if (!verifyRemoved.contains(id)) return false;
        final peer = latest[id];
        return peer != null && !_isRemoved(peer.geodomRecent);
      });
      cleared.removeWhere(
          (id, _) => verifyCleared.contains(id) && latest[id]?.hash.isNotEmpty == true);
      for (final completed in {
        _recentSyncRemovals: acknowledged,
        _recentSyncForgotten: cleared,
      }.entries) {
        if (!current()) return;
        if (completed.value.isEmpty) continue;
        final remaining = _pending(completed.key);
        for (final entry in completed.value.entries) {
          if (remaining[entry.key] == entry.value) remaining.remove(entry.key);
        }
        await bind.mainSetLocalOption(
            key: completed.key, value: jsonEncode(remaining));
      }
      if (!current()) return;
      final cacheJson = jsonEncode({
        'access_token': token,
        'peers': latestList
            .map((e) => {
                  ...e.toCustomJson(includingHash: true),
                  'geodom_recent_hash_present': e.geodomRecentHashPresent,
                })
            .toList(),
      });
      if (_savedState != cacheJson || _savedToken != token) {
        await bind.mainSetCommon(key: _recentCacheKey, value: cacheJson);
        if (!current()) return;
        _savedState = cacheJson;
        _savedToken = token;
        await bind.mainLoadRecentPeers();
      }
      if (!failed) synced = true;
    } catch (_) {
      // Local recent records remain available while the account API is offline.
    } finally {
      if (enabled) {
        if (synced) {
          _consecutiveFailures = 0;
          _next = DateTime.now().add(const Duration(seconds: 30));
        } else {
          _consecutiveFailures = (_consecutiveFailures + 1).clamp(1, 4);
          const delays = [30, 60, 120, 300];
          _next = DateTime.now()
              .add(Duration(seconds: delays[_consecutiveFailures - 1]));
        }
      }
    }
  }

  bool _savedCredential(String encoded) {
    if (encoded.length != 44) return false;
    try {
      return base64Decode(encoded).length == 32;
    } on FormatException {
      return false;
    }
  }
}
