project(
  'systemd-pilot',
  version: '4.1.1',
  meson_version: '>= 1.0.0',
  license: 'GPL-3.0-or-later',
  default_options: ['warning_level=2'],
)

app_id = 'io.github.mfat.systemdpilot'

i18n = import('i18n')
gnome = import('gnome')
python = import('python')

# Fails early if Python 3 is missing; the launcher's interpreter is set by the 'python' option.
python.find_installation('python3')

pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
localedir = get_option('prefix') / get_option('localedir')

subdir('data')
subdir('src')
subdir('po')

if get_option('tests')
  subdir('tests')
endif

# Distribution packages refresh these caches with triggers, so the tools
# may be missing from a package build root.
gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: find_program('gtk4-update-icon-cache', 'gtk-update-icon-cache', required: false).found(),
  update_desktop_database: find_program('update-desktop-database', required: false).found(),
)
