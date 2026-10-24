"use strict";

const $ = (s) => document.querySelector(s);
const ROUTES = ["", "IV", "IM", "IO", "SC", "SL", "PO", "IN", "INH", "ET", "PR", "TOP"];
const VITALS = ["HR", "BP", "SpO2", "RR", "GCS", "Temp", "BGL", "Pain"];
const SAMPLES = {
  "English - chest pain": "62 year old male, crushing chest pain since 30 minutes. Gave aspirin 300 mg chewed at 10:42. Nitroglycerin 0.4 mg sublingual at 10:45, pain reduced from 8 to 5. 18 gauge IV in the left AC. 12 lead ECG done, ST elevation in inferior leads. BP 140/90, heart rate 96, SpO2 95 percent.",
  "English - opioid overdose": "Unresponsive female, about 25, pinpoint pupils, resp rate 6. Bagged with BVM. Naloxone 2 mg intranasal at 22:10, repeated 2 mg after 3 minutes. Patient woke up, GCS now 14. SpO2 went from 82 to 97 percent.",
  "Hinglish - anaphylaxis": "Female patient, umar 30 saal, bee sting ke baad saans phool rahi thi. 5 minute pehle adrenaline 0.5 mg IM right thigh mein diya. Left hand mein 20 gauge cannula lagaya aur NS 500 ml start kiya. Avil 22.75 mg IV diya. BP 90/60, SpO2 92.",
  "Hinglish - asthma": "Bachcha 10 saal ka, asthma attack. Asthalin 2.5 mg nebulization diya 15:05 pe, phir ipravent bhi neb mein mila ke diya. Oxygen 6 litre mask se start kiya. SpO2 89 se 95 ho gaya.",
  "Hindi - hypoglycaemia": "मरीज़ 70 साल के पुरुष, बेहोश मिले। शुगर 38 आई। दाहिने हाथ में कैनुला लगाया और 25 ग्राम डेक्सट्रोज़ नस में दिया 09:15 पर। 10 मिनट बाद होश आ गया, GCS 15।",
  "Trauma - fall": "Male about 40 fell from 2nd floor. C-collar applied, spine board immobilization. Left femur fracture, splinted. Fentanyl 50 mcg IV at 18:20. TXA 1 g IV over 10 minutes. Ondansetron 4 mg IV for nausea. BP 100/70, HR 118.",
};

let draft = null;       // machine output, frozen
let rec = null;         // record being edited
let modelInfo = {};
let asrMeta = null;     // {audio_sha256, language, ...}
let noteTime = null;    // pinned note time (ISO): set by hand or by a recording; null = live current time
let noteTimeSource = null;  // "manual" | "recording" | null (live)
let amendFrom = null;   // {id, label, incident} while correcting a signed version

