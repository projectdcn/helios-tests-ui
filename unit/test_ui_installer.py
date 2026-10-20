"""The Linux installer bundle (tools/build_ui_installer.py + tools/installer/): what goes into the bundle and what
never does, the wheelhouse for three Python versions (markers evaluated for the target Python), the starter rack
profiles, install.sh --dry-run, and the hooks that let an installed UI keep its code (app/) apart from its results
(HELIOS_RESULTS_ROOT, MANIFEST.txt for the jump-host sync, --version)."""
from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_ui_installer as bi  # noqa: E402

from ui import version as uiversion  # noqa: E402
from ui.config import load_config  # noqa: E402

INSTALLER = ROOT / "tools" / "installer"
PYPROJECT = """[project]
name = "x"
version = "0"
dependencies = ["pytest>=7.4", "PyYAML>=6.0"]
"""


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_AUTHOR_DATE": "2026-10-16T12:00:00", "GIT_COMMITTER_DATE":
           "2026-10-16T12:00:00"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def _repo(tmp_path: Path, with_installer: bool = True) -> Path:
    repo = tmp_path / "repo"
    files = {
        "pyproject.toml": PYPROJECT,
        "ui/__main__.py": "print('ui')\n",
        "ui/server.py": "x = 1\n",
        "tools/select_cases.py": "y = 2\n",
        "testplan/manifest.yaml": "cases: []\n",
        "testplan/full_rack_testplan.xlsx": "XLSX",
        "testplan/HPE plan.xlsx": "XLSX",
        "inventory/example-fullrack.yaml": "rack: x\n",
        "inventory/mr3.local.yaml": "rack: mr3\n",                 # never in a bundle (hostnames)
        "inventory/mr3.local.yaml.bak-20261016-120000": "old\n",
        "results/runs/mr3/mr3-1/tc_summary.csv": "tc_id,verdict\n",
        "notes.bak": "old\n",
    }
    if with_installer:
        for f in ("install.sh", "uninstall.sh", "README-INSTALL.txt", "racks.yaml.template"):
            files[f"tools/installer/{f}"] = (INSTALLER / f).read_text()
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for sh in ("install.sh", "uninstall.sh"):
        if with_installer:
            (repo / "tools/installer" / sh).chmod(0o755)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "add", "-f", ".")
    _git(repo, "commit", "-q", "-m", "c")
    return repo


def _wheel(dest: Path, name: str, requires: list[str] = (), tag: str = "py3-none-any", ver: str = "1.0") -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    w = dest / f"{name}-{ver}-{tag}.whl"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n" + "".join(f"Requires-Dist: {r}\n" for r in requires)
    with zipfile.ZipFile(w, "w") as z:
        z.writestr(f"{name}-{ver}.dist-info/METADATA", meta)
    return w


def _fake_wheelhouse(tmp_path: Path) -> Path:
    wh = tmp_path / "wh"
    _wheel(wh, "pytest", ["pluggy>=1"])
    _wheel(wh, "pluggy")
    for py in ("310", "311", "312"):
        _wheel(wh, "PyYAML", tag=f"cp{py}-cp{py}-manylinux2014_x86_64", ver="6.0.3")
    (wh / "PYTHONS").write_text("3.10 3.11 3.12\n")
    return wh


