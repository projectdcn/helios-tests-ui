# helios-tests

pytest automation for the **AMD Helios full-rack scale-up test plan**
(`Helios_ScaleUp_TestPlan-AMD.xlsx`): 6 SONiC switch trays (2 ASICs each),
18 compute trays (4 GPUs each), with the AFM controller and the IFoE fabric.

## Layout

```
helios/                 framework
  inventory.py          rack YAML -> hosts, thresholds, command templates
  transport.py          SSH (paramiko) + replay transport (canned outputs)
  sonic.py              SONiC switch driver (multi-ASIC: -n asicN, sonic-db-cli)
  compute.py            compute-tray driver + command-template rendering
  afm.py                AFM REST + AggWatch WebSocket client, request log (secrets redacted), rack_health()
  afm_ops.py            AFM lifecycle ops: vPOD moves, compute/switch lifecycle, agent/controller/link ops over SSH
  parsers.py            SONiC tables, sonic-db-cli, rccl-tests, TransferBench, perftest, Prometheus
  datapath.py           shared workloads: L2 ping (afmctl discovery), TransferBench (multi-rank), RCCL
  afmctl.py             afmctl JSON/ping parsers (device, port, IFCP, accelerator-addr)
  preflight.py          datapath gate: reachability, GPUs ACTIVE, per-plane ping probe, live drops
  perftest.py           ib_*_bw server/client pairs for scale-out RDMA
  wait.py               polling helpers (recovery, reboot / SSH return)
  rack.py               Rack, fan_out(), Checks (soft asserts)
  pytest_plugin.py      --inventory/--tc/..., per-switch params, results CSV
tests/                  rack tests, each tagged @pytest.mark.tc("TC-...")
  l1/                   switch hardware (TC-SWH) + scale-up datapath (TC-AFM-001..005A)
  switch/               SONiC switch agent (TC-SWA)
  hw/                   hardware (TC-HW)
  scaleout/             mortaro RDMA perftest (TC-IBS)
  ras/                  RAS datapath reset / reboot / power cycle (TC-RAS-reset)
  telemetry/            AMD device-metrics-exporter (TC-CTL-139..150)
  platform/             NTP, DNS, log rotation, topo logs, OOB names across reboot
  ifoe/                 IFoE driver unload/reload (TC-AFM-LU-001, TC-HW-017)
  sideband/             sideband bring-up + IFCP (TC-BU, TC-SEQ-006, TC-SBM-062, TC-E2E-062)
  afm/                  AFM controller API: auth, cluster/objects, policies, RBAC, audit, metrics,
                        techsupport, AggWatch, compute Reconfigure (TC-AFM-*, TC-CTL-*, TC-AFM-AGGW-*)
unit/                   offline tests + fault-injection self-test (no rack needed)
selftest/               replay inventory + canned outputs (make_replay.py); fake_afm.py (HTTPS+WS AFM),
                        fake_rack.py (simulated rack + switch SSH), inventory-afm3.yaml (3-switch fake)
inventory/              example-fullrack.yaml (copy to *.local.yaml)
testplan/manifest.yaml  all 542 plan cases, normalized (tools/import_testplan.py)
tools/coverage.py       automated vs. total, per category
```

## How to run

Run everything from the repo root. The machine you run from needs SSH access
to every switch and compute in the inventory. To set up on a new machine (for
example a lab host):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[tools,dev]'
```

### 1. Without a rack: check the framework

These use recorded SONiC and GPU outputs from `selftest/replay`, so no hardware
is needed.

```bash
.venv/bin/pytest unit -n 6                                                  # 307 tests: L1 + added-switch faults, 16 AFM API + 180 AFM product-fault checks, healthy fakes (~13 min)
.venv/bin/pytest tests --inventory selftest/inventory.yaml                  # 216 pass, 244 skip (disruptive skipped)
.venv/bin/pytest tests --inventory selftest/inventory.yaml --allow-disruptive   # 361 pass, 99 skip (one-switch fake)
.venv/bin/pytest tests/afm --inventory selftest/inventory-afm3.yaml --allow-disruptive   # 253 pass, 55 skip (3-switch fake; MCH/equipment cases skip here)
.venv/bin/pytest tests/afm/test_afm_mch.py --inventory selftest/inventory-mch.yaml --allow-disruptive      # 36 pass, 1 skip (heliosmch fake)
HELIOS_SELFTEST_DIR_PW=selftest-dir-pw .venv/bin/pytest tests/afm/test_afm_equipment.py \
    --inventory selftest/inventory-equip.yaml --allow-disruptive          # 15 pass (fake LDAP/RADIUS/syslog/upgrades/mgmt network)
