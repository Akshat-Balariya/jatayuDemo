"""Deterministic post-processing of LLM output.

The LLM does the language understanding; this module makes the result trustworthy:
canonical names + codes, unit/route normalisation, absolute timestamps,
evidence verification against the transcript, and safety warnings.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher, get_close_matches

from .lexicon import DRUGS, MASS_TO_MG, PROCEDURES, ROUTES, UNITS
from .schemas import ClinicalRecord, LLMExtraction, Medication, Procedure, Vital


def _clean(s: str) -> str:
    s = unicodedata.normalize("NFC", s).lower().strip()
    return re.sub(r"\s+", " ", s)


def _build_index(table: dict[str, list[str]]) -> dict[str, str]:
    idx = {}
    for canon, syns in table.items():
        idx[_clean(canon)] = canon
        for s in syns:
            idx[_clean(s)] = canon
    return idx


# Short synonyms that are also ordinary words; only matched when they are the whole string.
_AMBIGUOUS = {"in", "et", "pr", "pause"}

_DRUG_IDX =_build_index({k: v[1] for k, v in DRUGS.items()})
_PROC_IDX = _build_index({k: v[1] for k, v in PROCEDURES.items()})
_ROUTE_IDX = _build_index(ROUTES)
_UNIT_IDX = _build_index(UNITS)


def _lookup(raw: str | None, idx: dict[str, str], fuzzy: bool = True) -> tuple[str | None, bool]:
    """Return (canonical, exact). Tries exact, then whole-word containment, then fuzzy."""
    if not raw:
        return None, True
    key = _clean(raw)
    if key in idx:
        return idx[key], True
    # longest synonym that appears as a whole word inside the raw string
    for syn in sorted(idx, key=len, reverse=True):
        if len(syn) >= 2 and syn not in _AMBIGUOUS and re.search(rf"(?<!\w){re.escape(syn)}(?!\w)", key):
            return idx[syn], True
    if fuzzy:
        close = get_close_matches(key, list(idx), n=1, cutoff=0.82)
        if close:
            return idx[close[0]], False
    return None, False


def mentions(text: str | None, idx: dict[str, str]) -> set[str]:
    """All canonical entries whose synonyms appear as whole words in `text`."""
    if not text:
        return set()
    t = _clean(text)
    return {canon for syn, canon in idx.items()
            if len(syn) >= 2 and syn not in _AMBIGUOUS and re.search(rf"(?<!\w){re.escape(syn)}(?!\w)", t)}


def canonical_drug(raw: str | None) -> tuple[str | None, str | None, bool]:
    canon, exact = _lookup(raw, _DRUG_IDX)
    if canon is None:
        return None, None, False
    return canon, DRUGS[canon][0], exact


def canonical_procedure(raw: str | None) -> tuple[str | None, str | None, bool]:
    canon, exact = _lookup(raw, _PROC_IDX)
    if canon is None:
        return None, None, False
    return canon, PROCEDURES[canon][0], exact


def canonical_route(raw: str | None) -> str | None:
    return _lookup(raw, _ROUTE_IDX, fuzzy=False)[0] if raw else None


def canonical_unit(raw: str | None) -> str | None:
    if not raw:
        return None
    return _lookup(raw, _UNIT_IDX, fuzzy=False)[0] or raw


# ---------- time ----------

_REL_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(min|mins|minute|minutes|मिनट|hr|hrs|hour|hours|घंटे|घंटा)\s*"
    r"(ago|back|before|earlier|pehle|pahle|पहले)",
    re.I,
)
_CLOCK_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b|\b([01]\d|2[0-3])([0-5]\d)\s*(hrs|hours|h)\b", re.I)
# Whisper writes "at ten forty-two" as "1042": accept a bare HHMM when it is the whole time expression.
_BARE_CLOCK_RE = re.compile(r"^(?:at\s+)?()()()([01]\d|2[0-3])([0-5]\d)(\s*)(?:baje|pe|par)?$", re.I)
_NOW_WORDS =("now", "just now", "abhi", "अभी", "right now", "on arrival", "at scene")


def resolve_time(raw: str | None, ref: datetime) -> tuple[str | None, str | None]:
    """Return (iso_time, warning)."""
    if not raw or not raw.strip():
        return ref.isoformat(timespec="seconds"), "time not stated; using capture time"
    text = _clean(raw)
    m = _REL_RE.search(text)
    if m:
        qty = float(m.group(1))
        is_hour = m.group(2).lower().startswith(("h", "घ"))
        delta = timedelta(hours=qty) if is_hour else timedelta(minutes=qty)
        return (ref - delta).isoformat(timespec="seconds"), None
    m = _CLOCK_RE.search(text) or _BARE_CLOCK_RE.search(text)
    if m:
        if m.group(1):
            hh, mm, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
            if ampm == "pm" and hh < 12:
                hh += 12
            if ampm == "am" and hh == 12:
                hh = 0
        else:
            hh, mm = int(m.group(4)), int(m.group(5))
        if hh > 23 or mm > 59:
            return None, f"could not parse time '{raw}'"
        t = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t - ref > timedelta(hours=1):  # e.g. 23:50 dictated just after midnight
            t -= timedelta(days=1)
        return t.isoformat(timespec="seconds"), None
    if any(w in text for w in _NOW_WORDS):
        return ref.isoformat(timespec="seconds"), None
    return ref.isoformat(timespec="seconds"), f"could not parse time '{raw}'; using capture time"


# ---------- evidence ----------

def evidence_in_transcript(evidence: str | None, transcript: str) -> bool:
    """True if the quoted evidence is (approximately) present in the transcript.

    Guards against the LLM inventing an administration that was never dictated.
    """
    if not evidence:
        return False
    ev, tr = _clean(evidence), _clean(transcript)
    if ev in tr:
        return True
    ev_tokens = re.findall(r"\w+", ev)
    tr_tokens = set(re.findall(r"\w+", tr))
    if ev_tokens and sum(t in tr_tokens for t in ev_tokens) / len(ev_tokens) >= 0.7:
        return True
    # sliding window fuzzy match for small ASR/LLM wording drift
    n = len(ev)
    step = max(1, n // 4)
    return any(SequenceMatcher(None, ev, tr[i:i + n]).ratio() >= 0.8 for i in range(0, max(1, len(tr) - n + 1), step))


# ---------- dose safety ----------

def dose_warning(drug: str | None, value: float | None, unit: str | None) -> str | None:
    if not drug or drug not in DRUGS or value is None or not unit:
        return None
    limit = DRUGS[drug][2]
    if not limit:
        return None
    max_val, max_unit = limit
    if unit in MASS_TO_MG and max_unit in MASS_TO_MG:
        if value * MASS_TO_MG[unit] > max_val * MASS_TO_MG[max_unit]:
            return f"dose {value:g} {unit} exceeds typical max single adult dose ({max_val:g} {max_unit}) - verify"
    return None


# ---------- main entry ----------

_NULLS = {"", "null", "none", "n/a", "na", "-"}
# Things the LLM sometimes lists as "procedures" that are really drug administrations or outcomes.
_NOT_PROCEDURES = {"nebulization", "nebulisation", "fluid administration", "medication administration",
                   "drug administration", "iv fluids", "rosc", "seizure control", "airway management", "assessment"}
_NO_DOSE_OK = ("oxygen", "sodium chloride 0.9%", "ringer lactate")
_LITRES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:l\b|lpm|lit(?:er|re)s?|लीटर)", re.I)


def _is_null(s: str | None) -> bool:
    return s is None or _clean(s) in _NULLS


def _ground_drug(llm_drug: str, evidence: str | None, warnings: list[str]) -> tuple[str | None, str | None, bool]:
    """Cross-check the LLM's drug name against the drug actually named in its evidence quote."""
    canon, rxcui, exact = canonical_drug(llm_drug)
    in_ev = mentions(evidence, _DRUG_IDX)
    if canon and (canon in in_ev or not in_ev):
        return canon, rxcui, exact
    if len(in_ev) == 1:
        fixed = in_ev.pop()
        warnings.append(f"model said '{llm_drug}', transcript says {fixed} - corrected")
        return fixed, DRUGS[fixed][0], True
    if canon and in_ev:
        warnings.append(f"'{llm_drug}' not named in its evidence - verify")
    return canon, rxcui, exact


