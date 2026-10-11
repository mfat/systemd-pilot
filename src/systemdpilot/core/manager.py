"""High-level systemd operations, independent of where they run."""

from __future__ import annotations

import base64
import dataclasses
import re
from collections.abc import Sequence

from .errors import CommandError, InvalidUnitName, PilotError, UnitActivationFailed, UnitExists
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
from .runner import CommandResult, CommandRunner
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

# Installs new unit files together, reloads systemd, then enables and starts
# one of them, all in one privileged call (so one password prompt). Run by sh
# with "$dir" set by a prefix, the systemctl scope flag, the overwrite, enable
# and start switches, the unit to enable and start, and the file names as
# positional parameters. Each file's content is one base64 line on stdin, in
# the same order. A file that already exists stops it before systemd is told
# about anything, and the files it had already put in place are taken back.
# The temporary files start with "." (hidden from systemd) and are gone before
# the reload.
_CREATE_SCRIPT = r"""
set -eu
flag=$1; overwrite=$2; enable=$3; start=$4; activate=$5; shift 5
mkdir -p -- "$dir"
pairs=; linked=
trap 'for p in $pairs; do rm -f -- "$dir/${p%%/*}"; done' EXIT
for name do
    IFS= read -r data
    tmp=$(mktemp "$dir/.$name.XXXXXX")
    pairs="$pairs ${tmp##*/}/$name"
    printf '%s' "$data" | base64 -d > "$tmp"
    chmod 644 -- "$tmp"
done
for p in $pairs; do
    tmp=${p%%/*}; name=${p#*/}
    if [ "$overwrite" = 1 ]; then
        mv -f -- "$dir/$tmp" "$dir/$name"
    elif ln -- "$dir/$tmp" "$dir/$name" 2>/dev/null; then
        linked="$linked $name"
    else
        for n in $linked; do rm -f -- "$dir/$n"; done
        printf '%s\n' "$dir/$name" >&2
        exit 17
    fi
done
for p in $pairs; do rm -f -- "$dir/${p%%/*}"; done
pairs=
systemctl "$flag" daemon-reload
if [ "$enable" = 1 ]; then
    msg=$(systemctl "$flag" enable -- "$activate" 2>&1) || { printf '%s\n' "$msg" >&2; exit 18; }
    # Without an [Install] section enable succeeds, but enables nothing.
    case $(systemctl "$flag" is-enabled -- "$activate" 2>/dev/null) in
        static | disabled | masked | "")
            printf '%s\n' "$activate has no [Install] section saying when to start it." >&2
            exit 18 ;;
    esac
fi
if [ "$start" = 1 ]; then systemctl "$flag" start -- "$activate" || exit 19; fi
"""
_EXISTS, _ENABLE_FAILED, _START_FAILED = 17, 18, 19

# Removes a drop-in, and its directory if nothing else is left in it.
_REMOVE_SCRIPT = r"""
set -eu
target=$1; dir=$2; shift 2
rm -f -- "$target"
rmdir -- "$dir" 2>/dev/null || true
systemctl "$@" daemon-reload
"""

OVERRIDE_FILE = "override.conf"
# Signals offered for a unit's processes.
SIGNALS = ("SIGTERM", "SIGHUP", "SIGKILL")

# Runs one systemctl command against the system and then the user instance, in
# a single round trip over SSH. After each part a marker line with its exit
# status goes to stdout and stderr, so the two outputs can be told apart.
_EACH_SCOPE_SCRIPT = r"""
for flag in --system --user; do
    systemctl --no-pager "$flag" "$@"
    status=$?
    printf '\n@@pilot-scope %s %s@@\n' "$flag" "$status"
    printf '\n@@pilot-scope %s %s@@\n' "$flag" "$status" >&2
done
"""
_SCOPE_MARK_RE = re.compile(r"\n?@@pilot-scope --(system|user) (\d+)@@\n?")

_JOURNAL_PERMISSION_HINTS = ("insufficient permissions", "no journal files were opened", "not seeing messages")

JOURNAL_GROUP = "systemd-journal"
# Journal access, see journal_access().
ACCESS_FULL, ACCESS_PENDING, ACCESS_MISSING = "full", "pending", "missing"
_USER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\$?$")

