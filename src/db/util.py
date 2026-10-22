"""Small shared helpers for the data layer (Phase 6)."""

from __future__ import annotations

import posixpath
import re
from datetime import datetime, timezone
from typing import Any, Optional


def parse_dt(value: Any) -> Optional[datetime]:
    """ISO string / datetime -> naive UTC datetime (None if empty or unparseable)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def normalize_rel_path(path_str: Optional[str]) -> Optional[str]:
    """
    Path relative to DOWNLOADS_DIR with forward slashes. Returns None for empty values and for
    absolute paths from another machine that contain no 'downloads/' segment to anchor on.
    """
    if not path_str or not str(path_str).strip():
        return None
    clean = str(path_str).replace("\\", "/").strip()
    low = clean.lower()
    if "/downloads/" in low:
        clean = clean[low.index("/downloads/") + len("/downloads/"):]
    elif low.startswith("downloads/"):
        clean = clean[len("downloads/"):]
    if re.match(r"^[a-zA-Z]:", clean) or clean.startswith("/"):
        return None
    clean = posixpath.normpath(clean)
    return None if clean.startswith("..") or clean == "." else clean
