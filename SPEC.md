# Field-to-Record — Feature Specification

| | |
|---|---|
| **Product** | Field-to-Record (Jatayu demo) |
| **Purpose** | Turn a first responder's spoken or typed on-scene note (English, Hindi, Hinglish) into a structured, timestamped, tamper-evident record of medications, procedures and vitals |
| **Deployment** | Single laptop, fully offline after one-time setup |
| **Stack** | Python 3.11 · FastAPI · faster-whisper · Ollama (local LLM) · SQLite · vanilla JS frontend · Jinja2 |
| **Status** | Working demo. Synthetic data only, no real PHI |
| **Spec date** | 2026-10-23 |

---

## 1. Feature summary

| # | Feature | Module | Status |
|---|---|---|---|
| F1 | Voice capture in the browser | `app/static/app.js` | Built |
| F2 | Offline speech-to-text (EN / HI / Hinglish) | `app/asr.py` | Built |
| F3 | Typed notes and sample notes | `app/static/app.js` | Built |
| F4 | LLM clinical entity extraction, JSON-schema constrained | `app/extractor.py` | Built |
| F5 | Deterministic normalisation (names, codes, units, routes) | `app/normalize.py`, `app/lexicon.py` | Built |
| F6 | Time resolution (relative → absolute) | `app/normalize.py` | Built |
| F7 | Evidence grounding / hallucination flag | `app/normalize.py` | Built |
| F8 | Dose sanity warnings | `app/normalize.py` | Built |
| F9 | Self-correction and implied-fact completion | `app/normalize.py` | Built |
| F10 | Responder review and edit UI | `app/static/` | Built |
| F11 | Confirm and sign (human in the loop) | `app/static/app.js`, `app/main.py` | Built |
| F12 | Append-only SHA-256 hash-chained record store | `app/audit.py` | Built |
| F13 | Field-level machine-vs-human edit diff | `app/audit.py` | Built |
| F14 | Chain verification and tamper detection | `app/audit.py`, `scripts/tamper_demo.py` | Built |
| F15 | Printable patient care report (HTML → PDF) | `app/report.py`, `app/templates/report.html` | Built |
| F16 | FHIR-lite bundle export | `app/schemas.py` | Built |
| F17 | Audit chain history view | `app/static/` | Built |
| F18 | System health / status pills | `app/main.py`, `app/static/app.js` | Built |
| F19 | Model warm-up on startup | `app/main.py` | Built |
| F20 | Configuration via environment variables | `app/config.py` | Built |
| F21 | Evaluation harness | `eval/run_eval.py` | Built |
| F22 | Unit tests | `tests/test_core.py`, `tests/test_history.py`, `tests/test_harness.py` | Built |
| F23 | Hash-linked version graph: amendments, branches, signed "make current" switches | `app/audit.py`, `app/main.py` | Built |
| F24 | Record history view (version graph, diffs, compare, switch) | `app/static/` | Built |
| F25 | LLM harness: transcript drug scan + hints, retry/repair, rule-based fallback, post-extraction checks | `app/harness.py` | Built |

---

## 2. Processing pipeline

```
mic / typed note
   ▼  F1–F3  capture (browser MediaRecorder or textarea)
   ▼  F2     faster-whisper transcription (+ audio SHA-256)
transcript
   ▼  F4     Ollama LLM, JSON-schema constrained → raw extraction
   ▼  F5–F9  normaliser: canonical names, RxNorm, units, routes, absolute times,
   │         evidence check, dose sanity, self-correction, implications
draft record
   ▼  F10    responder reviews / edits
   ▼  F11    confirm & sign
   ▼  F12–13 append to SQLite hash chain (draft + final + diff + hashes + model versions)
   ▼  F15–16 HTML report  ·  FHIR-lite JSON
```

---

## 3. Feature details