def _ground_route(drug: str | None, llm_route: str | None, evidence: str | None) -> str | None:
    if drug == "oxygen":
        return "INH"
    route = canonical_route(llm_route) or (llm_route.upper() if llm_route else None)
    in_ev = mentions(evidence, _ROUTE_IDX)
    if len(in_ev) == 1:  # an explicit route word in the quote beats the model's guess
        return in_ev.pop()
    return route


def normalize(ext: LLMExtraction, transcript: str, ref: datetime) -> ClinicalRecord:
    meds: list[Medication] = []
    for m in ext.medications:
        if _is_null(m.drug) or _is_null(m.evidence):
            continue
        warnings: list[str] = []
        canon, rxcui, exact = _ground_drug(m.drug, m.evidence, warnings)
        if canon is None:
            warnings.append(f"unrecognised drug '{m.drug}' - verify")
        elif not exact:
            warnings.append(f"drug fuzzy-matched from '{m.drug}' - verify")
        unit = canonical_unit(m.dose_unit)
        dose = m.dose_value
        if dose is None:  # model skipped a dose that is plainly in its own quote
            if canon == "oxygen" and (lm := _LITRES_RE.search(_clean(m.evidence))):
                dose, unit = float(lm.group(1)), "L/min"
            elif len(doses := _DOSE_RE.findall(_clean(m.evidence))) == 1:
                dose, unit = float(doses[0][0]), canonical_unit(doses[0][1])
        route = _ground_route(canon, m.route, m.evidence)
        iso, tw = resolve_time(m.time, ref)
        if tw:
            warnings.append(tw)
        if dose is None and canon not in _NO_DOSE_OK:
            warnings.append("dose missing")
        if not route:
            warnings.append("route missing")
        if dw := dose_warning(canon, dose, unit):
            warnings.append(dw)
        found = evidence_in_transcript(m.evidence, transcript)
        if not found:
            warnings.append("not found in transcript - possible hallucination")
        meds.append(Medication(
            drug=canon or m.drug, drug_raw=m.drug, rxnorm=rxcui,
            dose_value=dose, dose_unit=unit, route=route, site=m.site,
            time=iso, time_raw=m.time, evidence=m.evidence, evidence_found=found, warnings=warnings,
        ))

    procs: list[Procedure] = []
    for p in ext.procedures:
        if _is_null(p.name) or _is_null(p.evidence) or _clean(p.name) in _NOT_PROCEDURES:
            continue
        warnings = []
        canon, code, exact = canonical_procedure(p.name)
        in_ev = mentions(p.evidence, _PROC_IDX)
        if canon not in in_ev and not in_ev:
            if drugs := mentions(p.evidence, _DRUG_IDX):
                # e.g. "NS 500 ml start kiya" filed as a procedure: it is really a drug administration
                meds.extend(_med_from_phrase(d, p, transcript, ref) for d in drugs
                            if not any(m.evidence == p.evidence and m.drug == d for m in meds))
                continue
            if canon == "Oxygen therapy":
                continue  # oxygen with no oxygen in the quote: model confusion, drop
        if in_ev and canon not in in_ev:
            if len(in_ev) == 1:
                canon = in_ev.pop()
                code, exact = PROCEDURES[canon][0], True
            else:
                warnings.append(f"'{p.name}' not named in its evidence - verify")
        elif canon and not in_ev:
            warnings.append(f"'{canon}' not named in its evidence - verify")
        if canon is None:
            warnings.append("procedure not in local vocabulary")
        elif not exact:
            warnings.append(f"procedure fuzzy-matched from '{p.name}' - verify")
        iso, tw = resolve_time(p.time, ref)
        if tw and "not stated" not in tw:
            warnings.append(tw)
        found = evidence_in_transcript(p.evidence, transcript)
        if not found:
            warnings.append("not found in transcript - possible hallucination")
        if any(x.name == (canon or p.name) and x.evidence == p.evidence for x in procs):
            continue  # same procedure from the same phrase listed twice
        procs.append(Procedure(
            name=canon or p.name, name_raw=p.name, code=code, details=p.details, site=p.site,
            time=iso, time_raw=p.time, outcome=p.outcome, evidence=p.evidence,
            evidence_found=found, warnings=warnings,
        ))

    vitals: list[Vital] = []
    for v in ext.vitals:
        if _is_null(v.value) or _is_null(v.evidence):
            continue
        warnings = []
        iso, tw = resolve_time(v.time, ref)
        if tw and "not stated" not in tw:
            warnings.append(tw)
        found = evidence_in_transcript(v.evidence, transcript)
        if not found:
            warnings.append("not found in transcript - possible hallucination")
        vitals.append(Vital(type=v.type, value=v.value, unit=v.unit, time=iso, time_raw=v.time,
                            evidence=v.evidence, evidence_found=found, warnings=warnings))

    _apply_implications(meds, procs, vitals, ref)
    return ClinicalRecord(
        reference_time=ref.isoformat(timespec="seconds"),
        patient=ext.patient, chief_complaint=ext.chief_complaint,
        medications=meds, procedures=procs, vitals=vitals,
    )


