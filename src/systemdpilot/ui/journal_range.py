"""Picking a custom range of time for the journal: a start, and an end or now."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from gi.repository import Adw, GLib, Gtk

from ..i18n import _


def range_text(since: datetime, until: datetime | None) -> str:
    """The range for its button, e.g. "Oct 10, 14:00 – Now"."""

    def when(moment: datetime) -> str:
        return moment.strftime("%b %d, %H:%M")

    return f"{when(since)} – {when(until) if until else _('Now')}"


def journal_time(moment: datetime) -> str:
    """A time as journalctl's --since and --until take it."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


class MomentRows:
    """A date, from a calendar in a popover, and a time, as hours and minutes."""

    def __init__(self, group: Adw.PreferencesGroup, moment: datetime, on_changed: Callable[[], None]):
        self._on_changed = on_changed
        self.date_row = Adw.ActionRow(title=_("Date"))
        self._calendar = Gtk.Calendar()
        self._date_button = Gtk.MenuButton(
            valign=Gtk.Align.CENTER,
            popover=Gtk.Popover(child=self._calendar),
            css_classes=["flat"],
        )
        self.date_row.add_suffix(self._date_button)
        self.date_row.set_activatable_widget(self._date_button)
        group.add(self.date_row)

        self.time_row = Adw.ActionRow(title=_("Time"))
        clock = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self._hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        for spin, name in ((self._hour, _("Hour")), (self._minute, _("Minute"))):
            spin.set_orientation(Gtk.Orientation.VERTICAL)
            spin.set_wrap(True)
            spin.connect("output", self._two_digits)
            spin.update_property([Gtk.AccessibleProperty.LABEL], [name])
        clock.append(self._hour)
        clock.append(Gtk.Label(label=":"))
        clock.append(self._minute)
        self.time_row.add_suffix(clock)
        group.add(self.time_row)
        self.set(moment)
        # Only after the first values, which would report changes before the dialog is built.
        self._calendar.connect("day-selected", self._on_day)
        for spin in (self._hour, self._minute):
            spin.connect("value-changed", lambda *_: self._on_changed())

    @property
    def moment(self) -> datetime:
        date = self._calendar.get_date()
        return datetime(
            date.get_year(),
            date.get_month(),
            date.get_day_of_month(),
            self._hour.get_value_as_int(),
            self._minute.get_value_as_int(),
        )

    def set(self, moment: datetime) -> None:
        self._calendar.select_day(GLib.DateTime.new_local(moment.year, moment.month, moment.day, 0, 0, 0))
        self._hour.set_value(moment.hour)
        self._minute.set_value(moment.minute)
        self._show_date()

    def set_sensitive(self, sensitive: bool) -> None:
        self.date_row.set_sensitive(sensitive)
        self.time_row.set_sensitive(sensitive)

    def _on_day(self, _calendar):
        self._show_date()
        self._date_button.popdown()
        self._on_changed()

    def _show_date(self) -> None:
        self._date_button.set_label(self.moment.strftime("%a %b %d %Y"))

    @staticmethod
    def _two_digits(spin: Gtk.SpinButton) -> bool:
        spin.set_text(f"{spin.get_value_as_int():02d}")
        return True


class RangeDialog(Adw.Dialog):
    """Calls ``on_apply(since, until)`` with the chosen range; ``until`` is None for "now"."""

    def __init__(
        self,
        since: datetime | None,
        until: datetime | None,
        on_apply: Callable[[datetime, datetime | None], None],
    ):
        super().__init__(title=_("Custom Range"), content_width=420)
        self._on_apply = on_apply
        now = datetime.now().replace(second=0, microsecond=0)

        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        cancel.connect("clicked", lambda *_: self.close())
        self._apply = Gtk.Button(label=_("_Show"), use_underline=True, css_classes=["suggested-action"])
        self._apply.connect("clicked", self._on_apply_clicked)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self._apply)

        page = Adw.PreferencesPage()
        start = Adw.PreferencesGroup(title=_("From"))
        self._since = MomentRows(start, since or now - timedelta(hours=24), self._validate)
        page.add(start)

        end = Adw.PreferencesGroup(title=_("To"))
        self._now = Adw.SwitchRow(title=_("Now"), subtitle=_("Include entries as they arrive"), active=until is None)
        self._now.connect("notify::active", lambda *_: self._validate())
        end.add(self._now)
        self._until = MomentRows(end, until or now, self._validate)
        page.add(end)

        self._error = Gtk.Label(css_classes=["error"], wrap=True, visible=False, margin_bottom=18)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        view.add_bottom_bar(self._error)
        self.set_child(view)
        self.set_default_widget(self._apply)
        self._validate()

    def _range(self) -> tuple[datetime, datetime | None]:
        return self._since.moment, None if self._now.get_active() else self._until.moment

    def _validate(self) -> None:
        self._until.set_sensitive(not self._now.get_active())
        since, until = self._range()
        error = ""
        if since >= (until or datetime.now()):
            error = _("The start must be before the end.")
        self._error.set_label(error)
        self._error.set_visible(bool(error))
        self._apply.set_sensitive(not error)

    def _on_apply_clicked(self, _button):
        since, until = self._range()
        self.close()
        self._on_apply(since, until)
