desktop_file = i18n.merge_file(
  input: app_id + '.desktop.in',
  output: app_id + '.desktop',
  type: 'desktop',
  po_dir: '../po',
  install: true,
  install_dir: get_option('datadir') / 'applications',
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('validate desktop file', desktop_utils, args: [desktop_file])
endif

metainfo_file = i18n.merge_file(
  input: app_id + '.metainfo.xml.in',
  output: app_id + '.metainfo.xml',
  po_dir: '../po',
  install: true,
  install_dir: get_option('datadir') / 'metainfo',
)

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('validate metainfo', appstreamcli, args: ['validate', '--no-net', '--explain', metainfo_file])
endif

install_data(app_id + '.gschema.xml', install_dir: get_option('datadir') / 'glib-2.0' / 'schemas')
gnome.compile_schemas(build_by_default: true, depend_files: files(app_id + '.gschema.xml'))

compile_schemas = find_program('glib-compile-schemas', required: false)
if compile_schemas.found()
  test('validate schema', compile_schemas, args: ['--strict', '--dry-run', meson.current_source_dir()])
endif

install_subdir('icons/hicolor', install_dir: get_option('datadir') / 'icons')

install_man('systemd-pilot.1')