_UNIT_ALT = "|".join(sorted((re.escape(s) for s in _UNIT_IDX), key=len, reverse=True))
_DOSE_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*({_UNIT_ALT})(?!\w)", re.I)


def _med_from_phrase(drug: str, p, transcript: str, ref: datetime) -> Medication:
    """Build a medication deterministically from a quoted phrase (dose/unit/route parsed by regex)."""
    dm = _DOSE_RE.search(_clean(p.evidence))
    if drug == "oxygen" and (lm := _LITRES_RE.search(f"{p.details or ''} {p.evidence}")):
        dm = None
        dose, unit = float(lm.group(1)), "L/min"
    else:
        dose = float(dm.group(1)) if dm else None
        unit = canonical_unit(dm.group(2)) if dm else None
    route = _ground_route(drug, None, p.evidence) or ("IV" if drug in _NO_DOSE_OK else None)
    iso, _ = resolve_time(p.time, ref)
    found = evidence_in_transcript(p.evidence, transcript)
    warnings = ["recovered from a misfiled procedure - verify"]
    if not found:
        warnings.append("not found in transcript - possible hallucination")
    return Medication(
        drug=drug, rxnorm=DRUGS[drug][0], dose_value=dose, dose_unit=unit, route=route, site=p.site,
        time=iso, time_raw=p.time, evidence=p.evidence, evidence_found=found, warnings=warnings,
    )


