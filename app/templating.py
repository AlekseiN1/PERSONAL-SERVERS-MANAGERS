"""Shared Jinja2 templates object (avoids circular imports)."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _static_version() -> str:
    """style.css mtime as a query param — so the browser doesn't serve a stale cached file."""
    css = STATIC_DIR / "style.css"
    return str(int(css.stat().st_mtime)) if css.exists() else "0"


templates.env.globals["static_version"] = _static_version
