"""Append-only, hash-chained record store with a hash-linked version graph (SQLite).

Two kinds of hash link protect the history:

* The **chain** (log order): every entry's hash = SHA-256(prev_hash + canonical JSON of its
  content), so changing, deleting or reordering any stored entry breaks it.
* The **version graph** (record history): a signed record is never changed. A correction is a
  new *version* whose ``parent`` / ``parent_hash`` point at the version it corrects, and both
  are covered by its own hash, like a git commit. Amending an older version starts a branch.

Entry kinds:

* ``create`` – the first version of a record; its ``lineage`` (record id) is its own id.
* ``amend``  – a new version; ``parent`` is the version it was made from.
* ``switch`` – a signed decision that another version is now current; ``parent`` is the
  version that was current, ``target`` the version that becomes current.

The current version of a record is the newest version created in it, unless a later switch
says otherwise. ``verify()`` checks the chain and every graph link.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import config

GENESIS = "0" * 64
VERSION_KINDS = ("create", "amend")

# Fields covered by the hash, in a fixed order. v1 = entries written before the version graph
# existed; they keep verifying under the fields they were signed with.
HASHED_FIELDS = {
    1: ("id", "seq", "created_at", "incident_id", "responder_id", "device_id", "language",
        "transcript", "transcript_sha256", "audio_sha256", "draft", "final", "edits", "model_info"),
}
HASHED_FIELDS[2] = HASHED_FIELDS[1] + ("kind", "lineage", "parent", "parent_hash", "reason", "target")
HASH_VERSION = 2

JSON_FIELDS = ("draft", "final", "edits", "model_info")
GRAPH_COLUMNS = {"kind": "TEXT", "lineage": "TEXT", "parent": "TEXT", "parent_hash": "TEXT",
                 "reason": "TEXT", "target": "TEXT", "hash_v": "INTEGER"}


class HistoryError(ValueError):
    """A request that would make an invalid history (unknown version, no change, ...)."""


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
    have = {r["name"] for r in c.execute("PRAGMA table_info(records)")}
    for col, typ in GRAPH_COLUMNS.items():  # migrate stores created before the version graph
        if col not in have:
            c.execute(f"ALTER TABLE records ADD COLUMN {col} {typ}")
    return c


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_hash(prev_hash: str, content: dict, version: int = HASH_VERSION) -> str:
    body = canonical({k: content.get(k) for k in HASHED_FIELDS[version]})
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def diff_records(draft: dict, final: dict, path: str = "") -> list[dict]:
    """Field-level diff: what changed between two versions (or machine draft and signed record)."""
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
    for k in JSON_FIELDS:
        d[k] = json.loads(d[k]) if d[k] else None
    if d.get("hash_v") is None:  # entry from before the version graph: a plain create
        d.update(hash_v=1, kind="create", lineage=d["id"])
    return d


def _all(c: sqlite3.Connection) -> list[dict]:
    return [_row_to_dict(r) for r in c.execute("SELECT * FROM records ORDER BY seq").fetchall()]


def _insert(c: sqlite3.Connection, rec: dict) -> dict:
    """Give `rec` its place in the chain (seq, prev_hash), hash it and store it."""
    last = c.execute("SELECT seq, hash FROM records ORDER BY seq DESC LIMIT 1").fetchone()
    rec["seq"] = (last["seq"] + 1) if last else 1
    rec["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["device_id"] = config.DEVICE_ID
    rec["hash_v"] = HASH_VERSION
    rec["prev_hash"] = last["hash"] if last else GENESIS
    rec["hash"] = compute_hash(rec["prev_hash"], rec)
    row = {k: (canonical(v) if k in JSON_FIELDS and v is not None else v) for k, v in rec.items()}
    c.execute(f"INSERT INTO records ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    return rec


def _current(rows: list[dict], lineage: str) -> str | None:
    """Replay a record's entries in log order to find its current version."""
    cur = None
    for r in rows:
        if r["lineage"] != lineage:
            continue
        cur = r["target"] if r["kind"] == "switch" else r["id"]
    return cur


