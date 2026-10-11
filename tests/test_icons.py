"""Every icon the app names is bundled in its GResource, under a name no icon theme has.

GTK looks an icon name up in the system's icon theme before the app's own
resources, so a bundled "view-refresh-symbolic" would lose to Breeze's or
Yaru's. Names of the app's own can only be found in its bundle.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PREFIX = "systemdpilot-"
APP_ICON = "io.github.mfat.systemdpilot"
# A quoted icon name in Python, or an element's text in a .ui file.
_NAME_RE = re.compile(r"""(?<=["'>])([a-z0-9.-]+-symbolic)(?=["'<])""")
_ALIAS_RE = re.compile(r'<file alias="icons/scalable/actions/([^"]+)\.svg">([^<]+)</file>')


def _named_icons() -> dict[str, list[str]]:
    """Icon names in the source, with the files naming them."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*")):
        if path.suffix in (".py", ".ui"):
            for name in _NAME_RE.findall(path.read_text()):
                if not name.startswith(APP_ICON):
                    found.setdefault(name, []).append(str(path.relative_to(ROOT)))
    return found


def _bundled_icons() -> dict[str, str]:
    """Icon name → source file, from the GResource manifest."""
    manifest = (SRC / "systemdpilot.gresource.xml").read_text()
    return dict(_ALIAS_RE.findall(manifest))


def test_icon_names_are_the_apps_own():
    foreign = {name: files for name, files in _named_icons().items() if not name.startswith(PREFIX)}
    assert not foreign, f"name these {PREFIX}… and bundle them: {foreign}"


def test_named_icons_are_bundled():
    bundled = _bundled_icons()
    missing = {name: files for name, files in _named_icons().items() if name not in bundled}
    assert not missing, f"add these to systemdpilot.gresource.xml: {missing}"


def test_bundled_icons_exist_and_are_used():
    named = _named_icons()
    for name, source in _bundled_icons().items():
        assert (SRC / source).is_file(), f"{name}: no file at {source}"
        assert name.startswith(PREFIX), f"{name}: bundled icons use the {PREFIX} prefix"
        assert name in named, f"{name} is bundled but nothing names it"
