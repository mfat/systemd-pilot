gnome.compile_resources(
  'systemd-pilot',
  'systemdpilot.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

conf = configuration_data()
python_shebang = get_option('python')
if python_shebang == ''
  python_shebang = '/usr/bin/env python3'
endif
conf.set('PYTHON', python_shebang)
conf.set('VERSION', meson.project_version())
conf.set('localedir', localedir)
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'systemd-pilot.in',
  output: 'systemd-pilot',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)

install_subdir(
  'systemdpilot',
  install_dir: pkgdatadir,
  exclude_files: [
    'ui/window.ui',
    'ui/unit-dialog.ui',
    'ui/host-dialog.ui',
    'ui/create-unit-dialog.ui',
    'ui/style.css',
  ],
  exclude_directories: ['__pycache__', 'core/__pycache__', 'ui/__pycache__'],
)

devenv = environment()
devenv.prepend('PYTHONPATH', meson.current_source_dir())
devenv.set('SYSTEMD_PILOT_RESOURCE', meson.current_build_dir() / 'systemd-pilot.gresource')
devenv.set('SYSTEMD_PILOT_VERSION', meson.project_version() + '-dev')
devenv.set('GSETTINGS_SCHEMA_DIR', meson.project_build_root() / 'data')
meson.add_devenv(devenv)
