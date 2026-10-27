"use strict";

let S = null;
let lastChanges = [];
let prevVersionId = null;

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/* --------------------------------------------------------------------------
   Runtime mode. The same app.js runs in two places:
     - local server  (python app.py)  -> /api/* answers directly
     - static host   (GitHub Pages)   -> state comes from data/state.json and
                                          every write is queued as a GitHub
                                          Action job (see config.json)
   Detection: ask for api/state relatively. A server answers JSON; a static
   host 404s -> static mode.
   -------------------------------------------------------------------------- */
let STATIC = false;
let JOB_CFG = { repo: null };
let pendingJob = null;
let jobTimer = null;
const JOB_TTL = 20 * 60 * 1000;

const relPath = (p) => String(p).replace(/^\//, "");

async function fetchJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON body */ }
  if (!res.ok) throw new Error(data.detail || res.statusText || "Request failed");
  return data;
}
async function fetchMaybe(url) {
  try { return await fetchJSON(url); } catch (e) { return null; }
}

async function detectStatic() {
  try {
    const res = await fetch(relPath("api/state"), { cache: "no-store" });
    const ct = res.headers.get("content-type") || "";
    if (res.ok && ct.includes("application/json")) return false;
  } catch (e) { /* no server reachable */ }
  return true;
}

async function api(path, method = "GET", body) {
  if (method === "GET") {
    if (!STATIC) return fetchJSON(relPath(path));
    if (relPath(path).startsWith("api/state")) return fetchJSON("data/state.json");
    throw new Error("Read-only view: " + relPath(path) + " does not exist on the hosted site.");
  }
  if (STATIC) return queueJob(path, body);
  const res = await fetch(relPath(path), {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) throw new Error(data.detail || res.statusText || "Request failed");
  return data;
}

/* --------------------------- queued jobs (static) --------------------------- */
function stateFingerprint(s) {
  if (!s) return "-";
  const c = s.current;
  return [c ? c.id : "-", c ? c.created_at : "-", (s.versions || []).length].join("|");
}

function jobLabel(op, payload) {
  const p = payload || {};
  if (op === "repair") {
    const r = (S && S.resources || []).find(x => x.id === p.resource_id);
    const day = S && S.days && S.days[p.day] ? S.days[p.day] : "day " + p.day;
    return `repair · ${r ? r.name : p.resource_id} · ${day} → ${p.value}`;
  }
  if (op === "generate") return "generate 4-week schedule";
  if (op === "restore") return `restore version ${p.version_id}`;
  if (op === "resources") return "save resources";
  if (op === "settings") return "save settings";
  if (op === "startstate") return "save starting state";
  return op;
}

function queuedResult(op) {
  const base = { ok: true, queued: true, version: { id: "queued", label: "queued on GitHub" } };
  if (op === "generate") return { ...base, warnings: [], info: [], relaxations: 0 };
  if (op === "repair") return { ...base, changes: [], warnings: [], info: [] };
  return { ok: true, queued: true };
}

function queueJob(path, payload) {
  const op = relPath(path).replace(/^api\//, "").split("?")[0];
  const jid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  if (!JOB_CFG.repo) {
    renderJobBanner("error", "This copy is not linked to a GitHub repository " +
      "(static/config.json is missing <b>repo</b>), so edits cannot be queued.");
    return queuedResult(op);
  }
  const job = { v: 1, op, jid, payload: payload || {} };
  const label = jobLabel(op, payload);
  pendingJob = { jid, op, at: Date.now(), label, fp: stateFingerprint(S) };
  try { localStorage.setItem("ss_pending_job", JSON.stringify(pendingJob)); } catch (e) { /* private mode */ }
  renderJobBanner("wait");
  startJobPoll();
  const url = `https://github.com/${JOB_CFG.repo}/issues/new` +
    `?title=${encodeURIComponent("[job] " + label)}` +
    `&body=${encodeURIComponent("```json\n" + JSON.stringify(job) + "\n```")}`;
  const w = window.open(url, "_blank");
  if (!w) location.href = url;
  return queuedResult(op);
}

function clearPending() {
  pendingJob = null;
  try { localStorage.removeItem("ss_pending_job"); } catch (e) { /* ignore */ }
}
function stopJobPoll() {
  if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
}
function startJobPoll() {
  if (jobTimer) return;
  pollJob();
  jobTimer = setInterval(pollJob, 10000);
}
async function pollJob() {
  if (!pendingJob) { stopJobPoll(); return; }
  if (Date.now() - pendingJob.at > JOB_TTL) {
    stopJobPoll();
    renderJobBanner("timeout");
    return;
  }
  const res = await fetchMaybe("data/last_result.json?ts=" + Date.now());
  if (res && res.jid === pendingJob.jid) {
    stopJobPoll();
    if (res.ok) { clearPending(); renderJobBanner("hide"); await applyJobResult(res); }
    else { clearPending(); renderJobBanner("error", esc(res.detail || "The job failed.")); }
    return;
  }
  const st = await fetchMaybe("data/state.json?ts=" + Date.now());
  if (st && stateFingerprint(st) !== stateFingerprint(S)) {
    // someone else (or a local run) pushed a new state while we waited
    await refresh();
  }
}

async function applyJobResult(res) {
  if (res.op === "repair") {
    lastChanges = res.changes || [];
    prevVersionId = res.previous_version_id || null;
  } else if (res.op === "generate") {
    lastChanges = []; prevVersionId = null;
  }
  await refresh();
  if (res.op === "generate") $("#gen-status").innerHTML = genStatusHtml(res);
  const n = (res.changes || []).length;
  renderJobBanner("ok", (res.op === "generate" ? "Schedule generated" :
    n ? `Applied — ${n} cell${n === 1 ? "" : "s"} changed` : "Applied") +
    ` <span class="muted">(${esc(res.label || res.op)})</span>`);
  if ((res.warnings || []).length) {
    alert("Finished with warnings:\n" + res.warnings.map(w => "• " + w.message).join("\n"));
  }
}

function resumePendingJob() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("ss_pending_job") || "null"); } catch (e) { saved = null; }
  if (!saved) return;
  pendingJob = saved;
  fetchMaybe("data/last_result.json?ts=" + Date.now()).then(r => {
    if (r && r.jid === pendingJob.jid) { const s = pendingJob; clearPending(); applyJobResult(r); }
    else if (Date.now() - pendingJob.at > JOB_TTL) { stopJobPoll(); renderJobBanner("timeout"); }
    else { renderJobBanner("wait"); startJobPoll(); }
  });
}

