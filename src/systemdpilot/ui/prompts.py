"""Small modal questions: passwords, host keys, confirmations, errors."""

from __future__ import annotations

import shlex
from collections.abc import Callable

from gi.repository import Adw, Gtk

from ..core.errors import CommandError, PilotError
from ..i18n import _
from . import widgets

# Taller output scrolls inside the dialog.
OUTPUT_MAX_HEIGHT = 300


def show_error(parent: Gtk.Widget, heading: str, error: BaseException | str) -> None:
    """Tell what went wrong in text that can be selected and copied.

    A failed command shows as a card of its command line and output.
    """
    if isinstance(error, CommandError):
        text = error.stderr.strip() or _("Exited with status {code}").format(code=error.returncode)
        if error.argv:
            text = f"$ {shlex.join(error.argv)}\n{text}"
        body = _output_card(text)
    else:
        text = str(error) if isinstance(error, (str, PilotError)) else f"{type(error).__name__}: {error}"
        body = widgets.label(text, "error-message", wrap=True, selectable=True)

    dialog = Adw.Dialog(title=heading, content_width=560)
    copy = Gtk.Button(label=_("_Copy"), use_underline=True, css_classes=["pill"])
    copy.connect("clicked", lambda b: (b.get_clipboard().set(text), b.set_label(_("Copied"))))
    close = Gtk.Button(label=_("_Close"), use_underline=True, css_classes=["pill", "suggested-action"])
    close.connect("clicked", lambda _b: dialog.close())
    buttons = Gtk.Box(spacing=12, halign=Gtk.Align.END, css_classes=["error-buttons"])
    buttons.append(copy)
    buttons.append(close)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, css_classes=["error-content"])
    content.append(body)
    content.append(buttons)
    view = Adw.ToolbarView(content=content)
    view.add_top_bar(Adw.HeaderBar())
    dialog.set_child(view)
    # Not the text: focusing a selectable label selects all of it.
    dialog.props.focus_widget = close
    dialog.set_default_widget(close)
    dialog.present(parent)


def _output_card(text: str) -> Gtk.Widget:
    output = widgets.label(text, "monospace", "error-output", wrap=True, selectable=True, valign=Gtk.Align.START)
    scroller = Gtk.ScrolledWindow(
        child=output,
        hscrollbar_policy=Gtk.PolicyType.NEVER,
        propagate_natural_height=True,
        max_content_height=OUTPUT_MAX_HEIGHT,
    )
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"])
    card.append(widgets.label(_("Command output"), "error-output-title"))
    card.append(Gtk.Separator())
    card.append(scroller)
    return card


def confirm(
    parent: Gtk.Widget,
    heading: str,
    body: str,
    action_label: str,
    on_confirm: Callable[[], None],
    destructive: bool = False,
) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("confirm", action_label)
    dialog.set_response_appearance(
        "confirm", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED
    )
    dialog.set_default_response("cancel" if destructive else "confirm")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: on_confirm() if r == "confirm" else None)
    dialog.present(parent)


def ask_password(
    parent: Gtk.Widget,
    heading: str,
    body: str,
    on_response: Callable[[str | None, bool], None],
    *,
    error: str = "",
    offer_remember: bool = False,
) -> None:
    """Ask for a password. Calls ``on_response(password_or_None, remember)``."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("ok", _("_Authenticate"))
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    if error:
        label = Gtk.Label(label=error, wrap=True, css_classes=["error"])
        box.append(label)
    group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    entry = Adw.PasswordEntryRow(title=_("Password"))
    entry.set_activates_default(True)  # Enter picks the default response, "ok"
    group.append(entry)
    remember = Adw.SwitchRow(title=_("Remember Password"))
    if offer_remember:
        group.append(remember)
    box.append(group)
    dialog.set_extra_child(box)

    def on_dialog_response(_dialog, response):
        if response == "ok":
            on_response(entry.get_text(), remember.get_active())
        else:
            on_response(None, False)

    dialog.connect("response", on_dialog_response)
    dialog.present(parent)
    entry.grab_focus()


def ask_trust_host_key(
    parent: Gtk.Widget, host: str, key_type: str, fingerprint: str, on_response: Callable[[bool], None]
) -> None:
    dialog = Adw.AlertDialog(
        heading=_("Unknown Host Key"),
        body=_(
            "The identity of “{host}” could not be verified. Compare this fingerprint with the "
            "server's before continuing.\n\n{key_type}\n{fingerprint}"
        ).format(host=host, key_type=key_type, fingerprint=fingerprint),
    )
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("trust", _("_Trust and Connect"))
    dialog.set_response_appearance("trust", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: on_response(r == "trust"))
    dialog.present(parent)
