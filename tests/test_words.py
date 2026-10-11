from datetime import datetime, timedelta

from systemdpilot.core.models import Unit
from systemdpilot.ui import words


def test_file_state_word_is_systemd_term():
    assert words.file_state_word("static") == "static"
    assert words.file_state_word("enabled-runtime") == "enabled-runtime"
    assert words.file_state_word("") == "—"
    assert words.file_state_word(None) == ""


def test_file_state_caption_is_plain_language():
    assert words.file_state_caption("static") == "Started when needed"
    assert words.file_state_caption("disabled") == "Does not start automatically"
    assert words.file_state_caption("enabled") == "Starts automatically"
    assert words.file_state_caption("masked") == "Blocked from starting"
    assert words.file_state_caption("") == "No unit file"


def test_file_state_tooltip_explains_fixed_states():
    assert "can’t be enabled or disabled" in words.file_state_tooltip("static").lower()
    assert "unmasked" in words.file_state_tooltip("masked")
    assert words.file_state_tooltip("disabled") == ""
    assert words.file_state_tooltip("enabled") == ""


def test_state_word_is_systemd_term():
    assert words.state_word(Unit("a.service", active_state="active", sub_state="running")) == "running"
    assert words.state_word(Unit("a.service", active_state="active", sub_state="exited")) == "exited"
    assert words.state_word(Unit("a.service", active_state="inactive", sub_state="dead")) == "dead"
    assert words.state_word(Unit("a.service", active_state="failed", sub_state="failed")) == "failed"
    assert words.state_word(Unit("a.service", active_state="activating", sub_state="start")) == "activating"
    assert words.state_word(Unit("a.service", active_state="deactivating", sub_state="stop")) == "deactivating"


def test_state_caption_is_plain_language():
    exited = Unit("a.service", active_state="active", sub_state="exited")
    dead = Unit("a.service", active_state="inactive", sub_state="dead")
    failed = Unit("a.service", active_state="failed", sub_state="failed")
    starting = Unit("a.service", active_state="activating", sub_state="start")
    assert words.state_caption(exited) == "Ran once, then finished"
    assert words.state_caption(dead) == "Not running"
    assert words.state_caption(failed) == "Stopped with an error"
    assert words.state_caption(starting) == "Starting…"


def test_state_sentence_stays_plain_language():
    running = Unit(
        "a.service",
        active_state="active",
        sub_state="running",
        since=datetime.now() - timedelta(hours=2),
    )
    exited = Unit("a.service", active_state="active", sub_state="exited")
    assert "ago" in words.state_sentence(running)
    assert words.state_sentence(exited) == "ran and finished normally"
