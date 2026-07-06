"""Small shared helpers."""

import re

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Turn a free-text task name into a safe entity-id/logger-name fragment."""
    slug = _SLUG_INVALID_RE.sub("_", name.strip().lower()).strip("_")
    return slug or "task"