def _members(bundle: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(bundle) as t:
        return {m.name: m for m in t.getmembers()}


def _read(bundle: Path, name: str) -> str:
    with tarfile.open(bundle) as t:
        return t.extractfile(name).read().decode()


# ------------------------------------------------------------------ the bundle

def test_excluded():
    for p in ("testplan/full_rack_testplan.xlsx", "testplan/HPE_AMD Helios Juniper ScaleUp TestPlan.xlsx",
              "inventory/mr3.local.yaml", "results/index.csv", "results/runs/mr3/x/tc_summary.csv", "a/b.bak",
              "inventory/x.yaml.bak-20261016-101010", "testplan/~$full.xlsx"):
        assert bi.excluded(p), p
    for p in ("ui/server.py", "testplan/manifest.yaml", "testplan/health-known.yaml", "inventory/example-fullrack.yaml",
              "selftest/inventory.yaml", "tools/results_index.py", "unit/test_results_index.py"):
        assert not bi.excluded(p), p


def test_bundle_contents(tmp_path):
    repo = _repo(tmp_path)
    out = bi.build(repo, tmp_path / "dist", wheelhouse=_fake_wheelhouse(tmp_path), log=lambda s: None)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    assert out.name == f"helios-ui-20261016-{sha[:7]}.tar.gz"
    top = out.name[:-len(".tar.gz")]
    m = _members(out)
    assert all(n.startswith(top + "/") for n in m)
    names = {n[len(top) + 1:] for n in m}
    # the code
    for f in ("app/ui/__main__.py", "app/ui/server.py", "app/tools/select_cases.py", "app/testplan/manifest.yaml",
              "app/inventory/example-fullrack.yaml", "app/BUILD_INFO.json", "app/MANIFEST.txt",
              "app/tools/installer/racks.yaml.template"):
        assert f in names, f
    # never: workbooks, local inventories, results, backups
    assert not [n for n in names if n.endswith(".xlsx") or ".local.yaml" in n or n.startswith("app/results")
                or ".bak" in n]
    # installer at the top, executable
    for f in ("install.sh", "uninstall.sh"):
        assert m[f"{top}/{f}"].mode & 0o111 == 0o111
    assert {"README-INSTALL.txt", "requirements.txt", "VERSION"} <= names
    # the wheelhouse, for three Python versions
    assert _read(out, f"{top}/wheelhouse/PYTHONS").split() == ["3.10", "3.11", "3.12"]
    assert sum(1 for n in names if n.startswith("wheelhouse/") and "PyYAML" in n) == 3
    assert {"wheelhouse/pytest-1.0-py3-none-any.whl", "wheelhouse/pluggy-1.0-py3-none-any.whl"} <= names
    reqs = _read(out, f"{top}/requirements.txt").split()
    assert reqs == ["pytest>=7.4", "PyYAML>=6.0", "openpyxl>=3.1"]
    info = json.loads(_read(out, f"{top}/app/BUILD_INFO.json"))
    assert info["commit"] == sha and info["version"] == f"20261016-{sha[:7]}" and info["pythons"] == ["3.10", "3.11", "3.12"]
    manifest = [ln for ln in _read(out, f"{top}/app/MANIFEST.txt").splitlines() if not ln.startswith("#")]
    assert manifest == sorted(n[4:] for n in names if n.startswith("app/") and n not in ("app/BUILD_INFO.json",
                                                                                        "app/MANIFEST.txt"))


def test_bundle_of_this_repository(tmp_path):
    """The real HEAD: the UI, the tests, the selftest rack and the installer are in; the committed AMD workbook and any
    git-ignored local file are not."""
    out = bi.build(ROOT, tmp_path / "dist", wheelhouse=_fake_wheelhouse(tmp_path), log=lambda s: None)
    top = out.name[:-len(".tar.gz")]
    names = {n[len(top) + 1:] for n in _members(out)}
    for f in ("app/ui/__main__.py", "app/ui/version.py", "app/tools/html_report.py", "app/conftest.py",
              "app/helios/pytest_plugin.py", "app/selftest/inventory.yaml", "app/testplan/health-known.yaml",
              "install.sh", "uninstall.sh", "README-INSTALL.txt"):
        assert f in names, f
    assert any(n.startswith("app/tests/") for n in names)
    assert not [n for n in names if n.endswith(".xlsx") or n.endswith(".local.yaml") or n.startswith("app/results/")]
    assert re.fullmatch(r"helios-ui-(\d{8}-[0-9a-f]{7}|.+)", top)


def test_refuses_a_ref_without_the_installer(tmp_path):
    repo = _repo(tmp_path, with_installer=False)
    with pytest.raises(SystemExit, match="commit the UI and tools/installer/"):
        bi.build(repo, tmp_path / "dist", wheelhouse=_fake_wheelhouse(tmp_path), log=lambda s: None)


def test_wheelhouse_downloads_per_python_and_target_markers(tmp_path):
    """pip download runs once per Python (manylinux, binary only); a dependency behind a marker of the TARGET Python
    (pip evaluates the host's) is fetched in a second round for that Python only."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        dest = Path(argv[argv.index("--dest") + 1])
        req = Path(argv[argv.index("-r") + 1]).read_text().split()
        if req[0].startswith("tomli"):
            _wheel(dest, "tomli")
        else:
            _wheel(dest, "pytest", ['tomli>=1; python_version < "3.11"', 'colorama; sys_platform == "win32"'], ver="8.0")
        return subprocess.CompletedProcess(argv, 0, "", "")

    req = tmp_path / "requirements.txt"
    req.write_text("pytest>=7.4\n")
    names = bi.build_wheelhouse(req, tmp_path / "wh", run=run, log=lambda s: None)
    assert names == ["pytest-8.0-py3-none-any.whl", "tomli-1.0-py3-none-any.whl"]
    assert (tmp_path / "wh" / "PYTHONS").read_text().split() == ["3.10", "3.11", "3.12"]
    pys = [a[a.index("--python-version") + 1] for a in calls]
    assert pys == ["3.10", "3.10", "3.11", "3.12"]          # the extra round: 3.10 only (tomli)
    for a in calls:
        assert a[3] == "download" and "--only-binary=:all:" in a and a[a.index("--implementation") + 1] == "cp"
        plats = [a[i + 1] for i, x in enumerate(a) if x == "--platform"]
        assert "manylinux2014_x86_64" in plats and all(p.endswith("x86_64") for p in plats)


def test_missing_for_checks_tags_and_versions(tmp_path):
    wh = tmp_path / "wh"
    _wheel(wh, "PyYAML", tag="cp312-cp312-manylinux2014_x86_64", ver="6.0.3")
    _wheel(wh, "cryptography", tag="cp311-abi3-manylinux_2_28_x86_64", ver="50.0")
    _wheel(wh, "mac_only", tag="cp312-cp312-macosx_11_0_arm64")
    assert bi.missing_for(["PyYAML>=6"], wh, "3.12") == []
    assert bi.missing_for(["PyYAML>=6"], wh, "3.11") == ["PyYAML>=6"]
    assert bi.missing_for(["PyYAML>=7"], wh, "3.12") == ["PyYAML>=7"]
    assert bi.missing_for(["cryptography"], wh, "3.12") == []          # abi3 >= cp311
    assert bi.missing_for(["cryptography"], wh, "3.10") == ["cryptography"]
    assert bi.missing_for(["mac_only"], wh, "3.12") == ["mac_only"]


# ------------------------------------------------------------------ starter rack profiles

def test_starter_racks_yaml_mirrors_ui_racks_and_points_into_data(tmp_path):
    prefix = tmp_path / "helios-ui"
    text = (INSTALLER / "racks.yaml.template").read_text().replace("@PREFIX@", str(prefix))
    f = tmp_path / "racks.yaml"
    f.write_text(text)
    got, dev = load_config(f), load_config(ROOT / "ui" / "racks.yaml")
    assert set(got) == set(dev)
    for n, p in got.items():
        d = dev[n]
        assert (p.runner, p.title, p.known) == (d.runner, d.title, d.known)
        assert p.results_dir == prefix / "data" / "results" / "runs" / n
        assert p.workbook == prefix / "data" / "testplan" / "full_rack_testplan.xlsx"
        if n == "selftest":
            assert p.inventory == ROOT / "selftest" / "inventory.yaml"        # part of the code
        else:
            assert p.inventory.parent == prefix / "data" / "inventories" and p.inventory.name == d.inventory.name
        if d.ssh:
            assert vars(p.ssh) == vars(d.ssh)
    assert "@PREFIX@" not in text and "password:" not in text.lower().replace("password_env", "")


# ------------------------------------------------------------------ install.sh / uninstall.sh

def test_scripts_parse_and_are_executable_in_git():
    for f in ("install.sh", "uninstall.sh"):
        subprocess.run(["bash", "-n", str(INSTALLER / f)], check=True)
        mode = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", f"tools/installer/{f}"],
                              capture_output=True, text=True).stdout.split()
        assert mode and mode[0] == "100755", f"{f} is not committed as executable"


def _bundle_dir(tmp_path: Path) -> Path:
    out = bi.build(_repo(tmp_path), tmp_path / "dist", wheelhouse=_fake_wheelhouse(tmp_path), log=lambda s: None)
    with tarfile.open(out) as t:
        t.extractall(tmp_path / "x", filter="data")
    return tmp_path / "x" / out.name[:-len(".tar.gz")]


def _sh(cmd: list[str], **env) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          env={**os.environ, "HELIOS_UI_INSTALL_ANY_OS": "1", **env})


def test_install_dry_run_changes_nothing(tmp_path):
    b = _bundle_dir(tmp_path)
    prefix = tmp_path / "home" / "helios-ui"
    r = _sh([str(b / "install.sh"), "--dry-run", "--online", "--no-systemd", "--python", sys.executable,
             "--prefix", str(prefix), "--port", "18799"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert not prefix.exists()
    out = r.stdout
    assert "[dry-run]" in out and f"-m venv {prefix}/venv" in out and f"write {prefix}/config/racks.yaml" in out
    assert f"results_dir: {prefix}/data/results/runs/mr3" in out           # the starter profile, as it would be
    assert "ssh -L 18799:127.0.0.1:18799" in out                        # the alternative
    assert "systemd --user is not available" in out
    # default: every address, said plainly; stored for upgrades
    assert "listen: 0.0.0.0:18799" in out and "-m ui --host 0.0.0.0 --port 18799" in out
    assert "Open:   http://" in out and ":18799/" in out and "WARNING: the UI has NO login" not in out
    assert "generate an access token" in out and "Access token (each browser logs in with it once" in out
    assert "helios-ui token    new one" in out and "token [new]" in out      # the helper's token command
    assert "INSTALLED_HOST=0.0.0.0" in out and "INSTALLED_PORT=18799" in out
    r = _sh([str(b / "install.sh"), "--dry-run", "--online", "--no-systemd", "--python", sys.executable,
             "--prefix", str(prefix), "--port", "18799", "--host", "127.0.0.1"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "listen: 127.0.0.1:18799" in r.stdout and "WARNING: the UI has NO login" not in r.stdout
    assert "then open  http://127.0.0.1:18799/" in r.stdout and "INSTALLED_HOST=127.0.0.1" in r.stdout
    assert "no access token needed" in r.stdout
    r = _sh([str(b / "install.sh"), "--dry-run", "--online", "--no-systemd", "--python", sys.executable,
             "--prefix", str(prefix), "--port", "18799", "--no-token"])
    assert r.returncode == 0 and "WARNING: the UI has NO login" in r.stdout and "Access token (" not in r.stdout


def test_upgrade_keeps_port_and_listens_on_0000_unless_host_given(tmp_path):
    """An upgrade keeps the port but listens on 0.0.0.0 unless --host is given - also when the installation listened
    on 127.0.0.1 (srv03 19 Oct: the upgrade stayed on 127.0.0.1 and the UI could not be opened from the laptop)."""
    b = _bundle_dir(tmp_path)
    prefix = tmp_path / "helios-ui"
    (prefix / "app").mkdir(parents=True)
    (prefix / ".install-state").write_text("INSTALLED_PORT=18800\nINSTALLED_MODE=systemd\nINSTALLED_VERSION=old\n")
    base = [str(b / "install.sh"), "--dry-run", "--online", "--no-systemd", "--python", sys.executable,
            "--prefix", str(prefix)]
    r = _sh(base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "listen: 0.0.0.0:18800" in r.stdout and "upgrade to" in r.stdout
    (prefix / ".install-state").write_text("INSTALLED_PORT=18800\nINSTALLED_HOST=127.0.0.1\nINSTALLED_MODE=nohup\n")
    out = _sh(base).stdout
    assert "listen: 0.0.0.0:18800" in out and "note: the installation listened on 127.0.0.1" in out
    out = _sh(base + ["--host", "127.0.0.1"]).stdout
    assert "listen: 127.0.0.1:18800" in out and "note:" not in out
    assert "listen: 0.0.0.0:18801" in _sh(base + ["--host", "0.0.0.0", "--port", "18801"]).stdout


def test_install_refuses_bad_input(tmp_path):
    b = _bundle_dir(tmp_path)
    base = [str(b / "install.sh"), "--dry-run", "--no-systemd", "--prefix", str(tmp_path / "p")]
    r = _sh(base + ["--port", "80"])
    assert r.returncode != 0 and "1024-65535" in r.stderr
    r = _sh(base + ["--host", "1.2.3.4;rm"])
    assert r.returncode != 0 and "--host '1.2.3.4;rm'" in r.stderr
    r = _sh(base + ["--prefix", str(tmp_path / "with space")])
    assert r.returncode != 0 and "no spaces" in r.stderr
    # offline: the wheelhouse has 3.10-3.12 only
    if sys.version_info[:2] not in ((3, 10), (3, 11), (3, 12)):
        r = _sh(base + ["--python", sys.executable])
        assert r.returncode != 0 and "wheels for Python 3.10 3.11 3.12 only" in r.stderr
    r = subprocess.run([str(b / "install.sh"), "--dry-run"], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "HELIOS_UI_INSTALL_ANY_OS"})
    if sys.platform != "linux":
        assert r.returncode != 0 and "for Linux" in r.stderr


def test_install_refuses_a_taken_port(tmp_path):
    b = _bundle_dir(tmp_path)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        # not a dry run, but it stops before writing anything: the port check comes first
        r = _sh([str(b / "install.sh"), "--online", "--no-systemd", "--python", sys.executable,
                 "--prefix", str(tmp_path / "p"), "--port", str(port)])
    finally:
        s.close()
    assert r.returncode != 0 and f"port {port} on 0.0.0.0 is already in use by another program" in r.stderr
    assert not (tmp_path / "p" / "app").exists()


def test_uninstall_refuses_what_is_not_an_installation(tmp_path):
    (tmp_path / "x").mkdir()
    r = _sh([str(INSTALLER / "uninstall.sh"), "--prefix", str(tmp_path / "x"), "--dry-run"])
    assert r.returncode != 0 and "not a helios-ui installation" in r.stderr


# ------------------------------------------------------------------ the hooks of an installed UI

def test_version_of_a_bundle_and_of_a_checkout(tmp_path):
    (tmp_path / "BUILD_INFO.json").write_text(json.dumps({"version": "20261016-abc1234", "commit": "abc"}))
    assert uiversion.version(tmp_path) == "20261016-abc1234"
    assert uiversion.version(ROOT).startswith("git ")
    assert uiversion.version(tmp_path / "nothing") == "unknown"
    r = subprocess.run([sys.executable, "-m", "ui", "--version"], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0 and r.stdout.startswith("helios-ui git ")


def test_port_in_use_is_a_clear_error(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        r = subprocess.run([sys.executable, "-m", "ui", "--port", str(s.getsockname()[1]), "--config",
                            str(ROOT / "ui" / "racks.yaml")],
                           cwd=ROOT, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT)})
    finally:
        s.close()
    assert r.returncode == 2 and "already in use" in r.stderr


def test_jump_host_sync_from_an_installed_bundle(tmp_path, monkeypatch):
    """No git in an installed app/: the files MANIFEST.txt lists are sent (never a workbook, a symlink or anything
    outside app/), under the same prefix git archive would use."""
    from ui import runner
    from unit.test_ui_runner import FakeExecutor, _profile
    app = tmp_path / "app"
    for f, t in (("ui/server.py", "x"), ("tools/rack_health.py", "y"), ("testplan/a.xlsx", "XLSX")):
        (app / f).parent.mkdir(parents=True, exist_ok=True)
        (app / f).write_text(t)
    (tmp_path / "secret.txt").write_text("nope")
    (app / "link.txt").symlink_to(tmp_path / "secret.txt")
    (app / "BUILD_INFO.json").write_text(json.dumps({"version": "v1", "commit": "c0ffee"}))
    (app / "MANIFEST.txt").write_text("# x\nui/server.py\ntools/rack_health.py\ntestplan/a.xlsx\nlink.txt\n"
                                      "../secret.txt\nmissing.py\n")
    monkeypatch.setattr(runner, "ROOT", app)
    ex = FakeExecutor("g02-g17-20261015-120000")
    lines = []
    runner.SshRunner(_profile(tmp_path), ex).sync_code(lines.append)
    assert not [a for a, _ in ex.calls if a[0] == "git"]
    data = next(i for a, i in ex.calls if a[0] == "ssh" and "tar -xf -" in a[-1])
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        assert sorted(t.getnames()) == ["helios-tests/tools/rack_health.py", "helios-tests/ui/server.py"]
        assert t.extractfile("helios-tests/ui/server.py").read() == b"x"
    assert "installed bundle v1 (commit c0ffee" in lines[0]


def test_results_root_env_hook(tmp_path, monkeypatch):
    import results_index
    import results_retention
    monkeypatch.delenv("HELIOS_RESULTS_ROOT", raising=False)
    assert results_index.default_results_root() == ROOT / "results"
    assert results_retention.default_results_root() == ROOT / "results"
    monkeypatch.setenv("HELIOS_RESULTS_ROOT", str(tmp_path / "data" / "results"))
    assert results_index.default_results_root() == tmp_path / "data" / "results"
    assert results_retention.default_results_root() == tmp_path / "data" / "results"
    # the command lines take it as their default
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "results_index.py"), "--help"], capture_output=True,
                       text=True, env={**os.environ, "HELIOS_RESULTS_ROOT": str(tmp_path / "r")})
    assert r.returncode == 0


def test_rack_health_and_plugin_default_results_dir_follow_the_env(tmp_path, monkeypatch):
    """rack_health.py / pytest without --results-dir: <HELIOS_RESULTS_ROOT>/runs/<rack>, not results/ of the code."""
    import rack_health
    monkeypatch.delenv("HELIOS_RESULTS_ROOT", raising=False)
    assert rack_health.default_results_dir("rk") == ROOT / "results" / "runs" / "rk"
    monkeypatch.setenv("HELIOS_RESULTS_ROOT", str(tmp_path / "res"))
    assert rack_health.default_results_dir("rk") == tmp_path / "res" / "runs" / "rk"
    assert rack_health.default_results_dir(None) == tmp_path / "res"
    assert 'os.environ["HELIOS_RESULTS_ROOT"]' in (ROOT / "helios" / "pytest_plugin.py").read_text()


def test_linux_keychain_note_and_version_endpoint(tmp_path, monkeypatch):
    """On Linux the page gets available: false + a note (the Remember box is disabled, the note says to type the
    password per run); GET /api/version answers for the installer's health check."""
    import http.client

    from ui import keychain as kcmod
    from ui.config import RackProfile
    from ui.keychain import Keychain
    from ui.server import App, make_server
    monkeypatch.setattr(kcmod.sys, "platform", "linux")
    inv = tmp_path / "inv.yaml"
    inv.write_text(yaml.safe_dump({"rack": "rk", "afm": {"auth": {"username": "admin"}}}))
    prof = RackProfile(name="rk", title="rk", runner="local", inventory=inv, results_dir=tmp_path / "res" / "runs" / "rk")
    app = App(profiles={"rk": prof}, keychain=Keychain())
    st = app.password_status("rk")
    assert st["available"] is False and st["saved"] is False
    assert st["note"] == "macOS Keychain not available on this system: type the AFM password for each run"
    srv = make_server(app, 0)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", "/api/version")
        r = c.getresponse()
        d = json.loads(r.read())
        assert r.status == 200 and d["version"] == uiversion.version() and d["root"] == str(ROOT)
        c.request("GET", "/api/racks/rk/password")
        assert json.loads(c.getresponse().read())["note"].startswith("macOS Keychain not available on this system")
    finally:
        srv.shutdown()
        srv.server_close()
    js = (ROOT / "ui" / "static" / "app.js").read_text()
    assert "d.note || d.error" in js and '$("#afm-pw-save").disabled = !d.available' in js


# ------------------------------------------------------------------ --host

def test_host_option_and_env():
    from ui.__main__ import parse_args, urls
    assert parse_args([], environ={}).host == "127.0.0.1"                 # the Mac dev run: unchanged
    assert parse_args([], environ={"HELIOS_UI_HOST": "0.0.0.0"}).host == "0.0.0.0"
    assert parse_args(["--host", "::"], environ={"HELIOS_UI_HOST": "0.0.0.0"}).host == "::"   # the option wins
    assert parse_args([], environ={"HELIOS_UI_HOST": "  "}).host == "127.0.0.1"
    for bad in ("a b", "http://x", "[::1]"):
        with pytest.raises(SystemExit):
            parse_args(["--host", bad], environ={})
    assert urls("127.0.0.1", 8765) == ["http://127.0.0.1:8765/"]
    u = urls("0.0.0.0", 8000)
    assert u[0] == f"http://{socket.getfqdn()}:8000/" and u[-1] == "http://127.0.0.1:8000/"
    assert urls("10.1.2.3", 80) == ["http://10.1.2.3:80/"] and urls("fe80::1", 80) == ["http://[fe80::1]:80/"]


def test_host_header_check_beyond_loopback(monkeypatch):
    """Bound to 0.0.0.0 the page may be opened by the server's names / the address the request came in on /
    HELIOS_UI_ALLOWED_HOSTS; any other Host (DNS rebinding) is refused. Loopback-bound: loopback names only."""
    from ui import server
    monkeypatch.setenv("HELIOS_UI_ALLOWED_HOSTS", "helios.lab, Alias2")
    names = server.host_names()
    assert {"helios.lab", "alias2", socket.gethostname().lower()} <= names

    class Conn:
        def getsockname(self):
            return ("10.9.8.7", 8765)

    def ok(host_header, server_names):
        h = type("H", (), {"headers": {"Host": host_header}, "server_names": server_names, "connection": Conn()})()
        return server.Handler._host_ok(h)
    assert ok("127.0.0.1:8765", frozenset()) and ok("localhost", frozenset())
    assert not ok("10.9.8.7:8765", frozenset())                            # loopback-bound: refused
    assert ok("10.9.8.7:8765", names) and ok("HELIOS.lab:8765", names) and ok(f"{socket.gethostname()}:1", names)
    assert not ok("evil.example:8765", names) and not ok("", names)


def test_port_in_use_names_the_host(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        r = subprocess.run([sys.executable, "-m", "ui", "--host", "127.0.0.1", "--port", str(s.getsockname()[1])],
                           cwd=ROOT, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
    finally:
        s.close()
    assert r.returncode == 2 and "on 127.0.0.1 is already in use" in r.stderr


def test_bundle_carries_helios_debug_next_to_app(tmp_path):
    """The link check repo goes in as helios-debug/ (committed HEAD, minus local inventories and results), with its
    commit in BUILD_INFO; install.sh copies it next to app/ where the HC-012 health check looks."""
    import subprocess
    dbg = tmp_path / "helios-debug"
    (dbg / "tools").mkdir(parents=True)
    (dbg / "inventory").mkdir()
    (dbg / "tools" / "link_check.py").write_text("print('link check')\n")
    (dbg / "inventory" / "example.yaml").write_text("rack: x\n")
    (dbg / "inventory" / "lab.local.yaml").write_text("rack: secret\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(dbg), *c], check=True)
    out = bi.build(_repo(tmp_path), tmp_path / "dist", wheelhouse=_fake_wheelhouse(tmp_path), log=lambda s: None,
                   debug_repo=dbg)
    with tarfile.open(out) as t:
        names = t.getnames()
        top = names[0].split("/")[0]
        info = json.loads(t.extractfile(f"{top}/app/BUILD_INFO.json").read())
    assert f"{top}/helios-debug/tools/link_check.py" in names and f"{top}/helios-debug/inventory/example.yaml" in names
    assert not any(n.endswith("lab.local.yaml") for n in names)
    assert len(info["helios_debug"]["commit"]) == 40
    assert 'step "link check -> $PREFIX/helios-debug"' in (ROOT / "tools/installer/install.sh").read_text()


@pytest.mark.skipif(sys.platform != "linux", reason="needs a second loopback address (127.0.0.2), Linux only")
def test_upgrade_keeps_loopback_when_a_proxy_holds_the_port_elsewhere(tmp_path):
    """srv03 19 Oct: nginx listens on the server's address, port 8765, in front of the UI on 127.0.0.1:8765 - the
    upgrade must stay on 127.0.0.1 instead of failing on 0.0.0.0; an explicit --host is never overridden."""
    b = _bundle_dir(tmp_path)
    prefix = tmp_path / "helios-ui"
    (prefix / "app").mkdir(parents=True)
    proxy = socket.socket()
    proxy.bind(("127.0.0.2", 0))
    proxy.listen()
    port = proxy.getsockname()[1]
    (prefix / ".install-state").write_text(f"INSTALLED_PORT={port}\nINSTALLED_HOST=127.0.0.1\nINSTALLED_MODE=nohup\n")
    base = [str(b / "install.sh"), "--dry-run", "--online", "--no-systemd", "--python", sys.executable, "--prefix", str(prefix)]
    fresh = [str(b / "install.sh"), "--dry-run", "--online", "--no-systemd", "--python", sys.executable,
             "--prefix", str(tmp_path / "fresh"), "--port", str(port)]
    try:
        r, r2, r3 = _sh(base), _sh(base + ["--host", "0.0.0.0"]), _sh(fresh)
    finally:
        proxy.close()
    # srv03 20 Oct: /root/helios-ui was gone (no install state) - the proxy case must still listen on loopback
    assert r3.returncode == 0 and f"listening on 127.0.0.1:{port} behind it" in r3.stdout, r3.stdout + r3.stderr
    assert "generate an access token" in r3.stdout and f"https://127.0.0.2:{port}/" in r3.stdout
    assert r.returncode == 0 and f"listening on 127.0.0.1:{port} behind it" in r.stdout, r.stdout + r.stderr
    assert "behind it" not in r2.stdout
