from datetime import datetime, timedelta

from systemdpilot.core.journal import (
    ERROR,
    PRESETS_BY_ID,
    REPEATED_WARNINGS,
    WARNING,
    find_issues,
    oldest_time,
    until_before,
)
from systemdpilot.core.models import LogEntry

NOW = datetime(2026, 10, 10, 3, 30)


def entry(message, identifier="app", priority=6, minutes=0, unit="", kernel=False, boot="b0"):
    return LogEntry(NOW - timedelta(minutes=minutes), priority, identifier, "1", message, unit, boot, kernel)


def test_finds_and_names_problems():
    entries = [
        entry("Failed password for root from 203.0.113.42 port 40090 ssh2", "sshd", 5, 1, "ssh.service"),
        entry("Failed password for admin from 203.0.113.42 port 40091 ssh2", "sshd", 5, 2, "ssh.service"),
        entry("Out of memory: Killed process 4821 (node) total-vm:1kB", "kernel", 3, 3, kernel=True),
        entry(
            "Process 3988 (evolution-alarm) of user 1000 dumped core.\n\nStack trace…",
            "systemd-coredump",
            2,
            4,
            "systemd-coredump@0-1-0.service",
        ),
        entry(
            "apport-autoreport.service: Failed with result 'exit-code'.", "systemd", 4, 5, "apport-autoreport.service"
        ),
        entry("Failed to start apport-autoreport.service.", "systemd", 3, 5, "apport-autoreport.service"),
        entry("Accepted publickey for admin from 192.168.1.20 port 51522 ssh2", "sshd", 6, 6, "ssh.service"),
    ]
    issues, flagged = find_issues(entries)
    by_kind = {i.kind: i for i in issues}
    assert set(by_kind) == {"ssh", "oom", "crash", "unit-failed"}
    assert by_kind["ssh"].names == ["203.0.113.42"] and by_kind["ssh"].unit == "ssh.service"
    assert by_kind["ssh"].severity == WARNING
    assert by_kind["oom"].names == ["node"]
    assert by_kind["crash"].names == ["evolution-alarm"] and by_kind["crash"].unit == ""
    failed = by_kind["unit-failed"]
    assert (failed.unit, failed.names, len(failed.entries)) == ("apport-autoreport.service", ["exit-code"], 2)
    assert 6 not in flagged  # an accepted login is not a problem
    # Errors first, then the most recent.
    assert [i.severity for i in issues] == [ERROR, ERROR, ERROR, WARNING]
    assert issues[0].kind == "oom"


def test_repeated_warnings_are_flagged_once_frequent():
    few = [entry(f"took {n}ms too long", "containerd", 4, n) for n in range(REPEATED_WARNINGS - 1)]
    assert find_issues(few) == ([], {})
    many = [entry(f"took {n}ms too long", "containerd", 4, n) for n in range(REPEATED_WARNINGS)]
    (issue,), flagged = find_issues(many)
    assert (issue.kind, issue.source, len(flagged)) == ("warnings", "containerd", REPEATED_WARNINGS)


def test_template_instances_count_as_one_source():
    entries = [
        entry("Could not parse core file", "systemd-coredump", 4, n, f"systemd-coredump@{n}-1-0.service")
        for n in range(REPEATED_WARNINGS)
    ]
    (issue,), _flagged = find_issues(entries)
    assert issue.source == "systemd-coredump" and issue.unit == ""


def test_presets():
    boot = "b0"
    assert PRESETS_BY_ID["ssh"].matches(entry("x", "sshd"), boot)
    assert PRESETS_BY_ID["oom"].matches(entry("Out of memory: Killed process 1 (a)", "kernel", kernel=True), boot)
    assert not PRESETS_BY_ID["oom"].matches(entry("Out of memory", "app"), boot)
    assert PRESETS_BY_ID["boot"].matches(entry("Failed to find module 'x'", "systemd-modules-load", 3), boot)
    assert not PRESETS_BY_ID["boot"].matches(entry("bad", "app", 3, boot="old"), boot)
    assert PRESETS_BY_ID["mac"].matches(entry('apparmor="DENIED" operation="open"', "audit"), boot)


def test_older_entries_end_just_before_the_oldest():
    entries = [entry("new"), entry("old", minutes=5), LogEntry(None, 6, "app", "1", "no time")]
    assert oldest_time(entries) == NOW - timedelta(minutes=5)
    assert oldest_time([]) is None
    moment = datetime.fromtimestamp(1791672206.580793)
    assert until_before(moment) == "@1791672206.580792"
    assert until_before(datetime.fromtimestamp(1791672206)) == "@1791672205.999999"