function renderJobBanner(kind, text) {
  const el = $("#job-status");
  if (!el) return;
  if (kind === "hide") { el.className = "hidden"; el.innerHTML = ""; return; }
  el.className = "";
  const actions = `<div class="modal-actions" style="margin-top:.5rem">
    <button class="btn small job-dismiss">Dismiss</button></div>`;
  const watch = JOB_CFG.repo
    ? ` <a class="btn link" href="https://github.com/${JOB_CFG.repo}/actions" target="_blank" rel="noopener">Watch the run ↗</a>`
    : "";
  if (kind === "wait") {
    el.innerHTML = `<div class="banner info"><b>Queued on GitHub Actions</b> —
      ${esc(pendingJob ? pendingJob.label : "job")}. This page refreshes itself when the
      run finishes (about a minute).${watch}${actions}</div>`;
  } else if (kind === "timeout") {
    el.innerHTML = `<div class="banner warn"><b>Still waiting.</b> The job has not reported
      back after 20 minutes — open ${watch || "the Actions tab"} and check the run.${actions}</div>`;
  } else if (kind === "ok") {
    el.innerHTML = `<div class="banner ok"><b>Done.</b> ${text}${actions}</div>`;
  } else {
    el.innerHTML = `<div class="banner warn"><b>The queued job failed.</b>
      <div>${text}</div>${actions}</div>`;
  }
  const b = $(".job-dismiss", el);
  if (b) b.onclick = () => { stopJobPoll(); clearPending(); renderJobBanner("hide"); };
}

async function refresh() {
  S = await api("api/state");
  renderHeader();
  renderSchedule();
  renderDay();
  renderPeople();
  renderSetup();
  renderHistory();
}

