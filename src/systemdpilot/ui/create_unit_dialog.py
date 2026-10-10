"""Write a new unit file, or edit an existing one, and optionally start or enable it."""

from __future__ import annotations

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..core.errors import InvalidUnitName, UnitExists
from ..core.manager import SystemdManager
from ..core.models import Scope, UnitAction
from ..core.templates import TEMPLATES, render
from ..core.validation import normalize_service_name
from . import prompts
from .operations import Operations
from .resources import template

try:
    import gi

    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
except (ValueError, ImportError):
    GtkSource = None


def _make_editor() -> tuple[Gtk.TextView, Gtk.TextBuffer]:
    if GtkSource is None:
        view = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        return view, view.get_buffer()

    buffer = GtkSource.Buffer()
    language = GtkSource.LanguageManager.get_default().get_language("ini")
    if language:
        buffer.set_language(language)
    view = GtkSource.View(
        buffer=buffer,
        monospace=True,
        show_line_numbers=True,
        auto_indent=True,
        highlight_current_line=True,
        top_margin=8,
        bottom_margin=8,
    )

    def update_scheme(*_args):
        dark = Adw.StyleManager.get_default().get_dark()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("Adwaita-dark" if dark else "Adwaita")
        if scheme:
            buffer.set_style_scheme(scheme)

    Adw.StyleManager.get_default().connect("notify::dark", update_scheme)
    update_scheme()
    return view, buffer


@template("create-unit-dialog.ui")
class CreateUnitDialog(Adw.Dialog):
    __gtype_name__ = "SystemdPilotCreateUnitDialog"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    header_title: Adw.WindowTitle = Gtk.Template.Child()
    create_button: Gtk.Button = Gtk.Template.Child()
    name_row: Adw.EntryRow = Gtk.Template.Child()
    template_row: Adw.ComboRow = Gtk.Template.Child()
    enable_row: Adw.SwitchRow = Gtk.Template.Child()
    start_row: Adw.SwitchRow = Gtk.Template.Child()
    editor_scroll: Gtk.ScrolledWindow = Gtk.Template.Child()

    def __init__(
        self,
        manager: SystemdManager,
        scope: Scope,
        machine_label: str,
        operations: Operations,
        *,
        on_created: Callable[[str], None],
        edit_name: str = "",
        edit_content: str = "",
    ):
        super().__init__()
        self.manager = manager
        self.scope = scope
        self.operations = operations
        self._on_created = on_created
        self._editing = bool(edit_name)
        self._edited = False

        scope_label = _("user service") if scope is Scope.USER else _("system service")
        if self._editing:
            short = edit_name.removesuffix(".service") if edit_name.endswith(".service") else edit_name
            self.set_title(_("Edit Service"))
            self.header_title.set_title(_("Edit {unit}").format(unit=short))
            self.create_button.set_label(_("_Save"))
            self.create_button.set_sensitive(True)
            self.name_row.set_text(edit_name)
            self.name_row.set_sensitive(False)
            self.template_row.set_visible(False)
            self.enable_row.set_visible(False)
            self.start_row.set_visible(False)
        else:
            self.header_title.set_title(_("New Service"))
        self.header_title.set_subtitle(f"{machine_label} · {scope_label}")

        self.view, self.buffer = _make_editor()
        self.editor_scroll.set_child(self.view)
        self.template_row.set_model(Gtk.StringList.new([t.title for t in TEMPLATES]))
        if self._editing:
            self._loading = True
            self.buffer.set_text(edit_content)
            self._loading = False
        else:
            self._load_template()
        self.buffer.connect("changed", self._on_buffer_changed)

    def _load_template(self):
        template = TEMPLATES[self.template_row.get_selected()]
        self._loading = True
        self.buffer.set_text(render(template, self.scope is Scope.USER))
        self._loading = False
        self._edited = False

    def _on_buffer_changed(self, _buffer):
        if not getattr(self, "_loading", False):
            self._edited = True

    def _name(self) -> str | None:
        try:
            return normalize_service_name(self.name_row.get_text())
        except InvalidUnitName:
            return None

    @Gtk.Template.Callback()
    def on_name_changed(self, row):
        if self._editing:
            return
        valid = self._name() is not None
        empty = not row.get_text().strip()
        if valid or empty:
            row.remove_css_class("error")
        else:
            row.add_css_class("error")
        self.create_button.set_sensitive(valid)

    @Gtk.Template.Callback()
    def on_template_changed(self, _row, _pspec):
        if self._editing:
            return
        if not self._edited:
            self._load_template()
            return
        prompts.confirm(
            self,
            _("Replace Unit File?"),
            _("Your changes to the unit file will be lost."),
            _("_Replace"),
            self._load_template,
            destructive=True,
        )

    @Gtk.Template.Callback()
    def on_cancel_clicked(self, _button):
        self.close()

    @Gtk.Template.Callback()
    def on_create_clicked(self, _button, overwrite: bool = False):
        name = self._name()
        if not name:
            return
        content = self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)
        enable = not self._editing and self.enable_row.get_active()
        start = not self._editing and self.start_row.get_active()
        manager, scope = self.manager, self.scope
        if self._editing:
            overwrite = True

        def work():
            path = manager.create_unit(name, content, scope, overwrite=overwrite)
            if enable:
                manager.control(name, UnitAction.ENABLE, scope)
            if start:
                manager.control(name, UnitAction.START, scope)
            return path

        def on_error(error):
            if isinstance(error, UnitExists):
                prompts.confirm(
                    self,
                    _("Replace Existing Service?"),
                    _("{path} already exists. Replacing it cannot be undone.").format(path=error.path),
                    _("_Replace"),
                    lambda: self.on_create_clicked(None, overwrite=True),
                    destructive=True,
                )
                return True
            return False

        def on_success(_path):
            self._on_created(name)
            self.close()

        self.set_sensitive(False)
        self.operations.run(
            manager,
            work,
            on_success=on_success,
            on_error=on_error,
            on_finish=lambda: self.set_sensitive(True),
            error_heading=_("Could Not Save Service") if self._editing else _("Could Not Create Service"),
        )