// ---------- utils ----------
const clone = (o) => JSON.parse(JSON.stringify(o));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const hm = (iso) => (iso && /T(\d\d:\d\d)/.exec(iso)?.[1]) || "";
function setHM(iso, val) {
  if (!val) return iso;
  const base = iso || rec.reference_time;
  return base.replace(/T\d\d:\d\d(:\d\d)?/, `T${val}:00`);
}
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
function localIso(d) {
  const p = (n) => String(Math.abs(n)).padStart(2, "0"), off = -d.getTimezoneOffset();
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` +
    `${off >= 0 ? "+" : "-"}${p(Math.trunc(off / 60))}:${p(off % 60)}`;
}
// Unstated and relative times ("10 min pehle") resolve against the note time. By default it is the current
// time, kept up to date in the field; a recording pins its start time; a time set by hand wins over both.
function setNoteTime(iso, source) {
  noteTime = iso; noteTimeSource = source;
  $("#note-time").value = hm(iso);
  $("#note-time").classList.remove("live");
  $("#note-time").title = source === "manual" ? "Set by hand: clear the field to go back to the current time"
    : "Time the recording started: set a time by hand to override it";
}
function liveNoteTime() {  // back to following the clock
  noteTime = null; noteTimeSource = null;
  tickNoteTime();
}
function tickNoteTime() {
  const f = $("#note-time");
  if (noteTime || document.activeElement === f) return;
  f.value = new Date().toTimeString().slice(0, 5);
  f.classList.add("live");
  f.title = "Current time (live): set a time by hand to pin it";
}
const currentNoteTime = () => noteTime || localIso(new Date());
function newIncident() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  $("#incident").value = `INC-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

// ---------- status ----------
async function refreshStatus() {
  try {
    const h = await api("/api/health");
    const set = (id, ok, text, cls) => { const el = $(id); el.textContent = text; el.className = "pill " + (cls || (ok ? "ok" : "bad")); };
    const l = h.llm;
    if (!l.running) set("#st-llm", false, "LLM offline · Ollama not running");
    else if (!l.installed) set("#st-llm", false, `LLM ${l.model} not installed`);
    else if (!l.loaded) set("#st-llm", true, `LLM ${l.model} · loading…`, "warn");
    else set("#st-llm", true, `LLM ${l.model} · ready`);
    set("#st-asr", true, h.asr.loaded ? `ASR ${h.asr.asr_model} · ${h.asr.asr_device}` : `ASR ${h.asr.asr_model} (loads on first use)`, h.asr.loaded ? "ok" : "warn");
    set("#st-chain", h.audit.ok, h.audit.ok ? `Chain OK · ${h.audit.records} records · ${h.audit.versions} versions` : "Chain BROKEN");
    if (!h.llm.ok && h.llm.error) $("#st-llm").title = h.llm.error;
    return l.loaded;
  } catch { $("#st-llm").textContent = "server offline"; $("#st-llm").className = "pill bad"; return false; }
}

// ---------- recording ----------
let mediaRec = null, chunks = [], t0 = 0, timer = null;
$("#rec").onclick = async () => {
  if (mediaRec && mediaRec.state === "recording") { mediaRec.stop(); return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { alert("Microphone not available: " + e.message); return; }
  chunks = [];
  // the first recording of a note fixes its time; later recordings append to the same note
  // the first recording of a note pins its time, unless a time was set by hand
  if (noteTimeSource !== "manual" && (!noteTime || !$("#note").value.trim())) setNoteTime(localIso(new Date()), "recording");
  mediaRec = new MediaRecorder(stream);
  mediaRec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  mediaRec.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(timer);
    $("#rec").classList.remove("on"); $("#rec").classList.add("busy");
    $("#rec-label").textContent = "Transcribing…";
    const fd = new FormData();
    fd.append("audio", new Blob(chunks, { type: mediaRec.mimeType }), "note.webm");
    fd.append("language", $("#lang").value);
    try {
      const r = await api("/api/transcribe", { method: "POST", body: fd });
      asrMeta = r;
      const note = $("#note");
      note.value = (note.value.trim() ? note.value.trim() + " " : "") + r.text;
      $("#asr-info").textContent = `${r.language} (${Math.round(r.language_probability * 100)}%) · ${r.duration_s}s audio · ${r.asr_latency_s}s on ${r.asr_device}`;
      refreshStatus();
    } catch (e) { alert(e.message); }
    $("#rec").classList.remove("busy");
    $("#rec-label").textContent = "Record";
  };
  mediaRec.start();
  t0 = Date.now();
  $("#rec").classList.add("on"); $("#rec-label").textContent = "Stop";
  timer = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    $("#rec-timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }, 250);
};

// ---------- extraction ----------
$("#extract").onclick = async () => {
  const text = $("#note").value.trim();
  if (!text) return;
  const btn = $("#extract");
  btn.disabled = true; btn.textContent = "Extracting…";
  $("#extract-info").textContent = "";
  try {
    const ref = currentNoteTime();
    const r = await api("/api/extract", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, reference_time: ref }) });
    renderHarness(r.model_info, r.harness);
    if (amendFrom) {
      appendAddendum(text, r, ref);
    } else {
      draft = r.record; rec = clone(r.record);
      tagItems(rec); resetChanges();
      modelInfo = { ...r.model_info, ...(asrMeta ? { asr_model: asrMeta.asr_model, asr_device: asrMeta.asr_device } : {}) };
    }
    $("#signed").hidden = true;
    renderReview();
  } catch (e) { $("#extract-info").textContent = "⚠ " + e.message; }
  btn.disabled = false; btn.textContent = extractLabel();
};
const extractLabel = () => (amendFrom ? `Extract & Add to ${amendFrom.incident}` : "Extract Record");

// While amending, a dictated note is an addendum: its items are appended to the record being amended
// and the note itself is signed with the amendment, so every added item stays grounded in its words.
function appendAddendum(text, r, ref) {
  if (amendFrom.addenda.some((a) => a.transcript === text)
      && !confirm("This note was already added to the record as an addendum. Add its items again?")) return;
  const add = r.record;
  tagItems(add);
  const uids = [];
  for (const k of ["medications", "procedures", "vitals"]) {
    for (const item of add[k]) { rec[k].push({ ...item, warnings: ["added by addendum", ...(item.warnings || [])] }); uids.push(item._uid); }
  }
  const filled = {};  // record fields the addendum filled in, so undo can restore them
  for (const k of ["age", "weight_kg"]) {
    if (rec.patient[k] == null && add.patient[k] != null) { filled[k] = add.patient[k]; rec.patient[k] = add.patient[k]; }
  }
  if (rec.patient.sex === "unknown" && add.patient.sex !== "unknown") { filled.sex = add.patient.sex; rec.patient.sex = add.patient.sex; }
  if (!rec.chief_complaint && add.chief_complaint) { filled.chief_complaint = add.chief_complaint; rec.chief_complaint = add.chief_complaint; }
  const addendum = { transcript: text, recorded_at: ref, audio_sha256: asrMeta?.audio_sha256 || null, model_info: r.model_info };
  amendFrom.addenda.push(addendum);
  const n = uids.length, num = amendFrom.addenda.length;
  logChange({ type: "addendum", uids, filled, addendum,
    label: `Addendum ${num}: added ${n} item${n === 1 ? "" : "s"} (“${text.length > 50 ? text.slice(0, 50) + "…" : text}”)` });
  $("#extract-info").insertAdjacentHTML("afterbegin",
    `<div class="small">Added ${n} item${n === 1 ? "" : "s"} to ${esc(amendFrom.incident)} from addendum ${num}. Review, give a reason, then sign.</div>`);
}

