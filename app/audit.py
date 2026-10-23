"""Append-only, hash-chained record store (SQLite).

Each record's hash = SHA-256(prev_hash + canonical JSON of its content). Changing any
stored byte, deleting a row, or reordering rows breaks the chain, which `verify()` reports.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import config

GENESIS = "0" * 64

# Fields covered by the hash, in a fixed order.
HASHED_FIELDS = (
    "id", "seq", "created_at", "incident_id", "responder_id", "device_id", "language",
    "transcript", "transcript_sha256", "audio_sha256", "draft", "final", "edits", "model_info",
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE IF NOT EXISTS records (
            seq INTEGER PRIMARY KEY, id TEXT UNIQUE, created_at TEXT, incident_id TEXT,
            responder_id TEXT, device_id TEXT, language TEXT, transcript TEXT,
            transcript_sha256 TEXT, audio_sha256 TEXT, draft TEXT, final TEXT, edits TEXT,
            model_info TEXT, prev_hash TEXT, hash TEXT)"""
    )
    return c


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_hash(prev_hash: str, content: dict) -> str:
    body = canonical({k: content[k] for k in HASHED_FIELDS})
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def diff_records(draft: dict, final: dict, path: str = "") -> list[dict]:
    """Field-level diff so the audit trail shows exactly what the human changed."""
    out = []
    if isinstance(draft, dict) and isinstance(final, dict):
        for k in sorted(set(draft) | set(final)):
            if k in ("warnings", "evidence_found"):
                continue
            out += diff_records(draft.get(k), final.get(k), f"{path}.{k}" if path else k)
    elif isinstance(draft, list) and isinstance(final, list):
        for i in range(max(len(draft), len(final))):
            a = draft[i] if i < len(draft) else None
            b = final[i] if i < len(final) else None
            out += diff_records(a, b, f"{path}[{i}]")
    elif draft != final:
        out.append({"field": path, "machine": draft, "human": final})
    return out


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("draft", "final", "edits", "model_info"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def append(*, incident_id: str, responder_id: str, transcript: str, language: str | None,
           audio_sha256: str | None, draft: dict, final: dict, model_info: dict) -> dict:
    with _conn() as c:
        last = c.execute("SELECT seq, hash FROM records ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = last["hash"] if last else GENESIS
        rec = {
            "id": uuid.uuid4().hex[:12],
            "seq": (last["seq"] + 1) if last else 1,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "incident_id": incident_id,
            "responder_id": responder_id,
            "device_id": config.DEVICE_ID,
            "language": language,
            "transcript": transcript,
            "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            "audio_sha256": audio_sha256,
            "draft": draft,
            "final": final,
            "edits": diff_records(draft, final),
            "model_info": model_info,
        }
        rec["prev_hash"] = prev_hash
        rec["hash"] = compute_hash(prev_hash, rec)
        row = {k: (canonical(v) if k in ("draft", "final", "edits", "model_info") else v) for k, v in rec.items()}
        c.execute(f"INSERT INTO records ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    return rec


def get(record_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    return _row_to_dict(r) if r else None


def list_records() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM records ORDER BY seq DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def verify() -> dict:
    """Walk the chain from genesis; report the first broken link, if any."""
    with _conn() as c:
        rows = [_row_to_dict(r) for r in c.execute("SELECT * FROM records ORDER BY seq").fetchall()]
    prev = GENESIS
    for i, r in enumerate(rows, start=1):
        if r["seq"] != i:
            return {"ok": False, "count": len(rows), "broken_at": r["id"], "reason": f"sequence gap before seq {r['seq']}"}
        if r["prev_hash"] != prev:
            return {"ok": False, "count": len(rows), "broken_at": r["id"], "reason": "prev_hash does not match previous record"}
        if compute_hash(prev, r) != r["hash"]:
            return {"ok": False, "count": len(rows), "broken_at": r["id"], "reason": "content changed after signing"}
        prev = r["hash"]
    return {"ok": True, "count": len(rows), "head": prev}
