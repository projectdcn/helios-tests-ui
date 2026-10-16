"use strict";
// helios-tests UI. Plain JS, no build step. The AFM password is read from the field when a run starts, sent once
// in that request and the field is cleared; it is never stored in the page, localStorage or a cookie.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
let racks = [], rack = null, es = null, inv = null, invMode = "form", lastPreview = null, rerunBase = null, quarNode = null;

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k instanceof Node ? k : String(k));
  return e;
}

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.headers["X-Helios-UI"] = "1";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((d.errors || [d.error || r.statusText]).join("\n"));
  return d;
}

// ---------------------------------------------------------------- tabs / rack
$$("nav button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(t) {
  $$("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  $$(".tab").forEach(s => s.classList.toggle("active", s.id === "tab-" + t));
  if (t === "runs") loadRuns();
  if (t === "results") loadResults();
  if (t === "inventory") loadInventory();
}

async function loadRacks() {
  racks = await api("GET", "/api/racks");
  const sel = $("#rack");
  const keep = sel.value;
  sel.replaceChildren(...racks.map(r => h("option", { value: r.name }, r.title)));
  if (keep) sel.value = keep;
  rackChanged();
}
$("#rack").addEventListener("change", () => { rackChanged(); const t = $("nav button.active").dataset.tab; showTab(t); });
function rackChanged() {
  rack = racks.find(r => r.name === $("#rack").value) || racks[0];
  if (!rack) return;
  const busy = rack.running ? ` · running ${rack.running}` : "";
  const q = rack.queued && rack.queued.length ? ` · ${rack.queued.length} queued` : "";
  $("#rackinfo").textContent = `${rack.runner === "ssh" ? "via " + rack.ssh.target + ":" + rack.ssh.port : "local"} · `
    + `${rack.inventory}${rack.inventory_exists ? "" : " (MISSING)"}${busy}${q}`;
  $("#pwenv").textContent = rack.password_env;
  $("#known-row").hidden = !rack.known;
  $("#known-file").textContent = rack.known || "";
}

// ---------------------------------------------------------------- run form
const form = $("#runform");
form.addEventListener("change", () => {
  const kind = form.kind.value;
  $("#health-opts").hidden = kind !== "health";
  $("#case-opts").hidden = kind !== "pytest";
});
$("#resolve").addEventListener("click", async () => {
  $("#resolved").textContent = "reading the workbook…";
  try {
    const f = {};
    for (const k of ["level", "scope", "disruptive", "health", "origin", "status", "suite"]) f[k] = form["s_" + k].value;
    f.rack = rack.name;
    const d = await api("POST", "/api/select", f);
    form.tc.value = d.tc.join(",");
    $("#resolved").textContent = `${d.tc.length} case(s) from ${d.workbook}`;
  } catch (e) { $("#resolved").textContent = e.message; }
});
form.addEventListener("submit", async ev => {
  ev.preventDefault();
  const kind = form.kind.value;
  const req = { rack: rack.name, kind, pytest_args: form.pytest_args.value, password: form.password.value };
  if (kind === "health") {
    req.known = form.known.checked;
    if (form.health_tc.value.trim()) req.tc = form.health_tc.value;
  } else {
    Object.assign(req, { tc: form.tc.value, marker: form.marker.value, paths: form.paths.value,
      switches: form.switches.value, computes: form.computes.value, allow_disruptive: form.allow_disruptive.checked });
    if (req.allow_disruptive && !confirm(`Run DISRUPTIVE cases on ${rack.title}?`)) return;
  }
  form.password.value = "";
  $("#runmsg").textContent = "";
  try {
    const run = await api("POST", "/api/runs", req);
    $("#runmsg").textContent = run.status === "queued" && run.queue_position > 1 ? `queued (#${run.queue_position})` : "started";
    showTab("runs");
    openLog(run.id);
  } catch (e) { $("#runmsg").textContent = e.message; $("#runmsg").className = "err"; }
});

// ---------------------------------------------------------------- runs + live log
$("#refresh-runs").addEventListener("click", loadRuns);
function what(job) {
  if (job.kind === "health") return "health check" + (job.tc.length ? " --tc " + job.tc.join(",") : "");
  if (job.kind === "rerun") return "re-run failed of " + job.base_run;
  return [job.tc.length ? job.tc.join(",") : "", job.marker ? "-m " + job.marker : "", job.paths.join(" ")].filter(Boolean).join(" ");
}
function resultLinks(name) {
  return h("a", { href: `/results/${encodeURIComponent(rack.name)}/${encodeURIComponent(name)}/` + "tc_summary.csv", target: "_blank" }, name);
}
async function loadRuns() {
  if (!rack) return;
  const runs = await api("GET", "/api/runs?rack=" + encodeURIComponent(rack.name));
  $("#runs tbody").replaceChildren(...runs.map(r => h("tr", {},
    h("td", {}, r.id),
    h("td", { class: "st-" + r.status }, r.status + (r.status === "queued" && r.queue_position ? ` #${r.queue_position}` : "")),
    h("td", {}, what(r.job)),
    h("td", {}, r.started || r.created),
    h("td", {}, r.rc ?? ""),
    h("td", { class: "v-" + (r.verdict || "") }, r.verdict || r.error || ""),
    h("td", {}, ...(r.results || []).map(n => [resultLinks(n), " "])),
    h("td", {}, h("button", { onclick: () => openLog(r.id) }, "Log"),
      ["queued", "running"].includes(r.status) ? h("button", { onclick: () => stopRun(r.id) }, r.status === "queued" ? "Cancel" : "Stop") : null))));
}
async function stopRun(id) {
  if (!confirm("Stop run " + id + "?")) return;
  await api("POST", `/api/runs/${encodeURIComponent(id)}/stop`, {});
  loadRuns();
}
function openLog(id) {
  if (es) es.close();
  const pre = $("#log");
  pre.textContent = "";
  $("#logtitle").textContent = "Log: " + id;
  es = new EventSource(`/api/runs/${encodeURIComponent(id)}/stream`);
  es.onmessage = ev => {
    const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.append(JSON.parse(ev.data) + "\n");
    if (atEnd) pre.scrollTop = pre.scrollHeight;
  };
  es.addEventListener("end", () => { es.close(); es = null; loadRuns(); loadRacks(); });
  es.onerror = () => { /* the browser reconnects with Last-Event-ID; the server resumes from 'from' */ };
  loadRuns();
}

// ---------------------------------------------------------------- results history
$("#refresh-results").addEventListener("click", loadResults);
async function loadResults() {
  if (!rack) return;
  const rows = await api("GET", `/api/racks/${encodeURIComponent(rack.name)}/history`);
  const base = n => `/results/${encodeURIComponent(rack.name)}/${encodeURIComponent(n)}/`;
  $("#results tbody").replaceChildren(...rows.map(r => h("tr", {},
    h("td", {}, r.name, r.merged_from ? h("div", { class: "muted" }, "merged from " + r.merged_from.join(" + ")) : null),
    h("td", {}, r.mtime),
    h("td", {}, Object.entries(r.counts).map(([v, n]) => `${n} ${v}`).join(", ")),
    h("td", { class: r.health ? "v-" + r.health.verdict : "" }, r.health ? `${r.health.verdict} (${r.health.blockers} blocker, ${r.health.warnings} warning)` : ""),
    h("td", {}, ...r.reports.map(f => [h("a", { href: base(r.name) + f, target: "_blank" }, f), " "]),
      h("a", { href: base(r.name) + "tc_summary.csv", target: "_blank", class: "muted" }, "csv")),
    h("td", {},
      r.reports.includes("report.html") ? null : h("button", { onclick: () => buildReport(r.name) }, "Build report"),
      r.health && r.health.verdict !== "GO" ? h("button", { onclick: () => askRerun(r.name) }, "Re-run failed") : null))));
}
async function buildReport(name) {
  try { await api("POST", `/api/racks/${encodeURIComponent(rack.name)}/report`, { run: name }); loadResults(); }
  catch (e) { alert(e.message); }
}
function askRerun(name) {
  rerunBase = name;
  $("#rerun-name").textContent = name;
  $("#rerun-box").hidden = false;
  $("#rerun-pw").focus();
}
$("#rerun-cancel").addEventListener("click", () => { $("#rerun-box").hidden = true; $("#rerun-pw").value = ""; });
$("#rerun-go").addEventListener("click", async () => {
  const pw = $("#rerun-pw").value;
  $("#rerun-pw").value = "";
  $("#rerun-box").hidden = true;
  try {
    const run = await api("POST", "/api/runs", { rack: rack.name, kind: "rerun", base_run: rerunBase, known: true, password: pw });
    showTab("runs");
    openLog(run.id);
  } catch (e) { alert(e.message); }
});

// ---------------------------------------------------------------- inventory editor
const COLS = { switches: ["name", "host", "user", "port", "key_file", "afm_name"],
               computes: ["name", "host", "user", "gpus", "bmc", "slot", "afm_name"] };
$("#inv-reload").addEventListener("click", loadInventory);
$("#inv-mode-form").addEventListener("click", () => setMode("form"));
$("#inv-mode-raw").addEventListener("click", () => setMode("raw"));
function setMode(m) {
  invMode = m;
  $("#inv-form").hidden = m !== "form"; $("#inv-raw").hidden = m !== "raw";
  $("#inv-mode-form").classList.toggle("active", m === "form"); $("#inv-mode-raw").classList.toggle("active", m === "raw");
  resetPreview();
}
function resetPreview() { lastPreview = null; $("#inv-save").disabled = true; $("#preview").replaceChildren(); $("#savemsg").textContent = ""; }
function msgs(errors, warnings) {
  return h("div", {}, ...(errors || []).map(e => h("div", { class: "err" }, "✗ " + e)),
    ...(warnings || []).map(w => h("div", { class: "warn" }, "! " + w)));
}
async function loadInventory() {
  if (!rack) return;
  resetPreview();
  $("#quar-box").hidden = true;
  inv = await api("GET", `/api/racks/${encodeURIComponent(rack.name)}/inventory`);
  $("#invpath").textContent = inv.path + (rack.runner === "ssh" ? " — local copy; pushed to the jump host only when a run syncs" : "");
  if (!inv.exists) { $("#invmsgs").replaceChildren(h("div", { class: "err" }, "inventory file not found")); return; }
  $("#invmsgs").replaceChildren(msgs(inv.errors, inv.warnings));
  $("#raw").value = inv.text;
  const f = inv.form || {};
  $("#f-rack").value = f.rack || "";
  $("#f-afm-url").value = (f.afm || {}).url || ""; $("#f-afm-user").value = (f.afm || {}).username || "";
  $("#f-afm-pwenv").value = (f.afm || {}).password_env || ""; $("#f-afm-tls").checked = !!(f.afm || {}).verify_tls;
  $("#f-transport").value = f.transport_yaml || ""; $("#f-defaults").value = f.defaults_yaml || "";
  $("#f-thresholds").value = f.thresholds_yaml || "";
  for (const key of ["switches", "computes"]) renderTable(key, f[key] || []);
}
function renderTable(key, rows) {
  const t = $("#t-" + key);
  const head = h("tr", {}, ...COLS[key].map(c => h("th", {}, c)), h("th", {}, "other keys (YAML)"), h("th", {}, "quarantine"), h("th", {}));
  t.replaceChildren(head, ...rows.map(r => rowEl(key, r)));
}
function rowEl(key, r) {
  const tr = h("tr", {});
  tr._q = r.quarantine || null;
  for (const c of COLS[key]) tr.append(h("td", {}, h("input", { "data-c": c, value: r[c] ?? "" })));
  tr.append(h("td", {}, h("input", { "data-c": "extra_yaml", value: r.extra_yaml || "" })));
  const q = r.quarantine;
  tr.append(h("td", { class: q ? "q" : "" },
    q ? `${q.reason}${q.since ? " · since " + q.since : ""}${q.until ? " · until " + q.until : ""} ` : "",
    r.name ? h("button", { type: "button", onclick: () => q ? liftQuarantine(r.name) : askQuarantine(r.name) }, q ? "Lift" : "Quarantine…") : null));
  tr.append(h("td", {}, h("button", { type: "button", title: "remove row", onclick: () => { tr.remove(); resetPreview(); } }, "✕")));
  return tr;
}
$$("[data-add]").forEach(b => b.addEventListener("click", () => {
  $("#t-" + b.dataset.add).append(rowEl(b.dataset.add, {}));
}));
$("#inv-form").addEventListener("input", resetPreview);
$("#raw").addEventListener("input", resetPreview);
function formPayload() {
  const f = { rack: $("#f-rack").value,
    afm: { url: $("#f-afm-url").value, username: $("#f-afm-user").value, password_env: $("#f-afm-pwenv").value, verify_tls: $("#f-afm-tls").checked },
    transport_yaml: $("#f-transport").value, defaults_yaml: $("#f-defaults").value, thresholds_yaml: $("#f-thresholds").value };
  for (const key of ["switches", "computes"]) {
    f[key] = $$("#t-" + key + " tr").slice(1).map(tr => {
      const r = { quarantine: tr._q };
      $$("input", tr).forEach(i => r[i.dataset.c] = i.value);
      return r;
    }).filter(r => Object.entries(r).some(([k, v]) => k !== "quarantine" && String(v || "").trim()));
  }
  return { form: f };
}
function showPreview(d, payload) {
  const pre = h("pre", { class: "diff" });
  for (const ln of (d.diff || "(no change)").split("\n")) {
    pre.append(h("span", { class: ln.startsWith("+") && !ln.startsWith("+++") ? "add" : ln.startsWith("-") && !ln.startsWith("---") ? "del" : "" }, ln + "\n"));
  }
  $("#preview").replaceChildren(msgs(d.errors, d.warnings), pre);
  lastPreview = d.errors.length || !d.diff ? null : payload;
  $("#inv-save").disabled = !lastPreview;
}
$("#inv-preview").addEventListener("click", async () => {
  const payload = invMode === "raw" ? { text: $("#raw").value } : formPayload();
  try { showPreview(await api("POST", `/api/racks/${encodeURIComponent(rack.name)}/inventory/check`, payload), payload); }
  catch (e) { $("#preview").replaceChildren(msgs(e.message.split("\n"))); }
});
$("#inv-save").addEventListener("click", async () => {
  if (!lastPreview) return;
  try {
    const d = await api("POST", `/api/racks/${encodeURIComponent(rack.name)}/inventory/save`, { ...lastPreview, sha: inv.sha });
    await loadInventory();
    $("#savemsg").textContent = d.saved ? `saved; backup ${d.backup}` : "no change";
  } catch (e) { $("#savemsg").textContent = e.message; $("#savemsg").className = "err"; }
});
function askQuarantine(node) {
  quarNode = node;
  $("#quar-node").textContent = node;
  $("#quar-reason").value = ""; $("#quar-until").value = "";
  $("#quar-box").hidden = false;
  $("#quar-reason").focus();
}
$("#quar-cancel").addEventListener("click", () => { $("#quar-box").hidden = true; });
$("#quar-preview").addEventListener("click", () => quarantinePreview({ node: quarNode, enabled: true,
  reason: $("#quar-reason").value, until: $("#quar-until").value }));
function liftQuarantine(node) { quarantinePreview({ node, enabled: false }); }
async function quarantinePreview(q) {
  const payload = { quarantine: q };
  try {
    showPreview(await api("POST", `/api/racks/${encodeURIComponent(rack.name)}/inventory/check`, payload), payload);
    $("#quar-box").hidden = true;
    $("#preview").scrollIntoView({ behavior: "smooth" });
  } catch (e) { $("#preview").replaceChildren(msgs(e.message.split("\n"))); }
}

loadRacks().catch(e => document.body.prepend(h("div", { class: "err" }, e.message)));
setInterval(() => { if ($("#tab-runs").classList.contains("active") && !es) loadRuns(); }, 5000);
