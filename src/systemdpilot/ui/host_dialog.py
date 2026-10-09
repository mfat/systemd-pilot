"""Add or edit a remote host."""

from __future__ import annotations

import copy
from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..core.errors import PilotError
from ..core.hosts import HostStore
from ..core.models import AuthMethod, Host
from .resources import template

_METHODS = [AuthMethod.PASSWORD, AuthMethod.KEY, AuthMethod.AGENT]


@template("host-dialog.ui")
class HostDialog(Adw.Dialog):
    """Emits ``saved(host_id)`` or ``remove-requested(host_id)``."""

    __gtype_name__ = "SystemdPilotHostDialog"
    __gsignals__ = {
        "saved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "remove-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    save_button: Gtk.Button = Gtk.Template.Child()
    name_row: Adw.EntryRow = Gtk.Template.Child()
    hostname_row: Adw.EntryRow = Gtk.Template.Child()
    port_row: Adw.SpinRow = Gtk.Template.Child()
    username_row: Adw.EntryRow = Gtk.Template.Child()
    auth_group: Adw.PreferencesGroup = Gtk.Template.Child()
    auth_row: Adw.ComboRow = Gtk.Template.Child()
    key_row: Adw.ActionRow = Gtk.Template.Child()
    secret_row: Adw.PasswordEntryRow = Gtk.Template.Child()
    remove_group: Adw.PreferencesGroup = Gtk.Template.Child()

    def __init__(self, store: HostStore, host: Host | None = None):
        super().__init__()
        self.store = store
        self.editing = host is not None
        self.host = copy.copy(host) if host else Host(name="", hostname="", username=GLib.get_user_name())
        self._key_path = self.host.key_path

        if self.editing:
            self.set_title(_("Edit Host"))
            self.remove_group.set_visible(True)
        self.name_row.set_text(self.host.name)
        self.hostname_row.set_text(self.host.hostname)
        self.port_row.set_value(self.host.port)
        self.username_row.set_text(self.host.username)
        self.auth_row.set_selected(_METHODS.index(self.host.auth))
        self._update_auth()
        self._validate()

    @property
    def method(self) -> AuthMethod:
        return _METHODS[self.auth_row.get_selected()]

    def _update_auth(self):
        method = self.method
        self.key_row.set_visible(method is AuthMethod.KEY)
        self.key_row.set_subtitle(self._key_path or _("No key selected"))
        self.secret_row.set_visible(method is not AuthMethod.AGENT)
        self.secret_row.set_title(_("Password") if method is AuthMethod.PASSWORD else _("Key Passphrase (optional)"))
        has_saved = self.editing and self.host.auth is method and self.store.secret(self.host)
        self.auth_group.set_description(
            _("Leave empty to keep the saved password.")
            if has_saved and method is AuthMethod.PASSWORD
            else _("Leave empty to keep the saved passphrase.")
            if has_saved
            else _("Leave the password empty to be asked when connecting.")
            if method is AuthMethod.PASSWORD
            else ""
        )

    def _validate(self) -> bool:
        ok = True
        for row in (self.name_row, self.hostname_row, self.username_row):
            empty = not row.get_text().strip()
            ok &= not empty
        if self.method is AuthMethod.KEY and not self._key_path:
            ok = False
        self.save_button.set_sensitive(ok)
        return ok

    @Gtk.Template.Callback()
    def on_field_changed(self, _row):
        self._validate()

    @Gtk.Template.Callback()
    def on_auth_changed(self, _row, _pspec):
        self._update_auth()
        self._validate()

    @Gtk.Template.Callback()
    def on_choose_key_clicked(self, _button):
        dialog = Gtk.FileDialog(title=_("Choose a Private Key"), modal=True)
        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(ssh_dir)))

        def on_done(dlg, result):
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            if file and file.get_path():
                self._key_path = file.get_path()
                self._update_auth()
                self._validate()

        dialog.open(self.get_root(), None, on_done)

    @Gtk.Template.Callback()
    def on_cancel_clicked(self, _button):
        self.close()

    @Gtk.Template.Callback()
    def on_save_clicked(self, _button):
        if not self._validate():
            return
        host = self.host
        old_auth = host.auth
        host.name = self.name_row.get_text()
        host.hostname = self.hostname_row.get_text()
        host.port = int(self.port_row.get_value())
        host.username = self.username_row.get_text()
        host.auth = self.method
        host.key_path = self._key_path if host.auth is AuthMethod.KEY else None

        secret = self.secret_row.get_text()
        if not secret:
            # Keep the stored secret only if it still means the same thing.
            secret = None if old_auth is host.auth else ""
        try:
            self.store.save(host, secret)
        except PilotError as e:
            self.toast_overlay.add_toast(Adw.Toast(title=str(e), use_markup=False, timeout=4))
            return
        self.emit("saved", host.id)
        self.close()

    @Gtk.Template.Callback()
    def on_remove_clicked(self, _button):
        self.emit("remove-requested", self.host.id)
        self.close()