### F1 — Voice capture
- One-button record / stop using the browser `MediaRecorder` API; the mic is released when recording stops.
- Live `mm:ss` timer while recording; button states: Record → Stop → Transcribing….
- Audio is uploaded as WebM to `/api/transcribe` with the language you picked.
- The new transcript is **appended** to anything already in the note box, so you can dictate in several parts.
- After transcription the UI shows the detected language and confidence, audio length, ASR latency and device.
- If no mic is available, an alert shows the error.

### F2 — Offline speech-to-text
- Engine: faster-whisper. Default model `large-v3-turbo` (`small` recommended for CPU-only machines).
- Language modes: `auto`, `en`, `hi`, `hinglish` (Hinglish is auto-detect plus a Hinglish bias prompt).
- **Domain prompting:** separate English and Hinglish initial prompts bias Whisper towards medical words and romanised Hinglish, instead of translating or forcing Devanagari.
- Decoding: beam size 5, VAD filter on, no conditioning on previous text (fewer repetition loops).
- **Device fallback:** tries CUDA float16, then CPU int8 (`FTR_ASR_DEVICE=auto|cuda|cpu`).
- **Offline-first load:** uses the cached model (`local_files_only`) first and downloads only if it isn't cached.
- On Windows, finds the CUDA/cuDNN DLLs from the `nvidia-*` pip wheels automatically.
- Thread-safe lazy load of a single shared model.
- Output: text, segments with start/end times, detected language and probability, duration, latency, **audio SHA-256**, model name and device.

### F3 — Typed notes and samples
- A free-text note box takes typed or pasted notes in any of the three languages.
- Six built-in sample notes: English chest pain, English opioid overdose, Hinglish anaphylaxis, Hinglish asthma, Hindi hypoglycaemia, trauma fall.
- Loading a sample clears any earlier audio metadata, so a typed note is never linked to an old recording's hash.

### F4 — LLM clinical entity extraction
- Local LLM through Ollama (`/api/chat`). Default `qwen2.5:7b`; any Ollama model can be set.
- **Schema-constrained output:** the Pydantic `LLMExtraction` JSON schema is passed as Ollama's `format`.
- Deterministic decoding: temperature 0, 4096-token context, model kept loaded for 30 minutes.
- The system prompt is written for Indian ambulance crews and covers:
  - Only things actually **given or performed**. It excludes drugs that were considered, refused, withheld, or are home medications.
  - Repeated doses listed as separate administrations ("repeated", "do baar", "phir se").
  - Spoken numbers turned into digits ("point three", "aadha", "do sau").
  - Route inferred from context in English, Hinglish and Devanagari ("chaba ke" → PO, "naak se" → IN, "नस में" → IV).
  - Oxygen recorded both as a medication (L/min, INH) and as the procedure "Oxygen therapy".
  - Drug administrations and outcomes are kept out of the procedures list.
  - Times copied exactly as spoken and never invented; values not said are left null.
  - Every item must include a verbatim `evidence` quote.
- Three few-shot examples: English, Hinglish, Devanagari Hindi.
- Extracted fields:
  - **Patient:** age, sex, weight (kg)
  - **Chief complaint**
  - **Medications:** drug, dose value, unit, route, site, time, evidence
  - **Procedures:** name, details, site, time, outcome, evidence
  - **Vitals:** type (HR, BP, SpO2, RR, GCS, Temp, BGL, Pain), value, unit, time, evidence
- Returns model name and latency. If Ollama can't be reached, the API returns HTTP 503 with a clear message.

