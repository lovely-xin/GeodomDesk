"""Pin the reviewed WebRTC dependency for every client target."""

UPSTREAM_URL = b"https://github.com/rustdesk-org/webrtc"
UPSTREAM_COMMIT = b"49c89bd8d30e6e62e4c1a96cee38fdc11f79ef63"
FORK_URL = b"https://github.com/lovely-xin/GeodomDesk"


def apply(files, commit):
    fork_commit = commit.encode("ascii")

    def manifest_pin(url, revision):
        return b'git = "' + url + b'", rev = "' + revision + b'"'

    def lock_pin(url, revision):
        return b'git+' + url + b'?rev=' + revision + b'#' + revision

    for name, pin in (
        ("Cargo.toml", manifest_pin),
        ("libs/hbb_common/Cargo.toml", manifest_pin),
        ("Cargo.lock", lock_pin),
    ):
        source = files[name]
        old = pin(UPSTREAM_URL, UPSTREAM_COMMIT)
        if old not in source or UPSTREAM_URL in source.replace(old, b""):
            raise ValueError("Upstream changed the WebRTC dependency in " + name)
        files[name] = source.replace(old, pin(FORK_URL, fork_commit))