function renderHarness(info, h) {
  const mode = {
    "llm": "",
    "llm-retry": `<span class="chip">model output repaired on retry</span>`,
    "rules-fallback": `<span class="chip bad">LLM unavailable: rule-based draft, review every field</span>`,
  }[h.mode];
  const icon = { pass: "✓", fixed: "↻", flag: "!" };
  $("#extract-info").innerHTML = `
    <div>${esc(info.llm_model)}${info.llm_latency_s ? ` · ${info.llm_latency_s}s` : ""} ${mode}</div>
    <details class="harness"><summary>Harness · ${h.checks.length} checks · ${h.fixed} fixed · ${h.flagged} flagged</summary>
      <ul>${h.checks.map((c) => `<li class="${c.status}"><b>${icon[c.status]} ${esc(c.name)}</b> — ${esc(c.detail)}</li>`).join("")}</ul>
      ${h.hints.length ? `<div class="muted">Told the model: ${h.hints.map(esc).join(" · ")}</div>` : ""}
    </details>`;
}

// ---------- review ----------
function chips(item) {
  const serious = /hallucination|exceeds|NOT given|missed by the model|not stated in the note|history or allergy/;
  return (item.warnings || []).map((w) => `<span class="chip ${serious.test(w) ? "bad" : ""}">${esc(w)}</span>`).join("");
}
function metaRow(item, cols, extra = "") {
  const ev = item.evidence ? `“${esc(item.evidence)}”` : "";
  return `<tr class="meta"><td></td><td colspan="${cols}"><div class="evidence">${ev} ${extra} ${chips(item)}</div></td></tr>`;
}
const inp = (list, i, key, val, cls = "", type = "text") =>
  `<input class="${cls}" type="${type}" data-list="${list}" data-i="${i}" data-k="${key}" value="${esc(val)}">`;
const sel = (list, i, key, val, opts, cls = "") =>
  `<select class="${cls}" data-list="${list}" data-i="${i}" data-k="${key}">${opts.map((o) => `<option ${o === (val || "") ? "selected" : ""}>${o}</option>`).join("")}</select>`;
const del = (list, i) => `<button class="x" title="Remove" data-del="${list}" data-i="${i}">✕</button>`;

function renderReview() {
  $("#review-empty").hidden = true;
  $("#review-body").hidden = false;
  $("#amend-bar").hidden = !amendFrom;
  if (amendFrom) $("#amend-what").textContent = `${amendFrom.incident} · ${amendFrom.label}`;
  $("#sign").textContent = amendFrom ? "Sign Amendment" : "Confirm & Sign";
  $("#extract").textContent = extractLabel();
  $("#p-age").value = rec.patient.age ?? "";
  $("#p-sex").value = rec.patient.sex;
  $("#p-wt").value = rec.patient.weight_kg ?? "";
  $("#p-cc").value = rec.chief_complaint ?? "";
  $("#cc-meta").innerHTML = (rec.chief_complaint_warnings || []).map((w) => `<span class="chip">${esc(w)}</span>`).join("");

  $("#t-meds tbody").innerHTML = rec.medications.map((m, i) => `
    <tr class="item">
      <td>${inp("medications", i, "time", hm(m.time), "w-time", "time")}</td>
      <td>${inp("medications", i, "drug", m.drug)}</td>
      <td>${inp("medications", i, "dose_value", m.dose_value ?? "", "w-dose", "number")}</td>
      <td>${inp("medications", i, "dose_unit", m.dose_unit ?? "", "w-unit")}</td>
      <td>${sel("medications", i, "route", m.route, ROUTES, "w-route")}</td>
      <td>${inp("medications", i, "site", m.site ?? "")}</td>
      <td>${del("medications", i)}</td>
    </tr>${metaRow(m, 6, m.rxnorm ? `<span class="code">RxNorm ${m.rxnorm}</span>` : "")}`).join("");

  $("#t-procs tbody").innerHTML = rec.procedures.map((p, i) => `
    <tr class="item">
      <td>${inp("procedures", i, "time", hm(p.time), "w-time", "time")}</td>
      <td>${inp("procedures", i, "name", p.name)}</td>
      <td>${inp("procedures", i, "details", p.details ?? "")}</td>
      <td>${inp("procedures", i, "site", p.site ?? "")}</td>
      <td>${inp("procedures", i, "outcome", p.outcome ?? "")}</td>
      <td>${del("procedures", i)}</td>
    </tr>${metaRow(p, 5, p.code ? `<span class="code">${p.code}</span>` : "")}`).join("");

  $("#t-vitals tbody").innerHTML = rec.vitals.map((v, i) => `
    <tr class="item">
      <td>${inp("vitals", i, "time", hm(v.time), "w-time", "time")}</td>
      <td>${sel("vitals", i, "type", v.type, VITALS, "w-route")}</td>
      <td>${inp("vitals", i, "value", v.value)}</td>
      <td>${inp("vitals", i, "unit", v.unit ?? "", "w-unit")}</td>
      <td>${del("vitals", i)}</td>
    </tr>${metaRow(v, 4)}`).join("");

  const n = [...rec.medications, ...rec.procedures, ...rec.vitals].reduce((a, x) => a + (x.warnings?.length || 0), 0)
    + (rec.chief_complaint_warnings?.length || 0);
  $("#warn-count").textContent = n ? `· ${n} item${n > 1 ? "s" : ""} need attention` : "· no warnings";
  $("#confirm").checked = false;
  $("#sign").disabled = true;
  renderChanges();
}

