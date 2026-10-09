#!/usr/bin/env python3
"""Check that every place that states the version agrees with meson.build.

With a tag argument (e.g. "v4.0.0"), also check the tag matches.
"""

import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent


def find(path: str, pattern: str) -> str:
    match = re.search(pattern, (root / path).read_text(), re.MULTILINE)
    if not match:
        sys.exit(f"{path}: no version found")
    return match.group(1)


versions = {
    "meson.build": find("meson.build", r"^\s*version:\s*'([^']+)'"),
    "debian/changelog": find("debian/changelog", r"^systemd-pilot \(([^)-]+)"),
    "packaging/rpm/systemd-pilot.spec": find("packaging/rpm/systemd-pilot.spec", r"^Version:\s*(\S+)"),
    "man page": find("data/systemd-pilot.1", r'"systemd Pilot ([^"]+)"'),
    "metainfo (latest release)": find(
        "data/io.github.mfat.systemdpilot.metainfo.xml.in", r'<release version="([^"]+)"'
    ),
}
if len(sys.argv) > 1:
    versions["git tag"] = sys.argv[1].removeprefix("refs/tags/").removeprefix("v")

expected = versions["meson.build"]
bad = {where: v for where, v in versions.items() if v != expected}
for where, version in versions.items():
    print(f"{where}: {version}")
if bad:
    sys.exit(f"Version mismatch, expected {expected}: {bad}")
