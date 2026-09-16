"""Where a QC project's Project ID leads: PMO 360 and monday.com.

"Project ID" follows monday.com's naming: it is the column on the Portfolio
board that holds the Castillo number for a project ("264-066", "2512-053").
PMO 360 stores the same value per portfolio, and the drawings print it beside
"CASTILLO PROJECT ID" in the title block, so it is the one key the three
systems share. In code it is ``castillo_project_id``, because ``project_id``
already names the QC app's own internal project key.

This module turns a project's Project ID into links and suggestions, and keeps
the monday resolution cached on the project row so pages render without
calling monday. The database column is ``projects.number``, which predates
this feature; SQLite migrations here are additive, so it keeps that name.

PMO 360 is linked by Project ID, not by its internal id: the QC app holds no
PMO 360 credentials, and PMO 360 resolves ``/portfolio?project_id=<value>``
itself -- including the cases where no portfolio, or several, carry it.
"""
from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import monday
from .db import (
    get_project, latest_pdf_for_project, projects_with_castillo_project_id,
    set_project_monday_link,
)

log = logging.getLogger(__name__)

DEFAULT_PMO360_BASE_URL = "https://pmo360.castillope.com"

# Raw columns replaced by the decorated fields below; not sent to clients, so
# the API has one name for each thing.
_RAW_COLUMNS = (
    "number", "castillo_project_id_set_by", "castillo_project_id_set_at",
    "monday_status", "monday_detail", "monday_item_id", "monday_item_name",
    "monday_board_id", "monday_board_name", "monday_checked_at",
)


def pmo360_url(castillo_project_id: str | None) -> str | None:
    """Deep link into PMO 360 for a Project ID; None when unset or disabled.

    PMO360_BASE_URL overrides the host (a staging QC app should point at
    staging PMO 360); set it to an empty string to hide the link. The path is
    a real route rather than "/" because PMO 360 only preserves a deep link
    through sign-in for non-root paths.
    """
    base = os.getenv("PMO360_BASE_URL")
    base = DEFAULT_PMO360_BASE_URL if base is None else base.strip().rstrip("/")
    if not castillo_project_id or not base:
        return None
    return f"{base}/portfolio?project_id={quote(castillo_project_id, safe='')}"


def decorate(project: dict[str, Any] | None) -> dict[str, Any] | None:
    """A projects row as the API returns it: Project ID, links, monday status."""
    if project is None:
        return None
    p = dict(project)
    pid = p.get("number") or None
    status = p.get("monday_status")
    if pid and not monday.is_configured() and status in (None, "not_configured"):
        status = "not_configured"
    linked = status == "linked"
    out = {k: v for k, v in p.items() if k not in _RAW_COLUMNS}
    out["castillo_project_id"] = pid
    out["castillo_project_id_set_by"] = p.get("castillo_project_id_set_by") if pid else None
    out["castillo_project_id_set_at"] = p.get("castillo_project_id_set_at") if pid else None
    out["links"] = {
        "pmo360": pmo360_url(pid),
        "monday_board": monday.board_url(p.get("monday_board_id")) if linked else None,
        "monday_item": monday.item_url(p.get("monday_item_id")) if linked else None,
    }
    out["monday"] = {
        "configured": monday.is_configured(),
        "status": status if pid else None,
        "detail": p.get("monday_detail") if pid else None,
        "item_name": p.get("monday_item_name") if pid else None,
        "board_name": p.get("monday_board_name") if pid else None,
        "checked_at": p.get("monday_checked_at") if pid else None,
    }
    return out