// ---------- change log: every edit in the review can be undone on its own ----------
// Items carry a client-only `_uid` so a change still finds its item after rows are added or removed.
// The server ignores unknown fields, so `_uid` never reaches a signed record.
let changeLog = [], uidSeq = 0, changeSeq = 0;
const LISTS = ["medications", "procedures", "vitals"];
function tagItems(r) { for (const k of LISTS) for (const it of r[k]) it._uid ??= `u${++uidSeq}`; }
function findItem(uid) {
  for (const k of LISTS) { const i = rec[k].findIndex((it) => it._uid === uid); if (i >= 0) return { list: k, i, item: rec[k][i] }; }
  return null;
}
const itemName = (list, it) => (list === "medications" ? it.drug : list === "procedures" ? it.name : it.type) || `new ${list.slice(0, -1)}`;
const showVal = (v) => (v == null || v === "" ? "—" : typeof v === "string" && /T\d\d:\d\d/.test(v) ? hm(v) : String(v));
function logChange(entry) {
  changeLog.push({ ...entry, id: ++changeSeq, at: new Date().toTimeString().slice(0, 5) });
  renderChanges();
}
function resetChanges() { changeLog = []; renderChanges(); }
function renderChanges() {
  const box = $("#changes");
  box.hidden = !changeLog.length;
  $("#change-count").textContent = changeLog.length;
  $("#change-list").innerHTML = [...changeLog].reverse().map((c) => `
    <li><span class="muted">${c.at}</span> ${esc(c.label)} <button class="link" data-undo="${c.id}">Undo</button></li>`).join("");
}

function undoChange(id) {
  const c = changeLog.find((x) => x.id === id);
  if (!c) return;
  if (c.type === "edit") {
    const target = c.uid ? findItem(c.uid)?.item : c.target === "patient" ? rec.patient : rec;
    if (!target) { alert("That item is no longer in the record, so this change can't be undone."); return; }
    Object.assign(target, c.restore);
  } else if (c.type === "remove") {
    rec[c.list].splice(Math.min(c.index, rec[c.list].length), 0, c.item);
  } else if (c.type === "add") {
    const f = findItem(c.uid);
    if (f && !confirm(`Undo will remove the row “${itemName(f.list, f.item)}”. Continue?`)) return;
    if (f) rec[f.list].splice(f.i, 1);
  } else if (c.type === "addendum") {
    const present = c.uids.map(findItem).filter(Boolean);
    if (!confirm(`Undo this addendum? It removes its ${present.length} item(s) from the record.`)) return;
    for (const uid of c.uids) { const f = findItem(uid); if (f) rec[f.list].splice(f.i, 1); }
    amendFrom.addenda = amendFrom.addenda.filter((a) => a !== c.addendum);
    for (const [k, v] of Object.entries(c.filled)) {  // put back fields the addendum filled, unless edited since
      if (k === "chief_complaint") { if (rec.chief_complaint === v) rec.chief_complaint = null; }
      else if (rec.patient[k] === v) rec.patient[k] = k === "sex" ? "unknown" : null;
    }
  }
  changeLog = changeLog.filter((x) => x !== c);
  renderReview();
}
$("#changes").addEventListener("click", (e) => {
  if (e.target.dataset.undo) undoChange(+e.target.dataset.undo);
  if (e.target.id === "undo-last" && changeLog.length) undoChange(changeLog[changeLog.length - 1].id);
});

