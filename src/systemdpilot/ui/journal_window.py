"""The journal of the current machine, in a window of its own."""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gio, GObject, Gtk

from .journal_view import JournalView


class JournalWindow(Adw.Window):
    """Hosts the main window's :class:`JournalView`; closing only hides it.

    The main window's actions are reachable as ``win.*``, so the Simple/Advanced
    switch in the journal stays in step with the services.
    """

    def __init__(self, journal: JournalView, actions: Gio.ActionGroup):
        super().__init__(default_width=1000, default_height=720, hide_on_close=True)
        self.journal = journal
        self.insert_action_group("win", actions)

        self.window_title = Adw.WindowTitle(title=_("Journal"))
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
        self.toast_overlay = Adw.ToastOverlay(child=view)
        self.set_content(self.toast_overlay)

    def set_machine(self, name: str) -> None:
        self.set_title(_("Journal — {machine}").format(machine=name))
        self.window_title.set_subtitle(name)

    def toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message, use_markup=False, timeout=3))
