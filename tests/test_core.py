import sqlite3
from datetime import datetime

import pytest

from app import audit, config
from app.normalize import (canonical_drug, canonical_procedure, canonical_route, canonical_unit,
                           dose_warning, evidence_in_transcript, normalize, resolve_time)
from app.schemas import LLMExtraction

REF = datetime.fromisoformat("2026-10-23T14:40:00+05:30")


@pytest.mark.parametrize("raw,canon", [
    ("Adrenaline", "epinephrine"), ("epi", "epinephrine"), ("एड्रेनालिन", "epinephrine"),
    ("Asthalin", "albuterol"), ("NS", "sodium chloride 0.9%"), ("Ondem", "ondansetron"),
    ("D50", "glucose"), ("ondansteron", "ondansetron"),
])
def test_drug_synonyms(raw, canon):
    assert canonical_drug(raw)[0] == canon


def test_unknown_drug():
    assert canonical_drug("unobtainium")[0] is None


@pytest.mark.parametrize("raw,route", [
    ("sublingual", "SL"), ("IM", "IM"), ("nas mein", "IV"), ("chewed", "PO"), ("nebulised", "INH"),
    ("in", "IN"), ("IM in thigh", "IM"),
])
def test_routes(raw, route):
    assert canonical_route(raw) == route


def test_units():
    assert canonical_unit("micrograms") == "mcg"
    assert canonical_unit("litres per minute") == "L/min"
    assert canonical_unit("मिलीग्राम") == "mg"


def test_procedures():
    assert canonical_procedure("cannula lagaya")[0] == "IV cannulation"
    assert canonical_procedure("chest compressions")[0] == "CPR"


@pytest.mark.parametrize("raw,expected", [
    ("14:32", "2026-10-23T14:32:00+05:30"),
    ("2:30 pm", "2026-10-23T14:30:00+05:30"),
    ("5 minute pehle", "2026-10-23T14:35:00+05:30"),
    ("10 मिनट पहले", "2026-10-23T14:30:00+05:30"),
    ("1 hour ago", "2026-10-23T13:40:00+05:30"),
    ("1405 hrs", "2026-10-23T14:05:00+05:30"),
    ("1042", "2026-10-23T10:42:00+05:30"),
    ("at 1042", "2026-10-23T10:42:00+05:30"),
    ("23:50", "2026-10-22T23:50:00+05:30"),
])
def test_resolve_time(raw, expected):
    assert resolve_time(raw, REF)[0] == expected


def test_missing_time_defaults_with_warning():
    iso, warn = resolve_time(None, REF)
    assert iso == REF.isoformat() and "not stated" in warn


def test_dose_warning_catches_decimal_error():
    assert dose_warning("epinephrine", 3, "mg")
    assert dose_warning("epinephrine", 0.3, "mg") is None
    assert dose_warning("fentanyl", 0.05, "mg") is None  # 50 mcg


def test_evidence_check():
    t = "Gave aspirin 300 mg chewed at 10:42."
    assert evidence_in_transcript("aspirin 300 mg chewed", t)
    assert not evidence_in_transcript("morphine 5 mg IV", t)


def test_normalize_flags_hallucination():
    ext = LLMExtraction.model_validate({"medications": [
        {"drug": "morphine", "dose_value": 5, "dose_unit": "mg", "route": "IV", "evidence": "morphine 5 mg IV"},
    ]})
    rec = normalize(ext, "Aspirin 300 mg given.", REF)
    assert not rec.medications[0].evidence_found
    assert any("hallucination" in w for w in rec.medications[0].warnings)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")


def _append(n):
    rec = {"reference_time": REF.isoformat(), "medications": [{"drug": "aspirin", "dose_value": 300}]}
    final = {**rec, "medications": [{"drug": "aspirin", "dose_value": 325}]}
    return audit.append(incident_id=f"INC-{n}", responder_id="EMT-1", transcript="aspirin 300", language="en",
                        audio_sha256=None, draft=rec, final=final, model_info={})


def test_audit_chain_and_tamper(fresh_db):
    r1, r2 = _append(1), _append(2)
    assert r2["prev_hash"] == r1["hash"]
    assert r1["edits"] == [{"field": "medications[0].dose_value", "machine": 300, "human": 325}]
    assert audit.verify()["ok"]
    with sqlite3.connect(config.DB_PATH) as c:
        c.execute("UPDATE records SET transcript = 'aspirin 3000' WHERE seq = 1")
    v = audit.verify()
    assert not v["ok"] and v["broken_at"] == r1["id"]


