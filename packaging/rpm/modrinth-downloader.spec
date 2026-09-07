Name:           modrinth-downloader
Version:        1.1.0
Release:        1%{?dist}
Summary:        Install Modrinth modpacks into the official Minecraft Launcher
License:        MIT
URL:            https://github.com/whyfle/modrinth-downloader
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
Requires:       python3

%description
Bridges Modrinth modpacks (.mrpack) directly into the official Minecraft
Launcher: parse URL, download with hash verification, install
Fabric/Forge/NeoForge/Quilt, inject launcher profile. Includes system
dependency bootstrap via Vista (`--install-deps --pm vista`).

%prep
%setup -q

%build
# nothing to compile (single-file Python)

%install
mkdir -p %{buildroot}%{_bindir}
install -m0755 modpacker.py %{buildroot}%{_bindir}/modrinth-downloader
ln -s modrinth-downloader %{buildroot}%{_bindir}/modpacker
mkdir -p %{buildroot}%{_datadir}/doc/%{name}
cp README.md USER_GUIDE.md %{buildroot}%{_datadir}/doc/%{name}/ 2>/dev/null || true

%files
%{_bindir}/modrinth-downloader
%{_bindir}/modpacker
%{_datadir}/doc/%{name}/

%changelog
* Mon Sep 07 2026 Whyfle <whyfle@local> - 1.1.0-1
- System dependencies via Vista (--check-deps/--install-deps)
