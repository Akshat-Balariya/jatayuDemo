"""A lightweight harness around the local LLM.

The LLM is good at reading messy Hinglish, but it is never trusted on its own. This module
wraps every extraction call with deterministic checks:

before the call
  * scan the transcript for every medicine it names (lexicon match, plus fuzzy match for
    speech-recognition misspellings like "paracitamol"), and whether each mention is negated
    ("refused", "nahi diya") or only context (allergy, home medication)
  * pass what was found to the model as hints

the call
  * invalid output is retried once with the validation error; if the model is unreachable or
    still invalid, a rule-based extractor builds the draft instead (clearly labelled)

after the call (on the normalised record)
  * recall:    every medicine named as given must be in the record, else it is added and flagged
  * negation:  a medicine the note says was NOT given is flagged
  * dose:      the dose number must appear in the item's source sentence
  * route:     the route must be a usual one for that drug
  * time:      not after the recording, not more than 12 h before it
  * duplicates: the same item emitted twice is removed unless the note says it was repeated

Every check's result goes into the record's model_info, so the signed record shows what was verified.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import get_close_matches

from . import config, extractor
from .lexicon import ALLOWED_ROUTES, DRUGS
from .normalize import (_AMBIGUOUS, _CLOCK_RE, _DOSE_RE, _DRUG_IDX, _LITRES_RE, _PROC_IDX, _REL_RE,
                        _ROUTE_IDX, _clean, _sentence_of, mentions, normalize, split_sentences)
from .schemas import ClinicalRecord, LLMExtraction, LLMMedication, LLMPatient, LLMProcedure, LLMVital

_NEGATION_RE = re.compile(
    r"(?<!\w)(refused|refuses|declined|not given|not administered|withheld|held off|didn'?t|did not|avoided|"
    r"nahi|nahin|nhi|mana)(?!\w)|नहीं|नही|मना", re.I)
_CONTEXT_RE = re.compile(
    r"(?<!\w)(allerg\w*|home medication|home meds?|takes|taking|regular(?:ly)?|history of|known case|prescribed|"
    r"considered|consider|leta hai|leti hai|khata hai|khati hai)(?!\w)|एलर्जी", re.I)
_ADMIN_RE = re.compile(
    r"(?<!\w)(gave|give|given|administered|started|start|push(?:ed)?|bolus|chewed|nebuli[sz]ed|neb|inhaled|"
    r"infused|injected|applied|sprayed|placed|diya|di|dia|lagaya|lagayi|chala|chalaya|chadhaya|khilaya|"
    r"pilaya|rakha|kiya|mila ke)(?!\w)|दिया|दी|चढ़ाया|लगाया|खिलाया", re.I)
_REPEAT_RE = re.compile(r"(?<!\w)(twice|repeat\w*|again|another|do baar|dobara|phir se|x ?2|2 baar)(?!\w)|दो बार|दोबारा",
                        re.I)
_CLAUSE_SPLIT_RE = re.compile(r"[,;:]|\s(?:but|lekin|and|aur|then|phir|uske baad|after that)\s", re.I)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_SUGAR_RE = re.compile(r"(?<!\w)(sugar|blood glucose|bgl|cbg|rbs|glucometer)(?!\w)|शुगर", re.I)

_EXACT_SYNS = sorted((s for s in _DRUG_IDX if len(s) >= 2 and s not in _AMBIGUOUS), key=len, reverse=True)
_FUZZY_SYNS = sorted(s for s in _DRUG_IDX if re.fullmatch(r"[a-z][a-z\-]{4,}", s))
_LATIN_WORD_RE = re.compile(r"[a-z][a-z\-]{4,}")


# ---------- transcript scan ----------

@dataclass
class DrugMention:
    canon: str        # generic name
    said: str         # the words used in the note
    sentence: str     # the original sentence
    clause: str       # the clause holding the mention (cleaned)
    before: str       # cleaned text between the previous drug mention and this one
    after: str        # cleaned text between this mention and the next drug mention
    fuzzy: bool       # matched a likely misspelling
    status: str       # "given" | "negated" | "context"


def _clause_at(text: str, pos: int) -> str:
    start, end = 0, len(text)
    for m in _CLAUSE_SPLIT_RE.finditer(text):
        if m.end() <= pos:
            start = m.end()
        elif m.start() > pos:
            end = m.start()
            break
    return text[start:end]


def scan_drugs(text: str) -> list[DrugMention]:
    """Every medicine the note names, in order, with how it is mentioned."""
    out = []
    for sent in split_sentences(text):
        cs = _clean(sent)
        hits: list[tuple[int, int, str, bool]] = []

        def free(s, e):
            return not any(s < he and hs < e for hs, he, _, _ in hits)

        for syn in _EXACT_SYNS:  # longest synonyms first, so "normal saline" wins over "saline"
            for m in re.finditer(rf"(?<!\w){re.escape(syn)}(?!\w)", cs):
                if free(m.start(), m.end()):
                    hits.append((m.start(), m.end(), _DRUG_IDX[syn], False))
        for m in _LATIN_WORD_RE.finditer(cs):  # misspellings from speech recognition
            if free(m.start(), m.end()):
                close = get_close_matches(m.group(), _FUZZY_SYNS, n=1, cutoff=0.84)
                if close:
                    hits.append((m.start(), m.end(), _DRUG_IDX[close[0]], True))
        hits.sort()
        for i, (s, e, canon, fuzzy) in enumerate(hits):
            clause = _clause_at(cs, s)
            status = "negated" if _NEGATION_RE.search(clause) else "context" if _CONTEXT_RE.search(clause) else "given"
            prev_end = hits[i - 1][1] if i else 0
            next_start = hits[i + 1][0] if i + 1 < len(hits) else len(cs)
            out.append(DrugMention(canon, cs[s:e], sent.strip(), clause, cs[prev_end:s], cs[e:next_start], fuzzy, status))
    return out


def drug_hints(found: list[DrugMention], text: str = "") -> list[str]:
    """Lines appended to the note for the model: what the lexicon recognised."""
    lines = []
    named = sorted({f"{m.said} = {m.canon}" + (" (probably misspelt)" if m.fuzzy else "")
                    for m in found if m.status == "given" and (m.said != m.canon or m.fuzzy)})
    if named:
        lines.append("Medicines recognised in this note (as written = generic name): " + "; ".join(named))
    negated = sorted({m.said for m in found if m.status == "negated"})
    if negated:
        lines.append("Mentioned but NOT given (refused or withheld) - do not list as given: " + ", ".join(negated))
    context = sorted({m.said for m in found if m.status == "context"})
    if context:
        lines.append("Mentioned only as history, allergy or a consideration - not given now: " + ", ".join(context))
    if any(m.canon == "glucose" for m in found) and _SUGAR_RE.search(text):
        lines.append("Blood sugar / glucose readings are vitals (BGL), not medicines.")
    return lines


# ---------- rule-based extraction (fallback and recovery) ----------

_VITAL_RES = [
    ("BP", re.compile(r"(?<!\w)(?:bp|b\.p\.?|blood pressure)\s*(?:is|was|of|hai)?\s*(\d{2,3}\s*(?:/|by|over|x)\s*\d{2,3})", re.I), "mmHg"),
    ("HR", re.compile(r"(?<!\w)(?:hr|heart rate|pulse|nadi)\s*(?:is|was|of|hai)?\s*(\d{2,3})", re.I), "bpm"),
    ("SpO2", re.compile(r"(?<!\w)(?:spo2|sp02|saturation|sats?)\s*(?:is|was|of|hai)?\s*(\d{2,3})", re.I), "%"),
    ("RR", re.compile(r"(?<!\w)(?:rr|resp(?:iratory)? rate)\s*(?:is|was|of)?\s*(\d{1,2})", re.I), "/min"),
    ("GCS", re.compile(r"(?<!\w)gcs\s*(?:is|was|now|hai)?\s*(\d{1,2})", re.I), None),
    ("BGL", re.compile(r"(?<!\w)(?:sugar|bgl|cbg|rbs|blood glucose|शुगर)\s*(?:is|was|of|level|hai|aayi|आई)?\s*(\d{2,3})", re.I), "mg/dL"),
]


def _parse_med(m: DrugMention) -> LLMMedication:
    seg = m.after + " " + m.before
    if m.canon == "oxygen" and (lm := _LITRES_RE.search(seg)):
        dose, unit = float(lm.group(1)), "L/min"
    else:
        dm = _DOSE_RE.search(m.after) or (list(_DOSE_RE.finditer(m.before)) or [None])[-1]
        dose, unit = (float(dm.group(1)), dm.group(2)) if dm else (None, None)
    routes = mentions(seg, _ROUTE_IDX)
    cs = _clean(m.sentence)
    tm = _REL_RE.search(cs) or _CLOCK_RE.search(cs)
    return LLMMedication(drug=m.said, dose_value=dose, dose_unit=unit, route=routes.pop() if len(routes) == 1 else None,
                         time=tm.group(0) if tm else None, evidence=m.sentence)


def administered(m: DrugMention) -> bool:
    """Named as given AND the words say so: a dose next to it or an administration verb in its clause.
    Keeps measurements ("blood glucose 42") and bare mentions out."""
    if m.status != "given" or re.search(r"(?<!\w)blood\s*$", m.before):
        return False
    return bool(_DOSE_RE.search(m.after) or _LITRES_RE.search(m.after) or _ADMIN_RE.search(m.clause))


def rules_extract(text: str, found: list[DrugMention] | None = None) -> LLMExtraction:
    """Deterministic extraction from the lexicon and patterns: the draft when the LLM can't help."""
    found = scan_drugs(text) if found is None else found
    meds = [_parse_med(m) for m in found if administered(m)]
    procs, vitals = [], []
    for sent in split_sentences(text):
        cs = _clean(sent)
        procs += [LLMProcedure(name=p, evidence=sent.strip()) for p in sorted(mentions(cs, _PROC_IDX))]
        for vtype, rx, unit in _VITAL_RES:
            if vm := rx.search(cs):
                value = re.sub(r"\s*(?:by|over|x)\s*", "/", vm.group(1)).replace(" ", "")
                vitals.append(LLMVital(type=vtype, value=value, unit=unit, evidence=vm.group(0)))
    low = _clean(text)
    age = re.search(r"(\d{1,3})\s*(?:-?\s*year|yr|saal|sal|साल)", low)
    sex = ("female" if re.search(r"(?<!\w)(female|woman|lady|mahila|aurat|ladki|महिला)(?!\w)", low)
           else "male" if re.search(r"(?<!\w)(male|man|aadmi|purush|ladka|uncle|पुरुष)(?!\w)", low) else "unknown")
    return LLMExtraction(patient=LLMPatient(age=age.group(1) if age else None, sex=sex),
                         medications=meds, procedures=procs, vitals=vitals)


