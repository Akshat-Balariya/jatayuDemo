"""Clinical entity extraction with a local LLM (Ollama) constrained to a JSON schema."""
from __future__ import annotations

import json
import time

import httpx

from . import config
from .lexicon import PROCEDURES
from .schemas import LLMExtraction

SYSTEM_PROMPT = """You are a clinical documentation assistant for ambulance / first-responder crews in India.
You receive a responder's on-scene note. It may be in English, Hindi (Devanagari), or Hinglish
(Hindi written in Latin script mixed with English), and may contain speech-recognition errors.

Extract ONLY what was actually administered or performed on the patient. Rules:
- medications: every drug, IV fluid (NS, RL, normal saline, dextrose) and oxygen GIVEN. Oxygen goes in
  medications (flow in L/min, route INH) AND in procedures as "Oxygen therapy".
  If a dose was given more than once ("repeated", "twice", "do baar", "phir se"), list EACH administration separately.
  Write `drug` exactly as named in the note (brand names like Avil, Ondem, Asthalin are fine) - never substitute another drug.
  `dose_value` is a number ("point three" -> 0.3, "aadha" -> 0.5, "do sau" -> 200). `dose_unit` like mg, mcg, g, mL, L/min, units, puffs.
  `route` as an abbreviation: IV, IM, IO, SC, SL, PO, IN (intranasal, "naak se"), INH (inhaled/nebulised/oxygen),
  ET, PR, TOP. Infer from context ("chewed"/"chaba ke" -> PO, "under the tongue"/"jeebh ke neeche" -> SL,
  "nas mein"/"नस में" -> IV, "thigh mein injection" -> IM).
- procedures: physical interventions performed. Use these names when they apply: {procedures}.
  A cannula/"gauge" mentioned means "IV cannulation". A measured blood sugar means "Glucose check".
  "patti"/bandage means "Wound dressing". Do NOT list drug administrations (nebulization, fluids, injections)
  or outcomes (ROSC, seizure stopped) as procedures.
- vitals: HR, BP, SpO2, RR, GCS, Temp, BGL, Pain with value as spoken (BP as "120/80").
- `time`: copy the time expression exactly as spoken ("14:32", "2:30 pm", "5 minute pehle", "10 min ago"), else null. Never invent times.
- `evidence`: copy the exact short phrase from the note that supports the item, in the note's original script.
  If nothing in the note supports an item, do not output that item at all.
- Do NOT include drugs that were only considered, refused, withheld, or are the patient's home medications.
- Do NOT guess values that were not said; use null.
- patient: age (as spoken, e.g. "54"), sex, weight_kg if stated. chief_complaint: short English phrase.
Return JSON only.""".replace("{procedures}", ", ".join(PROCEDURES))

FEW_SHOT = [
    (
        "54 year old male chest pain, 300 mg aspirin chewed at 10:05, NTG 0.4 mg sublingual, "
        "BP 150/90, SpO2 93 percent, oxygen 4 litres nasal cannula. Patient refused morphine.",
        {
            "patient": {"age": "54", "sex": "male", "weight_kg": None},
            "chief_complaint": "chest pain",
            "medications": [
                {"drug": "aspirin", "dose_value": 300, "dose_unit": "mg", "route": "PO", "site": None, "time": "10:05", "evidence": "300 mg aspirin chewed at 10:05"},
                {"drug": "nitroglycerin", "dose_value": 0.4, "dose_unit": "mg", "route": "SL", "site": None, "time": None, "evidence": "NTG 0.4 mg sublingual"},
                {"drug": "oxygen", "dose_value": 4, "dose_unit": "L/min", "route": "INH", "site": None, "time": None, "evidence": "oxygen 4 litres nasal cannula"},
            ],
            "procedures": [
                {"name": "Oxygen therapy", "details": "nasal cannula 4 L/min", "site": None, "time": None, "outcome": None, "evidence": "oxygen 4 litres nasal cannula"},
            ],
            "vitals": [
                {"type": "BP", "value": "150/90", "unit": "mmHg", "time": None, "evidence": "BP 150/90"},
                {"type": "SpO2", "value": "93", "unit": "%", "time": None, "evidence": "SpO2 93 percent"},
            ],
        },
    ),
    (
        "Mahila, umar lagbhag 30, bee sting ke baad saans phool rahi hai. 5 minute pehle adrenaline "
        "0.5 mg IM right thigh mein diya. Left hand mein 20 gauge cannula lagaya, NS start kiya.",
        {
            "patient": {"age": "30", "sex": "female", "weight_kg": None},
            "chief_complaint": "anaphylaxis after bee sting, breathlessness",
            "medications": [
                {"drug": "adrenaline", "dose_value": 0.5, "dose_unit": "mg", "route": "IM", "site": "right thigh", "time": "5 minute pehle", "evidence": "5 minute pehle adrenaline 0.5 mg IM right thigh mein diya"},
                {"drug": "normal saline", "dose_value": None, "dose_unit": None, "route": "IV", "site": "left hand", "time": None, "evidence": "NS start kiya"},
            ],
            "procedures": [
                {"name": "IV cannulation", "details": "20 gauge", "site": "left hand", "time": None, "outcome": None, "evidence": "Left hand mein 20 gauge cannula lagaya"},
            ],
            "vitals": [],
        },
    ),
    (
        "मरीज़ बेहोश है, शुगर 45 आई। 25 ग्राम डेक्सट्रोज़ नस में दिया 14:20 पर।",
        {
            "patient": {"age": None, "sex": "unknown", "weight_kg": None},
            "chief_complaint": "unconscious, hypoglycaemia",
            "medications": [
                {"drug": "dextrose", "dose_value": 25, "dose_unit": "g", "route": "IV", "site": None, "time": "14:20", "evidence": "25 ग्राम डेक्सट्रोज़ नस में दिया 14:20 पर"},
            ],
            "procedures": [
                {"name": "Glucose check", "details": None, "site": None, "time": None, "outcome": "45", "evidence": "शुगर 45 आई"},
            ],
            "vitals": [
                {"type": "BGL", "value": "45", "unit": "mg/dL", "time": None, "evidence": "शुगर 45 आई"},
            ],
        },
    ),
]

_SCHEMA = LLMExtraction.model_json_schema()


class LLMUnavailable(RuntimeError):
    pass


def _messages(text: str) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for note, out in FEW_SHOT:
        msgs.append({"role": "user", "content": f"Note:\n{note}"})
        msgs.append({"role": "assistant", "content": json.dumps(out, ensure_ascii=False)})
    msgs.append({"role": "user", "content": f"Note:\n{text}"})
    return msgs


def extract(text: str, model: str | None = None) -> tuple[LLMExtraction, dict]:
    model = model or config.LLM_MODEL
    payload = {
        "model": model,
        "messages": _messages(text),
        "format": _SCHEMA,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 4096},
    }
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=payload, timeout=config.LLM_TIMEOUT_S)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMUnavailable(f"Local LLM not reachable at {config.OLLAMA_URL} ({e}). Is Ollama running with model '{model}'?") from e
    content = r.json()["message"]["content"]
    ext = LLMExtraction.model_validate_json(content)
    return ext, {"llm_model": model, "llm_latency_s": round(time.perf_counter() - t0, 2)}


def health() -> dict:
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=3)
        names = [m["name"] for m in r.json().get("models", [])]
        present = any(n == config.LLM_MODEL or n.split(":")[0] == config.LLM_MODEL for n in names)
        return {"ok": present, "model": config.LLM_MODEL, "available": names}
    except httpx.HTTPError as e:
        return {"ok": False, "model": config.LLM_MODEL, "error": str(e)}