def _check_same_source(parent: dict, final: dict, transcript: str) -> None:
    """An amendment corrects or extends the same record. Every quoted item must come from the record's
    transcript (the original dictation plus any addenda signed with this amendment); a different note
    must be signed as a new record."""
    from .normalize import evidence_in_transcript  # local import: normalize is heavier than this module

    if final.get("reference_time") != (parent["final"] or {}).get("reference_time"):
        raise HistoryError("this looks like a different note (its recording time differs from the record "
                           "being amended); sign it as a new record instead")
    foreign = [item["evidence"] for key in ("medications", "procedures", "vitals")
               for item in final.get(key) or []
               if item.get("evidence") and not evidence_in_transcript(item["evidence"], transcript)]
    if foreign:
        raise HistoryError("these quotes are not in the record's transcript or its addendum: "
                           + "; ".join(f"“{q}”" for q in foreign[:3]) + ". Sign a different note as a new record")


def _with_addenda(parent: dict, addenda: list[dict]) -> tuple[str, dict]:
    """The amended version's transcript (original + addenda) and model_info (addendum audio and models)."""
    transcript = parent["transcript"] or ""
    for a in addenda:
        stamp = (a.get("recorded_at") or "")[11:16]
        transcript += f"\n\n[Addendum{' ' + stamp if stamp else ''}] {a['transcript'].strip()}"
    info = dict(parent["model_info"] or {})
    if addenda:
        info["addenda"] = [*info.get("addenda", []), *[
            {"recorded_at": a.get("recorded_at"), "audio_sha256": a.get("audio_sha256"),
             "transcript_sha256": hashlib.sha256(a["transcript"].strip().encode("utf-8")).hexdigest(),
             "model_info": a.get("model_info") or {}} for a in addenda]]
    return transcript, info


# Fields that are bookkeeping, not clinical content: kept in the stored diff, hidden when shown to people.
HIDDEN_FIELDS = {"drug_raw", "name_raw", "time_raw", "evidence", "evidence_found", "warnings", "rxnorm", "code",
                  "chief_complaint_warnings", "reference_time"}
_LIST_LABELS = {"medications": "Medication", "procedures": "Procedure", "vitals": "Vital"}


def _show(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, dict):  # a whole item added or removed
        if "drug" in value:
            dose = f" {value['dose_value']:g} {value.get('dose_unit') or ''}" if value.get("dose_value") is not None else ""
            return f"{value['drug']}{dose} {value.get('route') or ''}".strip()
        if "name" in value:
            return value["name"] + (f" ({value['details']})" if value.get("details") else "")
        if "type" in value:
            return f"{value['type']} {value.get('value', '')} {value.get('unit') or ''}".strip()
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str) and len(value) >= 16 and value[10:11] == "T":  # ISO time
        return value[11:16]
    return str(value)


def describe_edits(edits: list[dict]) -> list[dict]:
    """Stored field diff → rows a person can read: label, before, after."""
    rows = []
    for e in edits or []:
        path = e["field"]
        leaf = path.rsplit(".", 1)[-1]
        if leaf in HIDDEN_FIELDS:
            continue
        parts = path.split(".")
        head = parts[0]
        if "[" in head:
            name, idx = head[:-1].split("[")
            label = f"{_LIST_LABELS.get(name, name)} {int(idx) + 1}"
        else:
            label = head.replace("_", " ").capitalize()
        if len(parts) > 1:
            label += " · " + " ".join(p.replace("_", " ") for p in parts[1:])
        before, after = _show(e["machine"]), _show(e["human"])
        change = "added" if e["machine"] is None and isinstance(e["human"], dict) else \
            "removed" if e["human"] is None and isinstance(e["machine"], dict) else "changed"
        rows.append({"label": label, "before": before, "after": after, "change": change})
    return rows


# ---------- writing ----------

def append(*, incident_id: str, responder_id: str, transcript: str, language: str | None,
           audio_sha256: str | None, draft: dict, final: dict, model_info: dict) -> dict:
    """Sign a new record: its first version (v1)."""
    rid = uuid.uuid4().hex[:12]
    with _conn() as c:
        return _insert(c, {
            "id": rid, "kind": "create", "lineage": rid, "parent": None, "parent_hash": None,
            "reason": None, "target": None,
            "incident_id": incident_id, "responder_id": responder_id, "language": language,
            "transcript": transcript,
            "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            "audio_sha256": audio_sha256, "draft": draft, "final": final,
            "edits": diff_records(draft, final), "model_info": model_info,
        })


