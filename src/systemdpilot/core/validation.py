"""Input validation for unit names."""

from __future__ import annotations

import re

from .errors import InvalidUnitName

UNIT_TYPES = (
    "service",
    "socket",
    "target",
    "device",
    "mount",
    "automount",
    "swap",
    "timer",
    "path",
    "slice",
    "scope",
)

# Allowed characters per systemd.unit(5): ASCII letters, digits, ":", "-", "_", ".", "\".
# "@" separates a template from its instance.
_NAME_RE = re.compile(r"^[A-Za-z0-9:_.\\-]+(@[A-Za-z0-9:_.\\-]*)?\.(" + "|".join(UNIT_TYPES) + r")$")
MAX_NAME_LENGTH = 255


def validate_unit_name(name: str) -> str:
    """Return ``name`` unchanged if it is a well-formed unit name, else raise."""
    if not name:
        raise InvalidUnitName(name, "the name is empty")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidUnitName(name, "the name is too long")
    if name.startswith(("-", ".")):
        raise InvalidUnitName(name, "the name must not start with “-” or “.”")
    if not _NAME_RE.match(name):
        raise InvalidUnitName(
            name,
            "only letters, digits and “:-_.\\@” are allowed, and the name must end in a unit type such as “.service”",
        )
    return name


def normalize_service_name(name: str) -> str:
    """Append ``.service`` if no unit type was given, then validate."""
    name = name.strip()
    if not any(name.endswith("." + t) for t in UNIT_TYPES):
        name += ".service"
    return validate_unit_name(name)