# ---------- post-extraction checks ----------

def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-6


def _post_checks(rec: ClinicalRecord, text: str, ref: datetime, found: list[DrugMention], mode: str) -> list[dict]:
    checks = []

    # names: every medicine must be a known generic AND be named (or plainly misspelt) in the note
    by_canon = {}
    for m in found:
        by_canon.setdefault(m.canon, m)
    unknown, unnamed = [], []
    for med in rec.medications:
        if med.drug not in DRUGS:
            unknown.append(med.drug)  # not in the lexicon at all
        m = by_canon.get(med.drug)
        if m:  # the scan saw it, possibly as a misspelling: replace the normaliser's "not named" warning
            med.warnings = [w for w in med.warnings if "not named anywhere in the transcript" not in w]
            if m.fuzzy and not any("misspel" in w for w in med.warnings):
                med.warnings.insert(0, f"matched to a likely misspelling '{m.said}' - verify")
        elif med.drug not in unknown:
            unnamed.append(med.drug)
    detail = "; ".join(filter(None, [f"not in the drug lexicon: {', '.join(unknown)}" if unknown else "",
                                     f"not named in the note: {', '.join(unnamed)}" if unnamed else ""]))
    checks.append(_check("Medicine names", "flag" if unknown or unnamed else "pass",
                         detail or "every medicine is a known generic named in the note"))

    # phantoms: an item whose own quote names no medicine and gives no dose ("Recheck sugar 110",
    # "cannula lagaya") is a reading or a procedure the model turned into a drug
    def quote_names_drug(med):
        return bool(scan_drugs(med.evidence or "")) or bool(_DOSE_RE.search(_clean(med.evidence or "")))

    grounded = {m.drug for m in rec.medications if quote_names_drug(m)}
    kept, dropped, doubtful = [], [], []
    for med in rec.medications:
        if quote_names_drug(med):
            kept.append(med)
        elif med.drug in grounded:
            dropped.append(med.drug)
        else:
            med.warnings.insert(0, "its quote does not name this medicine - verify")
            doubtful.append(med.drug)
            kept.append(med)
    rec.medications = kept
    checks.append(_check("Phantom items", "fixed" if dropped else "flag" if doubtful else "pass",
                         "; ".join(filter(None, [f"removed (already recorded from a real quote): {', '.join(dropped)}" if dropped else "",
                                                 f"quote names no medicine: {', '.join(doubtful)}" if doubtful else ""]))
                         or "every item's quote names its medicine or dose"))

    # recall: every medicine named as given (with a dose or an administration word) is in the record
    present = {m.drug for m in rec.medications}
    recovered = []
    for m in found:
        if m.canon in present or not administered(m):
            continue
        extra = normalize(LLMExtraction(medications=[_parse_med(m)]), text, ref).medications
        for med in extra:
            med.warnings.insert(0, "missed by the model - added from the transcript, verify")
            rec.medications.append(med)
        present.add(m.canon)
        recovered.append(m.canon)
    checks.append(_check("Medicine recall", "fixed" if recovered else "pass",
                         f"added from the transcript: {', '.join(recovered)}" if recovered
                         else f"all {len({m.canon for m in found if administered(m)})} medicine(s) named as given are in the record"))

    # negation / context: the note says it was not given now
    negated = []
    for med in rec.medications:
        sentence = _clean(_sentence_of(med.evidence, text) or med.evidence or "")
        here = [m for m in found if m.canon == med.drug and _clean(m.sentence) == sentence]
        if here and all(m.status == "negated" for m in here):
            med.warnings.insert(0, "note says this was NOT given (refused or withheld) - verify")
            negated.append(med.drug)
        elif here and all(m.status != "given" for m in here):
            med.warnings.insert(0, "note mentions this as history or allergy, not as given - verify")
            negated.append(med.drug)
    checks.append(_check("Negation", "flag" if negated else "pass",
                         f"listed but the note says not given: {', '.join(negated)}" if negated
                         else "no listed medicine is negated in the note"))

    # dose grounding: the number must appear in the item's sentence
    ungrounded = []
    for med in rec.medications:
        if med.dose_value is None:
            continue
        sentence = _sentence_of(med.evidence, text) or med.evidence or ""
        nums = [float(n) for n in _NUMBER_RE.findall(sentence)]
        if nums and not any(_close(med.dose_value, n) or _close(med.dose_value, n * 1000) or _close(med.dose_value * 1000, n)
                            for n in nums):
            med.warnings.append(f"dose {med.dose_value:g} not stated in the note - verify")
            ungrounded.append(med.drug)
    checks.append(_check("Dose grounding", "flag" if ungrounded else "pass",
                         f"dose not found in the note: {', '.join(ungrounded)}" if ungrounded
                         else "every dose appears in its source sentence"))

    # route plausibility
    odd = []
    for med in rec.medications:
        allowed = ALLOWED_ROUTES.get(med.drug)
        if med.route and allowed and med.route not in allowed:
            med.warnings.append(f"route {med.route} is unusual for {med.drug} - verify")
            odd.append(f"{med.drug} {med.route}")
    checks.append(_check("Route plausibility", "flag" if odd else "pass",
                         f"unusual route: {', '.join(odd)}" if odd else "every route is a usual one for its drug"))

    # time sanity
    bad_times = []
    for item in [*rec.medications, *rec.procedures]:
        try:
            t = datetime.fromisoformat(item.time) if item.time else None
        except ValueError:
            t = None
        if t is None:
            continue
        if t - ref > timedelta(minutes=5):
            item.warnings.append("time is after the recording - verify")
            bad_times.append(getattr(item, "drug", None) or item.name)
        elif ref - t > timedelta(hours=12):
            item.warnings.append("time is more than 12 h before the recording - verify")
            bad_times.append(getattr(item, "drug", None) or item.name)
    checks.append(_check("Time sanity", "flag" if bad_times else "pass",
                         f"implausible time: {', '.join(bad_times)}" if bad_times
                         else "all times fall within 12 h before the recording"))

    # duplicates: same item from the same words, unless the note says it was repeated
    seen, kept, dropped = set(), [], []
    for med in rec.medications:
        key = (med.drug, med.dose_value, med.dose_unit, med.route, med.time, _clean(med.evidence or ""))
        if key in seen and not _REPEAT_RE.search(_sentence_of(med.evidence, text) or ""):
            dropped.append(med.drug)
            continue
        seen.add(key)
        kept.append(med)
    rec.medications = kept
    checks.append(_check("Duplicates", "fixed" if dropped else "pass",
                         f"removed duplicate: {', '.join(dropped)}" if dropped else "no duplicated items"))
    return checks