$("#review-body").addEventListener("change", (e) => {
  const t = e.target, list = t.dataset.list;
  if (!list) return;
  const item = rec[list][+t.dataset.i], k = t.dataset.k;
  const restore = { [k]: item[k] };
  if (k === "drug") restore.rxnorm = item.rxnorm;
  if (k === "name") restore.code = item.code;
  if (k === "time") item.time = setHM(item.time, t.value);
  else if (k === "dose_value") item.dose_value = t.value === "" ? null : parseFloat(t.value);
  else item[k] = t.value === "" ? null : t.value;
  if (k === "drug") item.rxnorm = null; // code no longer matches the edited name
  if (k === "name") item.code = null;
  if (showVal(restore[k]) === showVal(item[k])) return;
  logChange({ type: "edit", uid: item._uid, restore,
    label: `${itemName(list, item)} · ${k.replace("_", " ")}: ${showVal(restore[k])} → ${showVal(item[k])}` });
});
$("#review-body").addEventListener("click", (e) => {
  const t = e.target;
  if (t.dataset.del) {
    const list = t.dataset.del, index = +t.dataset.i, item = rec[list][index];
    if (!confirm(`Remove “${itemName(list, item)}” from the record? You can undo this from the change list.`)) return;
    rec[list].splice(index, 1);
    logChange({ type: "remove", list, index, item, label: `Removed ${itemName(list, item)}` });
    renderReview();
  }
  if (t.dataset.add) {
    const now = new Date().toTimeString().slice(0, 5);
    const time = setHM(rec.reference_time, now);
    const blank = {
      medications: { drug: "", dose_value: null, dose_unit: "mg", route: null, site: null, time, warnings: ["added manually"], evidence_found: false },
      procedures: { name: "", details: null, site: null, outcome: null, time, warnings: ["added manually"], evidence_found: false },
      vitals: { type: "HR", value: "", unit: null, time, warnings: ["added manually"], evidence_found: false },
    }[t.dataset.add];
    blank._uid = `u${++uidSeq}`;
    rec[t.dataset.add].push(blank);
    logChange({ type: "add", uid: blank._uid, label: `Added a ${t.dataset.add.slice(0, -1)} row` });
    renderReview();
  }
});
function patientField(id, target, key, parse) {
  $(id).onchange = (e) => {
    const obj = target === "patient" ? rec.patient : rec, old = obj[key], val = parse(e.target.value);
    obj[key] = val;
    if (showVal(old) !== showVal(val)) {
      logChange({ type: "edit", target, restore: { [key]: old }, label: `${key.replace("_", " ")}: ${showVal(old)} → ${showVal(val)}` });
    }
  };
}
patientField("#p-age", "patient", "age", (v) => v || null);
patientField("#p-sex", "patient", "sex", (v) => v);
patientField("#p-wt", "patient", "weight_kg", (v) => (v ? parseFloat(v) : null));
patientField("#p-cc", "record", "chief_complaint", (v) => v || null);
$("#confirm").onchange = (e) => ($("#sign").disabled = !e.target.checked);

// ---------- sign ----------
$("#sign").onclick = async () => {
  if (rec.medications.some((m) => !m.drug) || rec.procedures.some((p) => !p.name)) { alert("Fill in or remove empty rows first."); return; }
  if (amendFrom) return signAmendment();
  $("#sign").disabled = true;
  try {
    const r = await api("/api/records", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        incident_id: $("#incident").value, responder_id: $("#responder").value,
        transcript: $("#note").value.trim(), language: asrMeta?.language || $("#lang").value,
        audio_sha256: asrMeta?.audio_sha256 || null, draft, final: rec, model_info: modelInfo,
      }),
    });
    $("#signed").hidden = false;
    $("#signed").innerHTML = `✓ Signed as record #${r.seq} · ${r.edits.length} human edit(s)<br>
      <span class="hash">${r.hash}</span><br>
      <a href="/records/${r.id}/report" target="_blank">Open report</a>
      <a href="/api/records/${r.id}/fhir" target="_blank">FHIR-lite JSON</a>`;
    resetChanges();
    newIncident();
    loadHistory(); refreshStatus();
  } catch (e) { alert(e.message); $("#sign").disabled = false; }
};

