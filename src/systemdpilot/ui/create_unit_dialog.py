"""Write new unit files, or edit an existing one, and optionally start or enable it."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from ..core.errors import InvalidUnitName, UnitActivationFailed, UnitExists
from ..core.local import LocalRunner
from ..core.manager import SystemdManager
from ..core.models import Scope, short_unit_name
from ..core.templates import TEMPLATES, render, set_exec_start, unit_names
from ..core.validation import validate_unit_name
from ..i18n import _
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

    handler = Adw.StyleManager.get_default().connect("notify::dark", update_scheme)
    view.connect("destroy", lambda _v: Adw.StyleManager.get_default().disconnect(handler))
    update_scheme()
    return view, buffer


def _text(buffer: Gtk.TextBuffer) -> str:
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)


@template("create-unit-dialog.ui")
class CreateUnitDialog(Adw.Dialog):
    __gtype_name__ = "SystemdPilotCreateUnitDialog"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    header_title: Adw.WindowTitle = Gtk.Template.Child()
    create_button: Gtk.Button = Gtk.Template.Child()
    name_row: Adw.EntryRow = Gtk.Template.Child()
    scope_row: Adw.ComboRow = Gtk.Template.Child()
    template_row: Adw.ComboRow = Gtk.Template.Child()
    program_row: Adw.ActionRow = Gtk.Template.Child()
    enable_row: Adw.SwitchRow = Gtk.Template.Child()
    start_row: Adw.SwitchRow = Gtk.Template.Child()
    editor_switcher: Gtk.StackSwitcher = Gtk.Template.Child()
    editor_stack: Gtk.Stack = Gtk.Template.Child()

    def __init__(
        self,
        manager: SystemdManager,
        scope: Scope | None,
        machine_label: str,
        operations: Operations,
        *,
        on_created: Callable[[str], None],
        edit_name: str = "",
        edit_content: str = "",
        override: bool = False,
    ):
        super().__init__()
        self.manager = manager
        # A new unit asks which systemd runs it; an edited one keeps its own.
        self.scope = scope or Scope.SYSTEM
        self._machine_label = machine_label
        self.operations = operations
        self._on_created = on_created
        self._editing = bool(edit_name)
        # A drop-in beside the unit file, as "systemctl edit" makes, rather than the file itself.
        self._override = self._editing and override
        self._edited = False
        self._loading = False
        # One editor per file of the template: (unit type, buffer, stack page).
        self._editors: list[tuple[str, Gtk.TextBuffer, Gtk.StackPage]] = []
        # The template the editors were filled from; the row may differ while asking to replace them.
        self._template_index = 0

        if self._override:
            self.set_title(_("Edit Override"))
            self.header_title.set_title(_("Override {unit}").format(unit=short_unit_name(edit_name)))
        elif self._editing:
            self.set_title(_("Edit Unit"))
            self.header_title.set_title(_("Edit {unit}").format(unit=short_unit_name(edit_name)))
        if self._editing:
            self.create_button.set_label(_("_Save"))
            self.create_button.set_sensitive(True)
            self.name_row.set_text(edit_name)
            self.name_row.set_sensitive(False)
            for row in (self.scope_row, self.template_row, self.program_row, self.enable_row, self.start_row):
                row.set_visible(False)
        else:
            self.header_title.set_title(_("New Unit"))
            # Only a file on this computer can be picked: in a sandbox the
            # picker hands out paths that only exist inside it.
            runner = manager.runner
            self.program_row.set_visible(isinstance(runner, LocalRunner) and not runner.sandboxed)
        self._show_scope()

        self.template_row.set_model(Gtk.StringList.new([t.title for t in TEMPLATES]))
        if self._editing:
            self._set_files([("", edit_content)])
        else:
            self._load_template()

    @property
    def _template(self):
        return TEMPLATES[self._template_index]

    def _show_scope(self):
        user = self.scope is Scope.USER
        scope_label = _("user unit") if user else _("system unit")
        self.header_title.set_subtitle(f"{self._machine_label} · {scope_label}")
        self.enable_row.set_title(_("Start at Login") if user else _("Start at Boot"))

    # -- editors ----------------------------------------------------------

    def _set_files(self, files: list[tuple[str, str]]):
        """Show one editor per file, reusing the ones there are."""
        while len(self._editors) < len(files):
            view, buffer = _make_editor()
            scroll = Gtk.ScrolledWindow(child=view, vexpand=True, min_content_height=240)
            page = self.editor_stack.add_titled(scroll, None, "")
            buffer.connect("changed", self._on_buffer_changed)
            self._editors.append(("", buffer, page))
        while len(self._editors) > len(files):
            _type, _buffer, page = self._editors.pop()
            self.editor_stack.remove(page.get_child())
        self._loading = True
        for i, (unit_type, content) in enumerate(files):
            _old, buffer, page = self._editors[i]
            self._editors[i] = (unit_type, buffer, page)
            buffer.set_text(content)
        self._loading = False
        self._edited = False
        self.editor_switcher.set_visible(len(files) > 1)
        self.editor_stack.set_visible_child(self._editors[0][2].get_child())
        self._update_names()

    def _contents(self) -> list[str]:
        return [_text(buffer) for _type, buffer, _page in self._editors]

    def _load_template(self):
        self._template_index = self.template_row.get_selected()
        self._set_files(render(self._template, self.scope is Scope.USER))

    def _on_buffer_changed(self, _buffer):
        if not self._loading:
            self._edited = True

    # -- names ------------------------------------------------------------

    def _names(self) -> list[str] | None:
        """The full names of the files to write, or None while the typed name is not valid."""
        try:
            if self._editing:  # any unit's file can be edited
                return [validate_unit_name(self.name_row.get_text())]
            return unit_names(self._template, self.name_row.get_text())
        except InvalidUnitName:
            return None

    def _update_names(self):
        """Name the editors' tabs, and check the name."""
        names = self._names()
        for i, (unit_type, _buffer, page) in enumerate(self._editors):
            page.set_title(names[i] if names else "." + unit_type)
        if self._editing:
            return
        text = self.name_row.get_text().strip()
        existing = [n for n in names or [] if self.manager.known_unit_file(n, self.scope)]
        self.name_row.remove_css_class("error")
        self.name_row.remove_css_class("warning")
        if text and names is None:
            self.name_row.add_css_class("error")
        if existing:
            # Only a warning: the list may be out of date, and creating asks before replacing.
            self.name_row.add_css_class("warning")
            self.name_row.set_title(_("Unit Name — {unit} already exists").format(unit=existing[0]))
        else:
            self.name_row.set_title(_("Unit Name"))
        self.create_button.set_sensitive(names is not None)

    @Gtk.Template.Callback()
    def on_name_changed(self, _row):
        if hasattr(self, "_editors"):  # not while being built
            self._update_names()

    # -- choices ----------------------------------------------------------

    @Gtk.Template.Callback()
    def on_scope_changed(self, row, _pspec):
        if not hasattr(self, "_editors") or self._editing:
            return  # still being built, or editing an existing service
        self.scope = Scope.USER if row.get_selected() == 1 else Scope.SYSTEM
        self._show_scope()
        if not self._edited:
            self._load_template()  # user services start with the login session, not at boot
        else:
            self._update_names()

    @Gtk.Template.Callback()
    def on_template_changed(self, _row, _pspec):
        if not hasattr(self, "_editors") or self._editing:
            return
        if self.template_row.get_selected() == self._template_index:
            return  # put back after the replacement was cancelled
        if not self._edited:
            self._load_template()
            return
        prompts.confirm(
            self,
            _("Replace Unit Files?"),
            _("Your changes to the unit files will be lost."),
            _("_Replace"),
            self._load_template,
            destructive=True,
            on_cancel=lambda: self.template_row.set_selected(self._template_index),
        )

    @Gtk.Template.Callback()
    def on_choose_program_clicked(self, _button):
        dialog = Gtk.FileDialog(title=_("Choose a Program"), modal=True)

        def on_done(dlg, result):
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            if file and file.get_path():
                self.set_program(file.get_path())

        dialog.open(self.get_root(), None, on_done)

    def set_program(self, path: str):
        """Make ``path`` the program the service runs, and show it."""
        for unit_type, buffer, page in self._editors:
            if unit_type == "service":
                buffer.set_text(set_exec_start(_text(buffer), path))
                self.editor_stack.set_visible_child(page.get_child())
                return

    @Gtk.Template.Callback()
    def on_cancel_clicked(self, _button):
        self.close()

    # -- writing ----------------------------------------------------------

    @Gtk.Template.Callback()
    def on_create_clicked(self, _button, overwrite: bool = False):
        names = self._names()
        if not names:
            return
        files = list(zip(names, self._contents(), strict=True))
        manager, scope = self.manager, self.scope
        if self._override:
            name, content = files[0]

            def work():
                return manager.write_override(name, content, scope)

            main_name = name
        else:
            if self._editing:
                overwrite = True
                main_name = names[0]
            else:
                main_name = next(n for n in names if n.endswith("." + self._template.activate))
            enable = not self._editing and self.enable_row.get_active()
            start = not self._editing and self.start_row.get_active()

            def work():
                return manager.create_units(
                    files,
                    scope,
                    overwrite=overwrite,
                    enable=enable,
                    start=start,
                    activate=main_name if enable or start else None,
                )

        def on_error(error):
            if isinstance(error, UnitExists):
                prompts.confirm(
                    self,
                    _("Replace Existing Unit?"),
                    _("{path} already exists. Replacing it cannot be undone.").format(path=error.path),
                    _("_Replace"),
                    lambda: self.on_create_clicked(None, overwrite=True),
                    destructive=True,
                )
                return True
            if isinstance(error, UnitActivationFailed):
                # The files are in place, so this dialog is done.
                parent = self.get_root()
                self._on_created(main_name)
                self.close()
                if error.action == "enabled":
                    heading = _("Created, but Could Not Enable {unit}")
                else:
                    heading = _("Created, but Could Not Start {unit}")
                prompts.show_error(parent, heading.format(unit=short_unit_name(main_name)), error)
                return True
            return False

        def on_success(_path):
            self._on_created(main_name)
            self.close()

        self.set_sensitive(False)
        self.operations.run(
            manager,
            work,
            on_success=on_success,
            on_error=on_error,
            on_finish=lambda: self.set_sensitive(True),
            error_heading=self._error_heading(),
        )

    def _error_heading(self) -> str:
        if self._override:
            return _("Could Not Save Override")
        return _("Could Not Save Unit") if self._editing else _("Could Not Create Unit")
