"""BMC of every switch / compute tray over Redfish (HTTPS, read-only GETs): TC-BMC-001..006 (tests/bmc/).

Each node's BMC is reached at its OWN address (inventory ``bmc:``, not the node's host), as its ``bmc_user:``, with
its own password ($HELIOS_BMC_PASSWORD_<NODE>, see helios.compute.bmc_env) or the rack default
($HELIOS_BMC_PASSWORD) - the UI saves them (never the inventory). ``snapshot()`` reads one BMC once: service root,
systems (power state, health), managers (firmware, health), chassis sensors (legacy Thermal / Power, else the
Sensors collection) and the newest log entries; the tests judge that snapshot with the pure functions below.

Thresholds (inventory ``thresholds.bmc``): timeout_s (15), verify_tls (false: BMCs have self-signed certificates),
log_window_h (24), log_entries_max (200), sensors_max (300).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from .compute import BMC_ENV, bmc_env

log = logging.getLogger("helios.cmd")


class BmcError(RuntimeError):
    pass


def credentials(host, environ=None) -> tuple[str, str, str, str | None]:
    """(bmc address, user, password, why not) of an inventory Host; ``why not`` names what is missing."""
    env = os.environ if environ is None else environ
    bmc = str(host.extra.get("bmc") or "").strip()
    user = str(host.extra.get("bmc_user") or "").strip()
    pw = env.get(bmc_env(host.name)) or env.get(BMC_ENV) or ""
    why = ("no bmc address in the inventory" if not bmc else "no bmc_user in the inventory" if not user
           else f"no BMC password (UI: Inventory tab default or the Setup node table; ${BMC_ENV})" if not pw else None)
    return bmc, user, pw, why


class Redfish:
    """Read-only Redfish client of one BMC (basic auth over HTTPS)."""

    def __init__(self, address: str, user: str, password: str, timeout: float = 15, verify: bool = False,
                 base: str | None = None):
        import requests
        import urllib3
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # an address may carry its scheme / port (https://10.0.0.5:8443); plain = https on 443
        self.base = (base or (address if address.startswith(("http://", "https://")) else f"https://{address}")).rstrip("/")
        self.address, self.timeout, self.verify = address, timeout, verify
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.s.headers["Accept"] = "application/json"

    def get(self, path: str) -> dict:
        url = path if path.startswith("http") else self.base + path
        try:
            r = self.s.get(url, timeout=self.timeout, verify=self.verify)
        except Exception as e:  # noqa: BLE001 - connection refused / timeout / TLS
            raise BmcError(f"{self.address}: {type(e).__name__}: {str(e)[:160]}") from None
        log.debug("[bmc %s] GET %s -> %s", self.address, path, r.status_code)
        if r.status_code in (401, 403):
            raise BmcError(f"{self.address}: login refused (HTTP {r.status_code}) - check bmc_user / the BMC password")
        if r.status_code != 200:
            raise BmcError(f"{self.address}: GET {path} -> HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise BmcError(f"{self.address}: GET {path}: not JSON") from None

    def members(self, path: str) -> list[str]:
        return [m["@odata.id"] for m in self.get(path).get("Members") or [] if isinstance(m, dict) and m.get("@odata.id")]

    def close(self):
        self.s.close()


def _health(o: dict) -> str:
    return str(((o or {}).get("Status") or {}).get("Health") or "")


def _sensor(name, reading, unit, o, health="") -> dict:
    th = o.get("Thresholds") or {}

    # legacy Thermal / Power names -> the Sensor schema's Thresholds keys
    new = {"UpperThresholdCritical": "UpperCritical", "UpperThresholdFatal": "UpperFatal",
           "LowerThresholdCritical": "LowerCritical", "LowerThresholdFatal": "LowerFatal",
           "UpperThresholdNonCritical": "UpperCaution", "LowerThresholdNonCritical": "LowerCaution"}

    def lim(*keys):
        for k in keys:
            v = o.get(k)
            if v is None and th:
                v = (th.get(new[k]) or {}).get("Reading")
            if isinstance(v, (int, float)):
                return v
        return None
    return {"name": name, "reading": reading, "unit": unit, "health": health or _health(o),
            "upper_crit": lim("UpperThresholdCritical", "UpperThresholdFatal"),
            "lower_crit": lim("LowerThresholdCritical", "LowerThresholdFatal"),
            "upper_warn": lim("UpperThresholdNonCritical"), "lower_warn": lim("LowerThresholdNonCritical")}


def snapshot(rf: Redfish, log_entries_max: int = 200, sensors_max: int = 300) -> dict:
    """Everything the BMC cases judge, from GETs only."""
    root = rf.get("/redfish/v1/")
    snap = {"root": {"product": root.get("Product"), "vendor": root.get("Vendor"),
                     "redfish": root.get("RedfishVersion")}, "systems": [], "managers": [], "sensors": [],
            "log_entries": [], "errors": []}
    for path in rf.members("/redfish/v1/Systems"):
        s = rf.get(path)
        snap["systems"].append({"id": s.get("Id"), "power": s.get("PowerState"), "health": _health(s),
                                "model": s.get("Model"), "bios": s.get("BiosVersion"), "path": path})
    for path in rf.members("/redfish/v1/Managers"):
        m = rf.get(path)
        snap["managers"].append({"id": m.get("Id"), "firmware": m.get("FirmwareVersion"), "health": _health(m),
                                 "model": m.get("Model"), "path": path})
    for path in rf.members("/redfish/v1/Chassis"):
        c = rf.get(path)
        got = False
        for res, groups in (("Thermal", ("Temperatures", "Fans")), ("Power", ("Voltages", "PowerSupplies"))):
            ref = (c.get(res) or {}).get("@odata.id")
            if not ref:
                continue
            try:
                d = rf.get(ref)
            except BmcError as e:
                snap["errors"].append(str(e))
                continue
            got = True
            for g in groups:
                for o in d.get(g) or []:
                    reading = next((o[k] for k in ("ReadingCelsius", "Reading", "ReadingVolts", "LineInputVoltage")
                                    if isinstance(o.get(k), (int, float))), None)
                    snap["sensors"].append(_sensor(f"{c.get('Id')}/{o.get('Name') or o.get('MemberId')}", reading,
                                                   o.get("ReadingUnits") or "", o))
        ref = (c.get("Sensors") or {}).get("@odata.id")
        if not got and ref:
            for spath in rf.members(ref)[:sensors_max]:
                try:
                    o = rf.get(spath)
                except BmcError as e:
                    snap["errors"].append(str(e))
                    continue
                snap["sensors"].append(_sensor(f"{c.get('Id')}/{o.get('Name') or o.get('Id')}", o.get("Reading"),
                                               o.get("ReadingUnits") or "", o))
    for base in [*(s["path"] for s in snap["systems"]), *(m["path"] for m in snap["managers"])]:
        try:
            services = rf.members(base.rstrip("/") + "/LogServices")
        except BmcError:
            continue
        for svc in services:
            try:
                entries_ref = (rf.get(svc).get("Entries") or {}).get("@odata.id")
                if not entries_ref:
                    continue
                col = rf.get(entries_ref)
            except BmcError as e:
                snap["errors"].append(str(e))
                continue
            for m in (col.get("Members") or [])[-log_entries_max:]:
                if isinstance(m, dict) and "Severity" not in m and m.get("@odata.id"):
                    try:
                        m = rf.get(m["@odata.id"])
                    except BmcError:
                        continue
                snap["log_entries"].append({"log": svc.rstrip("/").rsplit("/", 1)[-1], "id": m.get("Id"),
                                            "severity": m.get("Severity"), "created": m.get("Created"),
                                            "message": str(m.get("Message") or "")[:200]})
    return snap


# ------------------------------------------------------------------ judging (pure)
def bad_power(snap: dict) -> list[str]:
    return [f"system {s['id']}: power {s['power']!r}" for s in snap["systems"] if str(s["power"]).lower() != "on"]


def bad_health(snap: dict) -> tuple[list[str], list[str]]:
    """(critical, warning) of the systems and managers."""
    crit, warn = [], []
    for kind in ("systems", "managers"):
        for o in snap[kind]:
            h = o["health"]
            if h == "Critical":
                crit.append(f"{kind[:-1]} {o['id']}: health Critical")
            elif h == "Warning":
                warn.append(f"{kind[:-1]} {o['id']}: health Warning")
    return crit, warn


def bad_sensors(snap: dict) -> tuple[list[str], list[str]]:
    """(critical, warning): health Critical / reading beyond a critical limit; Warning / beyond a non-critical one."""
    crit, warn = [], []
    for s in snap["sensors"]:
        r, label = s["reading"], f"{s['name']} = {s['reading']}{(' ' + s['unit']) if s['unit'] else ''}"
        if s["health"] == "Critical" or (r is not None and ((s["upper_crit"] is not None and r >= s["upper_crit"])
                                                            or (s["lower_crit"] is not None and r <= s["lower_crit"]))):
            crit.append(f"{label} (health {s['health'] or '-'}, critical limits {s['lower_crit']}..{s['upper_crit']})")
        elif s["health"] == "Warning" or (r is not None and ((s["upper_warn"] is not None and r >= s["upper_warn"])
                                                             or (s["lower_warn"] is not None and r <= s["lower_warn"]))):
            warn.append(f"{label} (health {s['health'] or '-'}, limits {s['lower_warn']}..{s['upper_warn']})")
    return crit, warn


def _when(created) -> datetime | None:
    try:
        return datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_critical(snap: dict, hours: float = 24, now: datetime | None = None) -> tuple[list[dict], int]:
    """(Critical log entries within ``hours`` - or of unknown time, Warning count in the window)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    crit, warn = [], 0
    for e in snap["log_entries"]:
        t = _when(e["created"])
        if t is not None and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        recent = t is None or t >= since
        if e["severity"] == "Critical" and recent:
            crit.append(e)
        elif e["severity"] == "Warning" and recent:
            warn += 1
    return crit, warn


def firmware_groups(snaps: dict[str, dict]) -> dict[str, list[str]]:
    """{BMC firmware version: [node, ...]} over the nodes' managers."""
    out: dict[str, list[str]] = {}
    for node, snap in snaps.items():
        for m in snap["managers"][:1]:
            out.setdefault(str(m["firmware"] or "?"), []).append(node)
    return out
