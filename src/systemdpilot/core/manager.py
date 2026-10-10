"""High-level systemd operations, independent of where they run."""

from __future__ import annotations

import dataclasses
import re

from .errors import CommandError, InvalidUnitName, PilotError, UnitExists
from .models import LogResult, Scope, Unit, UnitAction
from .parsers import (
    merge_units,
    parse_int,
    parse_journal,
    parse_list_unit_files,
    parse_list_units,
    parse_properties,
    parse_show_units,
    parse_unix_timestamp,
    strip_ansi,
)
from .runner import CommandRunner
from .validation import validate_unit_name

SYSTEM_UNIT_DIR = "/etc/systemd/system"
USER_UNIT_DIR = "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Writes stdin to "$1" atomically and reloads systemd. Run by sh with the
# target path and systemctl flags as positional parameters, so nothing
# user-provided is ever interpolated into the script itself. The temporary
# file is created next to the target, never in a world-writable directory.
_INSTALL_SCRIPT = r"""
set -eu
target=$1; dir=$2; shift 2
mkdir -p -- "$dir"
tmp=$(mktemp "$target.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
cat > "$tmp"
chmod 644 -- "$tmp"
if [ "${PILOT_OVERWRITE:-0}" = 1 ]; then
    mv -f -- "$tmp" "$target"
else
    # ln fails if the target exists, atomically; a check before mv would race.
    ln -- "$tmp" "$target" 2>/dev/null || { echo "exists" >&2; exit 17; }
fi
trap - EXIT
rm -f -- "$tmp"
systemctl "$@" daemon-reload
"""

_JOURNAL_PERMISSION_HINTS = ("insufficient permissions", "no journal files were opened", "not seeing messages")

JOURNAL_GROUP = "systemd-journal"
# Journal access, see journal_access().
ACCESS_FULL, ACCESS_PENDING, ACCESS_MISSING = "full", "pending", "missing"
_USER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\$?$")

_RUNTIME_PROPERTIES = "Id,MainPID,MemoryCurrent,ActiveEnterTimestamp,StateChangeTimestamp"

# Only what the journal view shows; keeps the JSON small over SSH.
_JOURNAL_FIELDS = ",".join(
    (
        "MESSAGE",
        "PRIORITY",
        "SYSLOG_IDENTIFIER",
        "_COMM",
        "_PID",
        "SYSLOG_PID",
        "UNIT",
        "USER_UNIT",
        "_SYSTEMD_UNIT",
        "_SYSTEMD_USER_UNIT",
        "_BOOT_ID",
        "_TRANSPORT",
    )
)


