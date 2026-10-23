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
