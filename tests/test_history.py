import hashlib
import json
import sqlite3

import pytest

from app import audit, config

BASE = {"reference_time": "2026-10-23T14:40:00+05:30", "patient": {"age": "54", "sex": "male"},
        "medications": [{"drug": "aspirin", "dose_value": 300, "dose_unit": "mg", "route": "PO"}],
        "procedures": [], "vitals": []}


def with_dose(rec, dose):
    out = json.loads(json.dumps(rec))
    out["medications"][0]["dose_value"] = dose
    return out


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")


def create():
    return audit.append(incident_id="INC-1", responder_id="EMT-1", transcript="aspirin 300", language="en",
                        audio_sha256=None, draft=BASE, final=BASE, model_info={"llm_model": "x"})


def test_amend_links_to_parent_by_hash():
    v1 = create()
    v2 = audit.amend(v1["id"], responder_id="EMT-2", reason="dose was 325", final=with_dose(BASE, 325))
    assert v2["kind"] == "amend" and v2["lineage"] == v1["id"]
    assert v2["parent"] == v1["id"] and v2["parent_hash"] == v1["hash"]
    assert v2["edits"] == [{"field": "medications[0].dose_value", "machine": 300, "human": 325}]
    assert v2["transcript_sha256"] == v1["transcript_sha256"]   # same source, new content
    assert audit.get(v1["id"])["final"] == BASE                   # the signed original is untouched
    h = audit.history(v1["id"])
    assert h["current"] == v2["id"] and [v["label"] for v in h["versions"]] == ["v1", "v2"]
    assert audit.verify()["ok"]


def test_amending_an_older_version_starts_a_branch_and_switch_is_signed():
    v1 = create()
    v2 = audit.amend(v1["id"], responder_id="EMT", reason="r1", final=with_dose(BASE, 325))
    v3 = audit.amend(v1["id"], responder_id="EMT", reason="r2", final=with_dose(BASE, 150))  # branch from v1
    h = audit.history(v1["id"])
    assert sorted(next(v for v in h["versions"] if v["id"] == v1["id"])["children"]) == sorted([v2["id"], v3["id"]])
    assert h["current"] == v3["id"]
    ev = audit.switch(v1["id"], v2["id"], responder_id="DR-1", reason="v2 matches the paper chart")
    assert ev["kind"] == "switch" and ev["parent"] == v3["id"] and ev["target"] == v2["id"]
    assert audit.history(v1["id"])["current"] == v2["id"]
    [row] = audit.list_lineages()
    assert row["versions"] == 3 and row["branches"] == 2 and row["id"] == v2["id"]
    v = audit.verify()
    assert v["ok"] and v["records"] == 1 and v["versions"] == 3 and v["switches"] == 1


def test_rejects_invalid_history_requests():
    v1 = create()
    other = create()
    with pytest.raises(audit.HistoryError):
        audit.amend(v1["id"], responder_id="EMT", reason="", final=with_dose(BASE, 325))   # no reason
    with pytest.raises(audit.HistoryError):
        audit.amend(v1["id"], responder_id="EMT", reason="x", final=BASE)                   # no change
    with pytest.raises(audit.HistoryError):
        audit.switch(v1["id"], other["id"], responder_id="EMT", reason="x")                  # other record
    with pytest.raises(audit.HistoryError):
        audit.switch(v1["id"], v1["id"], responder_id="EMT", reason="x")                     # already current


def _rewrite(seq, **cols):
    """Tamper with a stored entry and re-hash the whole chain, as an attacker with DB access would."""
    with sqlite3.connect(config.DB_PATH) as c:
        sets = ", ".join(f"{k} = ?" for k in cols)
        c.execute(f"UPDATE records SET {sets} WHERE seq = ?", (*cols.values(), seq))
    with audit._conn() as c:
        prev = audit.GENESIS
        for r in audit._all(c):
            h = audit.compute_hash(prev, r, r["hash_v"])
            c.execute("UPDATE records SET prev_hash = ?, hash = ? WHERE seq = ?", (prev, h, r["seq"]))
            prev = h


def test_tampered_content_breaks_the_chain():
    v1 = create()
    audit.amend(v1["id"], responder_id="EMT", reason="r", final=with_dose(BASE, 325))
    with sqlite3.connect(config.DB_PATH) as c:
        c.execute("UPDATE records SET reason = 'nothing to see' WHERE seq = 2")
    v = audit.verify()
    assert not v["ok"] and v["reason"] == "content changed after signing"