// ---------- amend a signed version ----------
async function startAmend(versionId) {
  if (rec && $("#signed").hidden && !confirm("Discard the record you are reviewing and amend a signed one instead?")) return;
  const v = await api(`/api/records/${versionId}`);
  amendFrom = { id: v.id, label: v.version_label, incident: v.incident_id, addenda: [] };
  draft = clone(v.final); rec = clone(v.final); modelInfo = v.model_info || {};
  tagItems(rec); resetChanges();
  $("#amend-reason").value = "";
  $("#signed").hidden = true;
  renderReview();
  $("#review").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#amend-reason").focus();
}
function endAmend() {
  amendFrom = null;
  $("#amend-bar").hidden = true;
  $("#sign").textContent = "Confirm & Sign";
  $("#extract").textContent = extractLabel();
}
$("#amend-cancel").onclick = () => {
  if (changeLog.length && !confirm(`Discard the amendment and its ${changeLog.length} change(s)?`)) return;
  endAmend(); draft = null; rec = null; resetChanges();
  $("#review-body").hidden = true; $("#review-empty").hidden = false; $("#warn-count").textContent = "";
};
async function signAmendment() {
  const reason = $("#amend-reason").value.trim();
  if (!reason) { alert("Enter a reason for the amendment."); $("#amend-reason").focus(); return; }
  $("#sign").disabled = true;
  try {
    const r = await api(`/api/records/${amendFrom.id}/amend`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ responder_id: $("#responder").value, reason, final: rec, addenda: amendFrom.addenda }),
    });
    const label = r.history.versions.find((v) => v.id === r.id).label;
    $("#signed").hidden = false;
    $("#signed").innerHTML = `✓ Signed ${esc(amendFrom.incident)} ${label}, amended from ${amendFrom.label} · ${r.edits.length} field(s) changed<br>
      <span class="hash">${r.hash}</span><br>
      <span class="small">parent ${amendFrom.label}: <span class="hash">${r.parent_hash.slice(0, 24)}…</span></span><br>
      <a href="/records/${r.id}/report" target="_blank">Open report</a>
      <a href="#" data-open-hist="${r.id}">View history</a>`;
    endAmend(); resetChanges();
    loadHistory(); refreshStatus();
  } catch (e) { alert(e.message); $("#sign").disabled = false; }
}
$("#signed").addEventListener("click", (e) => {
  const id = e.target.dataset.openHist;
  if (id) { e.preventDefault(); openHistory(id); }
});

// ---------- record list ----------
async function loadHistory() {
  const rows = await api("/api/records");
  $("#t-history tbody").innerHTML = rows.map((r) => `
    <tr><td>${r.seq}</td><td>${new Date(r.updated_at).toLocaleString()}</td><td>${esc(r.incident_id)}</td>
    <td>${esc(r.responder_id)}</td><td>${r.meds}</td><td>${r.procs}</td>
    <td>${r.versions}${r.branches > 1 ? ` <span class="badge branch">${r.branches} branches</span>` : ""}</td>
    <td class="hash">${r.hash.slice(0, 16)}…</td>
    <td class="actions"><button class="link" data-hist="${r.id}">History</button> · <button class="link" data-amend="${r.id}">Amend</button> ·
      <a href="/records/${r.id}/report" target="_blank">report</a> · <a href="/api/records/${r.id}/fhir" target="_blank">json</a></td></tr>`).join("")
    || `<tr><td colspan="9" class="muted">No signed records yet.</td></tr>`;
}
$("#t-history").addEventListener("click", (e) => {
  const t = e.target;
  if (t.dataset.hist) openHistory(t.dataset.hist);
  if (t.dataset.amend) startAmend(t.dataset.amend);
});

