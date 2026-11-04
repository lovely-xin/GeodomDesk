"""Compose client changes, merge upstream and stamp the official version/build revision."""
import json
import re
import subprocess
from pathlib import Path

from channel import make_release_id
from . import account_login, branding, identity, update_channel, upstream
from .edits import SourceEdits, replace_once

ROOT = Path(__file__).resolve().parents[1]


def prepare(source, version, build_number=1):
    source = source.resolve()
    release_id = make_release_id(version, build_number)
    cfg = json.loads((ROOT/'client-channel.json').read_text(encoding='utf-8'))
    baseline = upstream.load_baseline(ROOT)
    changes = SourceEdits({name: data.decode('utf-8') for name, data in baseline.items()})
    identity.apply(changes)
    account_login.apply(changes)
    update_channel.apply(changes, cfg)
    changes['src/geodomdesk_update.rs'] = (ROOT/'client/geodomdesk_update.rs').read_text(encoding='utf-8')
    changes['src/geodomdesk_auth.rs'] = (ROOT/'client/geodomdesk_auth.rs').read_text(encoding='utf-8')
    files = {name: text.encode('utf-8') for name, text in changes.items()}
    files.update(branding.assets(ROOT))
    merged = upstream.merge(source, baseline, files)
    cargo = merged['Cargo.toml'].decode()
    actual = re.search(r'(?m)^version\s*=\s*"([^"]+)"', cargo).group(1)
    if actual != version:
        raise ValueError('Official tag/version mismatch: '+actual+' != '+version)
    portable = merged['libs/portable/Cargo.toml'].decode()
    portable_version = re.search(r'(?m)^version\s*=\s*"([^"]+)"',portable).group(1)
    merged['libs/portable/Cargo.toml'] = re.sub(r'(?m)^version\s*=\s*"[^"]+"', 'version = "'+version+'"', portable, count=1).encode()
    merged['Cargo.lock'] = replace_once(merged['Cargo.lock'].decode(),
        'name = "rustdesk-portable-packer"\nversion = "'+portable_version+'"',
        'name = "rustdesk-portable-packer"\nversion = "'+version+'"').encode()
    merged['flutter/pubspec.yaml'] = re.sub(r'(?m)^version:.*$', 'version: '+version+'+'+str(build_number), merged['flutter/pubspec.yaml'].decode(), count=1).encode()
    merged['src/geodomdesk_update.rs'] = replace_once(merged['src/geodomdesk_update.rs'].decode(),
        'const BUILD_NUMBER: u64 = 1;', 'const BUILD_NUMBER: u64 = '+str(build_number)+';').encode()
    upstream_commit = cfg['upstream_commit']
    if (source/'.git').exists():
        upstream_commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
    for name, data in merged.items():
        path = source/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
    (source/'geodomdesk-build-config.json').write_bytes(json.dumps({**cfg,'version':version,
        'build_number':build_number,'release_id':release_id,'upstream_commit':upstream_commit},indent=2).encode())
    print('Prepared GeodomDesk '+version+' (custom build '+str(build_number)+')')
