"""Patch the pinned TURN crate without replacing the other WebRTC crates."""

import hashlib
import io
import os
import re
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

from .edits import replace_once

REPOSITORY = "https://github.com/rustdesk-org/webrtc"
REVISION = "49c89bd8d30e6e62e4c1a96cee38fdc11f79ef63"
ARCHIVE_SHA256 = "751c4ccdc08cb3a941ed36df05973b833ce6072d72d72da14cac27338874be5c"
VENDOR = "third_party/geodomdesk-turn"
TEST = Path(__file__).with_name("turn_permission_test.rs")


def pinned_source():
    supplied = os.environ.get("GEODOMDESK_WEBRTC_ARCHIVE")
    if supplied:
        with open(supplied, "rb") as archive:
            data = archive.read()
    else:
        url = f"https://codeload.github.com/rustdesk-org/webrtc/tar.gz/{REVISION}"
        with urllib.request.urlopen(url, timeout=60) as archive:
            data = archive.read()
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Unexpected pinned WebRTC source archive")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for entry in archive:
            parts = PurePosixPath(entry.name).parts
            if not entry.isfile() or parts[:2] != (f"webrtc-{REVISION}", "turn"):
                continue
            name = PurePosixPath(*parts[2:])
            if ".." in name.parts:
                raise ValueError("Invalid path in TURN archive")
            files[name.as_posix()] = archive.extractfile(entry).read()
    return files


def apply(files):
    cargo = files["Cargo.toml"].decode()
    files["Cargo.toml"] = (cargo + f'\n[patch."{REPOSITORY}"]\nturn = {{ path = "{VENDOR}" }}\n').encode()
    files["Cargo.lock"] = replace_once(
        files["Cargo.lock"].decode(),
        'name = "turn"\nversion = "0.10.0"\n'
        f'source = "git+{REPOSITORY}?rev={REVISION}#{REVISION}"\n',
        'name = "turn"\nversion = "0.10.0"\n',
    ).encode()
    vendor = pinned_source()

    def edit(name, before, after):
        vendor[name] = replace_once(vendor[name].decode(), before, after).encode()

    for name in ("util", "stun"):
        edit("Cargo.toml", f'path = "../{name}"', f'git = "{REPOSITORY}", rev = "{REVISION}"')
    vendor["Cargo.toml"] += b"\n[workspace]\n"
    edit("src/error.rs", '    #[error("try again")]\n',
         '    #[error("TURN peer forbidden (403)")]\n    ErrPeerForbidden,\n    #[error("try again")]\n')
    path = "src/client/relay_conn.rs"
    edit(path, "use std::io;\nuse std::net::SocketAddr;",
         "use std::collections::HashSet;\nuse std::io;\nuse std::net::{IpAddr, SocketAddr};")
    edit(path, "const MAX_RETRY_ATTEMPTS: u16 = 3;",
         "const MAX_RETRY_ATTEMPTS: u16 = 3;\nconst MAX_DENIED_PEERS: usize = 128;")
    edit(path, "    perm_map: PermissionMap,", "    perm_map: PermissionMap,\n    denied_peers: HashSet<IpAddr>,")
    edit(path, "            perm_map: PermissionMap::new(),",
         "            perm_map: PermissionMap::new(),\n            denied_peers: HashSet::new(),")
    edit(path, "        } else {\n            let perm = Arc::new(Permission::default());",
         "        } else {\n"
         "            if self.denied_peers.contains(&addr.ip()) {\n"
         "                return Err(Error::ErrPeerForbidden);\n"
         "            }\n            let perm = Arc::new(Permission::default());")
    edit(path, "                self.perm_map.delete(&addr);\n                return Err(err);",
         "                self.perm_map.delete(&addr);\n"
         "                if err == Error::ErrPeerForbidden && self.denied_peers.len() < MAX_DENIED_PEERS {\n"
         "                    self.denied_peers.insert(addr.ip());\n"
         "                }\n                return Err(err);")
    relay = vendor[path].decode()
    start = relay.index("    async fn create_permissions(")
    end = relay.index("    pub fn set_nonce_from_msg(", start)
    permission = replace_once(
        relay[start:end],
        '            } else {\n                return Err(Error::Other(format!("{} (error {})", res.typ, code)));',
        "            } else if code.code == CODE_FORBIDDEN {\n"
        "                return Err(Error::ErrPeerForbidden);\n"
        '            } else {\n                return Err(Error::Other(format!("{} (error {})", res.typ, code)));',
    )
    vendor[path] = (relay[:start] + permission + relay[end:]).encode()
    vendor["src/client/relay_conn/relay_conn_test.rs"] += TEST.read_bytes()
    files.update({f"{VENDOR}/{name}": data for name, data in vendor.items()})
