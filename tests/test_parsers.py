import json

from systemdpilot.core.models import Unit
from systemdpilot.core.parsers import (
    merge_units,
    parse_journal,
    parse_list_unit_files,
    parse_list_units,
    parse_properties,
    strip_ansi,
    unit_file_body,
)


def test_strip_ansi():
    assert strip_ansi("\x1b[0;1;32m●\x1b[0m sshd.service") == "● sshd.service"


def test_unit_file_body_strips_path_and_dropins():
    text = (
        "# /usr/lib/systemd/system/demo.service\n"
        "[Unit]\n"
        "Description=Demo\n"
        "# Keep this comment\n"
        "\n"
        "# /etc/systemd/system/demo.service.d/override.conf\n"
        "[Service]\n"
        "Environment=FOO=1\n"
    )
    assert unit_file_body(text) == "[Unit]\nDescription=Demo\n# Keep this comment\n\n"


def test_unit_file_body_empty():
    assert unit_file_body("") == ""
    assert unit_file_body("# /only/header\n") == ""


def test_parse_list_units_json():
    text = json.dumps(
        [
            {"unit": "sshd.service", "load": "loaded", "active": "active", "sub": "running", "description": "OpenSSH"},
            {"unit": "", "load": "x"},
        ]
    )
    assert parse_list_units(text) == [Unit("sshd.service", "OpenSSH", "loaded", "active", "running")]


def test_parse_list_units_json_with_ansi():
    text = "\x1b[0m" + json.dumps([{"unit": "a.service", "active": "failed", "sub": "failed"}])
    assert parse_list_units(text)[0].is_failed


def test_parse_list_units_plain_fallback():
    text = (
        "  sshd.service  loaded active running OpenSSH server daemon\n"
        "● foo.service   loaded failed failed  Foo & <bar>\n"
    )
    units = parse_list_units(text)
    assert [u.name for u in units] == ["sshd.service", "foo.service"]
    assert units[0].description == "OpenSSH server daemon"
    assert units[1].description == "Foo & <bar>"


def test_parse_list_unit_files():
    text = json.dumps([{"unit_file": "a.service", "state": "enabled", "preset": "enabled"}])
    assert parse_list_unit_files(text) == {"a.service": "enabled"}
    assert parse_list_unit_files("b.service disabled enabled\n") == {"b.service": "disabled"}


def test_merge_units_has_no_duplicates():
    loaded = [Unit("a.service", "A", "loaded", "active", "running")]
    files = {"a.service": "enabled", "b.service": "disabled", "t@.service": "static"}

    merged = merge_units(loaded, files, include_unloaded=True)
    assert [u.name for u in merged] == ["a.service", "b.service"]
    assert merged[0].file_state == "enabled"
    assert merged[0].description == "A"
    assert merged[1].active_state == "inactive"

    assert [u.name for u in merge_units(loaded, files, include_unloaded=False)] == ["a.service"]


def test_parse_properties():
    props = parse_properties("Id=a.service\nExecStart={ path=/bin/x ; argv[]=/bin/x a=b }\nbad line\n")
    assert props["Id"] == "a.service"
    assert props["ExecStart"] == "{ path=/bin/x ; argv[]=/bin/x a=b }"
    assert "bad line" not in props


def test_parse_journal():
    lines = [
        {
            "__REALTIME_TIMESTAMP": "1700000000000000",
            "PRIORITY": "3",
            "SYSLOG_IDENTIFIER": "sshd",
            "_PID": "42",
            "MESSAGE": "boom",
        },
        {"__REALTIME_TIMESTAMP": "x", "MESSAGE": [104, 105]},
        "not json",
    ]
    text = "\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines)
    entries = parse_journal(text)
    assert len(entries) == 2
    assert entries[0].priority == 3 and entries[0].identifier == "sshd" and entries[0].pid == "42"
    assert entries[0].timestamp is not None
    assert entries[1].message == "hi" and entries[1].timestamp is None and entries[1].priority == 6


def test_parse_show_units_splits_blocks():
    from systemdpilot.core.parsers import parse_show_units

    text = "Id=a.service\nMainPID=12\n\nId=b.service\nMainPID=0\n"
    assert parse_show_units(text) == {
        "a.service": {"Id": "a.service", "MainPID": "12"},
        "b.service": {"Id": "b.service", "MainPID": "0"},
    }


def test_parse_unix_timestamp_and_int():
    from systemdpilot.core.parsers import parse_int, parse_unix_timestamp

    assert parse_unix_timestamp("@1728450657").year == 2024
    assert parse_unix_timestamp("") is None
    assert parse_unix_timestamp("@0") is None
    assert parse_unix_timestamp("Fri 2024-10-09 11:10:57 UTC") is None
    assert parse_int("4096") == 4096
    assert parse_int("[not set]") is None
    assert parse_int(str(2**64 - 1)) is None


def test_parse_journal_unit_boot_and_transport():
    line = json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1700000000000000",
            "MESSAGE": "a.service: Failed with result 'exit-code'.",
            "SYSLOG_IDENTIFIER": "systemd",
            "UNIT": "a.service",
            "_SYSTEMD_UNIT": "init.scope",
            "_BOOT_ID": "abc",
            "_TRANSPORT": "journal",
        }
    )
    kernel = json.dumps({"MESSAGE": "oops", "_TRANSPORT": "kernel", "SYSLOG_IDENTIFIER": "kernel"})
    entry, kernel_entry = parse_journal(line + "\n" + kernel)
    assert (entry.unit, entry.boot_id, entry.kernel) == ("a.service", "abc", False)
    assert kernel_entry.kernel and kernel_entry.unit == ""
