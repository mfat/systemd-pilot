from datetime import datetime, timedelta

from systemdpilot.core.models import Scope, Unit
from systemdpilot.ui import words
from systemdpilot.ui.services_view import matches_filter


def test_file_state_word_is_systemd_term():
    assert words.file_state_word("static") == "static"
    assert words.file_state_word("enabled-runtime") == "enabled-runtime"
    assert words.file_state_word("") == "—"
    assert words.file_state_word(None) == ""


def test_file_state_caption():
    assert words.file_state_caption("enabled") == "starts at boot"
    assert words.file_state_caption("static") == "no [Install]"
    assert words.file_state_caption("masked") == "→ /dev/null"
    assert words.file_state_caption("disabled") == ""


def test_enable_caption():
    assert words.enable_caption(Unit("a.service", file_state="enabled")) == "starts at boot"
    assert words.enable_caption(Unit("a.service", file_state="enabled", scope=Scope.USER)) == "starts at login"


def test_can_mask_unmask():
    assert words.can_mask(Unit("a.service", file_state="enabled"))
    assert words.can_mask(Unit("a.service", file_state="static"))
    assert not words.can_mask(Unit("a.service", file_state="masked"))
    assert not words.can_mask(Unit("a.service", file_state=""))
    assert words.can_unmask(Unit("a.service", file_state="masked"))
    assert words.can_unmask(Unit("a.service", file_state="masked-runtime"))
    assert not words.can_unmask(Unit("a.service", file_state="disabled"))


def test_state_word_is_systemd_term():
    assert words.state_word(Unit("a.service", active_state="active", sub_state="running")) == "running"
    assert words.state_word(Unit("a.service", active_state="active", sub_state="exited")) == "exited"
    assert words.state_word(Unit("a.service", active_state="inactive", sub_state="dead")) == "dead"
    assert words.state_word(Unit("a.service", active_state="failed", sub_state="failed")) == "failed"
    assert words.state_word(Unit("a.service", active_state="activating", sub_state="start")) == "activating"


def test_state_sentence():
    running = Unit(
        "a.service",
        active_state="active",
        sub_state="running",
        since=datetime.now() - timedelta(hours=2),
    )
    exited = Unit("a.service", active_state="active", sub_state="exited")
    assert "ago" in words.state_sentence(running)
    assert words.state_sentence(exited) == "active (exited)"


def test_matches_filter_scope_and_kind():
    system = Unit("a.service", scope=Scope.SYSTEM, active_state="active", sub_state="running")
    user = Unit("a.service", scope=Scope.USER, active_state="failed", sub_state="failed")
    assert matches_filter(system, "all", "all") and matches_filter(user, "all", "all")
    assert matches_filter(system, "system", "running") and not matches_filter(user, "system", "all")
    assert matches_filter(user, "user", "failed") and not matches_filter(system, "user", "all")
    assert matches_filter(system, "system", "all") and not matches_filter(system, "all", "failed")


def test_matches_filter_presets():
    looping = Unit("a.service", active_state="activating", sub_state="auto-restart", file_state="enabled")
    enabled_dead = Unit("b.service", active_state="inactive", sub_state="dead", file_state="enabled")
    by_hand = Unit("c.service", active_state="active", sub_state="running", file_state="disabled")
    masked = Unit("d.service", load_state="masked", active_state="inactive", file_state="masked")
    missing = Unit("e.service", load_state="not-found", active_state="inactive")
    assert matches_filter(looping, "all", "auto-restart") and looping.kind == "running"
    assert matches_filter(enabled_dead, "all", "enabled-dead") and not matches_filter(looping, "all", "enabled-dead")
    assert matches_filter(by_hand, "all", "active-disabled") and not matches_filter(
        enabled_dead, "all", "active-disabled"
    )
    assert matches_filter(masked, "all", "masked") and not matches_filter(masked, "all", "load-error")
    assert matches_filter(missing, "all", "load-error") and not matches_filter(missing, "user", "load-error")
