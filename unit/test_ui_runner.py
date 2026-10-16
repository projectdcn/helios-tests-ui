"""UI execution layer without any network: the jump-host (SshRunner) command lines through an injected executor,
Job validation, and the rack profiles file."""
from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from ui.config import ConfigError, RackProfile, SshProfile, load_config
from ui.runner import Job, JobError, LocalRunner, SshRunner, build_args

PW = "s3cret-Pa55-for-test"
INV = {"rack": "g02-g17", "switches": [{"name": "st1", "host": "10.255.4.71"}],
       "computes": [{"name": "ct01", "host": "10.255.4.47"}],
       "afm": {"url": "https://10.255.4.71", "auth": {"username": "admin", "password_env": "HELIOS_AFM_PASSWORD"}}}


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: setattr(self, "stdin_closed", True)
        self.stdout = iter(lines)
        self.rc, self.pid = rc, 4242

    def wait(self, timeout=None):
        return self.rc

    def terminate(self):
        self.terminated = True


class FakeExecutor:
    """Records every command; answers ls / tar / test -d like the jump host would."""

    def __init__(self, new_run: str, base_on_remote: bool = True):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.popens: list[tuple[list[str], FakeProc]] = []
        self.new_run, self.base_on_remote, self.ls_count = new_run, base_on_remote, 0

    def run(self, argv, input=None, timeout=None, cwd=None):
        self.calls.append((list(argv), input))
        out, rc = b"", 0
        if argv[0] == "git":
            out = b"CODE-TAR"
        elif argv[0] == "ssh":
            cmd = argv[-1]
            if "ls -1" in cmd:
                self.ls_count += 1
                out = b"g02-g17-20261014-101010\n" + (f"{self.new_run}\n".encode() if self.ls_count > 1 else b"")
            elif "tar -czf" in cmd:
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w:gz") as t:
                    for name, data in ((f"{self.new_run}/tc_summary.csv", b"tc_id,verdict\nHC-001,PASS\n"),
                                       (f"{self.new_run}/health.json", b'{"verdict": "GO"}')):
                        ti = tarfile.TarInfo(name)
                        ti.size = len(data)
                        t.addfile(ti, io.BytesIO(data))
                out = buf.getvalue()
            elif cmd.startswith("test -d"):
                rc = 0 if self.base_on_remote else 1
        return subprocess.CompletedProcess(argv, rc, out, b"")

    def popen(self, argv, env=None, cwd=None, stdin=False):
        p = FakeProc([b"collecting ...\n", b"RACK HEALTH: GO   (results/x)\n"])
        self.popens.append((list(argv), p))
        return p

    def ssh_cmds(self) -> list[str]:
        return [a[-1] for a, _ in self.calls if a[0] == "ssh"]


def _profile(tmp_path: Path, inv: dict = INV) -> RackProfile:
    f = tmp_path / "fullrack-jump-afm.local.yaml"
    f.write_text(yaml.safe_dump(inv, sort_keys=False))
    return RackProfile(name="g02-g17", title="full rack", runner="ssh", inventory=f, results_dir=tmp_path / "results",
                       known="testplan/health-known.yaml",
                       ssh=SshProfile(host="165.204.38.103", port=8059, user="tenant-hpe",
                                      code_dir="~/helios/helios-tests", python="~/helios/venv/bin/python",
                                      inventory="~/helios/fullrack-jump-afm.yaml"))