```

### 2. On a real rack

**Step 1: make your inventory.** Files named `*.local.yaml` are git-ignored.

```bash
cp inventory/example-fullrack.yaml inventory/rack1.local.yaml
```

**Step 2: edit `inventory/rack1.local.yaml`.**
- Under `switches:` and `computes:`, set each `host:` to the real hostname or IP.
- Under `defaults:`, set the SSH user, and either `key_file` (recommended: install your
  key with `ssh-copy-id`, then no passwords are needed) or `password_env` for each node type.
- The SSH users need **passwordless sudo** on switches and computes (`afmctl`, `dmesg`,
  `docker` and resets run under `sudo -n`).
- Fill in the `VERIFY` items (see [Calibrate on the first rack run](#calibrate-on-the-first-rack-run)).
  Until these are set, those tests skip and name the missing setting.
- For the scale-out RDMA cases, add `rdma:` ports to the two computes named in
  `commands.perftest.nodes`. For DC/AC power cycles, add `bmc:` / `pdu_outlet:`.
- Set your pass limits under `thresholds:`, such as `expected_fans`,
  `expected_psus`, `psu_capacity_w` and the bandwidth floors.

**Step 3: export secrets,** if you used `password_env`:

```bash
export HELIOS_SWITCH_PASSWORD='yourpassword'
```

**Step 4: run.** Start small with one switch and two computes, then run the
full L1 suite:

```bash
.venv/bin/pytest tests --inventory inventory/rack1.local.yaml --switches sw1 --computes c01,c02 -v
.venv/bin/pytest tests -m l1 --inventory inventory/rack1.local.yaml --log-file=results/run.log
```

`--log-file` records every SSH command, its output, exit code and duration. The
terminal shows live progress (start, 30 s heartbeat, finish) for long commands, and
every remote command runs under `timeout -k` on the node, so a hung tool is killed.

**Datapath pre-flight.** Every IFoE traffic test (RCCL smoke, TC-AFM-001..005A,
TC-HW-008/017, RAS, IFoE driver) first passes a gate, once per run:
- every selected compute answers SSH and all GPUs are `ACTIVE`
- one `afmctl` L2 ping per GPU over every LINK_UP port, so each switching plane
  carries a frame first
- no switch port cabled to the tested GPUs is dropping more than 100 frames/s

If the gate fails, those tests report **BLOCKED** with the reason, instead of hanging.
On MR3 this caught a switching plane that silently black-holes port 9 on every GPU,
which otherwise hung the first RCCL collective. Tune or disable it under
`thresholds.datapath.preflight`.
To avoid passing `--inventory` every time, set it once:

```bash
export HELIOS_INVENTORY=inventory/rack1.local.yaml
```

**AFM controller API (`tests/afm`).** Set the `afm:` section of the inventory
(`url`, `auth.username`, `auth.password_env`, `tenant`), then export the password
in the same terminal:

```bash
read -s HELIOS_AFM_PASSWORD && export HELIOS_AFM_PASSWORD
.venv/bin/pytest tests/afm --inventory inventory/mr3.local.yaml -v
```

Without the password, only the unauthenticated cases run; the rest SKIP. The
tests create throw-away `helios-*` objects and delete them afterwards.

The disruptive AFM cases (149) need `--allow-disruptive`:
- **Lifecycle:** agent and controller restarts, vPOD moves, compute and switch decommission/delete/readmit.
- **Fabric links:** ports shut with `config interface ... shutdown`.
- **Reconfigure.**

Each one first checks AFM health (BLOCKED if AFM is unhealthy) and restores what it changed. They also need SSH to the switch trays; AFM SwitchNodes are matched to inventory switches by management IP, or by `afm_name:`. Switch-lifecycle and controller-failover cases skip on a one-switch mini rack.

### 3. Running specific cases

| What you want | Add this |
|---|---|
| Specific plan IDs | `--tc TC-SWH-002,TC-AFM-001` |
| Part of the rack | `--switches sw1,sw2` / `--computes c01,c02` |
| Switch hardware tests only | `tests/l1/test_switch_hw.py` |
| Datapath tests only (ping, TransferBench, RCCL) | `tests/l1/test_afm_datapath.py` |
| Switch agent / hardware / RDMA / RAS only | `tests/switch` / `tests/hw` / `tests/scaleout` / `tests/ras` |
| Metrics exporter / platform / IFoE driver only | `tests/telemetry` / `tests/platform` / `tests/ifoe` |
| AFM controller API only | `tests/afm` |
| By level or marker | `-m l1` / `-m "l1 and not stress"` |
| Tests that restart services, reset devices, reload drivers or reboot | `--allow-disruptive` |
| Manual tests (e.g. the TC-SWH-009 heat test) | `--run-manual` |

For example, to run only the PSU checks on `sw1` and `sw2`:

```bash
.venv/bin/pytest tests --tc TC-SWH-018,TC-SWH-019,TC-SWH-021 --switches sw1,sw2
```

### 4. Results

Each run creates a folder `results/<rack>-<timestamp>/` with two files:
- `tc_summary.csv`: one PASS / FAIL / BLOCKED / SKIP / PARTIAL verdict per plan ID
  (BLOCKED = the datapath pre-flight refused to start traffic).
- `tc_results.csv`: one row per test, with the failure message and measured
  values such as bandwidth and bus bandwidth.

### Access needed per case

`tools/access_needed.py` adds an **Access needed** column to a workbook: column I on Testcases, column H on
mortaro. It says what each case needs to run: AFM API, switch SSH, compute SSH, datapath traffic, BMC/PDU, a
special rack or server, or manual work.
- **Automated cases** are classified from the code: which suite, disruptive or not, and whether the test or
  its helpers drive a switch or compute.
- **The rest** are classified from the plan category and title. Values inferred from a title only are marked "(est.)".
- **Summary only:** `--summary` prints the counts per class without touching a workbook.

```bash
.venv/bin/python tools/access_needed.py testplan/full_rack_testplan.xlsx [--dry-run]
```

Add `--automation` to also rewrite column D (Automaton Yes/No) from the code:
- **Yes:** a test exists for the case. Whether it ran on this rack is the status column (blank = not run yet).
- **No:** not automated.
(Until 15 Oct 2026, `Inprogress` meant automated but never run on a rack; those rows are now Yes.)

### Record results in the test-plan workbook

```bash
.venv/bin/python tools/update_testplan.py testplan/full_rack_testplan.xlsx results/<run> [more runs] \
    --sw-version "AFM 0.1.0-AICC-817" --label "g02-g17 full rack, <date>" [--dry-run]
