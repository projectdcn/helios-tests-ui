import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "metrics_exporter_mod", Path(__file__).resolve().parents[1] / "tests" / "telemetry" / "test_metrics_exporter.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class _Inv:
    def __init__(self, cmds):
        self.cmds = cmds

    def cmd(self, key, default=None):
        return self.cmds.get(key.split(".", 1)[1], default)


class _Compute:
    def __init__(self, listening=True, check_rc=0, docker_names=""):
        self.listening, self.check_rc, self.docker_names, self.cmds = listening, check_rc, docker_names, []

    def run(self, cmd, sudo=False, timeout=120):
        self.cmds.append(cmd)
        if cmd.startswith("ss "):
            return SimpleNamespace(ok=True, stdout="LISTEN 0 4096 *:12347 *:*" if self.listening else "")
        if cmd.startswith("docker ps"):
            return SimpleNamespace(ok=True, stdout=self.docker_names)
        return SimpleNamespace(ok=self.check_rc == 0, stdout="")


CHECK = {"container": "amd-device-metrics-exporter-gpu", "port": 12347, "running_check": "crictl ps --name '^{container}$' | grep -q ."}


def test_running_check_replaces_docker():
    c = _Compute()
    assert mod._container_up(c, _Inv(CHECK))
    assert not any(x.startswith("docker") for x in c.cmds)
    assert "crictl ps --name '^amd-device-metrics-exporter-gpu$' | grep -q ." in c.cmds


def test_running_check_failing_or_port_closed_is_down():
    assert not mod._container_up(_Compute(check_rc=1), _Inv(CHECK))
    assert not mod._container_up(_Compute(listening=False), _Inv(CHECK))


def test_default_still_uses_docker():
    assert mod._container_up(_Compute(docker_names="device-metrics-exporter"), _Inv({}))
    assert not mod._container_up(_Compute(docker_names=""), _Inv({}))
