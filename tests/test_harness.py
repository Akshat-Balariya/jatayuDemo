"""Harness tests with a fake LLM: each failure mode the harness guards against, reproduced exactly."""
from datetime import datetime

import pytest

from app import extractor, harness
from app.schemas import LLMExtraction

REF = datetime.fromisoformat("2026-10-24T09:00:00+05:30")


def fake_llm(monkeypatch, *outputs):
    """Make extractor.extract return (or raise) each item of `outputs` in turn; records the calls."""
    calls = []

    def _extract(text, model=None, hints=None, repair=None):
        calls.append({"hints": hints, "repair": repair})
        out = outputs[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return LLMExtraction.model_validate(out), {"llm_model": "fake", "llm_latency_s": 0.01}

    monkeypatch.setattr(extractor, "extract", _extract)
    return calls


def med(drug, dose, unit, route, evidence, time=None):
    return {"drug": drug, "dose_value": dose, "dose_unit": unit, "route": route, "time": time, "evidence": evidence}


def check(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


# ---------- scan ----------

def test_scan_finds_brands_misspellings_and_negation():
    found = harness.scan_drugs("Dolo 650 diya. Paracitamol bhi. Patient ne morphine lene se mana kar diya. "
                               "Allergic to diclofenac. Takes aspirin daily.")
    by = {(m.canon, m.status, m.fuzzy) for m in found}
    assert ("acetaminophen", "given", False) in by          # Dolo is a brand of paracetamol
    assert ("acetaminophen", "given", True) in by           # "Paracitamol" is a misspelling
    assert ("morphine", "negated", False) in by
    assert ("aspirin", "context", False) in by               # home medication, not given now


def test_hints_tell_the_model_what_the_lexicon_saw():
    hints = harness.drug_hints(harness.scan_drugs("Ondem 4 mg IV diya. Morphine refused."))
    assert any("ondem = ondansetron" in h for h in hints)
    assert any("NOT given" in h and "morphine" in h for h in hints)


def test_measurements_are_not_administrations():
    found = harness.scan_drugs("Blood glucose 42. Dextrose 25 g IV diya.")
    assert [harness.administered(m) for m in found] == [False, True]


# ---------- the call ----------

def test_hints_are_passed_to_the_model(monkeypatch):
    text = "Ondem 4 mg IV diya."
    calls = fake_llm(monkeypatch, {"medications": [med("ondem", 4, "mg", "IV", "Ondem 4 mg IV diya")]})
    _, _, _, report = harness.extract_record(text, REF)
    assert report["mode"] == "llm" and any("ondansetron" in h for h in calls[0]["hints"])


def test_invalid_output_is_repaired_on_retry(monkeypatch):
    text = "Aspirin 300 mg chewed."
    calls = fake_llm(monkeypatch, extractor.LLMOutputError("bad json", "{oops"),
                     {"medications": [med("aspirin", 300, "mg", "PO", "Aspirin 300 mg chewed")]})
    rec, _, _, report = harness.extract_record(text, REF)
    assert report["mode"] == "llm-retry" and calls[1]["repair"][0] == "{oops"
    assert check(report, "Output schema")["status"] == "fixed"
    assert [m.drug for m in rec.medications] == ["aspirin"]


def test_falls_back_to_rules_when_the_model_is_down(monkeypatch):
    text = "62 year old male. Aspirin 300 mg chewed at 10:42. Nitroglycerin 0.4 mg sublingual. BP 140/90, pulse 96."
    fake_llm(monkeypatch, extractor.LLMUnavailable("ollama down"))
    rec, _, _, report = harness.extract_record(text, REF)
    assert report["mode"] == "rules-fallback" and check(report, "Output schema")["status"] == "flag"
    meds = {(m.drug, m.dose_value, m.route) for m in rec.medications}
    assert meds == {("aspirin", 300, "PO"), ("nitroglycerin", 0.4, "SL")}
    assert {(v.type, v.value) for v in rec.vitals} >= {("BP", "140/90"), ("HR", "96")}
    assert rec.patient.age == "62" and rec.patient.sex == "male"


# ---------- post checks ----------

def test_missed_medicine_is_recovered_and_flagged(monkeypatch):
    text = "Aspirin 300 mg chewed. Ondem 4 mg IV diya."
    fake_llm(monkeypatch, {"medications": [med("aspirin", 300, "mg", "PO", "Aspirin 300 mg chewed")]})
    rec, _, _, report = harness.extract_record(text, REF)
    ond = next(m for m in rec.medications if m.drug == "ondansetron")
    assert (ond.dose_value, ond.route) == (4, "IV") and "missed by the model" in ond.warnings[0]
    assert check(report, "Medicine recall")["status"] == "fixed"


def test_negated_medicine_is_flagged(monkeypatch):
    text = "Patient refused morphine. Paracetamol 1 g IV diya."
    fake_llm(monkeypatch, {"medications": [med("morphine", 5, "mg", "IV", "Patient refused morphine"),
                                           med("paracetamol", 1, "g", "IV", "Paracetamol 1 g IV diya")]})
    rec, _, _, report = harness.extract_record(text, REF)
    morphine = next(m for m in rec.medications if m.drug == "morphine")
    assert "NOT given" in morphine.warnings[0]
    assert check(report, "Negation")["status"] == "flag"


def test_dose_not_in_the_note_is_flagged(monkeypatch):
    text = "Adrenaline 0.5 mg IM thigh mein diya."
    fake_llm(monkeypatch, {"medications": [med("adrenaline", 5, "mg", "IM", "Adrenaline 0.5 mg IM thigh mein diya")]})
    rec, _, _, report = harness.extract_record(text, REF)
    assert any("not stated in the note" in w for w in rec.medications[0].warnings)
    assert check(report, "Dose grounding")["status"] == "flag"


def test_mcg_mg_conversion_is_not_flagged(monkeypatch):
    text = "Fentanyl 50 mcg IV."
    fake_llm(monkeypatch, {"medications": [med("fentanyl", 0.05, "mg", "IV", "Fentanyl 50 mcg IV")]})
    _, _, _, report = harness.extract_record(text, REF)
    assert check(report, "Dose grounding")["status"] == "pass"


def test_unusual_route_is_flagged(monkeypatch):
    text = "Aspirin 300 mg given."
    fake_llm(monkeypatch, {"medications": [med("aspirin", 300, "mg", "IV", "Aspirin 300 mg given")]})
    rec, _, _, report = harness.extract_record(text, REF)
    assert any("unusual for aspirin" in w for w in rec.medications[0].warnings)


def test_time_after_recording_is_flagged(monkeypatch):
    text = "Aspirin 300 mg chewed at 09:40."   # recorded at 09:00
    fake_llm(monkeypatch, {"medications": [med("aspirin", 300, "mg", "PO", "Aspirin 300 mg chewed at 09:40", "09:40")]})
    rec, _, _, report = harness.extract_record(text, REF)
    assert any("after the recording" in w for w in rec.medications[0].warnings)


def test_time_long_before_recording_is_flagged(monkeypatch):
    text = "Aspirin 300 mg chewed at 11:30."   # 2.5 h after a 09:00 recording, so read as yesterday
    fake_llm(monkeypatch, {"medications": [med("aspirin", 300, "mg", "PO", "Aspirin 300 mg chewed at 11:30", "11:30")]})
    rec, _, _, _ = harness.extract_record(text, REF)
    assert any("more than 12 h before" in w for w in rec.medications[0].warnings)


def test_duplicates_removed_unless_repeated(monkeypatch):
    once = "Naloxone 2 mg intranasal."
    item = med("naloxone", 2, "mg", "IN", once)
    fake_llm(monkeypatch, {"medications": [item, item]})
    rec, _, _, report = harness.extract_record(once, REF)
    assert len(rec.medications) == 1 and check(report, "Duplicates")["status"] == "fixed"

    twice = "Naloxone 2 mg intranasal, repeated after 3 minutes."
    item = med("naloxone", 2, "mg", "IN", twice)
    fake_llm(monkeypatch, {"medications": [item, item]})
    rec, _, _, _ = harness.extract_record(twice, REF)
    assert len(rec.medications) == 2


def test_misspelt_name_is_grounded_not_flagged_as_unnamed(monkeypatch):
    text = "Paracitamol 650 mg muhse diya."
    fake_llm(monkeypatch, {"medications": [med("acetaminophen", 650, "mg", "PO", "Paracitamol 650 mg muhse diya")]})
    rec, _, _, report = harness.extract_record(text, REF)
    w = rec.medications[0].warnings
    assert not any("not named anywhere" in x for x in w) and any("misspelling 'paracitamol'" in x for x in w)
    assert check(report, "Medicine names")["status"] == "pass"


def test_drug_unknown_to_the_lexicon_is_flagged(monkeypatch):
    text = "Gave unobtainium 5 mg IV."
    fake_llm(monkeypatch, {"medications": [med("unobtainium", 5, "mg", "IV", "Gave unobtainium 5 mg IV")]})
    _, _, _, report = harness.extract_record(text, REF)
    assert check(report, "Medicine names")["status"] == "flag"


def test_harness_summary_goes_into_model_info(monkeypatch):
    fake_llm(monkeypatch, {"medications": [med("aspirin", 300, "mg", "PO", "Aspirin 300 mg chewed")]})
    _, _, info, _ = harness.extract_record("Aspirin 300 mg chewed.", REF)
    assert info["harness"]["mode"] == "llm" and info["harness"]["checks"]["Negation"] == "pass"


def test_phantom_item_from_a_reading_is_removed(monkeypatch):
    text = "D50 25 grams IV push. Recheck sugar 110."
    fake_llm(monkeypatch, {"medications": [med("glucose", 25, "g", "IV", "D50 25 grams IV push"),
                                           med("glucose", None, None, None, "Recheck sugar 110")]})
    rec, _, _, report = harness.extract_record(text, REF)
    assert len(rec.medications) == 1 and check(report, "Phantom items")["status"] == "fixed"


def test_phantom_item_without_a_twin_is_flagged(monkeypatch):
    text = "Cannula lagaya."
    fake_llm(monkeypatch, {"medications": [med("normal saline", None, None, "IV", "Cannula lagaya")]})
    rec, _, _, report = harness.extract_record(text, REF)
    assert "does not name this medicine" in rec.medications[0].warnings[0]
    assert check(report, "Phantom items")["status"] == "flag"