_RUNTIME_PROPERTIES = (
    "Id,MainPID,MemoryCurrent,ActiveEnterTimestamp,ActiveExitTimestamp,InactiveEnterTimestamp,InactiveExitTimestamp"
)

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
        self._unit_files: dict[Scope, dict[str, str]] = {}

    def invalidate_unit_files(self) -> None:
        """Drop cached ``list-unit-files`` output (after enable/disable, etc.)."""
        self._unit_files = {}

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
        """Loaded units of one scope, of every type. Fast, but ``file_state`` is not filled in (None)."""
        result = self._list(scope, self._list_units_args(include_inactive)).check()
        return _with_scope(parse_list_units(result.stdout), scope)

    def list_services(self, include_inactive: bool = False) -> list[Unit]:
        """Loaded system and user units of every type, in one round trip.

        The user part is left out when there is no user instance to ask, as is
        usual over SSH unless the user is logged in or has lingering enabled.
        """
        results = self._list_each_scope(self._list_units_args(include_inactive))
        units = _with_scope(parse_list_units(results[Scope.SYSTEM].check().stdout), Scope.SYSTEM)
        if results[Scope.USER].ok:
            units += _with_scope(parse_list_units(results[Scope.USER].stdout), Scope.USER)
        return units

    @staticmethod
    def _list_units_args(include_inactive: bool) -> list[str]:
        args = ["list-units"]
        if include_inactive:
            args.append("--all")
        return args

    def complete_units(self, loaded: list[Unit], include_unloaded: bool = False) -> list[Unit]:
        """Add startup states and runtime details (PID, memory, since when) to ``loaded``.

        Optionally adds units that have a unit file but are not loaded.
        This asks systemd for every unit file's state, which can take seconds.
        Prefer :meth:`attach_file_states` then :meth:`add_runtime` in the UI so
        enable switches appear before the slower uptime/memory pass.
        """
        return self.add_runtime(self.attach_file_states(loaded, include_unloaded))

    def attach_file_states(self, loaded: list[Unit], include_unloaded: bool = False) -> list[Unit]:
        """Add unit-file states (enabled/disabled/…) without fetching uptimes.

        Unloaded units are only added for the scopes that ``loaded`` has units of.
        """
        scopes = [scope for scope in Scope if any(u.scope is scope for u in loaded)]
        missing = [scope for scope in scopes if scope not in self._unit_files]
        args = ["list-unit-files"]
        if len(missing) == 2:
            results = self._list_each_scope(args)
        else:
            results = {scope: self._list(scope, args) for scope in missing}
        for scope, result in results.items():
            # Startup states are a nice-to-have; don't fail the listing over them.
            self._unit_files[scope] = parse_list_unit_files(result.stdout) if result.ok else {}
        merged = []
        for scope in scopes:
            part = [u for u in loaded if u.scope is scope]
            merged += merge_units(part, self._unit_files[scope], include_unloaded=include_unloaded, scope=scope)
        return sorted(merged, key=lambda u: (u.name.lower(), u.scope is Scope.USER))

    def add_runtime(self, units: list[Unit]) -> list[Unit]:
        """Fill in main PID, memory and since when, for units that are or were running."""
        shown: dict[tuple[Scope, str], dict[str, str]] = {}
        for scope in Scope:
            names = []
            for unit in units:
                if unit.scope is scope and (unit.is_active or unit.is_failed or unit.active_state == "deactivating"):
                    try:
                        names.append(validate_unit_name(unit.name))
                    except InvalidUnitName:
                        pass
            if names:
                for name, props in self._show_runtime(scope, names).items():
                    shown[scope, name] = props
        if not shown:
            return units
        completed = []
        for unit in units:
            props = shown.get(unit.key)
            if props is None:
                completed.append(unit)
                continue
            since = props.get(_since_property(unit.active_state), "")
            completed.append(
                dataclasses.replace(
                    unit,
                    main_pid=parse_int(props.get("MainPID", "")) or 0,
                    memory=parse_int(props.get("MemoryCurrent", "")),
                    since=parse_unix_timestamp(since),
                )
            )
        return completed

    def _show_runtime(self, scope: Scope, names: list[str]) -> dict[str, dict[str, str]]:
        show = ["show", f"--property={_RUNTIME_PROPERTIES}"]
        result = self.runner.run(self._systemctl(scope, *show, "--timestamp=unix", "--", *names))
        if not result.ok and "timestamp" in result.stderr.lower():
            # systemd < 248 has no --timestamp; times are then left out.
            result = self.runner.run(self._systemctl(scope, *show, "--", *names))
        return parse_show_units(result.stdout) if result.ok else {}

    def _list_each_scope(self, args: list[str]) -> dict[Scope, CommandResult]:
        """:meth:`_list` for both scopes in one command."""
        results = self._run_each_scope([*args, "--output=json"])
        system = results[Scope.SYSTEM]
        if not system.ok and "json" in system.stderr.lower():
            results = self._run_each_scope([*args, "--plain", "--no-legend"])
        return results

    def _run_each_scope(self, args: list[str]) -> dict[Scope, CommandResult]:
        result = self.runner.run(["sh", "-c", _EACH_SCOPE_SCRIPT, "sh", *args])
        out, err = _split_scopes(result.stdout), _split_scopes(result.stderr)
        results = {}
        for scope in Scope:
            argv = tuple(self._systemctl(scope, *args))
            if scope.value not in out:
                # The script never got this far (no shell, lost connection…).
                results[scope] = CommandResult(argv, result.returncode or 1, "", result.stderr)
                continue
            stdout, code = out[scope.value]
            results[scope] = CommandResult(argv, code, stdout, err.get(scope.value, ("", code))[0])
        return results

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
        """All of a unit's properties, with times as ``@<unix seconds>`` where systemd can."""
        validate_unit_name(name)
        result = self.runner.run(self._systemctl(scope, "show", "--timestamp=unix", "--", name))
        if not result.ok and "timestamp" in result.stderr.lower():
            # systemd < 248 has no --timestamp; times then stay in its own words.
            result = self.runner.run(self._systemctl(scope, "show", "--", name))
        return parse_properties(result.check().stdout)

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
        # One argument with "=": names such as "-.mount" must not read as options.
        argv.append(f"--user-unit={name}" if scope is Scope.USER else f"--unit={name}")
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
        until: str = "",
        boot: int | None = None,
        kernel: bool = False,
        lines: int = 1500,
        priority: int | None = None,
        privileged: bool = False,
    ) -> LogResult:
        """The newest ``lines`` journal entries, newest first.

        ``since`` and ``until`` are journalctl times such as "today", "24 hours ago"
        or "2026-10-10 14:00:00";
        ``boot`` is 0 for this boot, -1 for the one before, and so on.
        ``priority`` keeps entries of that syslog priority and more severe, so
        ``lines`` counts only those: 4 is warnings and worse.
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
        if until:
            argv.append(f"--until={until}")
        if kernel:
            argv.append("--dmesg")
        if priority is not None:
            argv.append(f"--priority={int(priority)}")
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

    def kill(self, name: str, signal: str, scope: Scope = Scope.SYSTEM) -> None:
        """Send ``signal`` to all of the unit's processes."""
        validate_unit_name(name)
        if signal not in SIGNALS:
            raise PilotError(f"Unsupported signal: {signal}")
        self.runner.run(
            self._systemctl(scope, "kill", f"--signal={signal}", "--", name),
            privileged=self._privileged(scope),
        ).check()

    def daemon_reload(self, scope: Scope = Scope.SYSTEM) -> None:
        self.runner.run(self._systemctl(scope, "daemon-reload"), privileged=self._privileged(scope)).check()

    def daemon_reload_all(self) -> None:
        """Reload the system instance, then the user one if there is one to reach."""
        self.daemon_reload(Scope.SYSTEM)
        self.runner.run(self._systemctl(Scope.USER, "daemon-reload"))

    def known_unit_file(self, name: str, scope: Scope) -> bool | None:
        """Whether systemd listed a unit file of this name, or None if the list was not fetched.

        From the last listing, so it may be out of date; creating a unit checks
        for itself.
        """
        files = self._unit_files.get(scope)
        return None if files is None else name in files

    def unit_path(self, name: str, scope: Scope) -> str:
        return f"{SYSTEM_UNIT_DIR if scope is Scope.SYSTEM else USER_UNIT_DIR}/{validate_unit_name(name)}"

    def override_path(self, name: str, scope: Scope) -> str:
        return f"{self.unit_path(name, scope)}.d/{OVERRIDE_FILE}"

    def write_override(self, name: str, content: str, scope: Scope = Scope.SYSTEM) -> str:
        """Save the unit's drop-in override, as ``systemctl edit`` does, and reload systemd.

        Empty content (or only comments) removes the override. Returns its path.
        """
        validate_unit_name(name)
        unit_dir = SYSTEM_UNIT_DIR if scope is Scope.SYSTEM else USER_UNIT_DIR
        lines = (line.strip() for line in content.splitlines())
        settings = any(line and not line.startswith(("#", ";", "[")) for line in lines)
        if content and not content.endswith("\n"):
            content += "\n"
        body = _INSTALL_SCRIPT if settings else _REMOVE_SCRIPT
        script = f'set -- "{unit_dir}/$1.d/{OVERRIDE_FILE}" "{unit_dir}/$1.d" "$2"\n' + body
        argv = ["env", "PILOT_OVERWRITE=1", "sh", "-c", script, "sh", name]
        argv.append("--user" if scope is Scope.USER else "--system")
        self.runner.run(argv, input=content if settings else None, privileged=self._privileged(scope)).check()
        return self.override_path(name, scope)

    def create_unit(
        self,
        name: str,
        content: str,
        scope: Scope = Scope.SYSTEM,
        *,
        overwrite: bool = False,
        enable: bool = False,
        start: bool = False,
    ) -> str:
        """Install a unit file and reload systemd. Returns the file's path."""
        activate = name if enable or start else None
        return self.create_units(
            [(name, content)], scope, overwrite=overwrite, enable=enable, start=start, activate=activate
        )[0]

    def create_units(
        self,
        files: Sequence[tuple[str, str]],
        scope: Scope = Scope.SYSTEM,
        *,
        overwrite: bool = False,
        enable: bool = False,
        start: bool = False,
        activate: str | None = None,
    ) -> list[str]:
        """Install unit files together, reload systemd, then enable and start ``activate``.

        Without ``overwrite`` no file is replaced: if one exists, none are
        installed and :class:`UnitExists` is raised. If the files are
        installed but enabling or starting fails, :class:`UnitActivationFailed`
        is raised. Returns the files' paths.
        """
        if not files:
            raise PilotError("No unit file to create")
        names = [validate_unit_name(name) for name, _content in files]
        if len(set(names)) != len(names):
            raise PilotError("The same unit file is given twice")
        if (enable or start) and activate not in names:
            raise PilotError("Only a unit that is being created can be enabled or started")
        lines = []
        for name, content in files:
            if not content.strip():
                raise PilotError(f"The unit file {name} is empty")
            if not content.endswith("\n"):
                content += "\n"
            lines.append(base64.b64encode(content.encode()).decode() + "\n")

        unit_dir = SYSTEM_UNIT_DIR if scope is Scope.SYSTEM else USER_UNIT_DIR
        # The user directory contains shell variables, so it is expanded by sh
        # itself; only the fixed script text goes through "-c".
        script = f'dir="{unit_dir}"\n' + _CREATE_SCRIPT
        flag = "--user" if scope is Scope.USER else "--system"
        argv = [
            "sh",
            "-c",
            script,
            "sh",
            flag,
            str(int(overwrite)),
            str(int(enable)),
            str(int(start)),
            activate or "",
            *names,
        ]

        result = self.runner.run(argv, input="".join(lines), privileged=self._privileged(scope))
        paths = [self.unit_path(name, scope) for name in names]
        if result.returncode == _EXISTS:
            lines = result.stderr.strip().splitlines()
            raise UnitExists(lines[-1] if lines else paths[0])
        if result.returncode in (_ENABLE_FAILED, _START_FAILED):
            action = "enabled" if result.returncode == _ENABLE_FAILED else "started"
            raise UnitActivationFailed(paths, activate or "", action, result.stderr)
        result.check()
        return paths


# A user's systemd shows the machine's devices, mounts and swap too, read-only.
SYSTEM_ONLY_TYPES = frozenset({"device", "mount", "automount", "swap"})


def _with_scope(units: list[Unit], scope: Scope) -> list[Unit]:
    if scope is Scope.USER:
        units = [u for u in units if u.type not in SYSTEM_ONLY_TYPES]
    return [dataclasses.replace(u, scope=scope) for u in units]


def _split_scopes(text: str) -> dict[str, tuple[str, int]]:
    """Output of :data:`_EACH_SCOPE_SCRIPT` as ``{"system"|"user": (output, exit status)}``."""
    parts = {}
    start = 0
    for match in _SCOPE_MARK_RE.finditer(text):
        parts[match.group(1)] = (text[start : match.start()], int(match.group(2)))
        start = match.end()
    return parts


def _since_property(active_state: str) -> str:
    """The timestamp ``systemctl status`` shows after "since" for this state."""
    if active_state in ("active", "reloading", "refreshing"):
        return "ActiveEnterTimestamp"
    if active_state in ("inactive", "failed"):
        return "InactiveEnterTimestamp"
    if active_state == "activating":
        return "InactiveExitTimestamp"
    return "ActiveExitTimestamp"
