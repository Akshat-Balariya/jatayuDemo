"""FastAPI app: everything is served from localhost, no internet required."""
from __future__ import annotations

import threading
from datetime import datetime

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import asr, audit, config, extractor, report
from .normalize import normalize
from .schemas import ExtractRequest, SaveRequest, to_fhir_lite

app = FastAPI(title="Field-to-Record")


@app.middleware("http")
async def no_cache(request, call_next):
    """Always serve the current UI files (avoids a stale app.js after an update)."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.on_event("startup")
def warm_up():
    """Check the local LLM (starting Ollama if needed) and load both models in the background."""
    def _llm():
        h = extractor.ensure_running(log=lambda m: print(m, flush=True))
        if not h["installed"]:
            return
        try:
            extractor.extract("Aspirin 300 mg PO.")
            print(f"[llm] {config.LLM_MODEL} ready", flush=True)
        except Exception as e:
            print(f"[llm] warm-up failed: {e}", flush=True)

    def _asr():
        try:
            asr.get_model()
            print(f"[asr] {asr.health()} ready", flush=True)
        except Exception as e:
            print(f"[asr] not loaded: {e}", flush=True)

    threading.Thread(target=_llm, daemon=True).start()
    threading.Thread(target=_asr, daemon=True).start()


@app.get("/api/health")
def health():
    return {"llm": extractor.health(), "asr": asr.health(), "audit": audit.verify(), "device_id": config.DEVICE_ID}


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...), language: str = Form("auto")):
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty audio")
    try:
        return asr.transcribe(data, language)
    except Exception as e:
        raise HTTPException(500, f"transcription failed: {e}")


@app.post("/api/extract")
def extract(req: ExtractRequest):
    if not req.text.strip():
        raise HTTPException(400, "empty note")
    ref = datetime.fromisoformat(req.reference_time).astimezone() if req.reference_time else datetime.now().astimezone()
    try:
        raw, info = extractor.extract(req.text)
    except extractor.LLMUnavailable as e:
        raise HTTPException(503, str(e))
    record = normalize(raw, req.text, ref)
    return {"record": record.model_dump(), "raw": raw.model_dump(), "model_info": info}


@app.post("/api/records")
def save(req: SaveRequest):
    rec = audit.append(
        incident_id=req.incident_id, responder_id=req.responder_id, transcript=req.transcript,
        language=req.language, audio_sha256=req.audio_sha256,
        draft=req.draft.model_dump(), final=req.final.model_dump(), model_info=req.model_info,
    )
    return {"id": rec["id"], "seq": rec["seq"], "hash": rec["hash"], "edits": rec["edits"]}


@app.get("/api/records")
def list_records():
    return [
        {k: r[k] for k in ("id", "seq", "created_at", "incident_id", "responder_id", "hash")}
        | {"meds": len(r["final"]["medications"]), "procs": len(r["final"]["procedures"]), "edits": len(r["edits"] or [])}
        for r in audit.list_records()
    ]


def _get(record_id: str) -> dict:
    rec = audit.get(record_id)
    if not rec:
        raise HTTPException(404, "record not found")
    return rec


@app.get("/api/records/{record_id}")
def get_record(record_id: str):
    return _get(record_id)


@app.get("/api/records/{record_id}/fhir")
def get_fhir(record_id: str):
    return to_fhir_lite(record_id, _get(record_id))


@app.get("/records/{record_id}/report", response_class=HTMLResponse)
def get_report(record_id: str):
    return report.render(_get(record_id))


@app.get("/api/audit/verify")
def verify():
    return audit.verify()


app.mount("/", StaticFiles(directory=config.ROOT / "app" / "static", html=True), name="static")