def test_ssh_health_run_mirrors_the_manual_procedure(tmp_path):
    ex = FakeExecutor("g02-g17-20261015-120000")
    r = SshRunner(_profile(tmp_path), ex)
    lines = []
    rc, new = r.execute("g02-g17-20261015-115959-abcd", Job.from_request({"kind": "health"}), PW, lines.append)
    assert rc == 0 and new == ["g02-g17-20261015-120000"]
    # every ssh goes to the jump host, port 8059, key auth only, one multiplexed connection
    for argv, _ in ex.calls + [(a, None) for a, _ in ex.popens]:
        if argv[0] == "ssh":
            assert argv[1:3] == ["-p", "8059"] and argv[-2] == "tenant-hpe@165.204.38.103"
            assert "BatchMode=yes" in argv and "ControlMaster=auto" in argv
    git = next(a for a, _ in ex.calls if a[0] == "git")
    assert git[3:] == ["archive", "--format=tar", "--prefix=helios-tests/", "HEAD", "--", ".", ":(exclude)testplan/*.xlsx"]
    cmds = ex.ssh_cmds()
    untar = next(i for i, (a, _) in enumerate(ex.calls) if a[0] == "ssh" and "tar -xf -" in a[-1])
    assert ex.calls[untar][0][-1] == 'mkdir -p "$HOME"/helios && tar -xf - -C "$HOME"/helios'
    assert ex.calls[untar][1] == b"CODE-TAR"
    push = next((a, i) for a, i in ex.calls if a[0] == "ssh" and a[-1].startswith("cat > "))
    assert push[1] == r.profile.inventory.read_bytes()
    assert '"$HOME"/helios/fullrack-jump-afm.yaml.bak-$(date' in push[0][-1]          # dated copy when it changes
    run_argv, proc = ex.popens[0]
    cmd = run_argv[-1]
    assert cmd == ('cd "$HOME"/helios/helios-tests && export PYTHONUNBUFFERED=1 && '
                   'IFS= read -r HELIOS_AFM_PASSWORD && export HELIOS_AFM_PASSWORD && '
                   'echo $$ > "$HOME"/helios/.helios-ui-g02-g17-20261015-115959-abcd.pid && '
                   'exec "$HOME"/helios/venv/bin/python tools/rack_health.py --inventory "$HOME"/helios/fullrack-jump-afm.yaml '
                   '--results-dir results --known testplan/health-known.yaml --no-html')
    # the password goes to the remote shell's stdin only: never on a command line or in the log
    assert proc.stdin.getvalue() == (PW + "\n").encode()
    assert not any(PW in " ".join(a) for a, _ in ex.calls + ex.popens)
    assert not any(PW in ln for ln in lines)
    # results copied back (tar from the remote results folder) and unpacked here
    assert any(c.startswith('cd "$HOME"/helios/helios-tests/results && tar -czf - -- g02-g17-20261015-120000') for c in cmds)
    assert (tmp_path / "results" / "g02-g17-20261015-120000" / "health.json").exists()
    assert any("rm -f" in c and ".helios-ui-g02-g17-20261015-115959-abcd.pid" in c for c in cmds)


def test_ssh_without_password_and_pytest_selection(tmp_path):
    ex = FakeExecutor("g02-g17-20261015-120000")
    r = SshRunner(_profile(tmp_path), ex)
    job = Job.from_request({"kind": "pytest", "tc": "TC-SWH-002, TC-SWH-030", "computes": "ct01,ct03",
                            "paths": "tests/l1"})
    r.execute("rid1", job, None, lambda s: None)
    cmd = ex.popens[0][0][-1]
    assert "read -r" not in cmd
    assert cmd.endswith('exec "$HOME"/helios/venv/bin/python -m pytest tests/l1 --inventory "$HOME"/helios/fullrack-jump-afm.yaml '
                        "--results-dir results -p no:cacheprovider --tc TC-SWH-002,TC-SWH-030 --computes ct01,ct03")
    assert ex.popens[0][1].stdin.getvalue() == b""


def test_ssh_rerun_failed_uploads_a_missing_base_run(tmp_path):
    p = _profile(tmp_path)
    base = p.results_dir / "g02-g17-20261014-101010"
    base.mkdir(parents=True)
    (base / "tc_summary.csv").write_text("tc_id,verdict\nHC-001,FAIL\n")
    ex = FakeExecutor("g02-g17-20261015-120000-merged", base_on_remote=False)
    SshRunner(p, ex).execute("rid2", Job.from_request({"kind": "rerun", "base_run": base.name}), PW, lambda s: None)
    up = next((a, i) for a, i in ex.calls if a[0] == "ssh" and "tar -xzf -" in a[-1])
    names = tarfile.open(fileobj=io.BytesIO(up[1]), mode="r:gz").getnames()
    assert f"{base.name}/tc_summary.csv" in names
    assert "--rerun-failed results/g02-g17-20261014-101010" in ex.popens[0][0][-1]


