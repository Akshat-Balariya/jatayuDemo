"""Data model.

`LLMExtraction` is the raw shape the local LLM must emit (enforced via JSON schema).
`ClinicalRecord` is the normalised, timestamped record the responder reviews and signs.
`to_fhir_lite` converts a signed record into a FHIR-flavoured bundle for interchange.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- Raw LLM output ----------

class LLMMedication(BaseModel):
    drug: str = Field(description="Drug name as spoken, translated to English if needed")
    dose_value: Optional[float] = None
    dose_unit: Optional[str] = None
    route: Optional[str] = None
    site: Optional[str] = None
    time: Optional[str] = Field(None, description="Time as spoken, e.g. '14:32', '5 min ago'")
    evidence: str = Field(description="Exact words from the transcript supporting this item")


class LLMProcedure(BaseModel):
    name: str
    details: Optional[str] = None
    site: Optional[str] = None
    time: Optional[str] = None
    outcome: Optional[str] = None
    evidence: str


class LLMVital(BaseModel):
    type: Literal["HR", "BP", "SpO2", "RR", "GCS", "Temp", "BGL", "Pain"]
    value: str
    unit: Optional[str] = None
    time: Optional[str] = None
    evidence: str


class LLMPatient(BaseModel):
    age: Optional[str] = None
    sex: Literal["male", "female", "unknown"] = "unknown"
    weight_kg: Optional[float] = None


class LLMExtraction(BaseModel):
    patient: LLMPatient = LLMPatient()
    chief_complaint: Optional[str] = None
    chief_complaint_evidence: Optional[str] = None
    medications: list[LLMMedication] = []
    procedures: list[LLMProcedure] = []
    vitals: list[LLMVital] = []


# ---------- Normalised record ----------

class Medication(BaseModel):
    drug: str
    drug_raw: Optional[str] = None
    rxnorm: Optional[str] = None
    dose_value: Optional[float] = None
    dose_unit: Optional[str] = None
    route: Optional[str] = None
    site: Optional[str] = None
    time: Optional[str] = None  # ISO-8601
    time_raw: Optional[str] = None
    evidence: Optional[str] = None
    evidence_found: bool = True
    warnings: list[str] = []


class Procedure(BaseModel):
    name: str
    name_raw: Optional[str] = None
    code: Optional[str] = None
    details: Optional[str] = None
    site: Optional[str] = None
    time: Optional[str] = None
    time_raw: Optional[str] = None
    outcome: Optional[str] = None
    evidence: Optional[str] = None
    evidence_found: bool = True
    warnings: list[str] = []


class Vital(BaseModel):
    type: str
    value: str
    unit: Optional[str] = None
    time: Optional[str] = None
    time_raw: Optional[str] = None
    evidence: Optional[str] = None
    evidence_found: bool = True
    warnings: list[str] = []


class ClinicalRecord(BaseModel):
    reference_time: str  # ISO time the note was captured; relative times resolve against it
    patient: LLMPatient = LLMPatient()
    chief_complaint: Optional[str] = None
    chief_complaint_warnings: list[str] = []
    medications: list[Medication] = []
    procedures: list[Procedure] = []
    vitals: list[Vital] = []


class ExtractRequest(BaseModel):
    text: str
    reference_time: Optional[str] = None


class SaveRequest(BaseModel):
    incident_id: str
    responder_id: str
    transcript: str
    language: Optional[str] = None
    audio_sha256: Optional[str] = None
    draft: ClinicalRecord   # what the machine produced
    final: ClinicalRecord   # what the responder confirmed
    model_info: dict = {}


class AmendRequest(BaseModel):
    responder_id: str
    reason: str             # required: why the signed record is being corrected
    final: ClinicalRecord   # the corrected record


class SwitchRequest(BaseModel):
    responder_id: str
    reason: str
    target: str             # version id that becomes current


# ---------- FHIR-lite ----------

def to_fhir_lite(record_id: str, saved: dict) -> dict:
    final = ClinicalRecord.model_validate(saved["final"])
    enc_ref = f"Encounter/{saved['incident_id']}"
    pat_ref = f"Patient/{record_id}-pt"
    entries: list[dict] = [
        {
            "resourceType": "Encounter",
            "id": saved["incident_id"],
            "status": "in-progress",
            "class": "EMER",
            "period": {"start": final.reference_time},
            "reasonCode": final.chief_complaint,
            "participant": saved["responder_id"],
        },
        {
            "resourceType": "Patient",
            "id": f"{record_id}-pt",
            "gender": final.patient.sex,
            "ageText": final.patient.age,
            "weightKg": final.patient.weight_kg,
        },
    ]
    for i, m in enumerate(final.medications):
        entries.append({
            "resourceType": "MedicationAdministration",
            "id": f"{record_id}-med{i}",
            "status": "completed",
            "medicationCodeableConcept": {
                "text": m.drug,
                "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": m.rxnorm}] if m.rxnorm else [],
            },
            "subject": pat_ref, "context": enc_ref,
            "effectiveDateTime": m.time,
            "performer": saved["responder_id"],
            "dosage": {
                "dose": {"value": m.dose_value, "unit": m.dose_unit},
                "route": m.route, "site": m.site,
            },
        })
    for i, p in enumerate(final.procedures):
        entries.append({
            "resourceType": "Procedure",
            "id": f"{record_id}-proc{i}",
            "status": "completed",
            "code": {"text": p.name, "coding": [{"system": "local", "code": p.code}] if p.code else []},
            "subject": pat_ref, "encounter": enc_ref,
            "performedDateTime": p.time,
            "bodySite": p.site, "outcome": p.outcome, "note": p.details,
        })
    for i, v in enumerate(final.vitals):
        entries.append({
            "resourceType": "Observation",
            "id": f"{record_id}-obs{i}",
            "status": "final", "category": "vital-signs",
            "code": {"text": v.type},
            "subject": pat_ref, "encounter": enc_ref,
            "effectiveDateTime": v.time,
            "valueString": f"{v.value} {v.unit or ''}".strip(),
        })
    entries.append({
        "resourceType": "Provenance",
        "id": f"{record_id}-prov",
        "recorded": saved["created_at"],
        "agent": [
            {"type": "author", "who": saved["responder_id"]},
            {"type": "assembler", "who": f"device:{saved['device_id']}", "model": saved.get("model_info")},
        ],
        "signature": {"type": "sha256-hash-chain", "hash": saved["hash"], "prevHash": saved["prev_hash"]},
        "entity": [
            {"role": "source", "transcriptSha256": saved.get("transcript_sha256"), "audioSha256": saved.get("audio_sha256")},
            # an amended version records the version it revises, by id and hash
            *([{"role": "revision", "what": f"Bundle/{saved['parent']}", "hash": saved.get("parent_hash")}]
              if saved.get("parent") else []),
        ],
        "reason": saved.get("reason"),
        "version": {"label": saved.get("version_label"), "of": saved.get("version_count"),
                    "isCurrent": saved.get("is_current"), "record": saved.get("lineage")},
    })
    return {"resourceType": "Bundle", "type": "collection", "id": record_id,
            "timestamp": saved["created_at"], "entry": entries}
