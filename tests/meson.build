pytest = find_program('pytest-3', 'pytest', required: false)
if pytest.found()
  test(
    'unit tests',
    pytest,
    args: ['-q', '-p', 'no:cacheprovider', meson.current_source_dir()],
    workdir: meson.project_source_root(),
    timeout: 120,
  )
endif