```

The tool writes Execution Status, SW Version and Notes for the plan IDs that ran:
- FAIL: any item failed. PASS: every executed item passed.
- BLOCKED: written as Pending with "Blocked: <reason>".
- Not executed: left untouched, unless you pass `--mark-skipped`.

Close the workbook in Excel first. A backup is saved next to it.

### 5. HTML report

Turn one or more run folders into one self-contained HTML page: a summary dashboard
(counts, charts, key findings, a run summary per suite), then every test with its
own collapsible command log.

```bash
.venv/bin/python tools/html_report.py results/<run-folder> [more run folders] -o results/report.html --title "..." --subtitle "..." --notes results/findings.txt
```

- **Run folders:** find them with `ls -td results/*-2*/` and pass them oldest first. When
  several folders contain the same test, a run that executed it wins over one that skipped
  or blocked it; otherwise the later run wins.
- **`--notes`:** an optional file with one finding per line. Start a line with `!`
  for a problem or `+` for good news.
- **`--workbook testplan/full_rack_testplan.xlsx`:** counts and tables follow the reviewed
  status in the workbook (raw verdict shown where it differs), with Level, Scope, Origin and
  Disruptive per case and status-by-scope / status-by-origin tables.
- **`--all-cases`** (with `--workbook`): the overall report - every workbook case (executed,
  not run, not automated), automation coverage from `tools/coverage.py`, a filterable table.
- **`--health-run results/<health-run>`:** adds the rack health summary section and the full health report as a
  second "Rack health check" tab of the same page (isolated frame; `#health-tab` opens it). `--health <health.json>`
  [`--health-link URL`] adds only the summary.
- **`--tz RUN=HOURS`:** UTC offset for run folders recorded before 14 Oct evening (runner-local
  `started`; Mac = -7, jump host = 0). Newer runs record UTC.
- **Size:** each embedded command log is clipped to its first and last 75,000 characters
  (full log in `results/<run>/logs/`), so reports stay under the 16 MB artifact limit.
- **Where to keep reports:** in `results/`, which is git-ignored. Reports contain lab
  hostnames and IPs; check that every password value reads `<REDACTED>` before sharing.
  The charts load Chart.js from a CDN.

### 6. Full-rack health check (GO / NO-GO)

Read-only gate to run before every disruptive batch, after each one, and whenever the rack state is unclear:

```bash
.venv/bin/python tools/rack_health.py --inventory <inventory> [--known testplan/health-known.yaml] [--baseline results/<run>/health.json]
```