function fmtDay(iso) {
  const dt = new Date(iso + "T00:00:00");
  return `${WD[(dt.getDay() + 6) % 7]} ${dt.getDate()} ${dt.toLocaleString("en", { month: "short" })}`;
}
function fmtShort(iso) {
  const dt = new Date(iso + "T00:00:00");
  return `${dt.getDate()} ${dt.toLocaleString("en", { month: "short" })}`;
}
function offPairLabel(start) {
  return WD[start % 7] + "+" + WD[(start + 1) % 7];
}
function openModal(html) {
  $("#modal-root").innerHTML = `<div class="modal-bg"><div class="modal">${html}</div></div>`;
}
function closeModal() { $("#modal-root").innerHTML = ""; }

function renderHeader() {
  const cur = S.current;
  const days = S.days || [];
  const span = days.length
    ? `${fmtShort(days[0])} – ${fmtShort(days[days.length - 1])} ${new Date(days[0] + "T00:00:00").getFullYear()}`
    : S.cycle_start;
  const ver = cur
    ? `<b>${esc(cur.label)}</b> · ${esc(String(cur.created_at).replace("T", " "))}`
    : "no schedule generated yet";
  $("#cycle-info").innerHTML = `Cycle ${esc(span)} · ${ver}`;
}

/* ------------------------------ schedule grid ------------------------------ */
function renderSchedule() {
  const box = $("#grid-container");
  const cur = S.current;
  renderDiffPanel();
  if (!cur) {
    box.innerHTML = `<div class="panel">No schedule yet. Go to <b>Setup</b> and press “Generate 4-week schedule”.</div>`;
    return;
  }
  const flags = cur.flags || [];
  const infoDays = new Set(flags.filter(f => f.code === "EXTRA_MORNING").map(f => f.day));
  const morning = cur.stats.morning;
  const changedSet = new Set(lastChanges.map(c => c.resource_id + ":" + c.day));
  const compMap = {};
  for (const st of cur.stats.resources) compMap[st.id] = new Set((st.comp_days || []).map(c => c.day));
  const todayISO = new Date().toLocaleDateString("sv");
  const meta = S.days.map((iso, i) => {
    const wd = (new Date(iso + "T00:00:00").getDay() + 6) % 7;
    return { iso, wd, wknd: wd >= 5, sep: i > 0 && i % 7 === 0, today: iso === todayISO };
  });
  const extraOf = m => `${m.wknd ? " wknd" : ""}${m.sep ? " sep" : ""}${m.today ? " today" : ""}`;

  let html = `<table class="grid"><thead>`;
  html += `<tr class="wkband"><th class="name"></th>`;
  for (let w = 0; w < 4; w++) html += `<th class="${w > 0 ? "sep" : ""}" colspan="7">Week ${w + 1}</th>`;
  html += `</tr><tr class="dayrow"><th class="name">Resource</th>`;
  for (let d = 0; d < 28; d++) {
    const m = meta[d];
    html += `<th class="day-head${extraOf(m)}" title="${esc(m.iso)}">` +
      `<span class="wd">${WD[m.wd]}</span><span class="n">${new Date(m.iso + "T00:00:00").getDate()}</span></th>`;
  }
  html += `</tr><tr class="mwrow"><th class="name">Morning weight</th>`;
  for (let d = 0; d < 28; d++) {
    const m = morning[d], dm = meta[d];
    const isW = m.type === "workday";
    const cls = isW ? (m.weight >= 7 ? "ok" : "bad") : "na";
    const pct = isW ? Math.min(100, Math.round((m.weight / 9) * 100)) : 0;
    const extra = infoDays.has(d) ? `<span class="tagx" title="4th morning person added">+1</span>` : "";
    html += `<td class="${cls}${extraOf(dm)}" title="${esc(m.date)} — morning availability weight ${isW ? m.weight : "n/a"} (floor 7)">` +
      `<span class="n">${isW ? m.weight : "–"}</span><span class="bar"><i style="width:${pct}%"></i></span>${extra}</td>`;
  }
  html += `</tr></thead><tbody>`;
  for (const r of S.resources) {
    if (!r.active) continue;
    const seq = cur.assign[r.id];
    html += `<tr><td class="name"><span class="nm">${esc(r.name)}</span><span class="sub">availability ${r.weight} · off ${offPairLabel(r.off_pair_start)}</span></td>`;
    for (let d = 0; d < 28; d++) {
      const v = seq ? seq[d] : "";
      const dm = meta[d];
      const isComp = v === "Off" && compMap[r.id] && compMap[r.id].has(d);
      const isAnchor = v === "Off" && (dm.wd === r.off_pair_start || dm.wd === (r.off_pair_start + 1) % 7);
      const cls = v === "Off" ? (isComp ? "off comp" : "off") + (isAnchor && !isComp ? " anchor" : "")
        : String(v).toLowerCase();
      const changed = changedSet.has(r.id + ":" + d) ? " changed" : "";
      const label = v === "Off" ? (isComp ? "C" : "") : v;
      const title = isComp ? `${dm.iso} — compensatory rest after night shift`
        : v === "Off" ? `${dm.iso} — ${isAnchor ? "rest day (planned pair)" : "extra rest"}`
          : `${dm.iso} — ${v}`;
      html += `<td class="cell ${cls}${extraOf(dm)}${changed}" data-rid="${r.id}" data-day="${d}" title="${esc(title)}">${label}</td>`;
    }
    html += `</tr>`;
  }
  html += `</tbody></table>`;
  box.innerHTML = html;
  $$("td.cell", box).forEach(td =>
    td.addEventListener("click", () => openCellEditor(td.dataset.rid, +td.dataset.day)));
}

