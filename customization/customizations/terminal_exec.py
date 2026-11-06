"""Compose the terminal command protocol and TCP recovery with the reviewed baseline."""

import subprocess
import tempfile
from pathlib import Path


def apply(changes, root):
    patch = Path(__file__).with_suffix(".patch")
    paths = [
        line.split(" b/", 1)[1]
        for line in patch.read_text(encoding="utf-8").splitlines()
        if line.startswith("diff --git ")
    ]
    with tempfile.TemporaryDirectory(prefix="geodomdesk-terminal-") as directory:
        staging = Path(directory)
        for name in paths:
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(changes[name].encode("utf-8"))
        subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=staging, check=True)
        for name in paths:
            changes[name] = (staging / name).read_text(encoding="utf-8")

    for name in (
        "bin/geodomdesk-cli.rs",
        "terminal_exec.rs",
        "terminal_exec/cli.rs",
        "terminal_exec/exec_bootstrap.ps1",
        "terminal_exec/windows.rs",
    ):
        changes["src/" + name] = (root / "client" / name).read_text(encoding="utf-8")
