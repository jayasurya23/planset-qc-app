"""Shared job registry + bounded analysis queue (single-replica, in-memory).

Every planset analysis (new run or re-analysis) is submitted here instead of
spawning an unbounded thread. A ``ThreadPoolExecutor`` with ``ANALYZE_CONCURRENCY``
workers runs at most N analyses at once; the rest wait in the executor's queue
with status ``queued``. The registry tracks each job's lifecycle
(queued -> running -> done/error) plus who started it, so every signed-in
engineer can watch the same shared "what's processing right now" feed
(``GET /api/jobs``) and the originator can be alerted on completion.

Single-replica only (matches the current deployment: SQLite on Azure Files,
one always-on replica). If the app is ever scaled to multiple replicas, this
in-memory registry/queue must move to a shared backing (DB or a real queue) —
the ``/api/jobs`` contract can stay the same.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from .progress import set_progress

# At most this many analyses run concurrently. Kept low by default because a
# single replica shares one OpenAI key and CPU — too many parallel deep runs
# thrash memory and risk rate limits. Override with ANALYZE_CONCURRENCY.
CONCURRENCY = max(1, int(os.getenv("ANALYZE_CONCURRENCY", "2")))

# How many finished (done/error) jobs to keep visible in the feed, and for how
# long, so the panel can show "just completed" and fire the completion alert.
_RETAIN_COUNT = 40
_RETAIN_SECS = 30 * 60


@dataclass
class Job:
    id: str  # == upload_id
    kind: str  # "analyze" | "reanalyze"
    project_name: Optional[str] = None
    run_name: Optional[str] = None
    started_by: Optional[str] = None  # display name (or email)
    created_by: Optional[str] = None  # email identity key
    status: str = "queued"  # queued | running | done | error
    submitted_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    run_id: Optional[str] = None
    error: Optional[str] = None


_lock = threading.RLock()
_jobs: dict[str, Job] = {}
_order: list[str] = []  # submission order (oldest first)
_executor = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="analysis")


def _now() -> float:
    return time.time()


def submit(job_id: str, kind: str, meta: dict[str, Any], fn: Callable[[], Any]) -> None:
    """Register a job (status=queued) and hand it to the bounded executor.

    ``fn`` is the actual analysis work; it must return the saved run dict on
    success (carrying an ``id``) or a dict with an ``error`` key on failure.
    """
    with _lock:
        _jobs[job_id] = Job(
            id=job_id,
            kind=kind,
            project_name=meta.get("project_name"),
            run_name=meta.get("run_name"),
            started_by=meta.get("started_by"),
            created_by=meta.get("created_by"),
            status="queued",
            submitted_at=_now(),
        )
        _order.append(job_id)
        _prune_locked()
    # Show "queued" immediately; the worker flips it to "analyzing" when a slot
    # opens. (If a slot is already free the executor starts it right away.)
    set_progress(job_id, "queued", "Queued — waiting for an open analysis slot…", 2)
    _executor.submit(_run_wrapped, job_id, fn)


def _run_wrapped(job_id: str, fn: Callable[[], Any]) -> None:
    with _lock:
        j = _jobs.get(job_id)
        if j is not None:
            j.status = "running"
            j.started_at = _now()
    set_progress(job_id, "analyze", "Starting analysis…", 10)
    result: Any = None
    try:
        result = fn()
    except Exception as exc:  # belt-and-braces; fn handles its own errors too
        result = {"error": str(exc)}
    with _lock:
        j = _jobs.get(job_id)
        if j is not None:
            j.finished_at = _now()
            if isinstance(result, dict) and result.get("id") and not result.get("error"):
                j.status = "done"
                j.run_id = result["id"]
            else:
                j.status = "error"
                if isinstance(result, dict):
                    j.error = result.get("error") or "Analysis failed"
                else:
                    j.error = "Analysis failed"
        _prune_locked()


def _prune_locked() -> None:
    """Drop finished jobs beyond the retention window (count or age)."""
    now = _now()
    finished = [i for i in _order if i in _jobs and _jobs[i].status in ("done", "error")]
    keep_recent = set(finished[-_RETAIN_COUNT:])
    for jid in list(_order):
        j = _jobs.get(jid)
        if j is None:
            _order.remove(jid)
            continue
        if j.status in ("done", "error"):
            too_old = j.finished_at is not None and (now - j.finished_at) > _RETAIN_SECS
            if jid not in keep_recent or too_old:
                _jobs.pop(jid, None)
                _order.remove(jid)


def list_jobs() -> list[dict[str, Any]]:
    """Shared activity feed: every tracked job, oldest-submitted first, with a
    1-based queue_position for those still waiting and merged live progress."""
    from .progress import get_progress  # local import: progress is a leaf module

    with _lock:
        jobs = [_jobs[i] for i in _order if i in _jobs]
    out: list[dict[str, Any]] = []
    qpos = 0
    for j in jobs:
        d = asdict(j)
        if j.status == "queued":
            qpos += 1
            d["queue_position"] = qpos
        else:
            d["queue_position"] = None
        p = get_progress(j.id) if j.status in ("queued", "running") else None
        if j.status == "done":
            d["pct"], d["step"], d["detail"] = 100, "done", "Complete"
        elif j.status == "error":
            d["pct"], d["step"], d["detail"] = 0, "error", j.error or "Failed"
        else:
            d["pct"] = (p or {}).get("pct", 0)
            d["step"] = (p or {}).get("step", j.status)
            d["detail"] = (p or {}).get("detail")
        out.append(d)
    return out


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        j = _jobs.get(job_id)
        return asdict(j) if j is not None else None


def stats() -> dict[str, int]:
    with _lock:
        jobs = list(_jobs.values())
    return {
        "concurrency": CONCURRENCY,
        "queued": sum(1 for j in jobs if j.status == "queued"),
        "running": sum(1 for j in jobs if j.status == "running"),
    }