def test_audit_detects_deleted_row(fresh_db):
    _append(1); _append(2); _append(3)
    with sqlite3.connect(config.DB_PATH) as c:
        c.execute("DELETE FROM records WHERE seq = 2")
    assert not audit.verify()["ok"]


def test_glossary_hints():
    from app.extractor import glossary_hints
    assert "ghutno = knee" in glossary_hints("Patient ko ghutno ka dard hai")
    assert "पेट = abdomen / stomach" in glossary_hints("पेट में दर्द")
    assert glossary_hints("Aspirin 300 mg given") == []


def test_relative_time_uses_recording_time():
    rec_time = datetime.fromisoformat("2026-10-23T09:00:00+05:30")
    ext = LLMExtraction.model_validate({"medications": [
        {"drug": "aspirin", "dose_value": 300, "dose_unit": "mg", "route": "PO", "time": "10 min ago", "evidence": "aspirin 300 mg"},
        {"drug": "ondansetron", "dose_value": 4, "dose_unit": "mg", "route": "IV", "evidence": "ondansetron 4 mg"},
    ]})
    rec = normalize(ext, "aspirin 300 mg 10 min ago, ondansetron 4 mg", rec_time)
    assert rec.medications[0].time == "2026-10-23T08:50:00+05:30"
    assert rec.medications[1].time == "2026-10-23T09:00:00+05:30"
    assert "recording time" in rec.medications[1].warnings[0]


def test_ungrounded_complaint_is_flagged():
    ext = LLMExtraction.model_validate({"chief_complaint": "chest pain", "chief_complaint_evidence": "seene mein dard"})
    assert normalize(ext, "ghutno ka dard", REF).chief_complaint_warnings
    ext = LLMExtraction.model_validate({"chief_complaint": "knee pain", "chief_complaint_evidence": "ghutno ka dard"})
    assert not normalize(ext, "ghutno ka dard", REF).chief_complaint_warnings


# Exact failure seen in testing: the model dropped "dolo" from its quote and called it ondansetron.
SPOKEN = ("50 sasal ke aadmi kumar mein bhoat daad cd se gir gaye. 10 minute pelle dolo 650 mg muhse diya. "
          "Right hand mein piece gage cannula lagaya aur ondansetron 4 mg IV diya. BP 130 x 85, Pulse 102.")


def test_drug_trimmed_from_quote_is_recovered_from_sentence():
    ext = LLMExtraction.model_validate({"medications": [
        {"drug": "ondansetron", "dose_value": 650, "dose_unit": "mg", "route": "PO",
         "time": "10 minute pelle", "evidence": "10 minute pelle 650 mg muhse diya"},
        {"drug": "ondansetron", "dose_value": 4, "dose_unit": "mg", "route": "IV",
         "evidence": "Right hand mein piece gage cannula lagaya aur ondansetron 4 mg IV diya"},
    ]})
    rec = normalize(ext, SPOKEN, REF)
    para, ond = rec.medications
    assert para.drug == "acetaminophen" and para.dose_value == 650
    assert para.time == "2026-10-23T14:30:00+05:30"           # "10 minute pelle" before recording time
    assert not any("exceeds" in w for w in para.warnings)
    assert ond.drug == "ondansetron"
    assert any(p.name == "IV cannulation" for p in rec.procedures)  # "piece gage" ~ gauge


def test_decimal_point_does_not_split_sentences():
    from app.normalize import split_sentences
    assert split_sentences("Adrenaline 0.5 mg IM diya. NS 500 ml start kiya") == [
        "Adrenaline 0.5 mg IM diya", " NS 500 ml start kiya"]


def test_report_splits_addenda_and_tabulates_models():
    from app.report import dictation_parts, model_rows
    parts = dictation_parts("aspirin 300 mg\n\n[Addendum 14:47] Ondem 4 mg IV diya")
    assert [p["label"] for p in parts] == ["Original dictation", "Addendum 1 · 14:47"]
    assert parts[1]["text"] == "Ondem 4 mg IV diya"
    rows = dict(model_rows({"asr_model": "large-v3-turbo", "asr_device": "cuda", "llm_model": "qwen2.5:7b",
                            "llm_latency_s": 7.5, "harness": {"mode": "llm", "checks": {"Negation": "pass", "Dose grounding": "flag"}}}))
    assert rows["Speech-to-text"] == "large-v3-turbo on cuda"
    assert rows["Language model"] == "qwen2.5:7b · 7.5 s"
    assert rows["Harness checks"] == "1 passed, 0 fixed, 1 flagged (Dose grounding)"