# ---------- entry point ----------

def extract_record(text: str, ref: datetime, model: str | None = None,
                   use_llm: bool = True) -> tuple[ClinicalRecord, LLMExtraction, dict, dict]:
    """Harnessed extraction. Returns (record, raw extraction, model_info, harness report)."""
    t0 = time.perf_counter()
    found = scan_drugs(text)
    hints = drug_hints(found, text)
    info: dict = {"llm_model": model or config.LLM_MODEL}
    raw, mode, error = None, "llm", None
    schema = _check("Output schema", "pass", "model output matched the schema first time")
    if use_llm:
        try:
            raw, i = extractor.extract(text, model, hints)
            info.update(i)
        except extractor.LLMOutputError as e:
            try:
                raw, i = extractor.extract(text, model, hints, repair=(e.content, str(e)))
                info.update(i)
                mode = "llm-retry"
                schema = _check("Output schema", "fixed", "invalid output was repaired on one retry")
            except (extractor.LLMOutputError, extractor.LLMUnavailable) as e2:
                error = f"invalid model output after retry: {str(e2)[:200]}"
        except extractor.LLMUnavailable as e:
            error = str(e)
    else:
        error = "LLM disabled"
    if raw is None:
        raw, mode = rules_extract(text, found), "rules-fallback"
        info.pop("llm_latency_s", None)
        schema = _check("Output schema", "flag", f"rule-based draft used ({error}); review every field")
    record = normalize(raw, text, ref)
    checks = [schema, *_post_checks(record, text, ref, found, mode)]
    report = {
        "mode": mode, "error": error, "hints": hints, "checks": checks,
        "flagged": sum(c["status"] == "flag" for c in checks),
        "fixed": sum(c["status"] == "fixed" for c in checks),
        "latency_s": round(time.perf_counter() - t0, 2),
    }
    info["harness"] = {"mode": mode, "checks": {c["name"]: c["status"] for c in checks}}
    return record, raw, info, report