def amend(version_id: str, *, responder_id: str, reason: str, final: dict,
          addenda: list[dict] | None = None) -> dict:
    """Sign a corrected version made from `version_id` (any version: an older one starts a branch).

    `addenda` are follow-up notes dictated while amending ({transcript, recorded_at, audio_sha256,
    model_info}); they are appended to the record's transcript so the items they add stay grounded.
    """
    if not (reason or "").strip():
        raise HistoryError("a reason is required to amend a signed record")
    addenda = [a for a in addenda or [] if (a.get("transcript") or "").strip()]
    with _conn() as c:
        rows = _all(c)
        parent = next((r for r in rows if r["id"] == version_id), None)
        if parent is None or parent["kind"] not in VERSION_KINDS:
            raise HistoryError(f"no record version {version_id!r}")
        transcript, model_info = _with_addenda(parent, addenda)
        _check_same_source(parent, final, transcript)
        edits = diff_records(parent["final"], final)
        if not edits:
            raise HistoryError("nothing changed compared with the version being amended")
        return _insert(c, {
            "id": uuid.uuid4().hex[:12], "kind": "amend", "lineage": parent["lineage"],
            "parent": parent["id"], "parent_hash": parent["hash"], "reason": reason.strip(), "target": None,
            "incident_id": parent["incident_id"], "responder_id": responder_id, "language": parent["language"],
            # the original source plus any addenda; the original recording's audio hash is kept
            "transcript": transcript,
            "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest() if addenda
            else parent["transcript_sha256"],
            "audio_sha256": parent["audio_sha256"], "model_info": model_info,
            "draft": parent["final"], "final": final, "edits": edits,
        })


def switch(record_id: str, target_id: str, *, responder_id: str, reason: str) -> dict:
    """Sign the decision that `target_id` is now the current version of the record."""
    if not (reason or "").strip():
        raise HistoryError("a reason is required to change the current version")
    with _conn() as c:
        rows = _all(c)
        by_id = {r["id"]: r for r in rows}
        lineage = by_id[record_id]["lineage"] if record_id in by_id else None
        target = by_id.get(target_id)
        if lineage is None or target is None or target["kind"] not in VERSION_KINDS or target["lineage"] != lineage:
            raise HistoryError("target is not a version of this record")
        cur = by_id[_current(rows, lineage)]
        if cur["id"] == target_id:
            raise HistoryError("that version is already current")
        return _insert(c, {
            "id": uuid.uuid4().hex[:12], "kind": "switch", "lineage": lineage,
            "parent": cur["id"], "parent_hash": cur["hash"], "target": target_id, "reason": reason.strip(),
            "incident_id": cur["incident_id"], "responder_id": responder_id, "language": None,
            "transcript": None, "transcript_sha256": None, "audio_sha256": None, "model_info": None,
            "draft": None, "final": None, "edits": diff_records(cur["final"], target["final"]),
        })


# ---------- reading ----------