### F5 — Normalisation and coding
- **Drug lexicon:** 35 pre-hospital drugs, each with RxNorm ingredient RXCUI (where available), synonyms (generic, Indian brand names such as Avil, Ondem, Asthalin, Dolo, plus Devanagari) and a typical maximum single adult dose.
- **Procedure lexicon:** 20 procedures with local codes (`PROC-IV`, `PROC-CPR`, …) and synonyms (for example "gauge"/"18g" → IV cannulation, "patti" → Wound dressing).
- **Routes:** IV, IM, IO, SC, SL, PO, IN, INH, ET, PR, TOP, with English, Hinglish and Devanagari synonyms.
- **Units:** mg, mcg, g, mL, L, L/min, units, mEq, puffs, tablet.
- Matching order: exact → longest whole-word synonym inside the string → fuzzy match (cutoff 0.82, drugs and procedures only).
- Short ambiguous synonyms (`in`, `et`, `pr`, `pause`) count only when they are the whole string.
- Unicode NFC and whitespace normalisation, so Devanagari matches reliably.
- Raw values are kept next to the canonical ones (`drug_raw`, `name_raw`, `time_raw`).
- Warnings: unrecognised drug, fuzzy-matched drug or procedure, procedure not in vocabulary, dose missing (not raised for oxygen, NS or RL), route missing.

### F6 — Time resolution
- Every item gets an absolute ISO-8601 time, resolved against the note's capture time (`reference_time`).
- The note time shown under the recorder decides `reference_time`: by default it is the **current time** (shown live, in grey, and taken at the moment of extraction); starting a recording pins the recording's start time; a time set by hand wins over both and stays until the field is emptied or the note is cleared (Clear / New Entry).
- Supported forms:
  - Relative: "10 min ago", "5 minute pehle", "2 घंटे पहले", plus minutes and hours.
  - Clock: `14:32`, `2:30 pm`, `14.32`, `1432 hrs`, a bare `1042` (how Whisper writes "ten forty-two"), with "baje", "pe" or "par" after it.
  - Now words: "now", "abhi", "अभी", "on arrival", "at scene".
- **Midnight rollover:** a clock time more than one hour in the future is moved to the previous day.
- No time stated → capture time is used, with a warning (shown for medications only).
- A time that can't be parsed → capture time plus a warning.

### F7 — Evidence grounding (hallucination guard)
- Each item's evidence quote is checked against the transcript in three steps:
  1. exact substring
  2. at least 70% of the quote's words appear in the transcript
  3. sliding-window fuzzy match, ratio ≥ 0.8 (allows small ASR or LLM wording drift)
- If none pass: `evidence_found=false` and the warning *"not found in transcript - possible hallucination"* (highlighted red in the UI).

### F8 — Dose sanity check
- Compares the dose with the drug's typical maximum single adult dose, converting between mcg, mg and g.
- Catches ASR decimal errors, for example "3 mg" epinephrine heard instead of "0.3 mg".
- Warning: *"dose X exceeds typical max single adult dose (Y) - verify"* (highlighted red).
- This is a demo heuristic, not clinical guidance.

### F9 — Self-correction and implied facts
- **Drug grounding:** if the model's drug name differs from the one drug named in its own evidence quote, the quote wins. The change is recorded as a warning ("model said X, transcript says Y - corrected").
- **Route grounding:** one explicit route word in the quote beats the model's guess. Oxygen is always INH.
- **Missing dose recovery:** if the model left out a dose that is clearly in its quote, it is parsed with a regex (including litres → L/min for oxygen).
- **Misfiled procedures:** a "procedure" whose quote really names a drug (for example "NS 500 ml start kiya") becomes a medication, marked "recovered from a misfiled procedure - verify".
- Things that aren't procedures (nebulisation, ROSC, assessment, …) are dropped from procedures.
- Duplicate procedures from the same phrase are merged.
- **Implications:**
  - A procedure named in any quote but missed by the model is added ("C-collar on, spine board" → two procedures).
  - Oxygen therapy ⇄ oxygen medication: each is added if only the other was found.
  - A BGL vital adds a "Glucose check" procedure, with the reading as its outcome.

### F10 — Review and edit UI
- Patient fields: age, sex, weight, chief complaint (all editable).
- Three editable tables: **Medications** (time, drug, dose, unit, route dropdown, site), **Procedures** (time, name, details, site, outcome), **Vitals** (time, type dropdown, value, unit).
- Under each row: the evidence quote, the code (RxNorm or procedure code) and warning chips. Hallucination and over-max warnings are red.
- A header counter shows "N items need attention" or "no warnings".
- Add a row with "+ add" (defaults to the current time and is marked "added manually"); remove a row with ✕.
- Editing a drug or procedure name clears its code, so a stale code is never kept.
- Time inputs edit only HH:MM; the date comes from the reference time.

