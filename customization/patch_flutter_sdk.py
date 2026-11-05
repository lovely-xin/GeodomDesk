"""Apply the official SDK fix, including when the Actions cache already contains it."""

import shutil
import subprocess
from pathlib import Path


def main():
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


if __name__ == "__main__":
    main()
