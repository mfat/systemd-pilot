"""A problem or an entry from the journal, beside its list or in a dialog."""

from __future__ import annotations

from gi.repository import Adw, Gtk, Pango

from .. import APP_ID
from ..core.journal import ERROR, Issue
from ..core.models import LogEntry
from ..i18n import _, ngettext
from . import widgets, words
from .journal_view import LEVEL_ICONS, JournalView, issue_meta, issue_text
from .resources import app_icon

ENTRY_LIMIT = 200  # a problem's entries listed; a crash loop can have thousands


class JournalDetails(Adw.Bin):
    """Shows what is selected in a :class:`JournalView`; opening a service goes through it."""

    def __init__(self, journal: JournalView):
        super().__init__()
        self.journal = journal
        self.item: Issue | LogEntry | None = None

        self.header_bar = Adw.HeaderBar(show_title=False)  # keeps the window buttons on this side
        view = Adw.ToolbarView()
        view.add_top_bar(self.header_bar)
        self._stack = Gtk.Stack(hhomogeneous=False)
        placeholder = Adw.StatusPage(description=_("Select a problem or entry to display its details."))
        icon = app_icon(128)
        if icon:
            placeholder.set_paintable(icon)
        else:
            placeholder.set_icon_name(APP_ID)
        self._stack.add_named(placeholder, "empty")
        self._content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=12,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        self._scroll = Gtk.ScrolledWindow(
            child=Adw.Clamp(maximum_size=900, child=self._content), hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._stack.add_named(self._scroll, "details")
        view.set_content(self._stack)
        self.set_child(view)

    def add_header_end(self, widget: Gtk.Widget) -> None:
        """A button of the window's, placed before the close button."""
        self.header_bar.pack_end(widget)

    def show(self, item: Issue | LogEntry | None) -> None:
        self.item = item
        widgets.clear(self._content)
        if item is None:
            self._stack.set_visible_child_name("empty")
            return
        if isinstance(item, Issue):
            self._show_issue(item)
        else:
            self._show_entry(item)
        self._scroll.get_vadjustment().set_value(0)
        self._stack.set_visible_child_name("details")

    @property
    def title(self) -> str:
        if isinstance(self.item, Issue):
            return issue_text(self.item)[0]
        return _("Journal Entry")

    # -- internals --------------------------------------------------------

    def _heading(self, icon: Gtk.Widget, title: str, badge: tuple[str, str] | None) -> None:
        box = Gtk.Box(spacing=14)
        icon.set_valign(Gtk.Align.START)
        box.append(icon)
        line = Gtk.Box(spacing=10, hexpand=True)
        line.append(widgets.label(title, "title-3", wrap=True))
        if badge:
            line.append(widgets.label(badge[0], "badge", badge[1], valign=Gtk.Align.CENTER))
        box.append(line)
        self._content.append(box)

    def _open_button(self, unit: str) -> None:
        if not unit or not self.journal.knows_unit(unit):
            return
        button = Gtk.Button(label=_("Open Service"), halign=Gtk.Align.START, css_classes=["pill"])
        button.connect("clicked", lambda *_: self.journal.emit("open-unit", unit))
        self._content.append(button)

    def _show_issue(self, issue: Issue) -> None:
        title, explanation = issue_text(issue)
        error = issue.severity == ERROR
        icon = Gtk.Image(
            icon_name="dialog-error-symbolic" if error else "dialog-warning-symbolic",
            pixel_size=24,
            css_classes=["error" if error else "warning"],
        )
        self._heading(icon, title, (_("Error"), "error") if error else (_("Warning"), "warning"))
        if explanation:
            self._content.append(widgets.label(explanation, "issue-explanation", wrap=True, selectable=True))
        self._content.append(widgets.label(issue_meta(issue), "dim-label", "caption", wrap=True))
        self._open_button(issue.unit)

        count = len(issue.entries)
        section = widgets.Section(ngettext("{n} Entry", "{n} Entries", count).format(n=count), _("Newest first"))
        for entry in issue.entries[:ENTRY_LIMIT]:
            section.list.append(widgets.log_row(entry))
        if count > ENTRY_LIMIT:
            more = _("…and {n} more").format(n=count - ENTRY_LIMIT)
            section.list.append(widgets.placeholder_row(more))
        self._content.append(section.box)

    def _show_entry(self, entry: LogEntry) -> None:
        level_word, level_css = words.level(entry.priority)
        first = entry.message.partition("\n")[0]
        mark = Gtk.Image(
            icon_name=LEVEL_ICONS.get(level_css, "dialog-information-symbolic"),
            pixel_size=24,
            css_classes=[level_css or "dim-label"],
        )
        self._heading(mark, first or _("Journal Entry"), (level_word, level_css) if level_css else None)
        self._open_button(entry.unit)

        message = Gtk.Label(
            label=entry.message,
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
            css_classes=["monospace", "entry-message"],
        )
        section = widgets.Section(_("Message"))
        section.box.remove(section.list)
        section.box.append(message)
        self._content.append(section.box)

        fields = widgets.Section(_("Fields"))
        if entry.timestamp:
            fields.list.append(self._field(_("Time"), entry.timestamp.strftime("%a %b %d %Y, %H:%M:%S")))
        fields.list.append(self._field(_("Level"), f"{level_word} ({entry.priority})"))
        source = entry.identifier + (f"[{entry.pid}]" if entry.pid else "")
        if source:
            fields.list.append(self._field(_("Source"), source))
        if entry.unit:
            fields.list.append(self._field(_("Unit"), entry.unit))
        if entry.boot_id:
            current = entry.boot_id == self.journal.boot_id
            fields.list.append(self._field(_("Boot"), _("This boot") if current else entry.boot_id))
        if entry.kernel:
            fields.list.append(self._field(_("Origin"), _("Kernel")))
        self._content.append(fields.box)

    @staticmethod
    def _field(title: str, value: str) -> Gtk.Widget:
        row = Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True, activatable=False)
        row.add_css_class("property")
        return row


class JournalDetailsDialog(Adw.Dialog):
    """:class:`JournalDetails` in a dialog, when the window has no room beside the list."""

    def __init__(self, journal: JournalView, item: Issue | LogEntry):
        super().__init__(content_width=720, content_height=600, width_request=360, height_request=294)
        self.details = JournalDetails(journal)
        self.details.show(item)
        self.set_title(self.details.title)
        self.set_child(self.details)