def get(entry_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM records WHERE id = ?", (entry_id,)).fetchone()
    return _row_to_dict(r) if r else None


def list_records() -> list[dict]:
    """Every chain entry, newest first."""
    with _conn() as c:
        return _all(c)[::-1]


def history(entry_id: str) -> dict | None:
    """The version graph of the record that `entry_id` belongs to."""
    with _conn() as c:
        rows = _all(c)
    me = next((r for r in rows if r["id"] == entry_id), None)
    if me is None:
        return None
    lineage = me["lineage"]
    entries = [r for r in rows if r["lineage"] == lineage]
    versions = [r for r in entries if r["kind"] in VERSION_KINDS]
    label = {v["id"]: f"v{i}" for i, v in enumerate(versions, start=1)}
    children = {v["id"]: [] for v in versions}
    for v in versions:
        if v["parent"]:
            children[v["parent"]].append(v["id"])
    current = _current(rows, lineage)
    return {
        "lineage": lineage,
        "incident_id": versions[0]["incident_id"],
        "current": current,
        "current_label": label[current],
        "versions": [{
            "id": v["id"], "label": label[v["id"]], "seq": v["seq"], "kind": v["kind"],
            "created_at": v["created_at"], "responder_id": v["responder_id"], "reason": v["reason"],
            "parent": v["parent"], "parent_label": label.get(v["parent"]), "parent_hash": v["parent_hash"],
            "hash": v["hash"], "children": children[v["id"]], "edits": v["edits"] or [],
            "changes": describe_edits(v["edits"]),
            "is_current": v["id"] == current, "final": v["final"],
            "meds": len((v["final"] or {}).get("medications", [])), "procs": len((v["final"] or {}).get("procedures", [])),
        } for v in versions],
        "events": [{
            "id": e["id"], "seq": e["seq"], "kind": e["kind"], "created_at": e["created_at"],
            "responder_id": e["responder_id"], "reason": e["reason"], "hash": e["hash"],
            "version": label.get(e["target"] if e["kind"] == "switch" else e["id"]),
            "from": label.get(e["parent"]),
        } for e in entries],
    }


def list_lineages() -> list[dict]:
    """One row per record (its current version), most recently changed first."""
    with _conn() as c:
        rows = _all(c)
    by_id = {r["id"]: r for r in rows}
    out = {}
    for r in rows:
        lin = out.setdefault(r["lineage"], {"versions": 0, "parents": set(), "first_seq": r["seq"]})
        lin["last_seq"], lin["updated_at"] = r["seq"], r["created_at"]
        if r["kind"] in VERSION_KINDS:
            lin["versions"] += 1
            if r["parent"]:
                lin["parents"].add(r["parent"])
    result = []
    for lineage, lin in out.items():
        cur = by_id[_current(rows, lineage)]
        leaves = sum(1 for r in rows if r["lineage"] == lineage and r["kind"] in VERSION_KINDS
                     and r["id"] not in lin["parents"])
        result.append({
            "lineage": lineage, "id": cur["id"], "seq": lin["first_seq"], "last_seq": lin["last_seq"],
            "created_at": by_id[lineage]["created_at"], "updated_at": lin["updated_at"],
            "incident_id": cur["incident_id"], "responder_id": cur["responder_id"], "hash": cur["hash"],
            "meds": len(cur["final"]["medications"]), "procs": len(cur["final"]["procedures"]),
            "edits": len(by_id[lineage]["edits"] or []), "versions": lin["versions"], "branches": leaves,
        })
    return sorted(result, key=lambda x: x["last_seq"], reverse=True)


def version_info(entry_id: str) -> dict | None:
    """A version plus its place in the record's history (for the report and FHIR export)."""
    rec = get(entry_id)
    if rec is None or rec["kind"] not in VERSION_KINDS:
        return None
    h = history(entry_id)
    me = next(v for v in h["versions"] if v["id"] == entry_id)
    rec.update(version_label=me["label"], version_count=len(h["versions"]), is_current=me["is_current"],
               current_id=h["current"], current_label=h["current_label"], parent_label=me["parent_label"])
    return rec


# ---------- verification ----------

def verify() -> dict:
    """Check the chain from genesis, then every link of every version graph."""
    with _conn() as c:
        rows = _all(c)
    n = len(rows)

    def broken(r, reason):
        return {"ok": False, "count": n, "broken_at": r["id"], "reason": reason}

    prev = GENESIS
    for i, r in enumerate(rows, start=1):
        if r["seq"] != i:
            return broken(r, f"sequence gap before seq {r['seq']}")
        if r["prev_hash"] != prev:
            return broken(r, "prev_hash does not match previous record")
        if compute_hash(prev, r, r["hash_v"]) != r["hash"]:
            return broken(r, "content changed after signing")
        prev = r["hash"]

    seen: dict[str, dict] = {}
    current: dict[str, str] = {}
    for r in rows:  # graph links; `seen` only holds earlier entries, so links can only point back
        kind = r["kind"]
        if kind == "create":
            if r["lineage"] != r["id"] or r["parent"] or r["parent_hash"]:
                return broken(r, "first version must start its own record")
            current[r["id"]] = r["id"]
        elif kind in ("amend", "switch"):
            parent = seen.get(r["parent"])
            if parent is None or parent["kind"] not in VERSION_KINDS:
                return broken(r, "parent version missing or not earlier in the log")
            if parent["lineage"] != r["lineage"]:
                return broken(r, "parent belongs to a different record")
            if parent["hash"] != r["parent_hash"]:
                return broken(r, "parent_hash does not match the parent version")
            if kind == "amend":
                current[r["lineage"]] = r["id"]
            else:
                target = seen.get(r["target"])
                if target is None or target["kind"] not in VERSION_KINDS or target["lineage"] != r["lineage"]:
                    return broken(r, "switch target is not an earlier version of this record")
                if current.get(r["lineage"]) != r["parent"]:
                    return broken(r, "switch does not start from the version that was current")
                if r["target"] == r["parent"]:
                    return broken(r, "switch does not change the current version")
                current[r["lineage"]] = r["target"]
        else:
            return broken(r, f"unknown entry kind {kind!r}")
        seen[r["id"]] = r

    versions = sum(1 for r in rows if r["kind"] in VERSION_KINDS)
    return {"ok": True, "count": n, "head": prev, "records": len(current), "versions": versions,
            "switches": n - versions}
