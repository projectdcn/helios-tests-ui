"""16 Oct: a UI run with no password typed inherited HELIOS_AFM_PASSWORD from the UI's environment, and a login
error printed the request body (with the password) into the run log. A run gets only the typed password, and the
log masks secret-looking text and the UI's own secret env values."""
from __future__ import annotations

import yaml

from ui.config import RackProfile
from ui.runner import Job, LocalRunner
from ui.runs import REDACTED, redact

SECRET = "Sup3r-Secret!x"


class Recorder:
    def __init__(self):
        self.env = None

    def popen(self, argv, env=None, cwd=None, stdin=False):
        self.env = env

        class P:
            pid, stdout, returncode = 0, iter(()), 0

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0
        return P()


def _runner(tmp_path):
    inv = tmp_path / "mr3.yaml"
    inv.write_text(yaml.safe_dump({"rack": "mr3", "afm": {"auth": {"password_env": "HELIOS_AFM_PASSWORD"}}}))
    rec = Recorder()
    return LocalRunner(RackProfile(name="mr3", title="mr3", runner="local", inventory=inv, results_dir=tmp_path / "r"),
                       rec), rec


def test_run_does_not_inherit_secret_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_AFM_PASSWORD", SECRET)
    monkeypatch.setenv("HELIOS_BMC_PASSWORD", SECRET)
    monkeypatch.setenv("SOME_API_TOKEN", SECRET)
    monkeypatch.setenv("HOME_OK", "keep")
    r, rec = _runner(tmp_path)
    try:
        r.execute("x", Job.from_request({"kind": "pytest", "tc": "TC-AFM-001"}), None, lambda line: None)
    except Exception:
        pass
    assert rec.env is not None and SECRET not in rec.env.values(), {k for k, v in rec.env.items() if v == SECRET}
    assert rec.env.get("HOME_OK") == "keep"


def test_typed_password_is_the_only_one_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_AFM_PASSWORD", SECRET)
    r, rec = _runner(tmp_path)
    try:
        r.execute("x", Job.from_request({"kind": "pytest", "tc": "TC-AFM-001"}), "typed-pw-1", lambda line: None)
    except Exception:
        pass
    assert rec.env["HELIOS_AFM_PASSWORD"] == "typed-pw-1"


def test_redact_masks_secret_text_and_values():
    body = """body = b'{"username": "admin", "password": "%s", "tenant": "default"}'""" % SECRET
    out = redact(body)
    assert SECRET not in out and f'"password": "{REDACTED}"' in out and '"username": "admin"' in out
    assert SECRET not in redact(f"{{'password': '{SECRET}'}}")
    assert redact(f"export HELIOS_AFM_PASSWORD={SECRET}") == f"export HELIOS_AFM_PASSWORD={REDACTED}"
    assert redact(f"value {SECRET} printed", values=[SECRET]) == f"value {REDACTED} printed"
    assert redact("nothing secret here") == "nothing secret here"
