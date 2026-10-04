Name:           scout-mcp-runtime
Version:        0.1.0
Release:        5%{?dist}
Summary:        Private Python runtime for Scout read-only MCP diagnostics
License:        Apache-2.0 AND MIT AND MIT-0 AND BSD-3-Clause AND PSF-2.0
ExclusiveArch:  x86_64
Source0:        scout-%{version}.tar.gz
Source1:        scout-mcp-wheels.tar.gz
BuildRequires:  python3
BuildRequires:  python3-pip
BuildRequires:  systemd-rpm-macros
Requires:       scout = %{version}-%{release}
Requires:       python3 >= 3.12
Requires:       python3 < 3.13
Requires:       firewalld
Requires:       acl
Requires(pre):  shadow-utils
# Vendored Python distributions are private to this runtime, not system provides.
%global debug_package %{nil}
%global __requires_exclude ^python3.*dist\\(.*\\)$
%global __provides_exclude ^python3.*dist\\(.*\\)$

%if 0%{?rhel} < 10
%{error:scout-mcp-runtime requires Rocky Linux 10}
%endif

%description
Optional pinned MCP SDK runtime, isolated from Scout's base Python dependencies.
The service remains disabled until scout-setup --apply-mcp is run. Wheels are
supplied as a separate source archive; the RPM build never accesses the network.

%prep
%autosetup -n scout-%{version}
mkdir wheelhouse
tar -xzf %{SOURCE1} -C wheelhouse

%build
# Resolve and download wheels before building the RPM, not during this step.

%install
%{__python3} -m venv --without-pip --system-site-packages %{buildroot}%{_libexecdir}/scout-mcp
%{__python3} -m pip --python %{buildroot}%{_libexecdir}/scout-mcp/bin/python install \
    --no-index --no-compile --ignore-installed --require-hashes --find-links=wheelhouse \
    -r packaging/mcp/requirements.lock
sed -i '/^command = /d' %{buildroot}%{_libexecdir}/scout-mcp/pyvenv.cfg
# Entry points installed by dependencies contain buildroot paths and are unused.
find %{buildroot}%{_libexecdir}/scout-mcp/bin -type f -delete
find %{buildroot}%{_libexecdir}/scout-mcp -type d -name __pycache__ -prune -exec rm -rf {} +
install -D -m 0644 packaging/scout-mcp.service %{buildroot}%{_unitdir}/scout-mcp.service

%pre
getent group scout-mcp >/dev/null || groupadd -r scout-mcp
getent passwd scout-mcp >/dev/null || \
  useradd -r -g scout-mcp -d /nonexistent -s /sbin/nologin \
    -c "Scout read-only diagnostics" scout-mcp
exit 0

%post
%systemd_post scout-mcp.service

%preun
%systemd_preun scout-mcp.service

%postun
%systemd_postun_with_restart scout-mcp.service

%files
%license LICENSE
%{_libexecdir}/scout-mcp
%{_unitdir}/scout-mcp.service

%changelog
* Sun Oct 04 2026 Scout contributors <noreply@github.com> - 0.1.0-5
- Package the optional pinned MCP 2.3.0 runtime for Rocky Linux 10
