"""Merge reviewed customizations into official source before writing any changes."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def load_baseline(root):
    baseline = {}
    for name, digest in json.loads((root / "source-lock.json").read_text(encoding="utf-8")).items():
        data = (root / "reference" / name).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Customization baseline is damaged: " + name)
        baseline[name] = data
    return baseline


def merge(source, baseline, changes):
    merged = {}
    with tempfile.TemporaryDirectory(prefix="geodomdesk-merge-") as temporary:
        temporary = Path(temporary)
        for name, original in baseline.items():
            path = source / name
            if not path.is_file():
                raise ValueError("Upstream removed a customized source file: " + name)
            current = path.read_bytes().replace(b"\r\n", b"\n")
            changed = changes[name]
            if changed == original:
                merged[name] = current
            elif current == original:
                merged[name] = changed
            else:
                (temporary / "base").write_bytes(original)
                (temporary / "custom").write_bytes(changed)
                (temporary / "current").write_bytes(current)
                result = subprocess.run(
                    [
                        "git",
                        "merge-file",
                        "-p",
                        str(temporary / "current"),
                        str(temporary / "base"),
                        str(temporary / "custom"),
                    ],
                    capture_output=True,
                )
                if result.returncode:
                    raise ValueError(
                        "Upstream/customization conflict: "
                        + name
                        + "; existing releases remain unchanged"
                    )
                merged[name] = result.stdout
    for name, data in changes.items():
        if name in baseline:
            continue
        path = source / name
        if name.endswith(".rs") and path.exists() and path.read_bytes() != data:
            raise ValueError("Upstream added a conflicting custom source file: " + name)
        merged[name] = data
    return merged
