"""Evaluate extraction (local LLM + normaliser) against hand-labelled synthetic dictations.

Usage:  python -m eval.run_eval [--model qwen2.5:7b] [--lang en|hinglish|hi]

Metrics (micro-averaged, multiset matching so repeated doses count):
  drug       - canonical drug name correct
  full_med   - drug + dose value + unit + route all correct (the safety-relevant number)
  procedure  - canonical procedure name correct
  complaint  - for notes labelled with it, the chief complaint names the right body part
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from app import extractor
from app.normalize import normalize

HERE = Path(__file__).parent


def prf(gold: Counter, pred: Counter) -> tuple[int, int, int]:
    tp = sum((gold & pred).values())
    return tp, sum(pred.values()) - tp, sum(gold.values()) - tp


def score(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def med_key(m: dict, full: bool):
    if not full:
        return m["drug"]
    dose = None if m.get("dose_value") is None else round(float(m["dose_value"]), 3)
    return (m["drug"], dose, m.get("dose_unit"), m.get("route"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--lang")
    ap.add_argument("--out", default=str(HERE / "results.json"))
    args = ap.parse_args()

    cases = [json.loads(l) for l in (HERE / "dictations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.lang:
        cases = [c for c in cases if c["lang"] == args.lang]

    totals = {k: [0, 0, 0] for k in ("drug", "full_med", "procedure")}
    by_lang: dict[str, dict] = {}
    latencies, details, complaint_hits = [], [], []
    ref = datetime.now().astimezone()

    for c in cases:
        t0 = time.perf_counter()
        raw, _ = extractor.extract(c["text"], model=args.model)
        latencies.append(time.perf_counter() - t0)
        rec = normalize(raw, c["text"], ref).model_dump()
        row = {"id": c["id"], "errors": []}
        for kind, gold, pred in (
            ("drug", Counter(med_key(m, False) for m in c["meds"]), Counter(med_key(m, False) for m in rec["medications"])),
            ("full_med", Counter(med_key(m, True) for m in c["meds"]), Counter(med_key(m, True) for m in rec["medications"])),
            ("procedure", Counter(c["procedures"]), Counter(p["name"] for p in rec["procedures"])),
        ):
            tp, fp, fn = prf(gold, pred)
            for i, v in enumerate((tp, fp, fn)):
                totals[kind][i] += v
                by_lang.setdefault(c["lang"], {k: [0, 0, 0] for k in totals})[kind][i] += v
            if fp or fn:
                row["errors"].append({"kind": kind, "missing": list(map(str, (gold - pred).elements())),
                                      "extra": list(map(str, (pred - gold).elements()))})
        if "complaint" in c:
            cc = (rec["chief_complaint"] or "").lower()
            ok = any(k in cc for k in c["complaint"])
            complaint_hits.append(ok)
            if not ok:
                row["errors"].append({"kind": "complaint", "expected": c["complaint"], "got": rec["chief_complaint"]})
        details.append(row)
        flag = "ok " if not row["errors"] else "ERR"
        print(f"[{flag}] {c['id']:7s} {latencies[-1]:5.1f}s", *(f"\n        {e}" for e in row["errors"]))

    print("\n=== Results (%d notes, model %s) ===" % (len(cases), args.model or extractor.config.LLM_MODEL))
    summary = {}
    for scope, t in [("all", totals)] + sorted(by_lang.items()):
        line = []
        for kind, (tp, fp, fn) in t.items():
            p, r, f = score(tp, fp, fn)
            summary.setdefault(scope, {})[kind] = {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3)}
            line.append(f"{kind} P={p:.2f} R={r:.2f} F1={f:.2f}")
        print(f"{scope:9s} | " + " | ".join(line))
    if complaint_hits:
        summary["complaint_accuracy"] = round(sum(complaint_hits) / len(complaint_hits), 3)
        print(f"complaint | {sum(complaint_hits)}/{len(complaint_hits)} chief complaints name the right body part")
    summary["latency_s"] = {"median": round(statistics.median(latencies), 2), "max": round(max(latencies), 2)}
    print(f"latency   | median {summary['latency_s']['median']}s, max {summary['latency_s']['max']}s")
    Path(args.out).write_text(json.dumps({"summary": summary, "details": details}, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
