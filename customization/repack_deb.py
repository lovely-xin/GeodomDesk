#!/usr/bin/env python3
"""Isolate an already compiled GeodomDesk DEB from stock RustDesk packaging."""
import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def repack(payload, output, version, build_number=0, cli=None):
    from channel import version_tuple

    version_tuple(version)
    with tempfile.TemporaryDirectory(prefix="geodomdesk-deb-") as tmp:
        root = Path(tmp) / "package"
        subprocess.run(["dpkg-deb", "-R", str(payload), str(root)], check=True)
        original = root / "usr/share/rustdesk"
        if not (original / "rustdesk").is_file():
            raise ValueError("Expected a Flutter DEB built from the prepared source")
        # DRM and third-party helper variants require a separate dependency audit.
        if (root / "usr/lib/rustdesk").exists():
            raise ValueError("This coexist package targets the standard Flutter build, without DRM")
        data = root / "usr/share/geodomdesk"
        original.rename(data)
        (data / "rustdesk").rename(data / "geodomdesk")
        # Rebuild packaging rather than inheriting scripts which stop/remove RustDesk.
        control_path = root / "DEBIAN/control"
        control = control_path.read_text()
        lines = []
        for line in control.splitlines():
            if line.startswith(("Conflicts:", "Replaces:", "Provides:", "Installed-Size:")):
                continue
            if line.startswith("Package:"):
                line = "Package: geodomdesk"
            elif line.startswith("Version:"):
                line = "Version: " + version + (f"+geodom.{build_number}" if build_number else "")
            elif line.startswith("Homepage:"):
                line = "Homepage: https://geodom.cn"
            lines.append(line)
        control_path.write_bytes(("\n".join(lines) + "\n").encode())
        for path in (root / "DEBIAN").iterdir():
            if path.name != "control":
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        scripts = {
            "postinst": """#!/bin/sh
set -e
if [ "$1" = configure ]; then
  systemctl daemon-reload
  systemctl enable --now geodomdesk.service
fi
""",
            "prerm": """#!/bin/sh
set -e
case "$1" in remove|upgrade) systemctl stop geodomdesk.service || true;; esac
case "$1" in remove) systemctl disable geodomdesk.service || true;; esac
""",
            "postrm": """#!/bin/sh
set -e
systemctl daemon-reload || true
""",
        }
        for name, content in scripts.items():
            path = root / "DEBIAN" / name
            path.write_bytes(content.encode())
            path.chmod(0o755)
        for rel in [
            "usr/bin/rustdesk",
            "usr/share/applications/rustdesk.desktop",
            "usr/share/applications/rustdesk-link.desktop",
            "usr/share/icons/hicolor/256x256/apps/rustdesk.png",
            "usr/share/icons/hicolor/scalable/apps/rustdesk.svg",
            "usr/lib/systemd/system/rustdesk.service",
            "usr/share/geodomdesk/files/systemd/rustdesk.service",
        ]:
            (root / rel).unlink(missing_ok=True)
        exe = root / "usr/bin/geodomdesk"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.symlink_to("/usr/share/geodomdesk/geodomdesk")
        if cli is not None:
            cli_path = exe.with_name("geodomdesk-cli")
            shutil.copyfile(cli, cli_path)
            cli_path.chmod(0o755)
        service = root / "usr/lib/systemd/system/geodomdesk.service"
        service.parent.mkdir(parents=True, exist_ok=True)
        service.write_bytes(
            b"""[Unit]
Description=GeodomDesk remote access
After=network.target systemd-user-sessions.service

[Service]
Type=simple
ExecStart=/usr/bin/geodomdesk --service
KillMode=control-group
TimeoutStopSec=30
Restart=on-failure
RestartSec=5
User=root
LimitNOFILE=100000
Environment="PULSE_LATENCY_MSEC=60" "PIPEWIRE_LATENCY=1024/48000"

[Install]
WantedBy=multi-user.target
"""
        )
        desktop = root / "usr/share/applications/geodomdesk.desktop"
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_bytes(
            b"""[Desktop Entry]
Name=GeodomDesk
GenericName=Remote Desktop
Exec=geodomdesk %u
Icon=geodomdesk
Terminal=false
Type=Application
Categories=Network;RemoteAccess;
StartupWMClass=geodomdesk
MimeType=x-scheme-handler/geodomdesk;
"""
        )
        icon = root / "usr/share/icons/hicolor/512x512/apps/geodomdesk.png"
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes((Path(__file__).parent / "assets/geodomdesk.png").read_bytes())
        # No stock package-owned paths may remain. Internal library names stay ABI-compatible.
        for path in root.rglob("*"):
            if path.name.lower().startswith("rustdesk"):
                raise ValueError(
                    "Stock path remains in coexist package: " + str(path.relative_to(root))
                )
        subprocess.run(["dpkg-deb", "--root-owner-group", "-b", str(root), str(output)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-number", type=int, default=0)
    parser.add_argument("--cli", type=Path)
    args = parser.parse_args()
    repack(args.input.resolve(), args.output.resolve(), args.version, args.build_number, args.cli)