- Runs the 48 `healthcheck` cases (`helios/healthcheck.py`; `pytest tests -m healthcheck` selects the same):
  existing plan cases (incl. TC-SWH-048 cabling vs AFM, TC-SWH-050 stale MACs) plus HC-000 (AFM rack state + snapshot),
  HC-001..006 (SSH, BMC power, GPUs ACTIVE, other GPU workload, GPU ECC / kernel hardware errors, tray resources) and
  HC-007..011 (leftover helios-* AFM objects, critical AFM events, controller restarts, tray versions, hardware
  inventory + compute<->switch links). HC-005 counts corrected CPU machine-check events without failing on them.
- In the workbook: rows HC-000…HC-011 (Origin "Added - helios-tests (15 Oct 2026)", Scope "Health check (…)"), and
  column M "Health check" = Blocker / Warning on all 48 cases (`tools/select_cases.py --health Blocker,Warning`).
- `testplan/health-known.yaml`: g02-g17 issues already reported to AMD, accepted with `--known` (shown as KNOWN).
- **GO** (exit 0): every blocker passed or is accepted in `--known`; **NO-GO** (1): a blocker failed, was blocked or
  only partially passed; **INCOMPLETE** (2): a blocker could not run (e.g. no `HELIOS_AFM_PASSWORD`).
- Writes `health.json` (verdict, blockers, warnings, snapshot), `health.html` and `health-notes.txt` into the run
  folder. `--baseline` lists what changed since an earlier run; `--run results/<run>` re-judges without the rack
  (only the cases that run collected count).
- `--rerun-failed results/<run>` re-runs that run's FAIL / PARTIAL / BLOCKED cases, merges (latest result per case)
  and judges; `--merge A B` merges by hand. Cases failing only on an unreachable node are grouped under one
  ROOT CAUSE line; failure messages start with a per-node count; AFM nodes are named with their tray; older merged
  results are flagged; HC-008 looks back to the previous run; known entries take `{reason, match, until}`.
- On g02-g17: run it on the jump host (`ssh -t -p 8059 tenant-hpe@165.204.38.103 'bash ~/helios/run-health.sh'`,
  password typed there, ~30 min), copy the run folder back, and add it to the overall report with `--health-run`.

### 7. Coverage: automated vs. missing

```bash
.venv/bin/python tools/coverage.py                               # per-category table
.venv/bin/python tools/coverage.py --missing > results/missing.txt   # plus every unautomated case
```

The table has one row per category:
- `automated`: how many of that category's cases have a test (a
  `@pytest.mark.tc` tag).
- `total`: how many cases the plan has in that category.

`--missing` also lists every case that has **no test yet**, with its ID, level
and title.

## Coverage (435 of 577 cases: 542 plan + 23 switch cases TC-SWH-037…059 + 12 health checks HC-000…011)

* **TC-SWH-001…036, switch hardware.** Firmware, fans, PSUs, platform/EEPROM,
  multi-ASIC namespaces, interface state/speed/MTU, error-counter deltas,
  VLAN tables. Each test is parametrized per switch tray. Numeric PSU and fan
  checks read STATE_DB, because CLI table columns vary between SONiC releases.
* **TC-AFM-001…004 and 005A, datapath.** L2 ping (within each tray, plus
  cross-tray in both directions), TransferBench in-domain and cross-domain,
  and the five RCCL collectives in-domain and cross-domain. RCCL results are
  checked for the `#wrong` column, out-of-bounds values, the max message size
  reached, and an optional per-collective bus-bandwidth floor.

* **TC-SWA-074, 075, 079, 129, 138, 140, 141, 223, switch agent.** Lifetime
  error counters, traffic flowing (RX/TX deltas), VLAN symmetry across ASICs,
  `sonic-installer` commands and set-next-boot (restores the original), LLDP
  discovery, plus disruptive restarts of a per-ASIC container and of SWSS
  (waits for critical processes and previously-up ports to recover).
* **TC-HW-002, 008, hardware.** Oper speed vs. configured speed per port;
  TransferBench smoke a2a in-domain.
* **TC-IBS-001…006, mortaro scale-out RDMA.** `ib_*_bw` server/client per NIC
  port pair, all pairs in parallel: cross-node, multi-verb, loopback, QP
  scaling (8 to 256, against the 8-QP baseline), the full 320-run matrix, and
  a configurable run.
* **TC-RAS-reset-001…004, 007…009, RAS.** `afmctl reset device --datapath`
  (all ports or partial stations), then in-domain or cross-domain
  TransferBench + RCCL; OS reboot, DC cycle (BMC) and AC cycle (PDU) of one
  node, then a datapath reset of the rest and cross-domain traffic.
  Not yet covered: 005/006 and 010…017, which need AFM's vPOD and reconfigure API.
