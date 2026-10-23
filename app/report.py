"""Human-readable patient care record (HTML, print-to-PDF from the browser)."""
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config

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


def render(rec: dict) -> str:
    edited = {e["field"].split(".")[0] for e in rec.get("edits") or []}
    return _env.get_template("report.html").render(rec=rec, f=rec["final"], edited=edited)
