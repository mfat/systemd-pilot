import json

from systemdpilot.core.models import Unit
from systemdpilot.core.parsers import (
    merge_units,
    parse_journal,
    parse_list_unit_files,
    parse_list_units,
    parse_properties,
    strip_ansi,
)


def test_strip_ansi():
    assert strip_ansi("\x1b[0;1;32m●\x1b[0m sshd.service") == "● sshd.service"


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
