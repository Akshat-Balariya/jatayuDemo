"""Live demo: silently edit a signed record in the database and show the audit chain catch it.

Usage (server running):  python scripts/tamper_demo.py [--restore]
"""
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "records.db"
BACKUP = DB.with_suffix(".tamper-backup.json")


def verify():
    return json.load(urllib.request.urlopen("http://localhost:8000/api/audit/verify"))


db = sqlite3.connect(DB)
if "--restore" in sys.argv:
    seq, final = json.loads(BACKUP.read_text(encoding="utf-8"))
    db.execute("UPDATE records SET final = ? WHERE seq = ?", (final, seq))
    db.commit()
    BACKUP.unlink()
    print("restored  ->", verify())
    sys.exit()

seq, final = db.execute("SELECT seq, final FROM records ORDER BY seq LIMIT 1").fetchone()
rec = json.loads(final)
if not rec["medications"]:
    sys.exit("first record has no medications to tamper with")
BACKUP.write_text(json.dumps([seq, final]), encoding="utf-8")
med = rec["medications"][0]
old = med["dose_value"]
med["dose_value"] = (old or 1) * 10
db.execute("UPDATE records SET final = ? WHERE seq = ?", (json.dumps(rec, ensure_ascii=False), seq))
db.commit()
print(f"before tamper: chain was signed")
print(f"tampered: record #{seq} {med['drug']} dose {old} -> {med['dose_value']}")
print("verify    ->", verify())
print("run with --restore to undo")
