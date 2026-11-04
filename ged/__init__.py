"""GED interrogeable — POC."""


def version() -> str:
    """Version installée (pyproject), ex. 0.9.0b1."""
    try:
        from importlib.metadata import version as _v

        return _v("alambic-search")
    except Exception:  # pragma: no cover — code lancé hors installation
        return "dev"


def version_label(v: str | None = None) -> str:
    """Version lisible : 0.9.0b1 → « 0.9.0 bêta 1 »."""
    import re

    v = v or version()
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", v)
    if not m or not m.group(2):
        return v
    return f"{m.group(1)} {dict(a='alpha', b='bêta', rc='RC')[m.group(2)]} {m.group(3)}"