def test_rehashed_chain_still_fails_when_a_parent_link_is_broken():
    """Even if the whole chain is re-hashed, a version must point at its parent's real hash."""
    v1 = create()
    audit.amend(v1["id"], responder_id="EMT", reason="r", final=with_dose(BASE, 325))
    _rewrite(1, final=json.dumps(with_dose(BASE, 3000)))  # rewrite history of v1
    v = audit.verify()
    assert not v["ok"] and "parent_hash" in v["reason"]


def test_switch_must_start_from_current_version():
    v1 = create()
    v2 = audit.amend(v1["id"], responder_id="EMT", reason="r", final=with_dose(BASE, 325))
    audit.switch(v1["id"], v1["id"], responder_id="EMT", reason="back")
    _rewrite(3, parent=v2["id"], parent_hash=v2["hash"], target=v2["id"])  # a switch to what is already current
    v = audit.verify()
    assert not v["ok"]


def test_entries_signed_before_the_graph_still_verify(tmp_path):
    """A store written by the old linear-chain code migrates in place and keeps verifying."""
    old_fields = audit.HASHED_FIELDS[1]
    rec = {"id": "legacy000001", "seq": 1, "created_at": "2026-10-23T09:00:00+00:00", "incident_id": "INC-0",
           "responder_id": "EMT", "device_id": "laptop-01", "language": "en", "transcript": "t",
           "transcript_sha256": hashlib.sha256(b"t").hexdigest(), "audio_sha256": None,
           "draft": BASE, "final": BASE, "edits": [], "model_info": {}}
    body = audit.canonical({k: rec[k] for k in old_fields})
    rec["prev_hash"] = audit.GENESIS
    rec["hash"] = hashlib.sha256((audit.GENESIS + body).encode()).hexdigest()
    with sqlite3.connect(config.DB_PATH) as c:
        c.execute("""CREATE TABLE records (seq INTEGER PRIMARY KEY, id TEXT UNIQUE, created_at TEXT,
            incident_id TEXT, responder_id TEXT, device_id TEXT, language TEXT, transcript TEXT,
            transcript_sha256 TEXT, audio_sha256 TEXT, draft TEXT, final TEXT, edits TEXT,
            model_info TEXT, prev_hash TEXT, hash TEXT)""")
        row = {k: (audit.canonical(v) if k in audit.JSON_FIELDS else v) for k, v in rec.items()}
        c.execute(f"INSERT INTO records ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    assert audit.verify()["ok"]
    v2 = audit.amend("legacy000001", responder_id="EMT", reason="r", final=with_dose(BASE, 325))
    assert v2["parent_hash"] == rec["hash"]
    assert audit.verify()["ok"]


def test_amendment_from_a_different_note_is_rejected():
    """Regression: an unrelated new dictation was signed as v2 of an older record."""
    v1 = create()
    other_note = dict(with_dose(BASE, 2), reference_time="2026-10-24T14:06:50+05:30")
    with pytest.raises(audit.HistoryError, match="different note"):
        audit.amend(v1["id"], responder_id="EMT", reason="r", final=other_note)
    foreign = with_dose(BASE, 2)
    foreign["medications"][0]["evidence"] = "Ibuprofen has been reduced to 2 mg per cc"
    with pytest.raises(audit.HistoryError, match="not in the record's transcript"):
        audit.amend(v1["id"], responder_id="EMT", reason="r", final=foreign)


def test_changes_are_described_for_people():
    old = dict(BASE, medications=[dict(BASE["medications"][0], evidence="aspirin 300", rxnorm="1191"),
                                  {"drug": "nitroglycerin", "dose_value": 0.4, "dose_unit": "mg", "route": "SL"}])
    new = dict(BASE, medications=[dict(BASE["medications"][0], dose_value=325.0, evidence="changed", rxnorm="x")])
    rows = audit.describe_edits(audit.diff_records(old, new))
    assert {"label": "Medication 1 · dose value", "before": "300", "after": "325", "change": "changed"} in rows
    assert {"label": "Medication 2", "before": "nitroglycerin 0.4 mg SL", "after": "—", "change": "removed"} in rows
    assert not any("evidence" in r["label"] or "rxnorm" in r["label"] for r in rows)
