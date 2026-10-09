"""High-level systemd operations, independent of where they run."""

from __future__ import annotations

from .errors import CommandError, PilotError, UnitExists
from .models import LogResult, Scope, Unit, UnitAction
from .parsers import merge_units, parse_journal, parse_list_unit_files, parse_list_units, parse_properties, strip_ansi
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
if [ "${PILOT_OVERWRITE:-0}" != 1 ] && [ -e "$target" ]; then
    echo "exists" >&2; exit 17
fi
tmp=$(mktemp "$target.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
cat > "$tmp"
chmod 644 -- "$tmp"
mv -f -- "$tmp" "$target"
trap - EXIT
systemctl "$@" daemon-reload
"""

_JOURNAL_PERMISSION_HINTS = ("insufficient permissions", "no journal files were opened", "not seeing messages")


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
        """Add startup states to ``loaded``, and optionally services that have a unit file but are not loaded.

        This asks systemd for every unit file's state, which can take seconds.
        """
        result = self._list(scope, ["list-unit-files", "--type=service"])
        # Startup states are a nice-to-have; don't fail the listing over them.
        files = parse_list_unit_files(result.stdout) if result.ok else {}
        return merge_units(loaded, files, include_unloaded=include_unloaded)

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

    def logs(self, name: str, scope: Scope = Scope.SYSTEM, lines: int = 500) -> LogResult:
        validate_unit_name(name)
        argv = ["journalctl", "--no-pager", "--output=json", f"--lines={int(lines)}"]
        argv += ["--user-unit", name] if scope is Scope.USER else ["--unit", name]
        result = self.runner.run(argv)
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
