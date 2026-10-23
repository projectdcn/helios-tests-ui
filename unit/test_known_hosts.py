"""helios.transport.load_known_hosts: a malformed known_hosts line is skipped (as OpenSSH does), not fatal for every
connection (g02-g17 jump host 22 Oct 18:53: a truncated line, 'Incorrect padding', stopped all switch SSH)."""
from __future__ import annotations

import paramiko

from helios.transport import load_known_hosts

GOOD = ("|1|fkhAHf2qB5kziTYWkDUAcyxPHss=|QDttiW5rHNQJCAKpHMsiDfDVBlo= ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n")
BAD = "|1|K3bK/jeuFyGdugFvXKx1l4HNz6s=|o0hboITZWMHj/6mSfjYghtHi4Zg= ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCkbE85w\n"


def test_a_truncated_line_is_skipped_and_the_file_left_alone(tmp_path, caplog):
    f = tmp_path / "known_hosts"
    f.write_text(GOOD + BAD + GOOD.replace("|1|fkh", "|1|xkh"))
    try:
        paramiko.SSHClient().load_system_host_keys(str(f))
        plain_fails = False
    except Exception:  # noqa: BLE001
        plain_fails = True
    c = paramiko.SSHClient()
    load_known_hosts(c, str(f))
    assert plain_fails and len(c._system_host_keys._entries) == 2 and "skipped malformed line(s) 2" in caplog.text
    assert f.read_text() == GOOD + BAD + GOOD.replace("|1|fkh", "|1|xkh")
    load_known_hosts(paramiko.SSHClient(), str(tmp_path / "missing"))        # no file: nothing to load, no error
