helios-tests web UI - installing on a Linux server
====================================================

This bundle holds the committed helios-tests code (app/), every Python package it needs as wheels for Linux x86_64
and CPython 3.10 / 3.11 / 3.12 (wheelhouse/), install.sh and uninstall.sh. It installs without internet access and
without root, for the user who runs it.

Needs: Linux x86_64, python3 >= 3.10 with the venv module (Debian/Ubuntu: python3-venv), ~250 MB disk.

Install
-------
    tar -xzf helios-ui-<version>.tar.gz
    cd helios-ui-<version>
    ./install.sh                      # --prefix DIR (default ~/helios-ui)  --port N (default 8765)
                                      # --host ADDR (default 0.0.0.0 = every address; 127.0.0.1 = tunnel only)
                                      # --python /usr/bin/python3.11  --online (pip from PyPI)  --dry-run

It creates
    ~/helios-ui/app/        the code (replaced by every upgrade)
    ~/helios-ui/venv/       Python virtual environment (replaced by every upgrade)
    ~/helios-ui/config/racks.yaml   rack profiles (written once; yours to edit; never overwritten)
    ~/helios-ui/data/       results/runs/<rack>/ (results, sessions, presets, defaults, run logs),
                            inventories/, testplan/  (never touched by an upgrade)
    ~/helios-ui/logs/       helios-ui.log
    ~/helios-ui/bin/        helios-ui {start|stop|restart|status|logs}, install.sh (rollback), uninstall.sh
and starts the UI: a systemd --user service (helios-ui.service) when systemd is there, else nohup through
bin/helios-ui. For the service to keep running after you log out an admin runs once:
    sudo loginctl enable-linger <you>

Copy these yourself (the installer copies no secrets and creates no keys)
-----------------------------------------------------------------------
    ~/helios-ui/data/inventories/mr3.local.yaml
    ~/helios-ui/data/inventories/fullrack-jump-afm.local.yaml   (copy of the jump host's ~/helios/fullrack-jump-afm.yaml)
    ~/helios-ui/data/testplan/full_rack_testplan.xlsx
    SSH keys in ~/.ssh: one the jump host accepts (ssh -p 8059 tenant-hpe@165.204.38.103 true must work without a
    prompt) for g02-g17, and the keys the MR3 inventory uses. The service has no ssh-agent: use a key it can read
    without a passphrase prompt (or ~/.ssh/config IdentityFile).
Then reload the page; a rack whose inventory is missing shows it as MISSING.

There is no macOS Keychain on Linux: type the AFM password in the Run form for each run (the page says so and the
"Remember" box is disabled).

Open it
-------
By default the UI listens on every address of the server (--host 0.0.0.0):
    open http://<server>:8765/            (install.sh prints the exact URL)

THE UI HAS NO LOGIN: anyone who can reach that port can start runs on the racks (disruptive ones included) and read
every result. Restrict the port with the host firewall to the team's subnet. install.sh never changes the firewall;
if firewalld or ufw is active and the port is not open it prints the command an admin would run, e.g.
    sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.0.0.0/24" port port="8765" protocol="tcp" accept' && sudo firewall-cmd --reload
    sudo ufw allow from 10.0.0.0/24 to any port 8765 proto tcp
To keep it on the server only, install (or upgrade) with --host 127.0.0.1 and use an SSH tunnel from the laptop:
    ssh -L 8765:127.0.0.1:8765 <user>@<server>     then open http://127.0.0.1:8765/
(laptop port 8765 taken by a local UI? ssh -L 8780:127.0.0.1:8765 ... and open http://127.0.0.1:8780/)
The page accepts the server's host names and addresses; a DNS alias it does not know is refused ("bad Host
header"): add a line HELIOS_UI_ALLOWED_HOSTS=alias1,alias2 to ~/helios-ui/config/ui.env (kept by upgrades), then
~/helios-ui/bin/helios-ui restart.

Upgrade, roll back, uninstall
-----------------------------
    ./install.sh                                  (from a newer bundle) app/ + venv/ replaced, config/ + data/ kept,
                                                  the port and --host of the installation kept (an install from
                                                  before --host existed becomes 0.0.0.0; give --host to choose),
                                                  the old ones kept as app.prev / venv.prev; refuses while a run
                                                  is going (--force stops it)
    ~/helios-ui/bin/install.sh --rollback         back to app.prev (run it again to roll forward)
    ~/helios-ui/bin/uninstall.sh                  remove service, app/, venv/, bin/; keep config/, data/, logs/
    ~/helios-ui/bin/uninstall.sh --purge [--yes]  remove everything

Command-line runs on the server use the same code and venv:
    cd ~/helios-ui/app && export HELIOS_RESULTS_ROOT=~/helios-ui/data/results
    ../venv/bin/python -m pytest tests --inventory ~/helios-ui/data/inventories/mr3.local.yaml
