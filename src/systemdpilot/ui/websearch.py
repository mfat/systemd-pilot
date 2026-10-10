"""“Search Google” in the context menu of any selected text in a window.

Labels, entries and text views each have their own context menu. Rather than
setting it up on every one of them, the window looks at the widget under a
right click (or the focused one, for the Menu key) just before that widget
opens its menu, and adds the item to it there. An open dialog takes the
input grab, so events start at it and skip the window: it gets its own watch.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from gi.repository import Adw, Gdk, Gio, Gtk

from ..i18n import _

SEARCH_URL = "https://www.google.com/search?q={}"
GROUP = "websearch"
# How much of the selection the menu item quotes.
QUOTE_CHARS = 30


def install(window: Adw.ApplicationWindow) -> None:
    _watch(window, window)
    watched = set()

    def on_dialog(*_args):
        dialog = window.get_visible_dialog()
        if dialog is not None and dialog not in watched:
            watched.add(dialog)
            dialog.connect("closed", watched.discard)
            _watch(window, dialog)

    window.connect("notify::visible-dialog", on_dialog)


def _watch(window: Gtk.Window, widget: Gtk.Widget) -> None:
    click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY, propagation_phase=Gtk.PropagationPhase.CAPTURE)
    click.connect("pressed", lambda _g, _n, x, y: _prepare(window, widget.pick(x, y, Gtk.PickFlags.DEFAULT)))
    widget.add_controller(click)

    keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", lambda _c, keyval, _code, state: _on_key(window, keyval, state))
    widget.add_controller(keys)


def _on_key(window: Gtk.Window, keyval: int, state: Gdk.ModifierType) -> bool:
    if keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK):
        _prepare(window, window.get_focus())
    return False  # let the widget open its menu


def _text_widget(widget: Gtk.Widget | None) -> Gtk.Widget | None:
    """The label, text or text view at ``widget``: itself or the nearest one around it."""
    while widget is not None:
        if isinstance(widget, Gtk.Label):
            return widget if widget.get_selectable() else None
        if isinstance(widget, Gtk.Text):
            return widget if widget.get_visibility() else None  # never offer to search a password
        if isinstance(widget, Gtk.TextView):
            return widget
        widget = widget.get_parent()
    return None


def selected_text(widget: Gtk.Widget) -> str:
    if isinstance(widget, Gtk.TextView):
        bounds = widget.get_buffer().get_selection_bounds()
        return widget.get_buffer().get_text(*bounds, False) if bounds else ""
    if isinstance(widget, Gtk.Label):
        has, start, end = widget.get_selection_bounds()
        return widget.get_text()[start:end] if has else ""
    bounds = widget.get_selection_bounds()  # Gtk.Text: PyGObject gives (start, end), or () for none
    return widget.get_chars(*bounds) if bounds else ""


def _prepare(window: Gtk.Window, picked: Gtk.Widget | None) -> None:
    widget = _text_widget(picked)
    if widget is None:
        return
    text = " ".join(selected_text(widget).split())

    # The selection doesn't change while the menu is open, so the action can keep it.
    action = Gio.SimpleAction.new("search", None)
    action.connect("activate", lambda *_: search(window, text))
    action.set_enabled(bool(text))
    group = Gio.SimpleActionGroup()
    group.add_action(action)
    widget.insert_action_group(GROUP, group)  # replaces the one from an earlier menu

    quoted = text if len(text) <= QUOTE_CHARS else text[: QUOTE_CHARS - 1].rstrip() + "…"
    menu = Gio.Menu()
    menu.append(
        _("Search Google for “{text}”").format(text=quoted) if text else _("Search Google"),
        f"{GROUP}.search",
    )
    widget.set_extra_menu(menu)


def search(window: Gtk.Window, text: str) -> None:
    if text:
        Gtk.UriLauncher.new(SEARCH_URL.format(quote_plus(text))).launch(window, None, None, None)
