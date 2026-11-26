"""Prepare the pinned Flutter SDK with the upstream RustDesk fixes."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlretrieve
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release.channel import sha256


WINDOWS_ENGINE_URL = "https://github.com/rustdesk/engine/releases/download/main/windows-x64-release.zip"
WINDOWS_ENGINE_SHA256 = "ec8cabf36ee4ff24c8d98de25b00e70781eb03876265aee84d0fe554a110036e"


def install_windows_engine(sdk, archive):
    subprocess.run(["cmd.exe", "/d", "/c", str(sdk / "bin/flutter.bat"), "precache", "--windows"], check=True)
    with TemporaryDirectory(prefix="geodomdesk-flutter-engine-") as temporary:
        if archive is None:
            archive = Path(temporary) / "windows-x64-release.zip"
            urlretrieve(WINDOWS_ENGINE_URL, archive)
        if sha256(archive) != WINDOWS_ENGINE_SHA256:
            raise ValueError("RustDesk Windows compatibility engine checksum mismatch")
        destination = sdk / "bin/cache/artifacts/engine/windows-x64-release"
        with ZipFile(archive) as package:
            package.extractall(destination)
    print("Installed the verified RustDesk legacy x64 Flutter engine")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-legacy", action="store_true")
    parser.add_argument("--engine-archive", type=Path)
    args = parser.parse_args()
    if args.engine_archive is not None and not args.windows_legacy:
        parser.error("--engine-archive requires --windows-legacy")
    sdk = Path(shutil.which("flutter")).resolve().parent.parent
    patch = Path(".github/patches/flutter_3.24.4_dropdown_menu_enableFilter.diff").resolve()
    command = ["git", "-C", str(sdk), "apply"]
    check = subprocess.run(
        command + ["--check", str(patch)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if check.returncode == 0:
        subprocess.run(command + [str(patch)], check=True)
        print("Applied official Flutter SDK fix")
    else:
        subprocess.run(command + ["--reverse", "--check", str(patch)], check=True)
        print("Official Flutter SDK fix already applied")
    if args.windows_legacy:
        install_windows_engine(sdk, args.engine_archive)


if __name__ == "__main__":
    main()
