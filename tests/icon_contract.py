#!/usr/bin/env python3
"""Verify the app icon resource and independent-project metadata contract.

Usage: icon_contract.py <path-to-app-bundle>
"""

import plistlib
import struct
import sys
from pathlib import Path

APP = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else None
if APP is None or not APP.is_dir():
    print("usage: icon_contract.py <path-to-app-bundle>", file=sys.stderr)
    sys.exit(2)

FAILURES = []
PASSES = []
EXPECTED_INFO = (
    "Pi Launcher is an independent, unofficial launcher for Pi. "
    "It is not affiliated with or endorsed by the Pi maintainers."
)


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


plist_path = APP / "Contents" / "Info.plist"
try:
    with plist_path.open("rb") as f:
        info = plistlib.load(f)
except (OSError, plistlib.InvalidFileException) as exc:
    print(f"icon-contract: cannot read {plist_path}: {exc}", file=sys.stderr)
    sys.exit(1)

check(
    "icon declaration names AppIcon",
    info.get("CFBundleIconFile") == "AppIcon",
    f"got {info.get('CFBundleIconFile')!r}",
)
check(
    "bundle info identifies the independent, unofficial launcher",
    info.get("CFBundleGetInfoString") == EXPECTED_INFO,
    f"got {info.get('CFBundleGetInfoString')!r}",
)
check(
    "copyright metadata carries the non-affiliation disclaimer",
    EXPECTED_INFO in info.get("NSHumanReadableCopyright", ""),
    f"got {info.get('NSHumanReadableCopyright')!r}",
)

icon_path = APP / "Contents" / "Resources" / "AppIcon.icns"
check("declared icon resource exists", icon_path.is_file(), str(icon_path))

icon_types = set()
parse_error = None if icon_path.is_file() else "icon resource is missing"
if icon_path.is_file():
    try:
        data = icon_path.read_bytes()
        if len(data) < 8 or data[:4] != b"icns":
            raise ValueError("missing icns header")
        declared_size = struct.unpack(">I", data[4:8])[0]
        if declared_size != len(data):
            raise ValueError(
                f"header size {declared_size} does not match file size {len(data)}"
            )
        offset = 8
        while offset < len(data):
            if offset + 8 > len(data):
                raise ValueError("truncated chunk header")
            chunk_type = data[offset:offset + 4].decode("ascii")
            chunk_size = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            if chunk_size < 8 or offset + chunk_size > len(data):
                raise ValueError(f"invalid {chunk_type!r} chunk size {chunk_size}")
            icon_types.add(chunk_type)
            offset += chunk_size
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        parse_error = str(exc)

check("icon resource is a valid icns container", parse_error is None, parse_error or "")

# Each entry is a standard macOS point size and its Retina representation.
# iconutil may encode 16/32 px legacy entries as ic04/ic05 or icp4/icp5.
required_entries = {
    "16x16": {"ic04", "icp4"},
    "16x16@2x": {"ic11"},
    "32x32": {"ic05", "icp5"},
    "32x32@2x": {"ic12", "icp6"},
    "128x128": {"ic07"},
    "128x128@2x": {"ic13"},
    "256x256": {"ic08"},
    "256x256@2x": {"ic14"},
    "512x512": {"ic09"},
    "512x512@2x": {"ic10"},
}
missing = [
    label for label, alternatives in required_entries.items()
    if icon_types.isdisjoint(alternatives)
]
check(
    "icon contains every standard and Retina size",
    not missing,
    f"missing={missing} chunks={sorted(icon_types)}",
)

print()
print(f"icon-contract: {len(PASSES)} passed, {len(FAILURES)} failed")
sys.exit(1 if FAILURES else 0)
