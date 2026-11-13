part of 'ab_model.dart';

const _recentSyncAccount = 'geodom-recent-sync-account';
const _recentSyncRemovals = 'geodom-recent-sync-removals';
const _recentSyncForgotten = 'geodom-recent-sync-forgotten';

class GeodomRecentSync {
  GeodomRecentSync(this.model);

  final AbModel model;
  final supported = false.obs;
  bool _running = false;
  String? _savedState;
  String? _savedToken;
  DateTime _next = DateTime.fromMillisecondsSinceEpoch(0);

  String get _account => gFFI.userModel.userName.value;
  String get _token => bind.mainGetLocalOption(key: 'access_token');

  bool get enabled =>
      !kIsWeb &&
      supported.value &&
      gFFI.userModel.isLogin &&
      _account.isNotEmpty &&
      bind.mainGetLocalOption(key: _recentSyncAccount) == _account;

  Future<void> setEnabled(bool value) async {
    final account = _account;
    final token = _token;
    if (value && (!supported.value || account.isEmpty || token.isEmpty)) return;
    final previousOwner = bind.mainGetLocalOption(key: _recentSyncAccount);
    if (previousOwner != account) {
      await bind.mainSetLocalOption(key: _recentSyncRemovals, value: '');
      await bind.mainSetLocalOption(key: _recentSyncForgotten, value: '');
    }
    if (_account != account || _token != token) return;
    await bind.mainSetLocalOption(
        key: _recentSyncAccount, value: value ? account : '');
    _savedState = null;
    _savedToken = null;
    _next = DateTime.fromMillisecondsSinceEpoch(0);
    await bind.mainLoadRecentPeers();
    schedule();
  }

  Map<String, int> _pending(String key) {
    try {
      final value = jsonDecode(bind.mainGetLocalOption(key: key));
      return Map<String, int>.from(value as Map);
    } catch (_) {
      return {};
    }
  }

  Future<void> remove(String id) async {
    if (!enabled) return;
    final book = model.addressbooks[_personalAddressBookName];
    if (book is! Ab || !book.peers.any((peer) => peer.id == id)) return;
    await _queue(_recentSyncRemovals, id);
  }

  Future<void> forgetCredential(String id) => _queue(_recentSyncForgotten, id);

  Future<void> _queue(String key, String id) async {
    if (!enabled) return;
    final pending = _pending(key);
    pending[id] = DateTime.now().millisecondsSinceEpoch;
    await bind.mainSetLocalOption(key: key, value: jsonEncode(pending));
    _next = DateTime.fromMillisecondsSinceEpoch(0);
    schedule();
  }

  void schedule() {
    if (_running || !enabled || DateTime.now().isBefore(_next)) return;
    final book = model.addressbooks[_personalAddressBookName];
    if (book is! Ab || !book.initialized || book.abLoading.value) return;
    _running = true;
    _next = DateTime.now().add(const Duration(seconds: 30));
    unawaited(_sync(book).whenComplete(() => _running = false));
  }

  int _timestamp(String profile) {
    try {
      return (jsonDecode(profile) as Map)['timestamp'] as int;
    } catch (_) {
      return 0;
    }
  }

