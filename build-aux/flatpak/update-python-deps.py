#!/usr/bin/env python3
"""Regenerate python3-deps.json, the Flatpak module with the Python dependencies.

Picks the newest release of each package from PyPI and, for compiled
packages, the manylinux wheels matching the runtime's Python for x86_64
and aarch64. Run it after bumping the runtime or to update dependencies:

    build-aux/flatpak/update-python-deps.py --python 3.14
"""

import argparse
import json
import re
import urllib.request
from pathlib import Path

# paramiko and everything it needs, in install order.
PACKAGES = ["pycparser", "cffi", "bcrypt", "cryptography", "pynacl", "invoke", "paramiko"]
ARCHES = ["x86_64", "aarch64"]


def pypi(package: str) -> dict:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json") as response:
        return json.load(response)


def pick_wheels(files: list[dict], python: str) -> dict[str, dict] | None:
    """Return {arch: file} for compiled wheels, or {"any": file} for pure ones."""
    pure = [f for f in files if f["filename"].endswith("-py3-none-any.whl")]
    if pure:
        return {"any": pure[0]}

    tag = "cp" + python.replace(".", "")
    chosen = {}
    for arch in ARCHES:
        candidates = []
        for f in files:
            name = f["filename"]
            if not name.endswith(".whl") or f"_{arch}" not in name or "musllinux" in name:
                continue
            m = re.search(r"-(cp\d+)-(cp\d+t?|abi3)-(manylinux.*)\.whl$", name)
            if not m:
                continue
            py, abi, _platform = m.groups()
            if abi == tag or (abi == "abi3" and int(py[2:]) <= int(tag[2:])):
                # Prefer an exact match over abi3, and newer abi3 baselines.
                candidates.append((abi == tag, int(py[2:]), name, f))
        if not candidates:
            return None
        chosen[arch] = max(candidates, key=lambda c: c[:3])[3]
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default="3.14", help="Python version of the Flatpak runtime")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("python3-deps.json"))
    args = parser.parse_args()

    sources = []
    for package in PACKAGES:
        info = pypi(package)
        version = info["info"]["version"]
        wheels = pick_wheels(info["urls"], args.python)
        if wheels is None:
            raise SystemExit(f"No suitable wheels for {package} {version}")
        for arch, f in wheels.items():
            source = {"type": "file", "url": f["url"], "sha256": f["digests"]["sha256"]}
            if arch != "any":
                source["only-arches"] = [arch]
            sources.append(source)
        print(f"{package} {version}: {', '.join(f['filename'] for f in wheels.values())}")

    module = {
        "name": "python3-deps",
        "buildsystem": "simple",
        "build-commands": [
            "pip3 install --verbose --prefix=/app --no-deps --no-build-isolation "
            '--no-index --find-links="file://${PWD}" ' + " ".join(PACKAGES)
        ],
        "sources": sources,
    }
    args.output.write_text(json.dumps(module, indent=4) + "\n")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
