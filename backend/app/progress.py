"""In-memory progress tracking for analysis runs."""
from __future__ import annotations

import threading

_lock = threading.Lock()
_progress: dict[str, dict] = {}  # upload_id -> {step, detail, pct}


def set_progress(upload_id: str, step: str, detail: str, pct: int) -> None:
    with _lock:
        _progress[upload_id] = {"step": step, "detail": detail, "pct": pct}


def get_progress(upload_id: str) -> dict | None:
    with _lock:
        return _progress.get(upload_id)


def clear_progress(upload_id: str) -> None:
    with _lock:
        _progress.pop(upload_id, None)