  Future<void> _sync(Ab book) async {
    final account = _account;
    final token = _token;
    bool current() => enabled && _account == account && _token == token;
    try {
      final base = Uri.parse(await bind.mainGetApiServer());
      if (token.isEmpty || !current() || base.scheme != 'https') return;
      final headers = getHttpHeaders();
      headers['Content-Type'] = 'application/json';
      if (!await _refreshPeers(book, current)) return;

      final saved = {for (final peer in book.peers) peer.id: peer};
      final pending = _pending(_recentSyncRemovals);
      final forgotten = _pending(_recentSyncForgotten);
      final acknowledged = <String, int>{};
      final cleared = <String, int>{};
      var wrote = false;
      Future<bool> send(Map<String, dynamic> update,
          {required bool add}) async {
        if (!current()) return false;
        final action = add ? 'add' : 'update';
        final prefix = base.path.replaceFirst(RegExp(r'/$'), '');
        final uri = base.replace(
            path: '$prefix/api/ab/peer/$action/${book.profile.guid}');
        final request = add ? http.post : http.put;
        final response =
            await request(uri, headers: headers, body: jsonEncode(update));
        return _jsonDecodeActionResp(response).isEmpty;
      }

      for (final entry in pending.entries.take(32)) {
        final remote = saved[entry.key];
        if (remote == null || entry.value <= _timestamp(remote.geodomRecent)) {
          acknowledged[entry.key] = entry.value;
          continue;
        }
        if (await send({
          'id': entry.key,
          'geodom_recent':
              jsonEncode({'timestamp': entry.value, 'removed': true}),
        }, add: false)) {
          wrote = true;
          acknowledged[entry.key] = entry.value;
        }
      }

      for (final entry in forgotten.entries.take(32)) {
        final remote = saved[entry.key];
        if (remote == null) {
          cleared[entry.key] = entry.value;
          continue;
        }
        final profile = <String, dynamic>{};
        if (_timestamp(remote.geodomRecent) > 0) {
          profile.addAll(jsonDecode(remote.geodomRecent) as Map<String, dynamic>);
        }
        final now = DateTime.now().millisecondsSinceEpoch;
        final remoteRevision = _timestamp(remote.geodomRecent);
        final removalRevision = pending[entry.key] ?? 0;
        final latest = removalRevision > remoteRevision ? removalRevision : remoteRevision;
        if (latest > now + 299000) continue;
        profile['timestamp'] = latest >= now ? latest + 1 : now;
        if (removalRevision > remoteRevision) {
          profile['removed'] = true;
        }
        if (await send({
          'id': entry.key,
          'hash': '',
          'geodom_recent': jsonEncode(profile),
        }, add: false)) {
          wrote = true;
          cleared[entry.key] = entry.value;
        }
      }

      final raw = await bind.mainLoadRecentPeersForAb(filter: '[]');
      final records = raw.isEmpty ? const [] : jsonDecode(raw) as List;
      var changes = 0;
      for (final row in records) {
        if (!current() || changes >= 32) break;
        if (row is! Map<String, dynamic>) continue;
        final peer = Peer.fromJson(row);
        final timestamp = _timestamp(peer.geodomRecent);
        if (timestamp == 0 ||
            pending.containsKey(peer.id) ||
            forgotten.containsKey(peer.id)) continue;
        final previous = saved[peer.id];
        if (previous != null && timestamp <= _timestamp(previous.geodomRecent))
          continue;
        final update = <String, dynamic>{
          'id': peer.id,
          'username': peer.username,
          'hostname': peer.hostname,
          'platform': peer.platform,
          'alias': peer.alias,
          'geodom_recent': peer.geodomRecent,
          if (_savedCredential(peer.hash)) 'hash': peer.hash,
        };
        changes++;
        if (await send(update, add: previous == null)) wrote = true;
      }

      if (!current()) return;
      if (wrote && !await _refreshPeers(book, current)) return;
      if (!current()) return;
      final latest = {for (final peer in book.peers) peer.id: peer};
      cleared.removeWhere((id, _) => latest[id]?.hash.isNotEmpty == true);
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
      final state = jsonEncode(model._serializeCache());
      if (_savedState != state || _savedToken != token) {
        model._saveCache();
        _savedState = state;
        _savedToken = token;
        await bind.mainLoadRecentPeers();
      }
    } catch (_) {
      // Local recent records remain available while the account API is offline.
    }
  }

  Future<bool> _refreshPeers(Ab book, bool Function() current) async {
    final fetched = <Peer>[];
    final ok = await book._fetchPeers(fetched, quiet: true, active: current);
    if (!ok || !current()) return false;
    final online = {for (final peer in book.peers) peer.id: peer.online};
    for (final peer in fetched) {
      peer.online = online[peer.id] ?? false;
    }
    book.peers.value = fetched;
    return true;
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