* **TC-CTL-139…150, GPU metrics exporter.** Container up and port 5000
  bound (started with your `docker run` template if absent), HTTP 200 in
  Prometheus format, per-GPU power, temperature, utilization and VRAM
  (used + free = total), ECC (uncorrectable must be 0), PCIe, XGMI, NIC, RDMA
  families, label sanity (sequential GPU IDs, hostname, one model), and a
  disruptive restart and upgrade. Scraped with curl on each compute; metric
  names are matched by regex families you can override.
* **TC-CTL-035, 059, TC-BUG-022, TC-E2E-118, TC-BUG-021, node platform.** NTP
  synchronized and clocks within 2 s of the runner on every node; DNS from the
  controller nodes; syslog rotation scheduled and /var/log not filling up;
  fresh topology logs under /var/log/pensando; OOB interface names and MACs
  unchanged across a switch reboot (disruptive).
* **TC-AFM-LU-001, TC-HW-017, IFoE driver.** Five unload/load cycles with GPU
  health after each and a TransferBench before/after regression check; one
  remove/re-add followed by L2 ping and TransferBench (both disruptive).

* **Added after the first MR3 runs:**
  * **TC-SWA-074 drop check:** lifetime drop ratio plus live drop rate per fabric port,
    reported with the tray/GPU/port the switch port is cabled to.
  * **TC-HW-013 destination MACs:** each GPU's per-port destination table is complete;
    every MAC is a static FDB entry on the switch port cabled to that accelerator/port.
  * **TC-BU-002/006/009/011 sideband bring-up:** afm-agent running, no agents on
    computes, exactly one sb-node-agent per compute slot, GPUs ACTIVE with all IFoE
    ports up.
  * **TC-SEQ-006 node-agent control channel:** agent REST `/api/health` connected and
    healthy, every config request applied.
  * **TC-BU-003 / TC-SBM-062 / TC-E2E-062 IFCP:** IFCP sessions up and bidirectional;
    each GPU port's IFCP-discovered switch peer matches the registered cabling.

