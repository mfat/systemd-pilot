"""Starting points for new unit files."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import InvalidUnitName
from .validation import UNIT_TYPES, validate_unit_name


@dataclass(frozen=True)
class UnitTemplate:
    id: str
    title: str
    # One file per unit type, in the order they are shown and written.
    files: tuple[tuple[str, str], ...]
    # The unit that is enabled and started: a timer or path unit starts its
    # service itself, so only it is.
    activate: str = "service"

    @property
    def types(self) -> tuple[str, ...]:
        return tuple(unit_type for unit_type, _content in self.files)


_LONG_RUNNING = """[Unit]
Description=My service
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/sleep infinity
Restart=on-failure
{run_as}
[Install]
WantedBy={target}
"""

_ONESHOT = """[Unit]
Description=My task

[Service]
Type=oneshot
ExecStart=/usr/bin/true
RemainAfterExit=yes

[Install]
WantedBy={target}
"""

# The service of a timer or path unit has no [Install] section: it is not
# enabled itself, the timer or path unit starts it.
_TRIGGERED_SERVICE = """[Unit]
Description={description}

[Service]
Type=oneshot
ExecStart=/usr/bin/true
"""

# A timer or path unit starts the service of the same name.
_TIMER = """[Unit]
Description=Run my scheduled task every day

[Timer]
OnCalendar=daily
# Run at the next start if the machine was off at the scheduled time.
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
"""

_PATH = """[Unit]
Description=Watch for the file change

[Path]
PathChanged={watched}

[Install]
WantedBy=paths.target
"""

# Only the system instance can run a service as another user.
_RUN_AS = """# To run as an unprivileged account instead of root:
#User=nobody
#Group=nogroup
"""

TEMPLATES = (
    UnitTemplate("simple", "Long-running service", (("service", _LONG_RUNNING),)),
    UnitTemplate("oneshot", "One-shot task", (("service", _ONESHOT),)),
    UnitTemplate(
        "timer",
        "Scheduled task (timer)",
        (("service", _TRIGGERED_SERVICE.format(description="My scheduled task")), ("timer", _TIMER)),
        activate="timer",
    ),
    UnitTemplate(
        "path",
        "Run when a file changes (path)",
        (("service", _TRIGGERED_SERVICE.format(description="React to a file change")), ("path", _PATH)),
        activate="path",
    ),
)


def render(template: UnitTemplate, user_scope: bool) -> list[tuple[str, str]]:
    """The template's files as ``[(unit type, content)]``."""
    values = {
        "target": "default.target" if user_scope else "multi-user.target",
        "run_as": "" if user_scope else "\n" + _RUN_AS,
        "watched": "%h/watched-file" if user_scope else "/etc/watched-file",
    }
    return [(unit_type, content.format(**values)) for unit_type, content in template.files]


def unit_names(template: UnitTemplate, name: str) -> list[str]:
    """Full unit names for the template's files, from what was typed.

    The type may be left out, or be any of the template's own types:
    "backup", "backup.service" and "backup.timer" all give
    ``["backup.service", "backup.timer"]`` for the timer template.
    """
    base = base_name(template, name)
    return [validate_unit_name(f"{base}.{unit_type}") for unit_type in template.types]


def base_name(template: UnitTemplate, name: str) -> str:
    """``name`` without its unit type, which must be one of the template's."""
    name = name.strip()
    for unit_type in template.types:
        if name.endswith("." + unit_type):
            return name[: -len(unit_type) - 1]
    for unit_type in UNIT_TYPES:
        if name.endswith("." + unit_type):
            made = " and ".join("." + t for t in template.types)
            raise InvalidUnitName(name, f"this template makes {made} units")
    return name


_EXEC_START_RE = re.compile(r"^ExecStart=.*$", re.MULTILINE)


def quote_exec_path(path: str) -> str:
    """``path`` as the program of an ``ExecStart=`` line.

    "%" starts a specifier and "$" a variable, so both are doubled; a path
    with spaces or quotes is put in double quotes, with ``\\`` and ``"``
    escaped.
    """
    path = path.replace("%", "%%").replace("$", "$$")
    if re.search(r"[\s\"'\\]", path):
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return path


def set_exec_start(content: str, path: str) -> str:
    """Make ``path`` the program of the first ``ExecStart=`` line.

    Without one, a line is added at the end of the [Service] section, or a
    section is added.
    """
    line = "ExecStart=" + quote_exec_path(path)
    if _EXEC_START_RE.search(content):
        return _EXEC_START_RE.sub(lambda _m: line, content, count=1)
    lines = content.splitlines(keepends=True)
    for i, text in enumerate(lines):
        if text.strip() == "[Service]":
            end = i + 1
            while end < len(lines) and not lines[end].lstrip().startswith("["):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1  # before the blank line that ends the section
            if end > 0 and not lines[end - 1].endswith("\n"):
                lines[end - 1] += "\n"
            lines.insert(end, line + "\n")
            return "".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    return content + ("\n" if content else "") + "[Service]\n" + line + "\n"
