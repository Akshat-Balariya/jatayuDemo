# Field-to-Record (Jatayu demo)

Turns a first responder's spoken or typed on-scene notes (English, Hindi or Hinglish) into a
**structured, timestamped, hash-chained record** of the medications and procedures given. That
record is the first link in an audit chain. Everything runs **offline** on a laptop.

```
mic / typed note
   │  faster-whisper (large-v3-turbo, GPU or CPU)            app/asr.py
   ▼
transcript ──► local LLM via Ollama, JSON-schema constrained   app/extractor.py
   │            (drug, dose, unit, route, site, time, evidence)
   ▼
normaliser: canonical names + RxNorm, units, routes,           app/normalize.py
            relative→absolute time, evidence-in-transcript check,
            dose sanity warnings
   ▼
responder review & edit UI  ──►  confirm & sign                app/static/
   ▼
SQLite append-only SHA-256 hash chain (machine draft + human   app/audit.py
edits + transcript/audio hashes + model versions)
   ▼
printable report (HTML → PDF)  +  FHIR-lite bundle JSON        app/report.py, app/schemas.py
```

## Setup (one time, needs internet)

```bash
# 1. Local LLM runtime
winget install Ollama.Ollama
ollama pull qwen2.5:7b

# 2. Python env (3.11)
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt

# 3. Pre-download the Whisper model so the first recording works offline
.venv/Scripts/python -c "from app import asr; asr.get_model()"
```

## Run (offline)

```bash
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```
Open http://localhost:8000. Turn Wi-Fi off to show it works offline.

## Test & evaluate

```bash
.venv/Scripts/python -m pytest -q          # normaliser + audit chain (no LLM needed)
.venv/Scripts/python -m eval.run_eval      # 22 synthetic EN/Hinglish/Hindi notes vs gold labels
.venv/Scripts/python scripts/tamper_demo.py            # edit a signed record in the DB -> chain breaks
.venv/Scripts/python scripts/tamper_demo.py --restore
```

Current results with `qwen2.5:7b` on an RTX 4060 laptop: drug F1 1.00, drug+dose+unit+route F1 1.00,
procedure F1 0.97, median 6–8 s per note. Whisper large-v3-turbo transcribes 22 s of audio in about 3.5 s on the GPU.
Note: the rules were tuned on this same set, so write fresh held-out notes before quoting these numbers.

## Configuration (environment variables)

| Var | Default | |
|---|---|---|
| `FTR_LLM_MODEL` | `qwen2.5:7b` | any Ollama model, e.g. `gemma3:4b`, `llama3.2:3b` |
| `FTR_ASR_MODEL` | `large-v3-turbo` | use `small` on CPU-only machines |
| `FTR_ASR_DEVICE` | `auto` | `cuda` / `cpu` |
| `FTR_DEVICE_ID` | `laptop-01` | stamped into every record |

## Safety design
- **Evidence grounding:** every extracted item must quote the transcript. Items whose quote cannot be found are flagged as a *possible hallucination*.
- **Dose sanity:** doses above a typical maximum single adult dose are flagged (catches ASR decimal errors such as "3 mg" epinephrine). These are demo heuristics, not clinical guidance.
- **Human in the loop:** nothing is saved until the responder confirms. The machine draft and the field-level human edits are both kept in the signed record.
- **Tamper evidence:** editing or deleting any stored record breaks the chain (`/api/audit/verify`).

## Data
Synthetic dictations only (`eval/dictations.jsonl`); no real patient data (PHI). RxNorm codes in `app/lexicon.py` are for the demo only.

## Future scope
Android (whisper.cpp + llama.cpp / MediaPipe LLM on device), Ed25519 per-device signatures, SNOMED CT procedure codes, sync to a hospital EHR when back online.