* **AFM controller API (`tests/afm`).**
  * **Auth.** Login, cookie flags (Secure, HttpOnly, SameSite=Strict), 401 without a session, invalid-request envelopes.
  * **Cluster and objects.** Quorum health, switch and compute specs (GPUs Ready, unique acc-ids, ports),
    pod and vPOD specs.
  * **Failover policies.** System policy defaults, CRUD, and the missing-threshold and non-existent cases.
  * **vPOD name rules.** `.system` names and duplicate names are rejected.
  * **RBAC.** Cluster, auth, monitoring and workload roles; bad-login counting; read-only users get 403 on Reconfigure.
  * **Audit, metrics, techsupport.** Audit events for CRUD and login, Cluster and SwitchPortStats metrics,
    and techsupport bundles.
  * **AggWatch WebSocket (28 cases).** Auth and expired sessions, single and multi-kind watches,
    Created/Updated/Deleted events, RBAC filtering and a role revoked mid-stream, CSWSH origin,
    no or malformed watch options, idle, concurrent connections, reconnect, 50 connect cycles, and a burst of 100 events.
  * **Disruptive, compute Reconfigure.** Single, sequential, parallel and back-to-back; also after
    vPOD add/remove, on an unhealthy, decommissioned or decommissioning compute, and in a degraded vPOD
    (RC-001…006, 017, 019, 020, 026). Plus the mini-rack switch guards (TC-AFM-035A).
  * **Disruptive, lifecycle (TC-AFM-006…047).**
    * **Agents and controllers:** compute sb-node-agent restart and SIGSTOP; switch afm-agent restart,
      stop/start and container restart; non-leader and leader controller restart.
    * **vPODs:** CRUD, and create/delete/Reconcile with the agents down.
    * **Compute lifecycle:** decommission (plain, forced, rejected while in a vPOD), delete and readmit.
    * **Switch lifecycle:** drain/reactivate, (force-)decommission, delete and readmit; the same with a
      controller restart and with the agent down; decommission with a port down.
  * **Disruptive, fabric links.** Flap and prolonged down on one or both ASICs (also checks no coupled port
    goes down); automatic vs manual data-fabric reconfiguration; link-fail timers; manual → automatic
    policy edit; reconciliation after an agent outage; vPOD created with a link down.
  * **Disruptive, failover scenarios and vPOD operations (TC-E2E, `test_afm_failover.py`).**
    * **Station patterns:** both ports of a station (FO-003), single ports on several stations (FO-013),
      full + partial station (FO-014), every port of one GPU (FO-037), several trays at once (FO-026).
    * **Sequences:** sequential failures with monotonic planes (FO-033); a second failure during
      reconfiguration (FO-024).
    * **Timers and policies:** automatic golden path with every event (FO-038); recovery just before the
      timer (FO-021); `link_failed` only after the threshold; manual mode holds with unchanged planes
      (FO-039, E2E-052); mode change while down (FO-022); in-use policy delete rejected (FO-023, E2E-040).
    * **Events:** link_down/up per port, for one switch or across switches.
    * **vPOD operations:** move several computes, full system vPOD, parallel CRUD, GPUs between user vPODs,
      per-vPOD VLANs on every switch, vPOD CRUD events.
    * **Station of a GPU port:** `gpu_port // thresholds.afm.ports_per_station` (2). Verified on MR3 c1 on 2026-10-13: `afmctl show station` maps station k to ports 2k and 2k+1, on all 4 GPUs and all 18 stations.
  * **More AFM API cases (44, `test_afm_api_more.py`, `test_afm_controller.py`, `test_afm_snapshot.py`,
    `test_afm_regression.py`, `test_afm_stress.py`).**
    * **AggWatch and auth:** resource-version filters (AGGW-009, CTL-009), audit records for watches
      (AGGW-017/018), bearer token (AGGW-002, opt-in), group roles (CTL-117), local-user login (CTL-131),
      login-attempt policy validation (BUG-033).
    * **Events:** default event policies (EVT-003), event retention (EVT-038), status-change events (EVT-021),
      alerts from an alert policy (CTL-098).
    * **Controller resilience:** stop/start of the leader's controller (CTL-074), surviving and rejoining
      after a single node failure (CTL-133/134), logs after a restart (CTL-124), full controller restart
      (STR-010, AR-003), AggWatch across non-leader and leader restarts (AGGW-023/024).
    * **Snapshot and restore:** AFM-051, auth objects (CTL-039), a removed compute (CTL-040), recovery key
      (CTL-055, opt-in, because the key downloads only once).
    * **Regression:**
      * blacklisted GPU ports across Reconfigure (RC-028)
      * controller stability on a compute rejoin or a stuck switch-agent (BUG-001/004)
      * switch state not stuck at unknown (BUG-007), ports recovering after an agent flap (BUG-016)
      * no duplicated GPU info (BUG-009), stable acc-ids (BUG-014)
      * Reconcile not clearing failures (BUG-025), no operation-failed or configDone hang (BUG-026/027)
      * drain reprograms GPUs (BUG-030); agent restart and pause (AR-001/002)
    * **Stress:** configure time at scale (STR-005), bulk vPOD churn (STR-011), link event storm (STR-013),
      about 5 % of ports down at create (STR-019), soak (STR-020), no transient 0/0 planes (STR-023),
      Reconcile at scale (STR-024).
  * **Compute side, IFCP, switch agent and traffic with AFM (56).** AFM ComputeNodes are matched to
    inventory trays by accelerator id. Files: `test_afm_compute.py`, `test_afm_ifcp.py`,
    `test_afm_switch_agent.py`, `test_afm_traffic.py`.
    * **Compute side:** compute port flap and port-down events (AFM-009, E2E-092), station path maps
      after link triggers and versus port state (AFM-058/060), forwarding table after a flap (E2E-099),
      GPU config phase on configfailed (E2E-110), accelerator reset + Reconfigure (RC-007).
    * **Node agent:** NA-013/016/018/022/024/040/047/048/049/050 and HW-025.
    * **Sideband sequence:** SEQ-005/010/012, BU-001.
    * **IFCP sessions from `afmctl` (ll_up / peer / errors):** baseline (TC-001); recovery after a link
      flap, agent restart, agent pause, controller restart, compute port shut, all links down
      (TC-002…008); triggers TRG-004/005/006/009/010.
    * **Switch agent:** user VLANs kept on decommission, fabric update on vPOD CRUD, switch states,
      automatic reconfigure on the policy timer (SWA-037/180/204/214).
    * **Traffic with AFM:** NA-003/004/005/006/019, HW-010, TRG-001, E2E-009 and RAS-reset
      005/006/010…017 (hard reset = AFM Reconfigure).
    * **Not automated:** SPDM session, SelectedLink and IFCP packet-format cases. They need data
      that neither afmctl nor the node-agent API exposes (AMD tooling or packet capture).
  * **Sideband agent placement and control path (23, `test_afm_sideband_more.py`).**
    * **Placement**, read from each switch's afm-agent process list: one sb-node-agent per slot, spread
      over the switches within `thresholds.afm.agent_balance_slack` (1) (SBM-135/136/050).
    * **Redistribution:** a switch's afm-agent stopped re-homes its agents (SBM-137), rebalance after recovery
      and after a switch delete + re-add (SBM-008/198), no thrashing after repeated flaps (SBM-009), a
      compute's GPU set never split across switches (SBM-011).
    * **Restarts:** pairwise concurrent afm-agent / afm-controller restarts (SBM-006), leader controller
      flap with every agent reconnecting (SBM-099).
    * **Lifecycle:** control path terminated on compute decommission (SBM-207: the agent exits or reports
      is-connected false; on g02-g17 the process stays after DELETE), event trail per phase (SBM-042),
      switch asic-info ports drop on compute delete (SBM-043), readmit with a port down (SBM-038),
      agent host switch deleted mid-admission (SBM-045), no stale events after a switch delete (SBM-030),
      force-decommission of an admitted switch (SBM-178).
    * **Control plane under data-path changes:** non-sideband link down (SBM-097), VLAN/FDB creation
      (SBM-114), CP switch down during vPOD CRUD (SBM-111), port MTU below the IFCP MTU (SBM-023,
      `commands.switch.port_mtu`, `thresholds.afm.mtu_low` / `mtu_low_expect`).
    * **Switch-agent start-up checks:** management IP on eth1 (BUG-019, `commands.switch.mgmt_to_eth1` /
      `mgmt_restore`) and an invalid fru.json (BUG-020, `thresholds.afm.fru_json_path`). Both skip until set.
  * **MCH racks (37, `test_afm_mch.py`).** These skip unless the pod rack-type contains `mch` (heliosmch /
    heliosmchmini) or `thresholds.mch.enabled` is set. The used ports come from the lane mask
    (`thresholds.mch.lane_mask`, default `000fff333000fff333`, one hex digit per station; `mask_order`
    VERIFY): 6 two-port, 6 one-port and 6 empty stations = 18 used ports.
    * **Layout (read-only):** station distribution, lane bitmask and strategy, MACs only on used ports,
      MI450 numbering, unused ports disabled in afmctl and AFM, 18 links up per GPU, switch slot ids 1-3,
      destination tables, path-to-port bit positions, no config or alarms on disabled ports, disabled ports
      in telemetry without data, mini-rack trunk ports and priority tagging, no VLAN on unused lanes.
    * **Dynamic (disruptive):** path maps per station type on link down and flap, lane mask on link
      down, MI450 port ids in events, unused-lane flap raises no event, drain/activate used ports only,
      reset RAS, vPOD with 18 planes, VLANs on vPOD CRUD, Reconfigure/Reconcile, degraded with a link or
      a plane down, lifecycle unchanged, layout kept across controller/agent restarts and snapshot restore.
    * **Traffic and templates:** L2 ping + TransferBench over 18 links, `mch.min_gbps` floor, mock IFoE
      driver (`commands.mch.mock_driver_check`), invalid rack type at bootstrap (`commands.mch.bootstrap_invalid`),
      AFM upgrade keeps MCH (`commands.afm.upgrade`).
  * **Lab equipment (15, `test_afm_equipment.py`).** Each case skips until its inventory entry exists:
    * LDAP / RADIUS login through the authn-policy (AFM-044, CTL-119) and snapshot/restore with both
      (CTL-042): `afm.ldap` / `afm.radius` `{authenticator, username, password_env}`.
    * Audit export to syslog (CTL-101): `afm.syslog {create: {path, body}, check}`.
    * Upgrades: rollout N-1 and back (CTL-054, `commands.afm.rollout` + `thresholds.afm.versions`), SONiC /
      ONIE / firmware upgrade of a switch (AFM-UPGRADE-004, HW-022, CTL-078), install from a local file
      (SWA-128), rollback after a failed upgrade (SWA-164), cluster move (CTL-127).
    * Management network cut and restored (`commands.mgmtnet.down` / `up`, run on the runner): compute
      unhealthy then healthy (AFM-008), event naming the compute (E2E-093), other controllers stable
      (BUG-003), leader moves and the old leader rejoins (CTL-050).
  * **Built from the g02-g17 procedures, with their script bugs fixed** (see `helios/afm_ops.py`):
    * membership is checked per GPU
    * a slot's agent is re-located on every poll
    * PortFailed is checked on every compute
    * 400/412 transients are retried
    * a vPOD is never left in needdatafabricreconfiguration
  * **Offline proof.**
    * `selftest/fake_afm.py` is an in-process HTTPS + WebSocket AFM. `selftest/fake_rack.py` simulates
      the rack behind it (agents, controllers, admission, vPOD membership, planes, links) and the switch
      SSH the disruptive cases drive.
    * The fake also has an MCH mode (`fake_topology.mch: full | mini`; 36 IFoE ports per GPU, the lane mask's
      18 used) and simulates the lab equipment (directory logins, syslog server, rollout, switch images,
      management-network cuts through a fake runner). Inventories: `selftest/inventory-mch.yaml`,
      `inventory-mchmini.yaml`, `inventory-equip.yaml`.
    * `unit/test_afm_selftest.py` injects 16 API faults and 180 product faults (lifecycle, links, failover,
      controller, regression, compute side, IFCP, traffic, sideband placement, MCH, lab equipment), including
      the full-rack issues (stale planes after readmit, VLAN wipe AIFM-1901, coupled ports). Each one must
      fail exactly the cases that should catch it.