### F11 — Confirm and sign
- "Confirm & sign" stays disabled until you tick *"I confirm this record reflects care I provided."*
- Signing is blocked while any medication or procedure row has an empty name.
- Fields sent: incident ID, responder ID, transcript, language, audio hash, **machine draft**, **final record**, model info (LLM and ASR).
- After signing you see the record number, number of human edits, full hash, and links to the report and FHIR JSON.
- Incident ID is generated automatically (`INC-YYYYMMDD-HHMM`) and refreshed after each sign. Responder ID defaults to `EMT-042`.

### F12 — Hash-chained record store
- SQLite (`data/records.db`) with a single append-only `records` table.
- `hash = SHA-256(prev_hash + canonical JSON of the hashed fields)`; the first record links to a genesis hash of 64 zeros.
- Hashed fields: id, seq, created_at, incident_id, responder_id, device_id, language, transcript, transcript SHA-256, audio SHA-256, draft, final, edits, model_info.
- Canonical JSON: sorted keys, compact separators, UTF-8 kept as is.
- Each record stores: a 12-character id, sequence number, UTC timestamp, device ID, and transcript and audio SHA-256.

### F13 — Edit diff
- A recursive field-level diff between the machine draft and the signed final record, e.g. `{field: "medications[0].dose_value", machine: 3, human: 0.3}`.
- Ignores the `warnings` and `evidence_found` fields.
- Stored inside the signed record and covered by its hash.

### F14 — Chain verification and tamper detection
- `verify()` walks the chain from genesis and reports the first broken link with a reason:
  - sequence gap (a deleted row)
  - `prev_hash` mismatch (rows reordered or replaced)
  - content changed after signing
- Exposed through the API, the UI "Verify chain" button and the health status pill.
- It then checks every version-graph link (F23): a version's parent exists earlier in the log, belongs to the same record and has the hash the version committed to; a switch starts from the version that was current and targets an earlier version of the same record.
- `scripts/tamper_demo.py` silently multiplies a signed dose by 10 in the DB to show the chain breaking; `--restore` undoes it.

### F15 — Printable report
- An HTML patient care record at `/records/{id}/report`, with a "Print / Save PDF" button that is hidden when printing.
- Sections: incident metadata (incident, responder, device, capture and sign times, language, patient, chief complaint), medications (with RxNorm), procedures, vitals, source dictation, audit block.
- Rows a human edited are highlighted. A table lists every edit as machine vs. responder value.
- Audit block: record id and seq, hash, previous hash, transcript and audio SHA-256 (or "typed note"), model info.
- Disclaimer footer.

### F16 — FHIR-lite export
- `GET /api/records/{id}/fhir` returns a FHIR-style `Bundle` (type `collection`) containing:
  - `Encounter` (class EMER, reason, participant)
  - `Patient` (gender, age, weight)
  - `MedicationAdministration` per medication (RxNorm coding, dose, route, site, time)
  - `Procedure` per procedure (local code, body site, outcome, note)
  - `Observation` per vital (category vital-signs)
  - `Provenance` (author = responder, assembler = device + models, signature = SHA-256 chain hash and prev hash, entities = transcript and audio hashes)
- "FHIR-lite" means FHIR-shaped but not validated against the FHIR R4 profiles.

### F17 — Audit chain history
- A table of all signed records (one row per record, showing its current version), most recently changed first: seq, last change, incident, responder, medication count, procedure count, version count (with a branch badge), current hash, and History, Amend, report and JSON actions.

### F18 — Health and status
- `GET /api/health` returns LLM status (model present in Ollama), ASR status (loaded, model, device), chain status and device ID.
- Header pills: LLM ready or not ready, ASR loaded (or "loads on first use"), Chain OK with record count or BROKEN.