def resolve_monday(
    project_id: str, *, force: bool = False, retry_missing: bool = True,
) -> dict[str, Any] | None:
    """Look the project's Project ID up on monday and store the outcome.

    Never raises for a monday failure -- the failure is recorded as the
    project's monday status, because a Project ID is worth saving even when
    monday is unreachable. Returns the decorated project, or None if it no
    longer exists.

    ``force`` refetches the Portfolio first. ``retry_missing`` refetches it
    once more when the value is not in the cached copy (a project added on
    monday since); a bulk caller that has just refreshed the Portfolio turns
    that off, or every unmatched project would refetch the whole board.
    """
    project = get_project(project_id)
    if project is None:
        return None
    pid = project.get("number")
    if not pid:
        return decorate(project)
    if not monday.is_configured():
        set_project_monday_link(project_id, castillo_project_id=pid, status="not_configured")
        return decorate(get_project(project_id))

    try:
        matches = monday.find_by_project_id(pid, force=force)
        if not matches and not force and retry_missing:
            # A project added on monday since the cached list was fetched.
            matches = monday.find_by_project_id(pid, force=True)
        if not matches:
            set_project_monday_link(
                project_id, castillo_project_id=pid, status="not_found",
                detail=f"No item on the monday Portfolio board has Project ID {pid}.")
        elif len(matches) > 1:
            names = ", ".join(m["name"] for m in matches[:5])
            set_project_monday_link(
                project_id, castillo_project_id=pid, status="ambiguous",
                detail=f"{len(matches)} Portfolio items share Project ID {pid}: {names}.")
        else:
            item = matches[0]
            board = monday.board_for_item(item["item_id"])
            set_project_monday_link(
                project_id, castillo_project_id=pid, status="linked",
                detail=None if board else "The Portfolio item links to no project board.",
                item_id=item["item_id"], item_name=item["name"],
                board_id=board["board_id"] if board else None,
                board_name=board["board_name"] if board else None,
            )
    except monday.MondayError as exc:
        log.warning("monday.com lookup for Project ID %s failed: %s", pid, exc)
        set_project_monday_link(
            project_id, castillo_project_id=pid, status="error", detail=str(exc))
    except Exception:                               # never fail the caller's save
        log.exception("monday.com lookup for Project ID %s crashed", pid)
        set_project_monday_link(
            project_id, castillo_project_id=pid, status="error",
            detail="The monday.com lookup failed unexpectedly; see the server log.")
    return decorate(get_project(project_id))


def duplicates(project_id: str, castillo_project_id: str | None) -> list[dict[str, Any]]:
    """Other QC projects carrying the same Project ID, as {id, name}."""
    if not castillo_project_id:
        return []
    return [{"id": d["id"], "name": d["name"]}
            for d in projects_with_castillo_project_id(castillo_project_id, exclude_id=project_id)]


# ── suggestions ────────────────────────────────────────────────────────────

def _name_tokens(name: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", (name or "").lower())


def _name_score(project_name: str, candidate: str) -> float:
    a, b = _name_tokens(project_name), _name_tokens(candidate)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    # "Aurora 1 IFC" should match a project named "Aurora 1 IFC 60% planset".
    contained = 1.0 if all(t in a for t in b) else 0.0
    return max(ratio, 0.9 * contained)


def suggestions(project_id: str, printed: str | None = None) -> list[dict[str, Any]]:
    """Candidate Project IDs for a project, best first.

    ``title_block``  printed beside "CASTILLO PROJECT ID" on its newest run's
                     drawings. Read from the stored run when available, else
                     from the PDF now (runs analysed before this existed).
    ``name``         a monday Portfolio item whose name resembles the project.

    Suggestions only: nothing here changes the project.
    """
    project = get_project(project_id)
    if project is None:
        return []
    out: list[dict[str, Any]] = []

    if not printed:
        pdf = latest_pdf_for_project(project_id)
        if pdf and Path(pdf).exists():
            try:
                import fitz
                from .analyzer import detect_castillo_project_id
                with fitz.open(pdf) as doc:
                    printed = detect_castillo_project_id(doc)
            except Exception:
                log.exception("Title-block Project ID scan failed for %s", pdf)
    printed = monday.normalize_project_id(printed)

    portfolio: list[dict[str, Any]] = []
    if monday.is_configured():
        try:
            portfolio = monday.portfolio()
        except monday.MondayError as exc:
            log.warning("monday.com Portfolio unavailable for suggestions: %s", exc)
    by_key = {monday.project_id_key(p.get("castillo_project_id")): p
              for p in portfolio if p.get("castillo_project_id")}

    if printed:
        hit = by_key.get(monday.project_id_key(printed))
        out.append({
            "castillo_project_id": printed, "source": "title_block",
            "name": hit["name"] if hit else None,
            "client": hit["client"] if hit else None,
            "on_monday": bool(hit) if portfolio else None,
        })

    scored = []
    for p in portfolio:
        if (not p.get("castillo_project_id")
                or monday.project_id_key(p["castillo_project_id"]) == monday.project_id_key(printed)):
            continue
        score = _name_score(project.get("name") or "", p.get("name") or "")
        if score >= 0.75:
            scored.append((score, p))
    scored.sort(key=lambda sp: -sp[0])
    for score, p in scored[:3]:
        out.append({"castillo_project_id": p["castillo_project_id"], "source": "name", "name": p["name"],
                    "client": p.get("client"), "on_monday": True, "score": round(score, 2)})
    return out