Some cases can't be fully checked on generic SONiC:
* TC-SWH-004 is passive: thermalctld must be running, and fans must ramp when
  a sensor is over its high threshold.
* TC-SWH-009 is `manual`.
* TC-SWH-005, 020 and 029 skip with a reason if the platform doesn't expose
  fan power, PSU manufacturer or PSU input type.

## Calibrate on the first rack run

The plan doesn't pin these down, so they're inventory templates (see the
`VERIFY` comments in `inventory/example-fullrack.yaml`). A test whose template
is unset skips with the key name; it never guesses:

* `commands.l2ping.cmd`: the L2 ping tool or script used by AFM's `ping check.sh`
* `commands.transferbench.in_domain` / `cross_domain`: the "2a 1G" invocation
  and the IFoE port-subset mechanism
* `commands.transferbench.bw_regex`: only needed if the summary line doesn't
  match the built-in patterns
* `commands.rccl.cross_domain`: the mpirun/RCCL environment for IFoE
* `commands.perftest.*`: `*_imm` verb flags, GID indexes and the IPv6 flag for
  your perftest build
* `commands.ras.datapath_reset` (defaults to the plan's `afmctl reset device --datapath`),
  `ready_check`, `partial_stations_down` / `_up`, `dc_cycle`, `ac_cycle`
* `commands.ifoe.unload` / `load` / `loaded_check`: the IFoE kernel module name
* `commands.metrics_exporter.run` (and `upgrade` / `upgrade_image`), plus
  `thresholds.metrics.families` if your exporter's metric names differ
* `thresholds.switch.expected_fans`, `expected_psus`, `psu_capacity_w`, and the
  bandwidth floors (`datapath.*`, including `perftest.min_gbps`)
* `thresholds.switch.expected_up_ports: described`: only ports with a cabling
  description must be up (mini racks leave most ports admin-up but uncabled)
* `thresholds.switch.port_speed_overrides` (e.g. 100G uplinks),
  `psu_identity_exempt_re` / `psu_load_balance` (busbar-fed trays)
* MCH and lab-equipment cases (commented in `inventory/example-fullrack.yaml`):
  `thresholds.mch.*` and `commands.mch.*`; `afm.ldap` / `afm.radius` / `afm.syslog`;
  `commands.switch.*` upgrade templates with `thresholds.switch.target_image` / `local_image`;
  `commands.afm.rollout` / `cluster_move` / `upgrade` with `thresholds.afm.versions` / `cluster_move_target`;
  `commands.mgmtnet.down` / `up`; `commands.switch.mgmt_to_eth1` / `mgmt_restore` and
  `thresholds.afm.fru_json_path` (BUG-019/020)


## Plan ID fixes

The workbook reuses some IDs. The canonical IDs are in `testplan/manifest.yaml`,
which keeps the original ID as `plan_id`:
* L1 *RCCL Cross Domain* becomes **TC-AFM-005A**. TC-AFM-005 is the L2
  failover-policy CRUD case.
* *Audit Events* becomes **TC-AFM-048A**. TC-AFM-048 is techsupport.
* The unnumbered *Switch Decommission/Delete/Drain Not allowed in Minirack*
  becomes **TC-AFM-035A**.

## Adding tests

1. Tag each test with `@pytest.mark.tc("<id>")` and a level marker. Mark tests
   that change rack state as `disruptive`.
2. Take `switch` (per-switch param), `computes`, `rack` or `rack.afm`, and
   `checks` (soft asserts, so one run reports every node's failures).
3. Add a replay rule plus a fault case in `unit/test_selftest_faults.py`, so
   the new check is proven to fail when it should.
4. Run `python tools/coverage.py --missing`.