### F19 — Warm-up
- On server startup a background thread loads Whisper and runs one test extraction, so the first real request is fast. Failures are logged and don't stop startup.

### F20 — Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FTR_LLM_MODEL` | `qwen2.5:7b` | Ollama model |
| `FTR_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
| `FTR_LLM_TIMEOUT` | `120` | LLM request timeout (s) |
| `FTR_ASR_MODEL` | `large-v3-turbo` | Whisper model |
| `FTR_ASR_DEVICE` | `auto` | `auto` / `cuda` / `cpu` |
| `FTR_DEVICE_ID` | `laptop-01` | Stamped into every record |
| `FTR_DATA_DIR` | `./data` | Location of the SQLite DB |

### F21 — Evaluation harness
- `python -m eval.run_eval [--model M] [--lang en|hinglish|hi] [--out file]`
- 22 hand-labelled synthetic dictations (`eval/dictations.jsonl`) in English, Hinglish and Hindi.
- Metrics (micro-averaged precision, recall and F1, with multiset matching so repeated doses count):
  - `drug`: canonical drug name
  - `full_med`: drug + dose + unit + route (the safety-critical tuple)
  - `procedure`: canonical procedure name
- Reported per language and overall, plus median and max latency. Per-case missing and extra items are written to `eval/results.json`.
- Current results (qwen2.5:7b, RTX 4060): drug F1 1.00, full_med F1 1.00, procedure F1 0.97, median 6–8 s per note. *These are tuned on the same set; a held-out set is still needed.*

### F22 — Unit tests
- `pytest -q`, no LLM needed. Covers drug synonyms, unknown drugs, routes, units, procedures, time resolution, missing-time default, dose-limit warning, evidence check, hallucination flag in `normalize`, chain integrity and tamper detection, and deleted-row detection.

### F23 — Hash-linked version graph (implements P1 and P2)
- Every chain entry has a `kind`:
  - `create`: the first version (v1) of a record; its `lineage` (record id) is its own id.
  - `amend`: a new version; `parent` = the version it was made from, `parent_hash` = that version's hash. Requires a non-empty `reason` and at least one changed field. Transcript, audio hash, incident and model info are inherited; `draft` = parent's final, `edits` = field diff parent → new.
  - **Addenda:** while amending, a dictated or typed follow-up note can be extracted with "Extract & Add"; its items are appended to the record being amended (marked "added by addendum"). The amended version's transcript is the original plus `[Addendum HH:MM] <note>`, and each addendum's time, audio hash, transcript hash and models are stored in `model_info.addenda`, all covered by the hash.
  - **Same-source guard:** an amendment must keep the record's recording time, and every quoted item must appear in the original transcript or an addendum signed with it; a different note is refused (sign it as a new record).
  - `switch`: a signed decision that another version is current; `parent`/`parent_hash` = the version that was current, `target` = the version that becomes current, `edits` = diff between them. Requires a reason.
- `kind`, `lineage`, `parent`, `parent_hash`, `reason` and `target` are covered by the entry's hash, so each version commits to its parent like a git commit. Amending an older version starts a branch.
- Current version = the newest version created in the record, unless a later switch names another.
- Hashing is versioned (`hash_v`): entries signed before the graph existed keep verifying with the v1 field list; the store migrates in place.
- FHIR `Provenance.entity` lists the revised version (`role = revision`, id and hash); the report shows "Version vN of M", the reason, and a link to the current version when it isn't current.

### F24 — Record history view
- "History" opens a dialog with the version graph (depth on x, one row per branch; arrows are parent → child hash links; the current version is filled).
- Selecting a version shows its type, signer, time, reason, hash, parent hash and its field changes from the parent; it can be compared with any other version.
- Actions: open the version's report, amend from it (an older version starts a new branch), or make it current with a required reason.
- A table lists every signed event for the record: created, amended (from → to), made current (was → now), with responder, reason and hash.

---

### F25 — LLM harness (`app/harness.py`)
The LLM is never trusted alone; every extraction goes through `harness.extract_record`.
- **Before the call:** `scan_drugs` finds every medicine the note names (exact lexicon match, longest synonym first, plus a fuzzy match for speech-recognition misspellings such as "paracitamol"), and classifies each mention by its clause as *given*, *negated* ("refused", "nahi diya", "mana kar diya", "नहीं") or *context* (allergy, home medication, considered). `drug_hints` turns this into lines appended to the note for the model, e.g. "ondem = ondansetron", "NOT given: morphine", and, when the note has a sugar reading, "glucose readings are vitals, not medicines".
- **The call:** output that fails schema validation is sent back once with the validation error (`LLMOutputError` → repair turn). If the model is unreachable or still invalid, `rules_extract` builds the draft deterministically (drugs, dose, unit, route, time, procedures, vitals, age and sex) and the result is labelled `rules-fallback`.
- **After the call** (on the normalised record), each check adds item warnings and a check result `pass` / `fixed` / `flag`:
  - *Output schema*: first-time valid, repaired on retry, or rule-based draft.
  - *Medicine names*: every medicine is a known generic and named (or plainly misspelt) in the note.
  - *Phantom items*: an item whose quote names no medicine and no dose is removed if that drug is already recorded from a real quote, else flagged.
  - *Medicine recall*: every medicine named as given (with a dose or an administration word, so "blood glucose 42" doesn't count) is in the record; a missing one is added from the transcript and flagged.
  - *Negation*: a listed medicine the note says was not given, or only mentions as history or allergy, is flagged.
  - *Dose grounding*: the dose number appears in its source sentence (mg/mcg conversions allowed).
  - *Route plausibility*: the route is in `ALLOWED_ROUTES` for that drug.
  - *Time sanity*: not after the recording, not more than 12 h before it.
  - *Duplicates*: identical items from the same words are removed unless the note says the dose was repeated.
- The report (mode, checks, hints) is returned by `/api/extract` and shown under "Extract" in the UI; a summary (mode and each check's status) is stored in the record's `model_info`, so it is covered by the signed hash.
- Sentence splitting ignores decimal points ("0.5 mg"), shared with the normaliser.
- Eval: `python -m eval.run_eval` runs the harnessed pipeline; `--rules-only` scores the fallback alone. Current: harnessed drug F1 1.00, full_med F1 1.00, procedure F1 0.97, complaints 4/4; rule-based fallback alone drug F1 0.94, full_med F1 0.84, procedure F1 0.99.

---

## 4. API reference

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | LLM / ASR / chain status, device ID |
| POST | `/api/transcribe` | multipart `audio`, `language` → transcript, segments, audio hash |
| POST | `/api/extract` | `{text, reference_time?}` → `{record, raw, model_info, harness}` (400 empty; LLM down → rule-based draft, `harness.mode = rules-fallback`) |
| POST | `/api/records` | Sign and append a record → `{id, seq, hash, edits}` |
| GET | `/api/records` | One summary per record (current version, version and branch counts) |
| GET | `/api/records/{id}` | A stored version plus its place in the history (404 if missing) |
| POST | `/api/records/{id}/amend` | `{responder_id, reason, final}` → new version linked to `{id}` by hash (400 no reason / no change) |
| POST | `/api/records/{id}/switch` | `{responder_id, reason, target}` → signed switch of the current version (400 invalid target) |
| GET | `/api/records/{id}/history` | Version graph of the record: versions, children, current, signed events |
| GET | `/api/records/{a}/diff/{b}` | Field diff between two versions of the same record |
| GET | `/api/records/{id}/fhir` | FHIR-lite bundle |
| GET | `/records/{id}/report` | HTML report |
| GET | `/api/audit/verify` | Chain verification result |
| GET | `/` | Static single-page UI |

---

## 5. Non-functional requirements

| Area | Requirement |
|---|---|
| Offline | Works with no network after setup; LLM and ASR run on localhost |
| Privacy | No data leaves the device; synthetic data only in the demo |
| Performance | ~3.5 s ASR for 22 s of audio on GPU; 6–8 s median extraction |
| Hardware | NVIDIA GPU recommended; CPU fallback supported (smaller Whisper model) |
| Languages | English, Hindi (Devanagari), Hinglish (romanised, code-mixed) |
| Integrity | Any stored change, deletion or reorder can be detected |
| Safety | Documentation aid only, not clinical decision support; nothing saved without human confirmation |

---

## 6. Known limitations

- No authentication. Responder ID is free text and nobody signs cryptographically per user or device; the chain shows tampering but doesn't prove who wrote a record. *(Device signatures planned: P3.)*
- A hash chain on a single device: someone who can rewrite the whole DB can rebuild a valid chain. *(Mitigated by P3.)*
- Raw audio is not stored, only its hash. *(Planned: P4.)*
- RxNorm codes and dose limits are demo-grade and not verified against a current release.
- Procedure codes are local, not SNOMED CT.
- FHIR output is not validated against R4 profiles.
- Eval numbers are on the same data the rules were tuned on.
- `@app.on_event("startup")` is deprecated in FastAPI (lifespan is the replacement).

---

## 7. Planned features

Audience for these specs: engineers.

### P1 — Record amendments (addenda) · built (F23)
- A signed record is never changed. A correction is a **new record** in the chain that points to the record it amends (`amends: <record id>`).
- The addendum holds the full corrected record, a field-level diff against the version it amends (same format as F13), the responder ID and a required reason.
- The addendum is hashed and chained like any other record, so the original and every correction stay verifiable.
- **Current view:** a record's latest version is the newest addendum in its line. The history table and the report show the latest version, with a link to earlier ones.
- Report and FHIR export use the latest version. FHIR `Provenance` lists the amended record (`entity.role = revision`).
- UI: an "Amend" action on a signed record opens the review editor pre-filled from the latest version.
- API (proposed): `POST /api/records/{id}/amend`, `GET /api/records/{id}/versions`.

### P2 — Version tree with branches · built (F23, F24)
- Generalises P1: each record version has a parent, so the versions of a record form a **tree**, not just a line.
- Amending an older version starts a new branch instead of overwriting the newer one.
- Users can view any branch, compare two versions (field diff), and **switch** which branch is the current one.
- Switching is itself a signed, chained event (who switched, when, from which version to which), so the audit trail shows why the current version changed.
- The hash chain stays a single append-only log: the tree is metadata on top of it (`parent` pointers plus "current branch" events). `verify()` still checks one linear chain.
- Design point for P1: store a `parent` pointer from the start, so P1 data moves to P2 without migration.

### P3 — Per-device Ed25519 signatures · next
- Each device generates an Ed25519 key pair on first run. The private key never leaves the device.
- Every record hash is signed with the device key. The signature and public key fingerprint are stored in the record and in FHIR `Provenance.signature`.
- `verify()` also checks each signature against the device's registered public key.
- Responders are still identified by free-text ID; per-user login is out of scope for now.
- Covers the current gap where anyone who can rewrite the whole DB could rebuild a valid chain (they would also need the device key).

### P4 — Encrypted audio retention · next
- The original recording is kept alongside the record, encrypted at rest (e.g. AES-256-GCM with a per-device key).
- The record's `audio_sha256` is the hash of the original (unencrypted) audio, so playback can be checked against the signed hash.
- The report gets a "Play source audio" link when the audio is present and matches its hash.
- Open details: retention period, key storage on Windows/Android, and whether audio syncs off the device.

### P5 — Other future scope (from README)
- Android app on the device (whisper.cpp + llama.cpp / MediaPipe LLM).
- SNOMED CT procedure codes.
- Sync to a hospital EHR when back online.

---

## 8. Decisions log

| Question | Decision |
|---|---|
| Audience for the spec | Engineers |
| Can a signed record be corrected? | Yes: addendum records now (P1), a tree-based version history with switchable branches later (P2) |
| Identity | Per-device keys (P3); no per-user login for now |
| Raw audio | Store it, encrypted (P4) |