function renderDiffPanel() {
  const el = $("#diff-panel");
  if (!lastChanges.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  const list = lastChanges.map(c =>
    `<div><b>${esc(c.name)}</b> · ${esc(c.date)} · ${esc(c.from)} → <b>${esc(c.to)}</b>` +
    (c.other ? ` <span class="muted">(another person — needed to keep coverage)</span>` : "") + `</div>`).join("");
  el.innerHTML = `<div class="banner info">Changes made by the last edit (${lastChanges.length}):
    <div class="change-list">${list}</div>
    <div style="margin-top:.5rem">
      <button class="btn small" id="undo-repair">Restore previous version</button>
      <button class="btn small" id="dismiss-diff">Dismiss</button>
    </div></div>`;
  $("#dismiss-diff").onclick = () => { lastChanges = []; renderSchedule(); };
  $("#undo-repair").onclick = async () => {
    if (!prevVersionId) return;
    await api("/api/restore", "POST", { version_id: prevVersionId });
    lastChanges = [];
    await refresh();
  };
}

function openCellEditor(rid, day) {
  const cur = S.current;
  const r = S.resources.find(x => x.id === rid);
  const old = cur.assign[rid][day];
  const tiles = [["Off", "rest"], ["S1", "08–17"], ["S2", "14–23"], ["S3", "23–08"], ["S4", "07–16"]];
  openModal(`
    <h3>Edit ${esc(r.name)} — ${fmtDay(S.days[day])}</h3>
    <p class="muted">Set to Off for sick leave, or pick another shift. Only the affected days are
    regenerated, with minimal changes to other people; the previous version stays in History.</p>
    <div class="radio-row">
      ${tiles.map(([v, t]) => `<label><input type="radio" name="cellv" value="${v}" ${v === old ? "checked" : ""}>${v} <em>${t}</em></label>`).join("")}
    </div>
    <div class="modal-actions">
      <button class="btn" id="m-cancel">Cancel</button>
      <button class="btn primary" id="m-apply">Apply &amp; repair</button>
    </div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-apply").onclick = async () => {
    const value = $('input[name="cellv"]:checked').value;
    if (value === old) return closeModal();
    const prevId = cur.id;
    try {
      const res = await api("/api/repair", "POST", { resource_id: rid, day, value });
      closeModal();
      lastChanges = res.changes || [];
      prevVersionId = prevId;
      await refresh();
      if ((res.warnings || []).length) {
        alert("Repair done with warnings:\n" + res.warnings.map(w => "• " + w.message).join("\n"));
      }
    } catch (e) { alert(e.message); }
  };
}

/* ------------------------------ day view ------------------------------ */
let selectedDay = 0;

function renderDay() {
  const sel = $("#day-select");
  sel.innerHTML = S.days.map((iso, i) => `<option value="${i}">${fmtDay(iso)}</option>`).join("");
  sel.value = String(Math.min(selectedDay, 27));
  sel.onchange = () => { selectedDay = +sel.value; renderDayBody(); };
  $("#day-prev").onclick = () => { selectedDay = Math.max(0, Math.min(27, selectedDay) - 1); renderDay(); };
  $("#day-next").onclick = () => { selectedDay = Math.max(0, Math.min(27, selectedDay) + 1); renderDay(); };
  renderDayBody();
}

function renderDayBody() {
  const host = $("#day-view");
  const cur = S.current;
  if (!cur) { host.innerHTML = `<div class="panel">No schedule yet.</div>`; return; }
  const d = Math.min(Math.max(selectedDay, 0), 27);
  const iso = S.days[d];
  const wd = (new Date(iso + "T00:00:00").getDay() + 6) % 7;
  const isWork = wd < 5 && !(S.settings.light_days || []).includes(iso);
  const byShift = { S1: [], S2: [], S3: [], S4: [], Off: [] };
  for (const r of S.resources) {
    if (!r.active) continue;
    byShift[cur.assign[r.id][d]].push(r);
  }
  const morningCrew = [...byShift.S1, ...byShift.S4];
  const morningWeight = morningCrew.reduce((s, r) => s + r.weight, 0);
  const flags = (cur.flags || []).filter(f => f.day === d);
  const compToday = new Set(cur.stats.resources
    .filter(st => (st.comp_days || []).some(c => c.day === d)).map(st => st.id));

  let html = `<div class="toolbar" style="margin-bottom:10px">
    <h2 style="margin:0">${fmtDay(iso)}</h2>
    <span class="muted">${isWork ? "Workday — morning floor 7, target 9" : "Light day (weekend/holiday) — one per role"}</span>
  </div>`;
  for (const f of flags) {
    if (f.severity === "info")
      html += `<div class="banner info">${esc(f.message)}</div>`;
    else
      html += `<div class="banner warn">${esc(f.message)}</div>`;
  }
  html += `<div class="day-cards">`;
  html += `<div class="card morning"><h3><span>Morning <span class="tm">08:00–17:00</span></span>
    <span class="pill ${isWork ? (morningWeight >= 7 ? "ok" : "bad") : "na"}">weight ${morningWeight}${isWork ? " / 9" : ""}</span></h3>`;
  for (const r of morningCrew)
    html += `<div class="who"><span>${esc(r.name)} <span class="muted">availability ${r.weight}</span></span>
      <span class="chip ${cur.assign[r.id][d].toLowerCase()}">${cur.assign[r.id][d]}</span></div>`;
  if (!morningCrew.length) html += `<div class="empty">nobody</div>`;
  html += `</div>`;
  html += `<div class="card evening"><h3><span>Evening <span class="tm">14:00–23:00</span></span></h3>`;
  for (const r of byShift.S2) html += `<div class="who"><span>${esc(r.name)}</span><span class="chip s2">S2</span></div>`;
  if (!byShift.S2.length) html += `<div class="empty">nobody</div>`;
  html += `</div>`;
  html += `<div class="card night"><h3><span>Night <span class="tm">23:00–08:00</span></span></h3>`;
  for (const r of byShift.S3) html += `<div class="who"><span>${esc(r.name)}</span><span class="chip s3">S3</span></div>`;
  if (!byShift.S3.length) html += `<div class="empty">nobody</div>`;
  html += `</div>`;
  html += `<div class="card rest"><h3><span>Off <span class="tm">${byShift.Off.length} of ${S.resources.filter(r => r.active).length}</span></span></h3>`;
  for (const r of byShift.Off)
    html += `<div class="who"><span>${esc(r.name)}</span>` +
      (compToday.has(r.id) ? `<span class="chip comp">C — after nights</span>`
        : `<span class="muted">rest</span>`) + `</div>`;
  if (!byShift.Off.length) html += `<div class="empty">nobody</div>`;
  html += `</div></div>`;
  host.innerHTML = html;
}

/* ------------------------------ people view ------------------------------ */
function renderPeople() {
  const host = $("#people-view");
  const cur = S.current;
  if (!cur) { host.innerHTML = `<div class="panel">No schedule yet.</div>`; return; }
  const ledger = S.ledger || {};
  const METRICS = [
    ["night", "Nights"], ["eve", "Evenings"], ["s4", "Support S4"],
    ["comp", "Comp days"], ["off", "Off days"],
  ];
  let html = `<div class="people-cards">`;
  for (const st of cur.stats.resources) {
    const seq = cur.assign[st.id] || [];
    const compSet = new Set((st.comp_days || []).map(c => c.day));
    const strip = seq.map((v, i) => {
      const isComp = v === "Off" && compSet.has(i);
      const cls = v === "Off" ? (isComp ? "comp" : "") : v.toLowerCase();
      const t = `${S.days[i]} — ${v}${isComp ? " (comp rest)" : v === "Off" ? " (rest)" : ""}`;
      return `<div class="c ${cls}" title="${esc(t)}"></div>`;
    }).join("");
    const debt = ledger[st.id] || {};
    let fair = "";
    for (const [key, label] of METRICS) {
      const d = debt[key + "_debt"] || 0;
      const over = d > 0.25, under = d < -0.25;
      const cls = over ? "over" : under ? "under" : "";
      const pct = Math.min(100, (Math.abs(d) / 5) * 100);
      const signed = (d > 0 ? "+" : "") + d.toFixed(1);
      const note = over ? `${label}: did more than the crew — expect fewer next cycle`
        : under ? `${label}: did fewer than the crew — expect more next cycle`
          : `${label}: balanced across cycles`;
      fair += `<div class="fr ${cls}" title="${esc(note)}">
        <span>${esc(label)}</span>
        <span class="bal"><i style="width:${pct}%"></i></span>
        <span class="val">${cls ? signed : "balanced"}</span>
      </div>`;
    }
    const extras = (st.extra_rest || []).filter(x => !compSet.has(x.day));
    const cyc = debt.cycles || 0;
    const cumTxt = cyc
      ? `cumulative · ${cyc} cycle${cyc === 1 ? "" : "s"}: nights ${debt.tot_night} · S2 ${debt.tot_eve} · S4 ${debt.tot_s4} · comps ${debt.tot_comp} · off ${debt.tot_off}`
      : "";
    html += `<div class="card">
      <h3><span>${esc(st.name)}</span> <span class="muted">availability ${st.weight} · off ${esc(st.off_pair)}</span></h3>
      <div class="strip">${strip}</div>
      <div class="counts">
        <span class="cnt s1"><span class="l">S1</span><span class="v">${st.counts.S1}</span></span>
        <span class="cnt s2"><span class="l">S2</span><span class="v">${st.counts.S2}</span></span>
        <span class="cnt s3"><span class="l">S3</span><span class="v">${st.counts.S3}</span></span>
        <span class="cnt s4"><span class="l">S4</span><span class="v">${st.counts.S4}</span></span>
        <span class="cnt off"><span class="l">Rest</span><span class="v">${st.off_days}</span></span>
      </div>
      <div class="kv"><span>Longest night run</span><span>${st.longest_night_run}</span></div>
      <div class="kv"><span>Comp rest after nights</span><span>${(st.comp_count || 0)
        ? `${st.comp_count} · ${(st.comp_days || []).map(x => fmtDay(x.date)).join(", ")}` : "none"}</span></div>
      <div class="fair">
        <div class="cap">Fairness across cycles — debt vs the crew</div>
        ${fair}
        ${cyc ? `<div class="cum">${esc(cumTxt)}</div>` : ""}
      </div>
      <div class="kv" style="margin-top:8px"><span>Rotation in cycle</span><span class="badge ${st.rotation_ok ? "ok" : "bad"}">${st.rotation_ok ? "S1+S2+S3 ✓" : "incomplete"}</span></div>
      ${extras.length ? `<div class="kv"><span>Extra rest day(s)</span><span>${extras.map(x => fmtDay(x.date)).join(", ")}</span></div>` : ""}
    </div>`;
  }
  html += `</div>`;
  host.innerHTML = html;
}

/* ------------------------------ setup ------------------------------ */
function resourceRow(r) {
  const offOpts = WD.map((_, i) =>
    `<option value="${i}" ${r && i === r.off_pair_start ? "selected" : ""}>${offPairLabel(i)}</option>`).join("");
  const w = r ? r.weight : 2;
  return `<tr data-rid="${r ? r.id : ""}">
    <td><input type="text" class="r-name" value="${r ? esc(r.name) : ""}" placeholder="Name"></td>
    <td><select class="r-weight">${[1, 2, 3].map(x => `<option value="${x}" ${x === w ? "selected" : ""}>${x}</option>`).join("")}</select></td>
    <td><select class="r-off">${offOpts}</select></td>
    <td><input type="checkbox" class="r-active" ${!r || r.active ? "checked" : ""}></td>
    <td><button class="btn small danger r-del">Delete</button></td>
  </tr>`;
}

function wireRow(tr) {
  const del = $(".r-del", tr);
  if (del) del.onclick = () => tr.remove();
}

function renderSetup() {
  $("#cycle-start").value = S.cycle_start;
  $("#priority").value = S.settings.priority || "floor_first";
  $("#light-days").value = (S.settings.light_days || []).join(", ");
  const act = S.resources.filter(r => r.active).length;
  $("#res-count").textContent = `— ${act} active / ${S.resources.length} total (7–9 supported)`;
  const host = $("#resources-editor");
  host.innerHTML = `<table class="simple"><thead><tr><th>Name</th><th>Availability 1–3</th>
    <th>Rest days (2 in a row)</th><th>Active</th><th></th></tr></thead>
    <tbody>${S.resources.map(resourceRow).join("")}</tbody></table>`;
  $$("#resources-editor tbody tr", host).forEach(wireRow);
  renderStartEditors();
}

function renderStartEditors() {
  const opts = ["Auto", "Off", "S1", "S2", "S3", "S4"];
  const build = (key) => {
    let html = `<table class="simple"><tbody>`;
    for (const r of S.resources) {
      const ov = (S[key].overrides || {})[r.id] || "Auto";
      const hint = key === "day0" ? (S.day0.effective[r.id] || "") : "";
      html += `<tr data-rid="${r.id}"><td>${esc(r.name)}</td><td><select class="ss-sel">` +
        opts.map(v => `<option value="${v}" ${v === ov ? "selected" : ""}>${v}${v === "Auto" && hint ? ` — (${hint})` : ""}</option>`).join("") +
        `</select></td></tr>`;
    }
    return html + `</tbody></table>`;
  };
  $("#day0-editor").innerHTML = build("day0");
  $("#day1-editor").innerHTML = build("day1");
}

async function saveResources() {
  const items = $$("#resources-editor tbody tr").map(tr => ({
    id: tr.dataset.rid || undefined,
    name: $(".r-name", tr).value,
    weight: +$(".r-weight", tr).value,
    off_pair_start: +$(".r-off", tr).value,
    active: $(".r-active", tr).checked,
  }));
  await api("/api/resources", "POST", { resources: items });
  await refresh();
}

function genStatusHtml(res) {
  const warns = (res.warnings || []).map(w => w.message);
  const infos = (res.info || []).map(w => w.message);
  let html = res.queued
    ? `<div class="banner info">Queued on GitHub Actions — this page refreshes itself when the run finishes.</div>`
    : `<div class="banner ok">Schedule generated: ${esc(res.version.label)}</div>`;
  if (warns.length) html += `<div class="banner warn"><b>Warnings (${warns.length}):</b>
    <ul>${warns.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>`;
  if (infos.length) html += `<div class="banner info"><b>Notes:</b>
    <ul>${infos.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>`;
  return html;
}

function wireSetup() {
  $("#save-settings").onclick = async () => {
    const light = $("#light-days").value.split(",").map(s => s.trim()).filter(Boolean);
    try {
      await api("/api/settings", "POST",
        { cycle_start: $("#cycle-start").value,
          priority: $("#priority").value, light_days: light });
      await refresh();
    } catch (e) { alert(e.message); }
  };
  $("#add-resource").onclick = () => {
    const tbody = $("#resources-editor tbody");
    const wrap = document.createElement("template");
    wrap.innerHTML = resourceRow(null);
    const tr = wrap.content.firstElementChild;
    tbody.appendChild(tr);
    wireRow(tr);
  };
  $("#save-resources").onclick = async () => {
    try { await saveResources(); } catch (e) { alert(e.message); }
  };
  $("#save-startstate").onclick = async () => {
    const collect = (sel) => {
      const out = {};
      $$(`${sel} tbody tr`).forEach(tr => {
        const v = $(".ss-sel", tr).value;
        out[tr.dataset.rid] = v === "Auto" ? null : v;
      });
      return out;
    };
    try {
      await api("/api/startstate", "POST",
        { day0: collect("#day0-editor"), day1: collect("#day1-editor") });
      await refresh();
    } catch (e) { alert(e.message); }
  };
  $("#generate").onclick = async () => {
    const st = $("#gen-status");
    st.innerHTML = "Generating… this can take a few seconds.";
    try {
      const res = await api("/api/generate", "POST", {});
      lastChanges = []; prevVersionId = null;
      await refresh();
      st.innerHTML = genStatusHtml(res);
    } catch (e) {
      st.innerHTML = `<div class="banner warn">${esc(e.message)}</div>`;
    }
  };
}

/* ------------------------------ history ------------------------------ */
function renderHistory() {
  const host = $("#versions-view");
  const vers = S.versions || [];
  if (!vers.length) { host.innerHTML = `<div class="panel">No versions yet.</div>`; return; }
  let html = `<div class="panel"><h2>Version history</h2><table class="simple versions">
    <thead><tr><th>Created</th><th>Label</th><th>Cycle</th><th>Warnings</th>
    <th>Relaxed rules</th><th>Night spread</th><th>Actions</th></tr></thead><tbody>`;
  vers.forEach((v, i) => {
    const isCur = S.current && v.id === S.current.id;
    const prev = vers[i + 1];
    const wcls = (v.warnings || 0) > 0 ? "count hot" : "count";
    const rcls = (v.relaxations || 0) > 0 ? "count hot" : "count";
    html += `<tr${isCur ? ' class="current"' : ""}>
      <td>${esc(String(v.created_at).replace("T", " "))}</td>
      <td>${esc(v.label)}${isCur ? " · <b>current</b>" : ""}</td>
      <td>${esc(v.cycle_id)}</td>
      <td><span class="${wcls}">${v.warnings || 0}</span></td>
      <td><span class="${rcls}">${v.relaxations || 0}</span></td>
      <td>${v.nights_spread ?? "–"}</td>
      <td class="ver-actions">
        ${isCur ? "" : `<button class="btn small v-restore" data-id="${v.id}">Restore</button>`}
        ${!STATIC && prev ? `<button class="btn small v-diff" data-from="${prev.id}" data-to="${v.id}">Changes</button>` : ""}
      </td></tr>`;
  });
  host.innerHTML = html + `</tbody></table></div>`;
  $$(".v-restore", host).forEach(b => b.onclick = async () => {
    try { await api("/api/restore", "POST", { version_id: b.dataset.id }); await refresh(); }
    catch (e) { alert(e.message); }
  });
  $$(".v-diff", host).forEach(b => b.onclick = async () => {
    try {
      const res = await api(`/api/diff?from_id=${b.dataset.from}&to_id=${b.dataset.to}`);
      const list = res.changes.map(c =>
        `<div><b>${esc(c.name)}</b> · ${esc(c.date)} · ${esc(c.from)} → <b>${esc(c.to)}</b></div>`).join("")
        || "<div>No differences.</div>";
      openModal(`<h3>Changes (${res.changes.length})</h3><div class="change-list">${list}</div>
        <div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>`);
    } catch (e) { alert(e.message); }
  });
}

/* ------------------------------ init ------------------------------ */
$$("#tabs button").forEach(btn => btn.onclick = () => {
  $$("#tabs button").forEach(b => b.classList.toggle("active", b === btn));
  ["schedule", "day", "people", "setup", "history"].forEach(t =>
    $("#tab-" + t).classList.toggle("hidden", t !== btn.dataset.tab));
});

async function boot() {
  STATIC = await detectStatic();
  if (STATIC) {
    const cfg = await fetchMaybe("config.json");
    JOB_CFG = (cfg && cfg.repo) ? { repo: cfg.repo } : { repo: null };
    $("#export-csv").href = "data/schedule.csv";
    resumePendingJob();
  }
  try { await refresh(); }
  catch (e) { alert("Failed to load: " + e.message); }
}

wireSetup();
boot();
