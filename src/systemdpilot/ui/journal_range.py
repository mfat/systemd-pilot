"""Picking a custom range of time for the journal: a start, and an end or now."""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import datetime, timedelta

from gi.repository import Adw, Gtk

from ..i18n import _


def range_text(since: datetime, until: datetime | None) -> str:
    """The range for its button, e.g. "Oct 10, 14:00 – Now"."""

    def when(moment: datetime) -> str:
        return moment.strftime("%b %d, %H:%M")

    return f"{when(since)} – {when(until) if until else _('Now')}"


def journal_time(moment: datetime) -> str:
    """A time as journalctl's --since and --until take it."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


TIME_FIELD_WIDTH = 64  # an hour or minute in the big clock, and the arrows over it
TIME_COLON_WIDTH = 16


class MomentEditor:
    """A date and time, edited as in GNOME Settings: a big clock with arrows, then month, day and year.

    :attr:`rows` go in an expander row: the clock's, then one each for the month, day and year.
    """

    def __init__(self, moment: datetime, on_changed: Callable[[], None]):
        self._on_changed = on_changed
        self._updating = False

        # The clock: arrows above and below the hours and the minutes, which can also be typed.
        clock = Gtk.Grid(halign=Gtk.Align.CENTER, row_spacing=6, margin_top=12, margin_bottom=12)
        self._hour = self._field(_("Hour"), 23)
        self._minute = self._field(_("Minute"), 59)
        digits = Gtk.Box(css_classes=["time-editor"])
        digits.append(self._hour)
        digits.append(Gtk.Label(label=":", width_request=TIME_COLON_WIDTH, css_classes=["time-colon"]))
        digits.append(self._minute)
        clock.attach(digits, 0, 1, 3, 1)
        clock.attach(Gtk.Box(width_request=TIME_COLON_WIDTH), 1, 0, 1, 1)  # keeps the minutes' arrows over them
        for column, field in ((0, self._hour), (2, self._minute)):
            for row, step, icon, name in ((0, 1, "go-up-symbolic", _("Up")), (2, -1, "go-down-symbolic", _("Down"))):
                button = Gtk.Button(icon_name=icon, halign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
                button.update_property([Gtk.AccessibleProperty.LABEL], [name])
                button.connect("clicked", lambda _b, f=field, s=step: self._step(f, s))
                cell = Gtk.Box(width_request=TIME_FIELD_WIDTH + 6)  # the field and its share of the box's padding
                cell.append(button)
                button.set_hexpand(True)
                clock.attach(cell, column, row, 1, 1)
        clock_row = Gtk.ListBoxRow(child=clock, activatable=False, selectable=False)

        months = [datetime(2000, m, 1).strftime("%B") for m in range(1, 13)]
        self._month = Adw.ComboRow(title=_("Month"), model=Gtk.StringList.new(months))
        self._day = Adw.SpinRow.new_with_range(1, 31, 1)
        self._day.set_title(_("Day"))
        self._year = Adw.SpinRow.new_with_range(1970, 2100, 1)
        self._year.set_title(_("Year"))
        self.rows = [clock_row, self._month, self._day, self._year]

        self.set(moment)
        # Only after the first values, which would report changes before the dialog is built.
        self._month.connect("notify::selected", lambda *_: self._changed())
        self._day.connect("notify::value", lambda *_: self._changed())
        self._year.connect("notify::value", lambda *_: self._changed())

    def _field(self, name: str, top: int) -> Gtk.Text:
        field = Gtk.Text(
            xalign=0.5,
            max_length=2,
            width_chars=2,
            max_width_chars=2,
            width_request=TIME_FIELD_WIDTH,
            input_purpose=Gtk.InputPurpose.DIGITS,
            css_classes=["time-field"],
        )
        field.top = top
        field.update_property([Gtk.AccessibleProperty.LABEL], [name])
        field.connect("activate", lambda f: self._commit(f))
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._commit(field))
        field.add_controller(focus)
        return field

    @staticmethod
    def _value(field: Gtk.Text) -> int:
        text = field.get_text().strip()
        return min(max(int(text), 0), field.top) if text.isdigit() else 0

    def _show(self, field: Gtk.Text, value: int) -> None:
        field.set_text(f"{value:02d}")

    def _commit(self, field: Gtk.Text) -> None:
        self._show(field, self._value(field))
        self._changed()

    def _step(self, field: Gtk.Text, step: int) -> None:
        self._show(field, (self._value(field) + step) % (field.top + 1))
        self._changed()

    def _changed(self) -> None:
        if self._updating:
            return
        # A month has as many days as it has, in that year.
        days = calendar.monthrange(int(self._year.get_value()), self._month.get_selected() + 1)[1]
        self._day.get_adjustment().set_upper(days)
        if self._day.get_value() > days:
            self._day.set_value(days)
        self._on_changed()

    @property
    def moment(self) -> datetime:
        return datetime(
            int(self._year.get_value()),
            self._month.get_selected() + 1,
            int(self._day.get_value()),
            self._value(self._hour),
            self._value(self._minute),
        )

    def set(self, moment: datetime) -> None:
        self._updating = True
        self._year.set_value(moment.year)
        self._month.set_selected(moment.month - 1)
        self._day.get_adjustment().set_upper(calendar.monthrange(moment.year, moment.month)[1])
        self._day.set_value(moment.day)
        self._show(self._hour, moment.hour)
        self._show(self._minute, moment.minute)
        self._updating = False
        self._changed()


def moment_text(moment: datetime) -> str:
    return moment.strftime("%a %b %d %Y, %H:%M")


class RangeDialog(Adw.Dialog):
    """Calls ``on_apply(since, until)`` with the chosen range; ``until`` is None for "now".

    From and To are expander rows that open on their date and time; To's switch
    is off for "now".
    """

    def __init__(
        self,
        since: datetime | None,
        until: datetime | None,
        on_apply: Callable[[datetime, datetime | None], None],
    ):
        # Grows as the rows open.
        super().__init__(title=_("Custom Range"), content_width=420, follows_content_size=True)
        self._on_apply = on_apply
        now = datetime.now().replace(second=0, microsecond=0)

        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        cancel.connect("clicked", lambda *_: self.close())
        self._apply = Gtk.Button(label=_("_Show"), use_underline=True, css_classes=["suggested-action"])
        self._apply.connect("clicked", self._on_apply_clicked)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self._apply)

        self._since = MomentEditor(since or now - timedelta(hours=24), self._validate)
        self._until = MomentEditor(until or now, self._validate)
        self._since_row = Adw.ExpanderRow(title=_("From"))
        self._until_row = Adw.ExpanderRow(title=_("To"), show_enable_switch=True, enable_expansion=until is not None)
        self._until_row.connect("notify::enable-expansion", lambda *_: self._validate())
        group = Adw.PreferencesGroup()
        for row, editor in ((self._since_row, self._since), (self._until_row, self._until)):
            for child in editor.rows:
                row.add_row(child)
            group.add(row)
        clamp = Adw.Clamp(
            child=group, maximum_size=420, margin_top=12, margin_bottom=24, margin_start=12, margin_end=12
        )
        page = Gtk.ScrolledWindow(
            child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, width_request=420
        )

        self._error = Gtk.Label(css_classes=["error"], wrap=True, visible=False, margin_bottom=18)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        view.add_bottom_bar(self._error)
        self.set_child(view)
        self.set_default_widget(self._apply)
        self._validate()

    @property
    def until_now(self) -> bool:
        return not self._until_row.get_enable_expansion()

    def _range(self) -> tuple[datetime, datetime | None]:
        return self._since.moment, None if self.until_now else self._until.moment

    def _validate(self) -> None:
        if not hasattr(self, "_until_row"):
            return  # still being built
        self._since_row.set_subtitle(moment_text(self._since.moment))
        self._until_row.set_subtitle(_("Now") if self.until_now else moment_text(self._until.moment))
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
