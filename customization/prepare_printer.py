"""Add checksum-pinned upstream printer components to the Windows bundle."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent


def prepare(destination, cache):
    destination, cache = destination.resolve(), cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    for asset in json.loads((ROOT / "config/printer-assets.json").read_text())["assets"]:
        archive = cache / asset["file_name"]
        if archive.is_file():
            data = archive.read_bytes()
        else:
            with urllib.request.urlopen(asset["url"], timeout=60) as response:
                data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise ValueError("Printer component checksum mismatch: " + asset["file_name"])
        archive.write_bytes(data)
        driver = asset["file_name"].startswith("rustdesk_printer_driver_")
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            for member in package.infolist():
                if member.is_dir():
                    continue
                name = member.filename
                if driver:
                    prefix = "rustdesk_printer_driver_v4-1.4/"
                    if not name.startswith(prefix):
                        raise ValueError("Unexpected printer driver layout")
                    target = destination / "drivers/RustDeskPrinterDriver" / name[len(prefix):]
                else:
                    if name != "printer_driver_adapter.dll":
                        raise ValueError("Unexpected printer adapter layout")
                    target = destination / name
                if not target.resolve().is_relative_to(destination):
                    raise ValueError("Printer archive path escapes the bundle")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(package.read(member))
    for name in ["drivers/RustDeskPrinterDriver/RustDeskPrinterDriver.inf", "printer_driver_adapter.dll"]:
        if not (destination / name).is_file():
            raise ValueError("Printer component missing: " + name)
    print("Verified printer driver and adapter added to the Windows bundle.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.destination, args.cache)
