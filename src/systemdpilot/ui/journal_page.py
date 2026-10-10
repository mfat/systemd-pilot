"""The journal of the current machine, as a page pushed over the whole main window."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from ..i18n import _
from .journal_view import JournalView


class JournalPage(Adw.NavigationPage):
    """Hosts the main window's :class:`JournalView` in place of the services.

    Nothing of the services page applies to the journal, so it covers all of it;
    swapping whole pages also leaves the services layout untouched underneath.
    """

    def __init__(self, journal: JournalView):
        super().__init__(tag="journal", title=_("systemd Journal"))
        self.journal = journal

        self.window_title = Adw.WindowTitle(title=_("systemd Journal"))
        header = Adw.HeaderBar(title_widget=self.window_title)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh.connect("clicked", lambda *_: journal.reload())
        header.pack_start(refresh)
        search_button = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text=_("Search"))
        header.pack_end(search_button)

        entry = Gtk.SearchEntry(placeholder_text=_("Search the journal"), hexpand=True)
        entry.connect("search-changed", lambda e: journal.set_query(e.get_text()))
        clamp = Adw.Clamp(maximum_size=600, child=entry)
        self.search_bar = Gtk.SearchBar(child=clamp)
        self.search_bar.connect_entry(entry)
        # Typing on this page searches the journal, not the services.
        self.search_bar.set_key_capture_widget(self)
        self.search_bar.bind_property(
            "search-mode-enabled",
            search_button,
            "active",
            GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL,
        )
        self.search_bar.connect(
            "notify::search-mode-enabled", lambda bar, _p: bar.get_search_mode() or entry.set_text("")
        )

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.search_bar)
        view.set_content(journal)
        self.set_child(view)

    def set_machine(self, name: str) -> None:
        self.window_title.set_subtitle(name)
