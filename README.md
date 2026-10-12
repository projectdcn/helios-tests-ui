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
  afm.py                AFM REST client (endpoint map comes from inventory)
  parsers.py            SONiC tables, sonic-db-cli, rccl-tests, TransferBench, perftest
  datapath.py           shared workloads: L2 ping, TransferBench, RCCL
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
unit/                   offline tests + fault-injection self-test (no rack needed)
selftest/               replay inventory + canned outputs (make_replay.py)
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
.venv/bin/pytest unit                                                       # unit tests + 28 injected-fault checks
.venv/bin/pytest tests --inventory selftest/inventory.yaml                  # 102 pass, 13 skip (disruptive skipped)
.venv/bin/pytest tests --inventory selftest/inventory.yaml --allow-disruptive   # 111 pass, 4 skip
```

### 2. On a real rack

**Step 1: make your inventory.** Files named `*.local.yaml` are git-ignored.

```bash
cp inventory/example-fullrack.yaml inventory/rack1.local.yaml
```

**Step 2: edit `inventory/rack1.local.yaml`.**
- Under `switches:` and `computes:`, set each `host:` to the real hostname or IP.
- Under `defaults:`, set the SSH user, and either `key_file` or `password_env`
  for each node type.
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

`--log-file` records every SSH command, its exit code and how long it took.
To avoid passing `--inventory` every time, set it once:

```bash
export HELIOS_INVENTORY=inventory/rack1.local.yaml
```

### 3. Running specific cases

| What you want | Add this |
|---|---|
| Specific plan IDs | `--tc TC-SWH-002,TC-AFM-001` |
| Part of the rack | `--switches sw1,sw2` / `--computes c01,c02` |
| Switch hardware tests only | `tests/l1/test_switch_hw.py` |
| Datapath tests only (ping, TransferBench, RCCL) | `tests/l1/test_afm_datapath.py` |
| Switch agent / hardware / RDMA / RAS only | `tests/switch` / `tests/hw` / `tests/scaleout` / `tests/ras` |
| By level or marker | `-m l1` / `-m "l1 and not stress"` |
| Tests that restart services, reset devices or reboot (TC-SWA-140/141, all RAS) | `--allow-disruptive` |
| Manual tests (e.g. the TC-SWH-009 heat test) | `--run-manual` |

For example, to run only the PSU checks on `sw1` and `sw2`:

```bash
.venv/bin/pytest tests --tc TC-SWH-018,TC-SWH-019,TC-SWH-021 --switches sw1,sw2
```

### 4. Results

Each run creates a folder `results/<rack>-<timestamp>/` with two files:
- `tc_summary.csv`: one PASS / FAIL / SKIP / PARTIAL verdict per plan ID.
- `tc_results.csv`: one row per test, with the failure message and measured
  values such as bandwidth and bus bandwidth.

### 5. Coverage: automated vs. missing

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

## Coverage (64 of 542 cases)

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
* `thresholds.switch.expected_fans`, `expected_psus`, `psu_capacity_w`, and the
  bandwidth floors (`datapath.*`, including `perftest.min_gbps`)

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
