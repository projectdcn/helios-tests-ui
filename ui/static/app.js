"use strict";
// helios-tests UI. Plain JS, no build step. The AFM password is read from the field when a run starts, sent once
// in that request and the field is cleared; it is never stored in the page, localStorage, a preset, the rack's
// saved default or a cookie. Opt-in, the server keeps it (macOS Keychain, or a 0600 file on Linux; the page only learns saved / not).
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
let racks = [], rack = null, es = null, inv = null, invMode = "form", lastPreview = null, rerunBase = null, quarNode = null;
let formRack = null, presetList = [], pwSaved = false, pwStore = "macOS Keychain";
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
  if (r.status === 401 && d.login) showLogin();
  if (!r.ok) {
    const e = new Error((d.errors || [d.error || r.statusText]).join("\n"));
    e.status = r.status; e.data = d;
    throw e;
  }
  return d;
}

// ---------------------------------------------------------------- access token login (HELIOS_UI_TOKEN on the server)
function showLogin() {
  if (!$("#login-box").hidden) return;
  $("#login-box").hidden = false;
  $("nav").hidden = true; $("main").hidden = true;
  $("#rack").closest("label").hidden = true; $("#rackinfo").hidden = true;   // no rack list before the login
  $("#login-box").token.focus();
}
$("#login-box").addEventListener("submit", async ev => {
  ev.preventDefault();
  const tok = $("#login-box").token.value;
  $("#login-box").token.value = "";
  try { await api("POST", "/api/login", { token: tok }); location.reload(); }
  catch (e) { $("#login-msg").textContent = e.message; $("#login-msg").className = "err"; $("#login-box").token.focus(); }
});
$("#logout").addEventListener("click", async () => { await api("POST", "/api/logout", {}).catch(() => {}); location.reload(); });
api("GET", "/api/version").then(v => { $("#logout").hidden = !v.auth; }).catch(() => {});

// ---------------------------------------------------------------- tabs / rack
$$("nav button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(t) {
  $$("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  $$(".tab").forEach(s => s.classList.toggle("active", s.id === "tab-" + t));
  if (t === "runs") loadRuns();
  if (t === "results") loadResults();
  if (t === "inventory") loadInventory();
  if (t === "run") loadSession();
}

// ---------------------------------------------------------------- case history (results/index.csv)
const OUTCOME_CLASS = { passed: "ok", failed: "err", error: "err", partial: "warn", blocked: "warn", skipped: "muted" };
$("#histform").addEventListener("submit", async ev => {
  ev.preventDefault();
  const tc = $("#histform").tc.value.trim();
  const body = $("#history tbody");
  body.replaceChildren();
  $("#histmsg").textContent = "reading the index…";
  try {
    const d = await api("GET", `/api/cases/${enc(tc)}/history`);
    $("#histmsg").textContent = `${d.rows.length} execution(s) of ${tc}`;
    body.replaceChildren(...d.rows.map(r => h("tr", {},
      h("td", { class: "mono" }, r.started), h("td", {}, r.rack), h("td", { class: "mono" }, r.run),
      h("td", { class: OUTCOME_CLASS[r.outcome] || "" }, r.outcome), h("td", { class: "mono" }, r.test),
      h("td", {}, r.message), h("td", {},
        r.log_url ? h("a", { href: r.log_url, target: "_blank", rel: "noopener" }, "log") : "", " ",
        r.report_url ? h("a", { href: r.report_url, target: "_blank", rel: "noopener" }, "report") : ""))));
  } catch (e) { $("#histmsg").textContent = e.message; }
});

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
    loadPwStatus();
    loadPlan();
    loadSession();
  }
}