def _apply_implications(meds: list[Medication], procs: list[Procedure], vitals: list[Vital], ref: datetime) -> None:
    """Clinical facts that imply each other, so the record is complete for audit."""
    # Procedures named inside any quote but missed by the model (e.g. "via 22 gauge" -> IV cannulation,
    # "C-collar on, spine board" -> two procedures).
    for item in [*procs, *meds]:
        for canon in mentions(item.evidence, _PROC_IDX) - {p.name for p in procs}:
            if canon == "Oxygen therapy":
                continue  # handled below
            procs.append(Procedure(name=canon, code=PROCEDURES[canon][0], time=item.time,
                                   evidence=item.evidence, evidence_found=item.evidence_found))
    names = {p.name for p in procs}
    # Oxygen is both a drug (with a flow rate) and a procedure.
    if "Oxygen therapy" in names and not any(m.drug == "oxygen" for m in meds):
        p = next(p for p in procs if p.name == "Oxygen therapy")
        lm = _LITRES_RE.search(f"{p.details or ''} {p.evidence or ''}")
        meds.append(Medication(
            drug="oxygen", rxnorm=DRUGS["oxygen"][0], dose_value=float(lm.group(1)) if lm else None,
            dose_unit="L/min" if lm else None, route="INH", site=p.site, time=p.time, time_raw=p.time_raw,
            evidence=p.evidence, evidence_found=p.evidence_found, warnings=["inferred from oxygen therapy"],
        ))
    if any(m.drug == "oxygen" for m in meds) and "Oxygen therapy" not in names:
        m = next(m for m in meds if m.drug == "oxygen")
        procs.append(Procedure(name="Oxygen therapy", code=PROCEDURES["Oxygen therapy"][0], time=m.time,
                               evidence=m.evidence, evidence_found=m.evidence_found,
                               details=f"{m.dose_value:g} {m.dose_unit}" if m.dose_value else None))
    # A measured blood glucose means a glucose check was performed.
    bgl = next((v for v in vitals if v.type == "BGL"), None)
    if bgl and "Glucose check" not in names:
        procs.append(Procedure(name="Glucose check", code=PROCEDURES["Glucose check"][0], time=bgl.time,
                               outcome=bgl.value, evidence=bgl.evidence, evidence_found=bgl.evidence_found))
