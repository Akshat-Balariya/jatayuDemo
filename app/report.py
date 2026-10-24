"""Human-readable patient care record (HTML, print-to-PDF from the browser)."""
import re
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import audit, config

_env = Environment(
    loader=FileSystemLoader(config.ROOT / "app" / "templates"),
    autoescape=select_autoescape(["html"]),
)


def _parse(value):
    try:
        return datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return None


_env.filters["dt"] = lambda v: (d.strftime("%d %b %Y %H:%M") if (d := _parse(v)) else (v or "-"))
_env.filters["num"] = lambda v: "-" if v is None else f"{v:g}"
_env.filters["hm"] = lambda v: (d.strftime("%H:%M") if (d := _parse(v)) else (v or "-"))


_ADDENDUM_RE = re.compile(r"\n\n\[Addendum(?: (\d\d:\d\d))?\] ")
_MODES = {"llm": "Local LLM", "llm-retry": "Local LLM (output repaired on retry)",
          "rules-fallback": "Rule-based draft (LLM unavailable)"}


def dictation_parts(transcript: str | None) -> list[dict]:
    """The original dictation and each addendum signed with an amendment, as separate blocks."""
    pieces = _ADDENDUM_RE.split(transcript or "")
    parts = [{"label": "Original dictation", "text": pieces[0]}]
    for i in range(1, len(pieces), 2):
        stamp, text = pieces[i], pieces[i + 1]
        parts.append({"label": f"Addendum {len(parts)}" + (f" · {stamp}" if stamp else ""), "text": text})
    return parts


def model_rows(info: dict | None) -> list[tuple[str, str]]:
    """model_info as labelled rows instead of raw JSON."""
    info = dict(info or {})
    rows = []
    if "asr_model" in info:
        rows.append(("Speech-to-text", f"{info.pop('asr_model')} on {info.pop('asr_device', '?')}"))
    if "llm_model" in info:
        latency = info.pop("llm_latency_s", None)
        rows.append(("Language model", info.pop("llm_model") + (f" · {latency:g} s" if latency is not None else "")))
    harness = info.pop("harness", None)
    if harness:
        rows.append(("Extraction", _MODES.get(harness.get("mode"), harness.get("mode", "?"))))
        statuses = harness.get("checks", {})
        counts = {s: sum(v == s for v in statuses.values()) for s in ("pass", "fixed", "flag")}
        flagged = [k for k, v in statuses.items() if v != "pass"]
        rows.append(("Harness checks", f"{counts['pass']} passed, {counts['fixed']} fixed, {counts['flag']} flagged"
                     + (f" ({', '.join(flagged)})" if flagged else "")))
    for i, a in enumerate(info.pop("addenda", []), start=1):
        m = a.get("model_info") or {}
        bits = [f"recorded {(a.get('recorded_at') or '')[11:16] or '?'}",
                f"{m['llm_model']}" if m.get("llm_model") else "",
                f"audio {a['audio_sha256'][:12]}…" if a.get("audio_sha256") else "typed",
                f"text {a['transcript_sha256'][:12]}…" if a.get("transcript_sha256") else ""]
        rows.append((f"Addendum {i}", " · ".join(b for b in bits if b)))
    rows += [(k.replace("_", " ").capitalize(), str(v)) for k, v in info.items()]  # anything unexpected, as is
    return rows


def render(rec: dict) -> str:
    changes = audit.describe_edits(rec.get("edits"))
    edited = {e["field"].split(".")[0] for e in rec.get("edits") or []
              if e["field"].rsplit(".", 1)[-1] not in audit.HIDDEN_FIELDS}
    return _env.get_template("report.html").render(
        rec=rec, f=rec["final"], edited=edited, changes=changes,
        dictation=dictation_parts(rec.get("transcript")), models=model_rows(rec.get("model_info")))