// ---------------------------------------------------------------- sessions (per rack, ui-sessions.json)
let sessData = null;
const SESSION_NAME = /^[A-Za-z0-9 _.:-]{1,60}$/;
function sessMsg(text, cls = "muted") { $("#sess-msg").textContent = text; $("#sess-msg").className = cls; }
async function loadSession() {
  if (!rack) return;
  const r = rack.name;
  try {
    const d = await api("GET", `/api/racks/${enc(r)}/sessions`);
    if (!rack || rack.name !== r) return;
    sessData = d;
    const cur = d.sessions.find(s => s.id === d.current);
    $("#sess-name").textContent = cur ? cur.name : "(none yet)";
    $("#sess-info").textContent = cur
      ? `started ${cur.created} · ${cur.runs.length} run(s)${cur.runs.length ? " · " + cur.summary : ""}`
      : "the first run starts one";
    $("#sess-name").title = cur ? cur.id : "";
  } catch (e) { if (rack && rack.name === r) sessMsg("sessions: " + e.message, "err"); }
}
function dateName() {
  const d = new Date();
  const mon = d.toLocaleString("en-GB", { month: "short" });
  return `${d.getDate()} ${mon} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
$("#sess-new").addEventListener("click", async () => {
  let name = prompt(`New session of ${rack.title}: the next runs of this rack are consolidated in it.\n`
    + "Name (letters, digits, space, - _ . : ; at most 60):", dateName());
  if (name === null) return;
  name = name.trim();
  if (!SESSION_NAME.test(name)) { sessMsg("session name: 1-60 letters, digits, spaces, '-', '_', '.' or ':'", "err"); return; }
  try {
    const d = await api("POST", `/api/racks/${enc(rack.name)}/sessions`, { name });
    sessMsg(`new session "${d.name}" started`, "ok");
    loadSession();
  } catch (e) { sessMsg(e.message, "err"); }
});

// ---------------------------------------------------------------- run form
const form = $("#runform");
const FILTERS = ["level", "scope", "disruptive", "health", "origin", "status", "suite"];
// THE original state of the Run form (what a fresh page shows; "Reset to default" goes back to it). fullState() shape.
const ORIGINAL_FORM = Object.freeze({ kind: "health", known: true, health_tc: "", tc: "", marker: "", paths: "tests",
  switches: "", computes: "", allow_disruptive: false, pytest_args: "", filters: Object.freeze({}) });
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
  form.remember_pw.checked = false;
  kindChanged();
  pickerRestore();
}
const lsKey = r => "helios-ui:runform:" + r;
function rememberForm() {
  if (!rack) return;
  try { localStorage.setItem(lsKey(rack.name), JSON.stringify(fullState())); } catch (e) { /* storage off: fine */ }
}
// the form a rack opens with: its saved default (server) if there is one, else the form last used in this browser,
// else ORIGINAL_FORM. The browser's / original form is shown at once and replaced when a saved default arrives.
async function restoreForm() {
  const r = rack.name;
  let s = null;
  try { s = JSON.parse(localStorage.getItem(lsKey(r)) || "null"); } catch (e) { s = null; }
  if (s && typeof s === "object" && !Array.isArray(s)) { delete s.password; fillForm(s); }
  else fillForm(ORIGINAL_FORM);
  formMsg("");
  try {
    const d = await api("GET", `/api/racks/${enc(r)}/defaults`);
    if (!rack || rack.name !== r || !d.settings) return;
    fillForm(d.settings);
    formMsg(`saved default of ${rack.title} loaded (saved ${d.saved})`);
  } catch (e) { if (rack && rack.name === r) formMsg("saved default: " + e.message, "err"); }
}
$("#default-save").addEventListener("click", async () => {
  if (!confirm(`Save this form as the default of ${rack.title}? The Run tab opens with it from now on (not the password).`)) return;
  try {
    const d = await api("POST", `/api/racks/${enc(rack.name)}/defaults`, { settings: formState() });
    formMsg(`saved as the default of ${rack.title} (${d.saved})`, "ok");
  } catch (e) { formMsg(e.message, "err"); }
});
// back to ORIGINAL_FORM; offers to delete the rack's saved default and this browser's remembered form too (so the
// next load is the original as well). Presets are never touched.
$("#default-reset").addEventListener("click", async () => {
  const r = rack.name, title = rack.title;
  fillForm(ORIGINAL_FORM);
  let hasDef = false, hasLs = false;
  try { hasDef = !!(await api("GET", `/api/racks/${enc(r)}/defaults`)).settings; } catch (e) { hasDef = false; }
  try { hasLs = localStorage.getItem(lsKey(r)) !== null; } catch (e) { hasLs = false; }
  if (!hasDef && !hasLs) { formMsg("form reset to the original state", "ok"); return; }
  const what = [hasDef ? `the saved default of ${title}` : "", hasLs ? "the form remembered in this browser" : ""]
    .filter(Boolean).join(" and ");
  if (!confirm(`The form is back to its original state.\n\nAlso delete ${what}, so the Run tab opens with the original `
               + "form next time too? (Presets are kept.)")) {
    formMsg(`form reset to the original state; ${what} kept`, "ok");
    return;
  }
  try {
    if (hasDef) await api("POST", `/api/racks/${enc(r)}/defaults/delete`, {});
    try { localStorage.removeItem(lsKey(r)); } catch (e) { /* storage off: nothing remembered */ }
    formMsg(`form reset to the original state; deleted ${what}`, "ok");
  } catch (e) { formMsg(e.message, "err"); }
});

// ---------------------------------------------------------------- AFM password saved on the UI server (opt-in)
// macOS: the Keychain; Linux: a 0600 password file (d.store names which)
async function loadPwStatus() {
  const r = rack.name;
  $("#pw-rack").textContent = rack.title;
  let d;
  try { d = await api("GET", `/api/racks/${enc(r)}/password`); }
  catch (e) { d = { saved: false, available: false, error: e.message }; }
  if (!rack || rack.name !== r) return;
  pwSaved = !!d.saved;
  const st = $("#pwstatus");
  pwStore = d.store || "macOS Keychain";
  $("#pw-store").textContent = pwStore.startsWith("password file") ? "on the UI server" : "in macOS Keychain";
  if (!d.available) { st.textContent = d.note || d.error || "no password store on the UI server: type the password for each run"; st.className = "muted"; }
  else if (d.error) { st.textContent = d.error; st.className = "err"; }
  else { st.textContent = pwSaved ? `saved (${pwStore})` : "not saved"; st.className = pwSaved ? "ok" : "muted"; }
  st.title = d.account ? `${pwStore}, account ${d.account}` : "";
  form.remember_pw.disabled = !d.available;
  $("#pw-save").disabled = !d.available;
  $("#pw-forget").disabled = !pwSaved;
}
$("#pw-save").addEventListener("click", async () => {   // save without starting a run; the field is cleared
  const pw = form.password.value;
  if (!pw) { formMsg("type the AFM password first, then Save password", "err"); return; }
  form.password.value = "";
  try {
    await api("POST", `/api/racks/${enc(rack.name)}/password`, { password: pw });
    formMsg(`AFM password of ${rack.title} saved (${pwStore}): leave the field empty to use it`, "ok");
  } catch (e) { form.password.value = pw; formMsg(e.message, "err"); }
  loadPwStatus();
});
$("#pw-forget").addEventListener("click", async () => {
  if (!confirm(`Delete the AFM password of ${rack.title} from the ${pwStore}?`)) return;
  try {
    const d = await api("POST", `/api/racks/${enc(rack.name)}/password/delete`, {});
    formMsg(d.deleted ? `AFM password of ${rack.title} deleted from the ${pwStore}` : "no password was saved", "ok");
  } catch (e) { formMsg(e.message, "err"); }
  loadPwStatus();
});
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
// ---------------------------------------------------------------- test-plan pick (filters -> case list -> tc ids)
// The workbook is read by the server (GET .../plan: dropdown values + cases, cached per workbook mtime); the pick
// itself (POST .../plan/select) is tools/select_cases.py's matching, so the UI lists exactly what the CLI would.
// "Don't overwrite my ids": pickerTc is what the picker last wrote into the tc box. A filter change or a tick
// replaces the box only while it still holds that (or is empty); once the user typed or edited it, a
// "Replace with N picked cases" link is offered instead. Loading a preset / Run again / the remembered form sets the
// filters without any event, ticks the listed cases that are in the saved ids and leaves the ids exactly as saved.
const PICK_COLS = ["level", "scope", "disruptive", "health", "origin", "status"];
const EMPTY = "(empty)";
let plan = null, planById = new Map(), pickSeq = 0, pickTimer = null, pickerTc = null, caseRows = [], lastPick = null;
const splitVals = v => (v || "").split(/,(?![^()]*\))/).map(x => x.trim()).filter(Boolean);
const normTc = v => (v || "").split(/[\s,]+/).filter(Boolean).join(",");
const valLabel = (col, v) => v === EMPTY ? (col === "status" ? "(not run)" : "(blank)") : v;
function pickMsg(text, cls = "muted") { $("#resolved").textContent = text; $("#resolved").className = cls; }

function renderPickers() {
  for (const div of $$("#pick .ms")) {
    const col = div.dataset.col, hidden = form["s_" + col];
    const chosen = splitVals(hidden.value), chosenL = new Set(chosen.map(x => x.toLowerCase()));
    const known = plan ? plan.columns[col] : [];
    const knownL = new Set(known.map(x => x.toLowerCase()));
    // values saved earlier that the workbook does not have (old preset, free text): kept and ticked, marked
    const opts = [...known, ...chosen.filter(c => !knownL.has(c.toLowerCase()))];
    const value = h("span", { class: "ms-value", id: `ms-${col}-v` });
    const boxes = opts.map(o => h("label", {},
      h("input", { type: "checkbox", value: o, checked: chosenL.has(o.toLowerCase()) }), " ", valLabel(col, o),
      knownL.has(o.toLowerCase()) ? null : h("span", { class: "muted" }, " (not in the workbook)")));
    const showValue = () => {
      const sel = boxes.map(b => b.firstChild).filter(b => b.checked).map(b => valLabel(col, b.value));
      value.textContent = sel.length ? sel.join(", ") : "any";
      value.title = value.textContent;
    };
    const changed = () => {
      hidden.value = boxes.map(b => b.firstChild).filter(b => b.checked).map(b => b.value).join(",");
      showValue();
      schedulePick();
    };
    boxes.forEach(b => b.firstChild.addEventListener("change", changed));
    const clear = h("button", { type: "button", class: "ms-clear", onclick: () => {
      boxes.forEach(b => { b.firstChild.checked = false; }); changed(); rememberForm(); } }, "Clear");
    const dd = h("details", { class: "ms-dd" },
      h("summary", { "aria-labelledby": `ms-${col}-l ms-${col}-v` }, value),
      h("div", { class: "ms-panel", role: "group", "aria-labelledby": `ms-${col}-l` },
        opts.length ? boxes : h("span", { class: "muted" }, plan ? "(no values)" : "(workbook not read)"),
        opts.length ? clear : null));
    dd.addEventListener("keydown", ev => {
      if (ev.key === "Escape" && dd.open) { dd.open = false; $("summary", dd).focus(); ev.preventDefault(); }
    });
    showValue();
    div.replaceChildren(div.querySelector(".ms-label"), dd);
  }
}
document.addEventListener("click", ev => {            // one dropdown open at a time; a click outside closes it
  for (const d of $$("#pick .ms-dd[open]")) if (!d.contains(ev.target)) d.open = false;
});
form.s_suite.addEventListener("change", () => schedulePick());

function schedulePick() {
  clearTimeout(pickTimer);
  pickTimer = setTimeout(() => runPick("filter"), 500);
}
// after the form was filled programmatically: no auto-overwrite, the list shows the saved ids ticked
function pickerRestore() {
  clearTimeout(pickTimer);
  pickSeq++;                                     // a pick still in flight belongs to the old form: ignore it
  $("#replace-tc").hidden = true;
  $("#case-search").value = "";
  pickerTc = null;
  renderPickers();
  if (plan) runPick("restore");
}
async function loadPlan() {
  const r = rack.name;
  plan = null; planById = new Map(); caseRows = []; lastPick = null;
  $("#caselist").hidden = true; $("#replace-tc").hidden = true;
  renderPickers();
  if (!rack.workbook) { pickMsg("no test-plan workbook configured for this rack ('workbook:' in ui/racks.yaml)"); return; }
  pickMsg("reading the workbook…");
  try {
    const p = await api("GET", `/api/racks/${enc(r)}/plan`);
    if (!rack || rack.name !== r) return;
    plan = p; planById = new Map(p.cases.map(c => [c.id, c]));
    renderPickers();
    await runPick("restore");
  } catch (e) { if (rack && rack.name === r) pickMsg(e.message, "err"); }
}
// mode: "filter" (filters changed: tick all, replace the ids only if the picker owns them), "fill" (the button:
// tick all and write the ids), "restore" (tick what the saved ids contain, leave the ids alone)
async function runPick(mode) {
  if (!rack || !plan) return;
  const seq = ++pickSeq, r = rack.name;
  try {
    const d = await api("POST", `/api/racks/${enc(r)}/plan/select`, { filters: filterState() });
    if (seq !== pickSeq || !rack || rack.name !== r) return;
    renderCases(d, mode);
  } catch (e) { if (seq === pickSeq) pickMsg(e.message, "err"); }
}
function renderCases(d, mode) {
  lastPick = d;
  const saved = new Set(normTc(form.tc.value).split(",").filter(Boolean));
  const seen = new Set();
  caseRows = d.tc.filter(id => !seen.has(id) && seen.add(id)).map(id => {
    const c = planById.get(id) || { id, title: "" };
    const cb = h("input", { type: "checkbox", value: id, "aria-label": `use ${id}` });
    cb.checked = mode === "restore" ? saved.has(id) : true;
    const tr = h("tr", {}, h("td", {}, cb), h("td", {}, h("code", {}, id)), h("td", {}, c.title),
      h("td", {}, c.level), h("td", {}, c.scope), h("td", {}, c.disruptive), h("td", {}, c.health),
      h("td", {}, c.status || "(not run)"));
    return { id, cb, tr, text: (id + " " + c.title).toLowerCase() };
  });
  const frag = document.createDocumentFragment();
  caseRows.forEach(x => frag.append(x.tr));
  $("#cases tbody").replaceChildren(frag);
  $("#caselist").hidden = !caseRows.length;
  $("#case-noauto").textContent = d.not_automated ? `· ${d.not_automated} matching case(s) not automated (not listed)` : "";
  applySearch();
  pickMsg(`${caseRows.length} case(s) from ${d.workbook}`);
  if (mode === "restore") {
    // the box is the picker's when it holds exactly the ticked cases (or nothing); otherwise it is the user's
    pickerTc = normTc(form.tc.value) === tickedIds().join(",") ? form.tc.value : null;
    $("#replace-tc").hidden = true;
    countMsg();
  } else syncTc(mode === "fill");
}
function tickedIds() { return caseRows.filter(x => x.cb.checked).map(x => x.id); }
function countMsg() {
  const shown = caseRows.filter(x => !x.tr.hidden).length;
  $("#case-count").textContent = `${tickedIds().length} of ${caseRows.length} selected`
    + (shown < caseRows.length ? ` · ${shown} shown` : "");
}
// put the ticked cases into the tc box, unless the user's own ids are there (then offer to replace them)
function syncTc(force = false) {
  countMsg();
  const v = tickedIds().join(",");
  const cur = normTc(form.tc.value);
  const owned = !cur || cur === v || (pickerTc !== null && cur === normTc(pickerTc));
  if (force || owned) {
    form.tc.value = v; pickerTc = v;
    $("#replace-tc").hidden = true;
    rememberForm();
  } else {
    const n = v ? v.split(",").length : 0;
    $("#replace-tc").textContent = `Replace the test case ids with the ${n} picked case(s)`;
    $("#replace-tc").title = "the test case ids were typed or edited by hand, so they were not replaced";
    $("#replace-tc").hidden = false;
  }
}
$("#replace-tc").addEventListener("click", ev => { ev.preventDefault(); syncTc(true); form.tc.focus(); });
$("#cases tbody").addEventListener("change", ev => { if (ev.target.type === "checkbox") syncTc(); });
function applySearch() {
  const q = $("#case-search").value.trim().toLowerCase();
  for (const x of caseRows) x.tr.hidden = !!q && !x.text.includes(q);
  countMsg();
}
$("#case-search").addEventListener("input", applySearch);
$("#case-search").addEventListener("keydown", ev => { if (ev.key === "Enter") ev.preventDefault(); });
for (const [id, on] of [["#case-all", true], ["#case-none", false]]) {
  $(id).addEventListener("click", () => {
    caseRows.filter(x => !x.tr.hidden).forEach(x => { x.cb.checked = on; });
    syncTc();
  });
}
$("#resolve").addEventListener("click", async () => {      // manual refresh: read the workbook again, tick all
  if (!rack) return;
  pickMsg("reading the workbook…");
  try {
    const p = await api("GET", `/api/racks/${enc(rack.name)}/plan`);
    plan = p; planById = new Map(p.cases.map(c => [c.id, c]));
    renderPickers();
    clearTimeout(pickTimer);
    await runPick("fill");
  } catch (e) { pickMsg(e.message, "err"); }
});
form.addEventListener("submit", async ev => {
  ev.preventDefault();
  const req = { rack: rack.name, ...formState(), password: form.password.value,
                remember_password: form.remember_pw.checked && !!form.password.value };
  if (form.remember_pw.checked && !req.password && !confirm("\"Remember in macOS Keychain\" is ticked but no password "
      + "is typed: nothing will be saved. Start anyway" + (pwSaved ? " (with the saved password)?" : "?"))) return;
  if (req.allow_disruptive && !confirm(`Run DISRUPTIVE cases on ${rack.title}?`)) return;
  rememberForm();
  form.password.value = "";
  $("#runmsg").textContent = ""; $("#runmsg").className = "";
  try {
    const run = await api("POST", "/api/runs", req);
    const kc = run.keychain || {};
    $("#runmsg").textContent = (run.status === "queued" && run.queue_position > 1 ? `queued (#${run.queue_position})` : "started")
      + (kc.saved ? "; password saved in the Keychain" : "") + (kc.error ? "; Keychain: " + kc.error : "");
    if (kc.error) $("#runmsg").className = "warn";
    form.remember_pw.checked = false;
    loadPwStatus();
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
function elapsed(started) {          // "2026-10-16 20:42:03" (the UI host's local time) -> "running 2m10s"
  const t = new Date(started.replace(" ", "T"));
  const s = Math.max(0, Math.round((Date.now() - t.getTime()) / 1000));
  return `running ${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}
async function loadRuns() {
  if (!rack) return;
  const runs = await api("GET", "/api/runs?rack=" + encodeURIComponent(rack.name));
  $("#runs tbody").replaceChildren(...runs.map(r => h("tr", {},
    h("td", {}, r.id),
    h("td", { class: "st-" + r.status }, r.status + (r.status === "queued" && r.queue_position ? ` #${r.queue_position}` : ""),
      r.status === "running" && r.started ? h("div", { class: "muted" }, `${elapsed(r.started)}${r.current ? " · " + r.current.split("::").pop() : ""}`) : null),
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
  es.addEventListener("end", () => { es.close(); es = null; loadRuns(); loadRacks(); loadSession(); });
  es.onerror = () => { /* the browser reconnects with Last-Event-ID; the server resumes from 'from' */ };
  loadRuns();
}

// ---------------------------------------------------------------- results history
$("#refresh-results").addEventListener("click", loadResults);
async function loadResults() {
  if (!rack) return;
  const [rows, sess] = await Promise.all([api("GET", `/api/racks/${encodeURIComponent(rack.name)}/history`),
    api("GET", `/api/racks/${enc(rack.name)}/sessions`).catch(e => ({ sessions: [], error: e.message }))]);
  const base = n => `/results/${encodeURIComponent(rack.name)}/${encodeURIComponent(n)}/`;
  const dl = (n, what) => `/api/racks/${enc(rack.name)}/results/${enc(n)}/${what}`;
  const runLink = n => h("a", { href: base(n) + (rows.some(x => x.name === n && x.reports.includes("report.html")) ? "report.html" : "tc_summary.csv"),
                                target: "_blank" }, n);
  $("#sessions tbody").replaceChildren(...(sess.error ? [h("tr", {}, h("td", { colspan: 7, class: "err" }, sess.error))] : []),
    ...(sess.sessions.length || sess.error ? [] : [h("tr", {}, h("td", { colspan: 7, class: "muted" }, "no session yet: the first run started from the Run tab starts one"))]),
    ...sess.sessions.map(s => h("tr", { class: s.current ? "current" : "" },
      h("td", {}, h("b", {}, s.name), s.current ? h("span", { class: "muted" }, " (current)") : null,
        h("div", { class: "muted" }, `${s.id} · started ${s.created}`)),
      h("td", {}, s.runs.length ? h("details", {}, h("summary", {}, `${s.runs.length} run(s)`),
        h("ul", {}, ...s.runs.map(n => h("li", {}, runLink(n))))) : "0"),
      h("td", {}, s.summary),
      h("td", {}, s.written || ""),
      h("td", {}, s.reports.includes("report.html") ? h("a", { href: base(s.id) + "report.html", target: "_blank" }, "session report") : "",
        s.folder ? [" ", h("a", { href: base(s.id) + "session.json", target: "_blank", class: "muted" }, "session.json")] : null),
      h("td", {}, s.folder ? h("a", { class: "btn", href: dl(s.id, "zip"), download: "", title: "the consolidated folder: csv files, logs, report, session.json" }, "Download results (.zip)") : ""),
      h("td", {}, s.runs.length ? h("button", { onclick: () => rebuildSession(s.id),
        title: "build the consolidated folder and its report again from the session's runs" }, "Rebuild") : null))));
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
async function rebuildSession(id) {
  try { await api("POST", `/api/racks/${enc(rack.name)}/sessions/${enc(id)}/rebuild`, {}); loadResults(); }
  catch (e) { alert(e.message); }
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
  // no file yet (a new rack): the empty form still gets its column headers; Preview + Save creates the file
  $("#invmsgs").replaceChildren(inv.exists ? msgs(inv.errors, inv.warnings)
    : h("div", { class: "warn" }, "! inventory file not found: fill in the form (or raw YAML), Preview, then Save to create it"));
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
  lastPreview = d.errors.length || !d.diff ? null : payload;
  const note = lastPreview ? h("div", { class: "warn unsaved" },
    "Preview only - NOT SAVED YET. Click ", h("b", {}, "Save (dated backup first)"), " to apply it to the inventory.") : null;
  $("#preview").replaceChildren(...[msgs(d.errors, d.warnings), note, pre].filter(Boolean));
  $("#inv-save").disabled = !lastPreview;
  $("#inv-save").classList.toggle("primary", !!lastPreview);
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
    $("#savemsg").textContent = d.saved ? (d.backup ? `saved; backup ${d.backup}` : "saved (new file)") : "no change";
    for (const n of $$("#preview .unsaved")) n.remove();
    $("#inv-save").classList.remove("primary");
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

loadRacks().catch(e => { if (e.status !== 401) document.body.prepend(h("div", { class: "err" }, e.message)); });
setInterval(() => { if ($("#tab-runs").classList.contains("active") && !es) loadRuns(); }, 5000);
