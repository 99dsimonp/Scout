Name:           scout
Version:        0.1.0
Release:        1%{?dist}
Summary:        Scout Bitbucket Cloud PR AI review service
License:        Apache-2.0
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros
Requires:       python3
Requires:       python3-tomli
Requires:       git
Requires:       systemd
Requires:       openssh-clients
Requires(pre):  shadow-utils

%description
Scout polls Bitbucket Cloud pull requests, runs the selected agent CLI against
local readonly worktrees, and publishes Code Insights reports and annotations.

%generate_buildrequires
%pyproject_buildrequires -w

%prep
%autosetup -n %{name}-%{version}

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files scout
install -D -m 0644 config/config.toml.example %{buildroot}%{_sysconfdir}/scout/config.toml
install -D -m 0644 config/review.schema.json %{buildroot}%{_sysconfdir}/scout/review.schema.json
install -D -m 0644 packaging/scout.service %{buildroot}%{_unitdir}/scout.service
install -D -m 0755 scripts/setup.sh %{buildroot}%{_bindir}/scout-setup

%pre
getent group scout >/dev/null || groupadd -r scout
getent passwd scout >/dev/null || \
  useradd -r -g scout -d %{_localstatedir}/lib/scout -s /sbin/nologin \
    -c "Scout PR review daemon" scout
exit 0

%post
%systemd_post scout.service

%preun
%systemd_preun scout.service

%postun
%systemd_postun_with_restart scout.service

%files -f %{pyproject_files}
%license LICENSE
%doc README.md DESIGN.md
%{_bindir}/scout
%{_bindir}/scout-setup
%config(noreplace) %{_sysconfdir}/scout/config.toml
%config(noreplace) %{_sysconfdir}/scout/review.schema.json
%{_unitdir}/scout.service

%changelog
* Thu May 14 2026 Scout contributors <noreply@github.com> - 0.1.0-1
- Initial package
