"use strict";
// helios-tests UI. Plain JS, no build step. The AFM password is read from the field when a run starts, sent once
// in that request and the field is cleared; it is never stored in the page, localStorage, a preset or a cookie.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
let racks = [], rack = null, es = null, inv = null, invMode = "form", lastPreview = null, rerunBase = null, quarNode = null;
let formRack = null, presetList = [];
const enc = encodeURIComponent;

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
  if (!r.ok) {
    const e = new Error((d.errors || [d.error || r.statusText]).join("\n"));
    e.status = r.status; e.data = d;
    throw e;
  }
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
  if (rack.name !== formRack) {        // the form of the rack picked: its last-used form, its presets
    formRack = rack.name;
    restoreForm();
    loadPresets();
  }
}

// ---------------------------------------------------------------- run form
const form = $("#runform");
const FILTERS = ["level", "scope", "disruptive", "health", "origin", "status", "suite"];
function kindChanged() {
  const kind = form.kind.value;
  $("#health-opts").hidden = kind !== "health";
  $("#case-opts").hidden = kind !== "pytest";
}
form.addEventListener("change", () => { kindChanged(); rememberForm(); });
form.addEventListener("input", rememberForm);
function formMsg(text, cls = "muted") { $("#formmsg").textContent = text; $("#formmsg").className = cls; }
function filterState() {
  const f = {};
  for (const k of FILTERS) f[k] = form["s_" + k].value;
  return f;
}
// what a run / preset of the current form contains (never the password)
function formState() {
  const kind = form.kind.value;
  const s = { kind, pytest_args: form.pytest_args.value };
  if (kind === "health") {
    s.known = form.known.checked;
    if (form.health_tc.value.trim()) s.tc = form.health_tc.value;
  } else {
    Object.assign(s, { tc: form.tc.value, marker: form.marker.value, paths: form.paths.value,
      switches: form.switches.value, computes: form.computes.value, allow_disruptive: form.allow_disruptive.checked,
      filters: filterState() });
  }
  return s;
}
// every field of the form (for the browser's last-used memory; never the password)
function fullState() {
  return { kind: form.kind.value, known: form.known.checked, health_tc: form.health_tc.value, tc: form.tc.value,
    marker: form.marker.value, paths: form.paths.value, switches: form.switches.value, computes: form.computes.value,
    allow_disruptive: form.allow_disruptive.checked, pytest_args: form.pytest_args.value, filters: filterState() };
}
const shq = a => /^[A-Za-z0-9_\/.,:=@%+-]+$/.test(a) ? a : "'" + a.replace(/'/g, "'\\''") + "'";
// fill the form from a preset, a past run's job (lists) or fullState() (strings); the password field is cleared
function fillForm(s) {
  const str = (v, sep = ",") => Array.isArray(v) ? v.join(sep) : (v ?? "");
  const kind = s.kind === "pytest" ? "pytest" : "health";
  const full = "health_tc" in s;
  form.kind.value = kind;
  form.known.checked = s.known !== false;
  form.health_tc.value = full ? str(s.health_tc) : (kind === "health" ? str(s.tc) : "");
  form.tc.value = full || kind === "pytest" ? str(s.tc) : "";
  form.marker.value = s.marker || "";
  form.paths.value = str(s.paths, " ") || "tests";
  form.switches.value = s.switches || "";
  form.computes.value = s.computes || "";
  form.allow_disruptive.checked = !!s.allow_disruptive;
  form.pytest_args.value = Array.isArray(s.pytest_args) ? s.pytest_args.map(shq).join(" ") : (s.pytest_args || "");
  const f = s.filters || {};
  for (const k of FILTERS) form["s_" + k].value = f[k] || (k === "suite" ? "all" : "");
  form.password.value = "";
  $("#resolved").textContent = "";
  kindChanged();
}
const lsKey = r => "helios-ui:runform:" + r;
function rememberForm() {
  if (!rack) return;
  try { localStorage.setItem(lsKey(rack.name), JSON.stringify(fullState())); } catch (e) { /* storage off: fine */ }
}
function restoreForm() {
  let s = null;
  try { s = JSON.parse(localStorage.getItem(lsKey(rack.name)) || "null"); } catch (e) { s = null; }
  if (s && typeof s === "object" && !Array.isArray(s)) { delete s.password; fillForm(s); }
  else { form.reset(); kindChanged(); }
  formMsg("");
}
// a preset or a past run's settings into the form (re-run of failed checks: the Results tab's re-run box)
function applySettings(s, label) {
  if (s.kind === "rerun") {
    showTab("results");
    askRerun(s.base_run);
    return;
  }
  fillForm(s);
  rememberForm();
  showTab("run");
  formMsg(`${label} loaded: check it, type the AFM password, then Start run`, "ok");
}

// ---------------------------------------------------------------- presets (per rack, on the server; no password)
async function loadPresets(select = "") {
  const sel = $("#preset");
  try { presetList = await api("GET", `/api/racks/${enc(rack.name)}/presets`); }
  catch (e) { presetList = []; formMsg("presets: " + e.message, "err"); }
  sel.replaceChildren(h("option", { value: "" }, presetList.length ? "(choose a preset)" : "(no presets)"),
    ...presetList.map(p => h("option", { value: p.name, title: "saved " + (p.saved || "") }, p.name)));
  sel.value = select;
}
$("#preset").addEventListener("change", () => {
  const p = presetList.find(x => x.name === $("#preset").value);
  if (p) applySettings(p.settings, `Preset "${p.name}"`);
});
$("#preset-save").addEventListener("click", async () => {
  const name = prompt(`Save the form as a preset of ${rack.title}.\nName (letters, digits, space, - _ . ; at most 60):`,
    $("#preset").value || "");
  if (name === null) return;
  const body = { name: name.trim(), preset: formState() };
  const url = `/api/racks/${enc(rack.name)}/presets`;
  try {
    let d;
    try { d = await api("POST", url, body); }
    catch (e) {
      if (e.status !== 409) throw e;
      if (!confirm(`A preset "${body.name}" exists for ${rack.title}. Overwrite it?`)) return;
      d = await api("POST", url, { ...body, overwrite: true });
    }
    await loadPresets(d.name);
    formMsg(`saved preset "${d.name}"`, "ok");
  } catch (e) { formMsg(e.message, "err"); }
});
$("#preset-delete").addEventListener("click", async () => {
  const name = $("#preset").value;
  if (!name) { formMsg("choose a preset to delete first"); return; }
  if (!confirm(`Delete preset "${name}" of ${rack.title}?`)) return;
  try {
    await api("POST", `/api/racks/${enc(rack.name)}/presets/delete`, { name });
    await loadPresets();
    formMsg(`deleted preset "${name}"`, "ok");
  } catch (e) { formMsg(e.message, "err"); }
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
  const req = { rack: rack.name, ...formState(), password: form.password.value };
  if (req.allow_disruptive && !confirm(`Run DISRUPTIVE cases on ${rack.title}?`)) return;
  rememberForm();
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
      h("a", { class: "btn", href: `/api/runs/${enc(r.id)}/log.txt`, download: "",
               title: "the log as a text file (a running run: what is there so far)" }, "Download log"),
      h("button", { onclick: () => runAgain(r), title: "fill the Run form with this run's settings (not the password)" }, "Run again"),
      ["queued", "running"].includes(r.status) ? h("button", { onclick: () => stopRun(r.id) }, r.status === "queued" ? "Cancel" : "Stop") : null))));
}
function runAgain(r) {
  if (!racks.find(x => x.name === r.rack)) {
    alert(`Run ${r.id} was on rack "${r.rack}", which is no longer in ui/racks.yaml: nothing was filled.`);
    return;
  }
  if (!r.job) { alert(`Run ${r.id} has no saved settings.`); return; }
  if (r.rack !== rack.name) { $("#rack").value = r.rack; rackChanged(); }
  applySettings({ ...r.job, filters: r.filters || {} }, `Settings of run ${r.id}`);
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
  const dl = (n, what) => `/api/racks/${enc(rack.name)}/results/${enc(n)}/${what}`;
  $("#results tbody").replaceChildren(...rows.map(r => h("tr", {},
    h("td", {}, r.name, r.merged_from ? h("div", { class: "muted" }, "merged from " + r.merged_from.join(" + ")) : null),
    h("td", {}, r.mtime),
    h("td", {}, Object.entries(r.counts).map(([v, n]) => `${n} ${v}`).join(", ")),
    h("td", { class: r.health ? "v-" + r.health.verdict : "" }, r.health ? `${r.health.verdict} (${r.health.blockers} blocker, ${r.health.warnings} warning)` : ""),
    h("td", {}, ...r.reports.map(f => [h("a", { href: base(r.name) + f, target: "_blank" }, f), " "]),
      h("a", { href: base(r.name) + "tc_summary.csv", target: "_blank", class: "muted" }, "csv")),
    h("td", {},
      h("a", { class: "btn", href: dl(r.name, "zip"), download: "", title: "every file of the folder" }, "Download results (.zip)"),
      ...["report.html", "health.html"].filter(f => r.reports.includes(f)).map(f =>
        h("a", { class: "btn", href: dl(r.name, "file/" + f), download: "" }, f === "report.html" ? "Download report" : "Download health report"))),
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