// ---------- version graph ----------
let hist = null, selected = null;
const fmtTime = (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
function diffTable(changes, edits = []) {  // rows from the server's describe_edits: {label, before, after, change}
  // a server older than this page sends only raw edits: show them plainly rather than failing
  const show = (v) => {
    if (v == null || v === "") return "—";
    if (typeof v === "object") {  // a whole item added or removed
      if (v.drug) return `${v.drug}${v.dose_value != null ? ` ${v.dose_value} ${v.dose_unit || ""}` : ""} ${v.route || ""}`.trim();
      return v.name || `${v.type || ""} ${v.value || ""} ${v.unit || ""}`.trim() || JSON.stringify(v);
    }
    return typeof v === "string" && /T\d\d:\d\d/.test(v) ? hm(v) : String(v);
  };
  const hidden = /\.(drug_raw|name_raw|time_raw|evidence|evidence_found|warnings|rxnorm|code)$|^(reference_time|chief_complaint_warnings)$/;
  changes ??= edits.filter((e) => !hidden.test(e.field)).map((e) => ({
    label: e.field, before: show(e.machine), after: show(e.human),
    change: e.machine == null && typeof e.human === "object" ? "added" : e.human == null && typeof e.machine === "object" ? "removed" : "changed",
  }));
  if (!changes.length) return `<p class="muted">No clinical changes.</p>`;
  return `<table><tbody>${changes.map((c) => `<tr><td>${esc(c.label)}${c.change !== "changed" ? ` <span class="muted">(${c.change})</span>` : ""}</td>
    <td><span class="diff-old">${esc(c.before)}</span> → <span class="diff-new">${esc(c.after)}</span></td></tr>`).join("")}</tbody></table>`;
}

async function openHistory(id, select) {
  hist = await api(`/api/records/${id}/history`);
  selected = select || (hist.versions.some((v) => v.id === id) ? id : hist.current);
  renderHistory();
  const dlg = $("#hist-dlg");
  if (!dlg.open) dlg.showModal();
}
$("#hist-close").onclick = () => $("#hist-dlg").close();
$("#hist-dlg").addEventListener("click", (e) => { if (e.target.id === "hist-dlg") e.target.close(); });

function layoutGraph(h) {
  // Tidy tree, git-style: depth along x, each branch on its own row; a parent shares its first child's row.
  const byId = Object.fromEntries(h.versions.map((v) => [v.id, v]));
  const pos = {};
  let row = 0;
  (function place(id, depth) {
    const kids = [...byId[id].children].sort((a, b) => byId[a].seq - byId[b].seq);
    if (!kids.length) { pos[id] = { x: depth, y: row++ }; return; }
    kids.forEach((k) => place(k, depth + 1));
    pos[id] = { x: depth, y: pos[kids[0]].y };
  })(h.versions[0].id, 0);
  return pos;
}

function renderHistory() {
  const h = hist, byId = Object.fromEntries(h.versions.map((v) => [v.id, v]));
  const n = h.versions.length, branches = h.versions.filter((v) => !v.children.length).length;
  $("#hist-title").textContent = `${h.incident_id} · history`;
  $("#hist-sub").textContent = `${n} version${n > 1 ? "s" : ""} · ${branches} branch${branches > 1 ? "es" : ""} · current ${h.current_label}`;

  const pos = layoutGraph(h), DX = 120, DY = 70, PAD = 34;
  const maxX = Math.max(...Object.values(pos).map((p) => p.x)), maxY = Math.max(...Object.values(pos).map((p) => p.y));
  const P = (id) => ({ x: PAD + pos[id].x * DX, y: PAD + pos[id].y * DY });
  const edges = h.versions.filter((v) => v.parent).map((v) => {
    const a = P(v.parent), b = P(v.id), r = 15;
    const d = a.y === b.y ? `M${a.x + r},${a.y} L${b.x - r - 4},${b.y}`
      : `M${a.x},${a.y + r} C${a.x},${b.y} ${a.x + DX / 2},${b.y} ${b.x - r - 4},${b.y}`;
    return `<path class="edge" d="${d}" marker-end="url(#arrow)"/>`;
  }).join("");
  const nodes = h.versions.map((v) => {
    const p = P(v.id);
    return `<g class="node ${v.is_current ? "current" : ""} ${v.id === selected ? "selected" : ""}" data-v="${v.id}">
      <circle cx="${p.x}" cy="${p.y}" r="15"><title>${v.label} · ${esc(v.reason || "original")}</title></circle>
      <text class="lbl in" x="${p.x}" y="${p.y + 4}" text-anchor="middle">${v.label}</text>
      <text class="sub" x="${p.x}" y="${p.y + 32}" text-anchor="middle">${v.hash.slice(0, 7)}</text></g>`;
  }).join("");
  const svg = $("#hist-graph");
  svg.setAttribute("width", PAD * 2 + maxX * DX + 20);
  svg.setAttribute("height", PAD * 2 + maxY * DY + 14);
  svg.innerHTML = `<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
    <path d="M0,0 L10,5 L0,10 z" fill="currentColor" style="color:var(--faint)"/></marker></defs>${edges}${nodes}`;
  svg.querySelectorAll(".node").forEach((g) => g.addEventListener("click", () => { selected = g.dataset.v; renderHistory(); }));

  const v = byId[selected];
  const others = h.versions.filter((o) => o.id !== v.id);
  $("#hist-detail").innerHTML = `
    <h4>${v.label} ${v.is_current ? `<span class="badge" style="background:var(--accent-soft);color:var(--accent)">current</span>` : ""}</h4>
    <dl>
      <dt>Type</dt><dd>${v.kind === "create" ? "Original signed record" : `Amendment of ${v.parent_label}`}</dd>
      <dt>Signed</dt><dd>${fmtTime(v.created_at)} by ${esc(v.responder_id)}</dd>
      ${v.reason ? `<dt>Reason</dt><dd>${esc(v.reason)}</dd>` : ""}
      <dt>Content</dt><dd>${v.meds} medication(s) · ${v.procs} procedure(s)</dd>
      <dt>Hash</dt><dd class="hash">${v.hash}</dd>
      ${v.parent ? `<dt>Parent hash</dt><dd class="hash">${v.parent_hash}</dd>` : ""}
    </dl>
    <b>${v.kind === "create" ? "Responder edits to the machine draft" : `Changes from ${v.parent_label}`}</b>
    ${diffTable(v.changes, v.edits)}
    ${others.length ? `<div class="hist-actions"><label class="inline">Compare with
      <select id="cmp">${["", ...others.map((o) => o.id)].map((id) => `<option value="${id}">${id ? byId[id].label : "—"}</option>`).join("")}</select></label></div>
      <div id="cmp-out"></div>` : ""}
    <div class="hist-actions">
      <a href="/records/${v.id}/report" target="_blank"><button>Open report</button></a>
      <button id="h-amend">${v.is_current ? "Amend" : "Amend from this version (new branch)"}</button>
    </div>
    ${v.is_current ? "" : `<div class="hist-actions">
      <input id="h-reason" placeholder="Reason for making ${v.label} current">
      <button class="primary" id="h-switch">Make ${v.label} current</button></div>`}`;

  $("#h-amend").onclick = () => { $("#hist-dlg").close(); startAmend(v.id); };
  if ($("#cmp")) $("#cmp").onchange = async (e) => {
    if (!e.target.value) { $("#cmp-out").innerHTML = ""; return; }
    const d = await api(`/api/records/${v.id}/diff/${e.target.value}`);
    $("#cmp-out").innerHTML = `<p class="small muted">From ${v.label} to ${byId[e.target.value].label}:</p>${diffTable(d.changes, d.edits)}`;
  };
  if ($("#h-switch")) $("#h-switch").onclick = async () => {
    const reason = $("#h-reason").value.trim();
    if (!reason) { $("#h-reason").focus(); return; }
    try {
      const r = await api(`/api/records/${v.id}/switch`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ responder_id: $("#responder").value, reason, target: v.id }),
      });
      hist = r.history; renderHistory(); loadHistory(); refreshStatus();
    } catch (err) { alert(err.message); }
  };

  const evText = (e) => e.kind === "create" ? `Created ${e.version}`
    : e.kind === "amend" ? `Amended ${e.from} → ${e.version}` : `Made ${e.version} current (was ${e.from})`;
  $("#hist-events tbody").innerHTML = [...h.events].reverse().map((e) => `
    <tr><td>#${e.seq}</td><td>${fmtTime(e.created_at)}</td><td><b>${evText(e)}</b></td>
    <td>${esc(e.responder_id)}</td><td>${esc(e.reason || "")}</td><td class="hash">${e.hash.slice(0, 12)}…</td></tr>`).join("");
}
$("#verify").onclick = async () => {
  const v = await api("/api/audit/verify");
  $("#verify-result").innerHTML = v.ok
    ? `<span class="verify-ok">✓ Chain and version graph intact — ${v.records} records, ${v.versions} versions, ${v.switches} switch event(s), head ${v.head.slice(0, 16)}…</span>`
    : `<span class="verify-bad">✗ Chain broken at entry ${esc(v.broken_at)}: ${esc(v.reason)}</span>`;
  refreshStatus();
};

