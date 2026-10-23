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
let noteTime = null;    // ISO time the note was recorded/started; unstated & relative times resolve against it

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
function setNoteTime(iso) {
  noteTime = iso;
  $("#note-time").value = hm(iso);
}
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
    set("#st-chain", h.audit.ok, h.audit.ok ? `Chain OK · ${h.audit.count} records` : "Chain BROKEN");
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
  if (!noteTime || !$("#note").value.trim()) setNoteTime(localIso(new Date()));
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
    if (!noteTime) setNoteTime(localIso(new Date()));
    const r = await api("/api/extract", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, reference_time: noteTime }) });
    draft = r.record; rec = clone(r.record);
    modelInfo = { ...r.model_info, ...(asrMeta ? { asr_model: asrMeta.asr_model, asr_device: asrMeta.asr_device } : {}) };
    $("#extract-info").textContent = `${r.model_info.llm_model} · ${r.model_info.llm_latency_s}s`;
    $("#signed").hidden = true;
    renderReview();
  } catch (e) { $("#extract-info").textContent = "⚠ " + e.message; }
  btn.disabled = false; btn.textContent = "Extract record →";
};

// ---------- review ----------
function chips(item) {
  return (item.warnings || []).map((w) => `<span class="chip ${/hallucination|exceeds/.test(w) ? "bad" : ""}">${esc(w)}</span>`).join("");
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
}

$("#review-body").addEventListener("change", (e) => {
  const t = e.target, list = t.dataset.list;
  if (!list) return;
  const item = rec[list][+t.dataset.i], k = t.dataset.k;
  if (k === "time") item.time = setHM(item.time, t.value);
  else if (k === "dose_value") item.dose_value = t.value === "" ? null : parseFloat(t.value);
  else item[k] = t.value === "" ? null : t.value;
  if (k === "drug") item.rxnorm = null; // code no longer matches the edited name
  if (k === "name") item.code = null;
});
$("#review-body").addEventListener("click", (e) => {
  const t = e.target;
  if (t.dataset.del) { rec[t.dataset.del].splice(+t.dataset.i, 1); renderReview(); }
  if (t.dataset.add) {
    const now = new Date().toTimeString().slice(0, 5);
    const time = setHM(rec.reference_time, now);
    const blank = {
      medications: { drug: "", dose_value: null, dose_unit: "mg", route: null, site: null, time, warnings: ["added manually"], evidence_found: false },
      procedures: { name: "", details: null, site: null, outcome: null, time, warnings: ["added manually"], evidence_found: false },
      vitals: { type: "HR", value: "", unit: null, time, warnings: ["added manually"], evidence_found: false },
    }[t.dataset.add];
    rec[t.dataset.add].push(blank);
    renderReview();
  }
});
$("#p-age").onchange = (e) => (rec.patient.age = e.target.value || null);
$("#p-sex").onchange = (e) => (rec.patient.sex = e.target.value);
$("#p-wt").onchange = (e) => (rec.patient.weight_kg = e.target.value ? parseFloat(e.target.value) : null);
$("#p-cc").onchange = (e) => (rec.chief_complaint = e.target.value || null);
$("#confirm").onchange = (e) => ($("#sign").disabled = !e.target.checked);

// ---------- sign ----------
$("#sign").onclick = async () => {
  if (rec.medications.some((m) => !m.drug) || rec.procedures.some((p) => !p.name)) { alert("Fill in or remove empty rows first."); return; }
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
    asrMeta = null;
    newIncident();
    loadHistory(); refreshStatus();
  } catch (e) { alert(e.message); $("#sign").disabled = false; }
};

// ---------- history ----------
async function loadHistory() {
  const rows = await api("/api/records");
  $("#t-history tbody").innerHTML = rows.map((r) => `
    <tr><td>${r.seq}</td><td>${new Date(r.created_at).toLocaleString()}</td><td>${esc(r.incident_id)}</td>
    <td>${esc(r.responder_id)}</td><td>${r.meds}</td><td>${r.procs}</td><td>${r.edits}</td>
    <td class="hash">${r.hash.slice(0, 16)}…</td>
    <td><a href="/records/${r.id}/report" target="_blank">report</a> · <a href="/api/records/${r.id}/fhir" target="_blank">json</a></td></tr>`).join("")
    || `<tr><td colspan="9" class="muted">No signed records yet.</td></tr>`;
}
$("#verify").onclick = async () => {
  const v = await api("/api/audit/verify");
  $("#verify-result").innerHTML = v.ok
    ? `<span class="verify-ok">✓ Chain intact — ${v.count} records, head ${v.head.slice(0, 16)}…</span>`
    : `<span class="verify-bad">✗ Chain broken at record ${esc(v.broken_at)}: ${esc(v.reason)}</span>`;
  refreshStatus();
};

// ---------- new entry ----------
function resetEntry() {
  $("#note").value = "";
  noteTime = null; $("#note-time").value = "";
  asrMeta = null; draft = null; rec = null; modelInfo = {};
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

// typed notes: the note's time is when typing started
$("#note").addEventListener("input", (e) => {
  if (!e.target.value.trim()) { noteTime = null; $("#note-time").value = ""; }
  else if (!noteTime) setNoteTime(localIso(new Date()));
});
$("#note-time").onchange = (e) => {
  if (e.target.value) noteTime = (noteTime || localIso(new Date())).replace(/T\d\d:\d\d(:\d\d)?/, `T${e.target.value}:00`);
};

// ---------- init ----------
for (const k of Object.keys(SAMPLES)) $("#samples").insertAdjacentHTML("beforeend", `<option>${esc(k)}</option>`);
$("#samples").onchange = (e) => {
  if (SAMPLES[e.target.value]) { $("#note").value = SAMPLES[e.target.value]; asrMeta = null; setNoteTime(localIso(new Date())); }
  e.target.value = "";
};
newIncident();
loadHistory();
// Poll fast until the local LLM is up, then slowly so the badges stay truthful.
(async function pollStatus() {
  const ok = await refreshStatus();
  setTimeout(pollStatus, ok ? 30000 : 4000);
})();