class SystemdManager:
    """Query and control systemd units through a :class:`CommandRunner`."""

    def __init__(self, runner: CommandRunner):
        self.runner = runner

    @staticmethod
    def _systemctl(scope: Scope, *args: str) -> list[str]:
        argv = ["systemctl", "--no-pager"]
        if scope is Scope.USER:
            argv.append("--user")
        return argv + list(args)

    @staticmethod
    def _privileged(scope: Scope) -> bool:
        return scope is Scope.SYSTEM

    # -- queries ----------------------------------------------------------

    def list_units(self, scope: Scope = Scope.SYSTEM, include_inactive: bool = False) -> list[Unit]:
        """Loaded services. Fast, but ``file_state`` is not filled in (None).

        Pass the result to :meth:`complete_units` for startup states.
        """
        args = ["list-units", "--type=service"]
        if include_inactive:
            args.append("--all")
        return parse_list_units(self._list(scope, args).check().stdout)

    def complete_units(
        self, loaded: list[Unit], scope: Scope = Scope.SYSTEM, include_unloaded: bool = False
    ) -> list[Unit]:
        """Add startup states and runtime details (PID, memory, since when) to ``loaded``.

        Optionally adds services that have a unit file but are not loaded.
        This asks systemd for every unit file's state, which can take seconds.
        Prefer :meth:`attach_file_states` then :meth:`add_runtime` in the UI so
        enable switches appear before the slower uptime/memory pass.
        """
        return self.add_runtime(self.attach_file_states(loaded, scope, include_unloaded), scope)

    def attach_file_states(
        self, loaded: list[Unit], scope: Scope = Scope.SYSTEM, include_unloaded: bool = False
    ) -> list[Unit]:
        """Add unit-file states (enabled/disabled/…) without fetching uptimes."""
        result = self._list(scope, ["list-unit-files", "--type=service"])
        # Startup states are a nice-to-have; don't fail the listing over them.
        files = parse_list_unit_files(result.stdout) if result.ok else {}
        return merge_units(loaded, files, include_unloaded=include_unloaded)

    def add_runtime(self, units: list[Unit], scope: Scope = Scope.SYSTEM) -> list[Unit]:
        """Fill in main PID, memory and since when, for units that are or were running."""
        names = []
        for unit in units:
            if unit.is_active or unit.is_failed or unit.active_state == "deactivating":
                try:
                    names.append(validate_unit_name(unit.name))
                except InvalidUnitName:
                    pass
        if not names:
            return units
        show = ["show", f"--property={_RUNTIME_PROPERTIES}"]
        result = self.runner.run(self._systemctl(scope, *show, "--timestamp=unix", "--", *names))
        if not result.ok and "timestamp" in result.stderr.lower():
            # systemd < 248 has no --timestamp; times are then left out.
            result = self.runner.run(self._systemctl(scope, *show, "--", *names))
        if not result.ok:
            return units
        shown = parse_show_units(result.stdout)
        completed = []
        for unit in units:
            props = shown.get(unit.name)
            if props is None:
                completed.append(unit)
                continue
            since = props.get("ActiveEnterTimestamp", "") if unit.kind == "running" else ""
            completed.append(
                dataclasses.replace(
                    unit,
                    main_pid=parse_int(props.get("MainPID", "")) or 0,
                    memory=parse_int(props.get("MemoryCurrent", "")),
                    since=parse_unix_timestamp(since or props.get("StateChangeTimestamp", "")),
                )
            )
        return completed

    def _list(self, scope: Scope, args: list[str]):
        """Run a list command as JSON, falling back to plain text on systemd < 246."""
        result = self.runner.run(self._systemctl(scope, *args, "--output=json"))
        if not result.ok and "json" in result.stderr.lower():
            result = self.runner.run(self._systemctl(scope, *args, "--plain", "--no-legend"))
        return result

    def status_text(self, name: str, scope: Scope = Scope.SYSTEM) -> str:
        validate_unit_name(name)
        result = self.runner.run(self._systemctl(scope, "status", "--full", "--lines=0", "--", name))
        # "systemctl status" exits 3 for inactive units and 4 for unknown ones.
        if result.returncode not in (0, 3):
            result.check()
        return strip_ansi(result.stdout)

    def properties(self, name: str, scope: Scope = Scope.SYSTEM) -> dict[str, str]:
        validate_unit_name(name)
        return parse_properties(self.runner.run(self._systemctl(scope, "show", "--", name)).check().stdout)

    def unit_file(self, name: str, scope: Scope = Scope.SYSTEM) -> str:
        validate_unit_name(name)
        result = self.runner.run(self._systemctl(scope, "cat", "--", name))
        if not result.ok:
            return ""
        return strip_ansi(result.stdout)

    def logs(self, name: str, scope: Scope = Scope.SYSTEM, lines: int = 500, privileged: bool = False) -> LogResult:
        """A unit's newest log entries, oldest first. ``privileged`` reads them as root."""
        validate_unit_name(name)
        argv = ["journalctl", "--no-pager", "--output=json", f"--lines={int(lines)}"]
        argv += ["--user-unit", name] if scope is Scope.USER else ["--unit", name]
        result = self.runner.run(argv, privileged=privileged)
        entries = parse_journal(result.stdout)
        warning = ""
        stderr = strip_ansi(result.stderr).strip()
        if any(h in stderr.lower() for h in _JOURNAL_PERMISSION_HINTS):
            warning = (
                "Some log entries may be hidden: the user is not allowed to read the full system journal. "
                "Adding the user to the “systemd-journal” group shows all entries."
            )
        elif not result.ok and not entries:
            raise CommandError(argv, result.returncode, result.stderr)
        return LogResult(entries, warning)

    def journal(
        self,
        *,
        since: str = "",
        boot: int | None = None,
        kernel: bool = False,
        lines: int = 1500,
        privileged: bool = False,
    ) -> LogResult:
        """The newest ``lines`` journal entries, newest first.

        ``since`` is a journalctl time such as "today" or "24 hours ago";
        ``boot`` is 0 for this boot, -1 for the one before, and so on.
        ``privileged`` reads it as root, so nothing is hidden.
        """
        argv = [
            "journalctl",
            "--no-pager",
            "--output=json",
            f"--output-fields={_JOURNAL_FIELDS}",
            "--all",  # long messages, such as crash reports, are otherwise left out
            "--reverse",
            f"--lines={int(lines)}",
        ]
        if boot is not None:
            argv.append(f"--boot={int(boot)}")
        if since:
            argv.append(f"--since={since}")
        if kernel:
            argv.append("--dmesg")
        result = self.runner.run(argv, privileged=privileged)
        entries = parse_journal(result.stdout)
        stderr = strip_ansi(result.stderr).strip()
        warning = ""
        if any(h in stderr.lower() for h in _JOURNAL_PERMISSION_HINTS):
            warning = (
                "Some entries may be hidden: the user is not allowed to read the full system journal. "
                "Adding the user to the “systemd-journal” group shows all entries."
            )
        elif not result.ok and not entries and "no such boot" not in stderr.lower():
            raise CommandError(argv, result.returncode, result.stderr)
        return LogResult(entries, warning)

    def _user(self) -> str:
        user = self.runner.run(["id", "-un"]).check().stdout.strip()
        if not _USER_NAME_RE.match(user):
            raise PilotError(f"Unexpected user name: {user!r}")
        return user

    def journal_access(self) -> str:
        """Whether the user can read the whole journal through the systemd-journal group.

        ACCESS_PENDING means the user was added to the group, but this login
        session started before that, so it does not apply yet.
        """
        groups = self.runner.run(["id", "-Gn"]).stdout.split()
        if JOURNAL_GROUP in groups:
            return ACCESS_FULL
        entry = self.runner.run(["getent", "group", JOURNAL_GROUP]).stdout.strip()
        members = entry.split(":")[3].split(",") if entry.count(":") >= 3 else []
        return ACCESS_PENDING if self._user() in members else ACCESS_MISSING

    def grant_journal_access(self) -> None:
        """Add the user to the systemd-journal group, which can read all logs.

        gpasswd edits the group file directly, so it also works for users
        that come from a directory service rather than /etc/passwd.
        """
        user = self._user()
        self.runner.run(["gpasswd", "-a", user, JOURNAL_GROUP], privileged=True).check()

    def boot_id(self) -> str:
        """This boot's ID as the journal writes it, or "" if unknown."""
        result = self.runner.run(["cat", "/proc/sys/kernel/random/boot_id"])
        return result.stdout.strip().replace("-", "") if result.ok else ""

    # -- changes ----------------------------------------------------------

    def control(self, name: str, action: UnitAction, scope: Scope = Scope.SYSTEM) -> None:
        validate_unit_name(name)
        self.runner.run(
            self._systemctl(scope, action.value, "--", name),
            privileged=self._privileged(scope),
        ).check()

    def daemon_reload(self, scope: Scope = Scope.SYSTEM) -> None:
        self.runner.run(self._systemctl(scope, "daemon-reload"), privileged=self._privileged(scope)).check()

    def unit_path(self, name: str, scope: Scope) -> str:
        return f"{SYSTEM_UNIT_DIR if scope is Scope.SYSTEM else USER_UNIT_DIR}/{validate_unit_name(name)}"

    def create_unit(self, name: str, content: str, scope: Scope = Scope.SYSTEM, *, overwrite: bool = False) -> str:
        """Install a unit file and reload systemd. Returns the file's path."""
        validate_unit_name(name)
        if not content.strip():
            raise PilotError("The unit file is empty")
        if not content.endswith("\n"):
            content += "\n"

        unit_dir = SYSTEM_UNIT_DIR if scope is Scope.SYSTEM else USER_UNIT_DIR
        # The user directory contains shell variables, so it is expanded by sh
        # itself; only the fixed script text goes through "-c".
        script = f'set -- "{unit_dir}/$1" "{unit_dir}" "$2"\n' + _INSTALL_SCRIPT
        env = ["env", f"PILOT_OVERWRITE={int(overwrite)}"]
        argv = [*env, "sh", "-c", script, "sh", name, "--user" if scope is Scope.USER else "--system"]

        result = self.runner.run(argv, input=content, privileged=self._privileged(scope))
        if result.returncode == 17:
            raise UnitExists(self.unit_path(name, scope))
        result.check()
        return self.unit_path(name, scope)
