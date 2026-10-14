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
.venv/bin/pip install -e '.[tools]'
```

### 1. Without a rack: check the framework

These use recorded SONiC and GPU outputs from `selftest/replay`, so no hardware
is needed.

```bash
.venv/bin/pytest unit                                                       # 131 tests: 67 L1 + 16 AFM API + 21 AFM lifecycle/link fault checks (~11 min)
.venv/bin/pytest tests --inventory selftest/inventory.yaml                  # 187 pass, 67 skip (disruptive skipped)
.venv/bin/pytest tests --inventory selftest/inventory.yaml --allow-disruptive   # 234 pass, 20 skip (one-switch fake)
.venv/bin/pytest tests/afm --inventory selftest/inventory-afm3.yaml --allow-disruptive   # 107 pass, 1 skip (3-switch fake)
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

The 49 `disruptive` cases need `--allow-disruptive`:
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

### 5. HTML report

Turn one or more run folders into one self-contained HTML page: a summary dashboard
(counts, charts, key findings, a run summary per suite), then every test with its
own collapsible command log.

```bash
.venv/bin/python tools/html_report.py results/<run-folder> [more run folders] -o results/report.html --title "..." --subtitle "..." --notes results/findings.txt
```

- **Run folders:** list them newest first with `ls -td results/*-2*/`. When several
  folders contain the same test, a run that executed it wins over one that skipped it.
- **`--notes`:** an optional file with one finding per line. Start a line with `!`
  for a problem or `+` for good news.
- **Where to keep reports:** in `results/`, which is git-ignored. Reports contain lab
  hostnames and IPs. The charts load Chart.js from a CDN.

### 6. Coverage: automated vs. missing

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

## Coverage (200 of 542 cases)

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
    * `unit/test_afm_selftest.py` injects 16 API faults and 21 lifecycle/link product faults, including
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
