"""Clinical entity extraction with a local LLM (Ollama) constrained to a JSON schema."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from . import config
from .lexicon import HINDI_GLOSSARY, PROCEDURES
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
- patient: age (as spoken, e.g. "54"), sex, weight_kg if stated.
- chief_complaint: the patient's main problem, translated LITERALLY into short English from what the note says
  (use the glossary provided with the note for Hindi words, e.g. "ghutno ka dard" -> "knee pain").
  Never substitute a different body part or symptom, never add symptoms that were not said; null if not stated.
  chief_complaint_evidence: the exact words from the note it came from.
Return JSON only.""".replace("{procedures}", ", ".join(PROCEDURES))

FEW_SHOT = [
    (
        "54 year old male chest pain, 300 mg aspirin chewed at 10:05, NTG 0.4 mg sublingual, "
        "BP 150/90, SpO2 93 percent, oxygen 4 litres nasal cannula. Patient refused morphine.",
        {
            "patient": {"age": "54", "sex": "male", "weight_kg": None},
            "chief_complaint": "chest pain",
            "chief_complaint_evidence": "chest pain",
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
            "chief_complaint": "breathlessness after bee sting",
            "chief_complaint_evidence": "bee sting ke baad saans phool rahi hai",
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
            "chief_complaint": "unconscious, low blood sugar",
            "chief_complaint_evidence": "मरीज़ बेहोश है, शुगर 45 आई",
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

FEW_SHOT.append((
    "Dadi ji, 72 saal, seedhi se phisal gayi, kamar mein dard aur pair mein sujan. Splint lagaya.",
    {
        "patient": {"age": "72", "sex": "female", "weight_kg": None},
        "chief_complaint": "lower back pain and leg swelling after slipping on stairs",
        "chief_complaint_evidence": "seedhi se phisal gayi, kamar mein dard aur pair mein sujan",
        "medications": [],
        "procedures": [
            {"name": "Splinting", "details": None, "site": None, "time": None, "outcome": None, "evidence": "Splint lagaya"},
        ],
        "vitals": [],
    },
))

_SCHEMA = LLMExtraction.model_json_schema()
_GLOSSARY_IDX = {syn: eng for eng, syns in HINDI_GLOSSARY.items() for syn in syns}


def glossary_hints(text: str) -> list[str]:
    """Hindi/Hinglish clinical words present in the note, with their English meaning."""
    low = text.lower()
    found = {}
    for syn, eng in _GLOSSARY_IDX.items():
        if re.search(rf"(?<!\w){re.escape(syn)}(?!\w)", low):
            found[syn] = eng
    return [f"{k} = {v}" for k, v in found.items()]


class LLMUnavailable(RuntimeError):
    pass


class LLMOutputError(ValueError):
    """The model answered, but not with a valid extraction."""

    def __init__(self, message: str, content: str):
        super().__init__(message)
        self.content = content


def _user_turn(text: str, extra: list[str] | None = None) -> dict:
    hints = glossary_hints(text)
    glossary = f"\n\nGlossary for Hindi words in this note: {'; '.join(hints)}" if hints else ""
    notes = "".join(f"\n{e}" for e in extra or [])
    return {"role": "user", "content": f"Note:\n{text}{glossary}{notes}"}


def _messages(text: str, hints: list[str] | None = None, repair: tuple[str, str] | None = None) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for note, out in FEW_SHOT:
        msgs.append(_user_turn(note))
        msgs.append({"role": "assistant", "content": json.dumps(out, ensure_ascii=False)})
    msgs.append(_user_turn(text, hints))
    if repair:  # show the model its invalid answer and the validation error, once
        bad, err = repair
        msgs.append({"role": "assistant", "content": bad[:4000]})
        msgs.append({"role": "user", "content": f"That output was invalid: {err[:600]}\n"
                                                "Return the corrected JSON only, following the schema exactly."})
    return msgs


def extract(text: str, model: str | None = None, hints: list[str] | None = None,
            repair: tuple[str, str] | None = None) -> tuple[LLMExtraction, dict]:
    """One model call. `hints` are extra lines appended to the note; `repair` = (bad output, error)."""
    model = model or config.LLM_MODEL
    payload = {
        "model": model,
        "messages": _messages(text, hints, repair),
        "format": _SCHEMA,
        "stream": False,
        "keep_alive": -1,  # stay resident while the app runs (fits in 8 GB VRAM alongside Whisper)
        "options": {"temperature": 0, "num_ctx": 4096},
    }
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=payload, timeout=config.LLM_TIMEOUT_S)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMUnavailable(f"Local LLM not reachable at {config.OLLAMA_URL} ({e}). Is Ollama running with model '{model}'?") from e
    content = r.json()["message"]["content"]
    try:
        ext = LLMExtraction.model_validate_json(content)
    except ValueError as e:  # pydantic ValidationError and JSON decode errors
        raise LLMOutputError(str(e), content) from e
    return ext, {"llm_model": model, "llm_latency_s": round(time.perf_counter() - t0, 2)}


def _matches(name: str) -> bool:
    return name == config.LLM_MODEL or name.split(":")[0] == config.LLM_MODEL


def health() -> dict:
    """running: Ollama reachable; installed: model pulled; loaded: model resident in memory (fast)."""
    out = {"ok": False, "model": config.LLM_MODEL, "running": False, "installed": False, "loaded": False}
    try:
        tags = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=3).json().get("models", [])
        out["running"] = True
        out["installed"] = any(_matches(m["name"]) for m in tags)
        ps = httpx.get(f"{config.OLLAMA_URL}/api/ps", timeout=3).json().get("models", [])
        out["loaded"] = any(_matches(m["name"]) for m in ps)
    except httpx.HTTPError as e:
        out["error"] = f"Ollama not running at {config.OLLAMA_URL} ({e})"
        return out
    if not out["installed"]:
        out["error"] = f"model not installed - run: ollama pull {config.LLM_MODEL}"
    out["ok"] = out["installed"]
    return out


def _find_ollama() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    for cand in (Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
                 Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")):
        if cand.is_file():
            return str(cand)
    return None


def ensure_running(log=print, wait_s: float = 600) -> dict:
    """Make sure the local Ollama server is up (start it if needed) and report the model's status."""
    h = health()
    if not h["running"]:
        exe = _find_ollama()
        if not exe:
            log("[llm] Ollama is not installed. Install it from ollama.com, then: ollama pull " + config.LLM_MODEL)
            return h
        log(f"[llm] Ollama not running - starting '{exe} serve'")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=(os.name != "nt"))
        # Ollama can take minutes to come up (e.g. GPU discovery after an unclean exit), so keep waiting.
        t0 = time.monotonic()
        next_log = 15
        while time.monotonic() - t0 < wait_s and not (h := health())["running"]:
            time.sleep(2)
            if time.monotonic() - t0 >= next_log:
                log(f"[llm] still waiting for Ollama to start ({next_log}s)...")
                next_log += 15
        if not h["running"]:
            log(f"[llm] Ollama did not start within {wait_s:.0f}s")
            return h
        log("[llm] Ollama started")
    else:
        log(f"[llm] Ollama is running at {config.OLLAMA_URL}")
    if h["installed"]:
        log(f"[llm] model {config.LLM_MODEL} installed" + (" and loaded" if h["loaded"] else " - loading into memory"))
    else:
        log(f"[llm] model {config.LLM_MODEL} NOT installed - run: ollama pull {config.LLM_MODEL}")
    return h
