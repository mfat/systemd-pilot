Name:           systemd-pilot
Version:        4.0.0
Release:        1%{?dist}
Summary:        Manage systemd services locally and over SSH

License:        GPL-3.0-or-later
URL:            https://github.com/mfat/systemd-pilot
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  meson >= 1.0
BuildRequires:  gettext
BuildRequires:  desktop-file-utils
BuildRequires:  appstream
BuildRequires:  glib2-devel
BuildRequires:  python3-devel

Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita >= 1.5
Requires:       libsecret
Requires:       gtksourceview5
Requires:       python3-paramiko
Requires:       polkit
Requires:       systemd

%description
systemd Pilot is a graphical front end for systemctl, built with GTK 4 and
libadwaita. It manages the services on this computer, or on remote machines
over SSH, from one window.

%prep
%autosetup

%build
%meson -Dtests=false -Dpython=%{python3}
%meson_build

%install
%meson_install
%find_lang %{name} || :
# Keeps the list non-empty while there are no translations yet.
echo '%%doc README.md' >> %{name}.lang

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.mfat.systemdpilot.desktop
appstreamcli validate --no-net %{buildroot}%{_metainfodir}/io.github.mfat.systemdpilot.metainfo.xml

%files -f %{name}.lang
%license LICENSE
%{_bindir}/systemd-pilot
%{_mandir}/man1/systemd-pilot.1*
%{_datadir}/systemd-pilot/
%{_datadir}/applications/io.github.mfat.systemdpilot.desktop
%{_metainfodir}/io.github.mfat.systemdpilot.metainfo.xml
%{_datadir}/glib-2.0/schemas/io.github.mfat.systemdpilot.gschema.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.mfat.systemdpilot.svg
%{_datadir}/icons/hicolor/symbolic/apps/io.github.mfat.systemdpilot-symbolic.svg

%changelog
* Fri Oct 09 2026 mFat <newmfat@gmail.com> - 4.0.0-1
- Rewrite for GTK 4 and libadwaita, built with meson