// ---------- new entry ----------
function resetEntry() {
  $("#note").value = "";
  liveNoteTime();
  asrMeta = null; draft = null; rec = null; modelInfo = {};
  endAmend(); resetChanges();
  $("#asr-info").textContent = ""; $("#extract-info").textContent = ""; $("#rec-timer").textContent = "00:00";
  $("#review-body").hidden = true; $("#review-empty").hidden = false;
  $("#signed").hidden = true; $("#warn-count").textContent = "";
  newIncident();
  $("#note").focus();
}
$("#new-entry").onclick = () => {
  const unsigned = (rec && $("#signed").hidden) || (!rec && $("#note").value.trim());
  if (unsigned && !confirm("Discard the current unsigned note and start a new entry?")) return;
  resetEntry();
};

// the transcript is only ever cleared by hand
$("#clear-note").onclick = () => {
  $("#note").value = ""; liveNoteTime(); asrMeta = null;
  $("#asr-info").textContent = ""; $("#rec-timer").textContent = "00:00";
  $("#note").focus();
};

// emptying the note releases a recording's pinned time (a time set by hand stays until cleared)
$("#note").addEventListener("input", (e) => {
  if (!e.target.value.trim() && noteTimeSource === "recording") liveNoteTime();
});
$("#note-time").onchange = (e) => {
  if (!e.target.value) { liveNoteTime(); return; }
  setNoteTime(localIso(new Date()).replace(/T\d\d:\d\d(:\d\d)?/, `T${e.target.value}:00`), "manual");
};
$("#note-time").onblur = tickNoteTime;
setInterval(tickNoteTime, 10000);

// ---------- init ----------
for (const k of Object.keys(SAMPLES)) $("#samples").insertAdjacentHTML("beforeend", `<option>${esc(k)}</option>`);
$("#samples").onchange = (e) => {
  if (SAMPLES[e.target.value]) {
    $("#note").value = SAMPLES[e.target.value]; asrMeta = null;
    if (noteTimeSource === "recording") liveNoteTime();
  }
  e.target.value = "";
};
newIncident();
tickNoteTime();
loadHistory();
// Poll fast until the local LLM is up, then slowly so the badges stay truthful.
(async function pollStatus() {
  const ok = await refreshStatus();
  setTimeout(pollStatus, ok ? 30000 : 4000);
})();