def test_ssh_stop_kills_the_remote_process(tmp_path):
    ex = FakeExecutor("x")
    SshRunner(_profile(tmp_path), ex).stop("rid3")
    cmd = ex.ssh_cmds()[-1]
    assert cmd == ('p=$(cat "$HOME"/helios/.helios-ui-rid3.pid 2>/dev/null) && '
                   '{ pkill -TERM -P "$p"; kill -TERM "$p"; }')


def test_ssh_refuses_an_inventory_with_a_password(tmp_path):
    inv = {**INV, "afm": {"url": "x", "auth": {"password": "oops"}}}
    ex = FakeExecutor("x")
    with pytest.raises(JobError, match="secret"):
        SshRunner(_profile(tmp_path, inv), ex).execute("rid4", Job.from_request({"kind": "health"}), None, print)
    assert not any("cat >" in c for c in ex.ssh_cmds())


def test_job_validation():
    with pytest.raises(JobError):
        Job.from_request({"kind": "pytest"})                              # nothing selected
    with pytest.raises(JobError):
        Job.from_request({"kind": "pytest", "tc": "TC-1;rm -rf /"})
    with pytest.raises(JobError):
        Job.from_request({"kind": "pytest", "tc": "TC-1", "paths": "../etc"})
    with pytest.raises(JobError):
        Job.from_request({"kind": "pytest", "marker": "x; ls"})
    with pytest.raises(JobError):
        Job.from_request({"kind": "rerun"})
    with pytest.raises(JobError):
        Job.from_request({"kind": "pytest", "tc": "TC-1", "pytest_args": "--inventory other.yaml"})
    j = Job.from_request({"kind": "health", "tc": "HC-001", "pytest_args": "--ignore tests/afm"})
    assert build_args(j, "inv.yaml", "res", "k.yaml", no_html=False) == [
        "tools/rack_health.py", "--inventory", "inv.yaml", "--results-dir", "res", "--known", "k.yaml",
        "--", "--tc", "HC-001", "--ignore", "tests/afm"]
    j = Job.from_request({"kind": "pytest", "marker": "healthcheck and not disruptive", "allow_disruptive": True})
    assert build_args(j, "i", "r", None, no_html=False)[-3:] == ["-m", "healthcheck and not disruptive",
                                                               "--allow-disruptive"]


def test_local_command(tmp_path):
    p = RackProfile(name="mr3", title="MR3", runner="local", inventory=tmp_path / "mr3.yaml",
                    results_dir=tmp_path / "res")
    argv = LocalRunner(p).command(Job.from_request({"kind": "health", "known": True}))
    assert argv[1:] == ["tools/rack_health.py", "--inventory", str(tmp_path / "mr3.yaml"), "--results-dir",
                        str(tmp_path / "res")]


def test_rack_profiles(tmp_path):
    racks = load_config()
    assert racks["g02-g17"].runner == "ssh" and racks["g02-g17"].ssh.port == 8059
    assert racks["g02-g17"].ssh.target == "tenant-hpe@165.204.38.103"
    assert racks["mr3"].runner == "local" and racks["mr3"].inventory.name == "mr3.local.yaml"
    bad = tmp_path / "racks.yaml"
    bad.write_text("racks:\n  x:\n    inventory: a.yaml\n    ssh: {host: h, password: nope}\n")
    with pytest.raises(ConfigError, match="secrets"):
        load_config(bad)


def test_ssh_stop_during_sync_does_not_start_the_run(tmp_path):
    ex = FakeExecutor("x")
    r = SshRunner(_profile(tmp_path), ex)
    r.stop("rid5")                                   # e.g. pressed while the code was being copied
    with pytest.raises(JobError, match="stopped before"):
        r.execute("rid5", Job.from_request({"kind": "health"}), PW, lambda s: None)
    assert ex.popens == []
