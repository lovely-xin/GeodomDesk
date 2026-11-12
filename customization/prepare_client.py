#!/usr/bin/env python3
"""Apply reviewed client-only customizations to an official RustDesk source release."""
import argparse
from pathlib import Path

from customizations.build import prepare


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-number", type=int, default=1)
    parser.add_argument("--standalone-cli", action="store_true",
                        help="prepare the independent CLI with embedded SSH")
    args = parser.parse_args()
    prepare(args.source, args.version, args.build_number, standalone=args.standalone_cli)
