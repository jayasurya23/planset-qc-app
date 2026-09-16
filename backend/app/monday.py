"""Read-only lookup of a Castillo Project ID on monday.com.

monday's Portfolio board is the source of truth for Project IDs ("264-066",
"2512-053"): one item per project, the value in the column TITLED
"Project ID". PMO 360 syncs its own copy from the same column. Each Portfolio
item links, through "Project Tasks Links", to the tasks of exactly one
per-project schedule board. That board is what engineers mean by "the
project's monday board", and it carries no Project ID of its own, so a
Project ID resolves in two steps: find the Portfolio item carrying it, then
read the board behind one of its linked tasks.

The second step is deliberately a separate, per-item call. Reading the link
column across the whole Portfolio returns every linked task id -- around 400
per project -- to learn one board id each.

Columns are addressed by title, never by id. Ids differ between boards and
change when a column is recreated; the title is what PMO 360's sync relies on
too.

Nothing here writes to monday: a document containing a mutation is refused
before it is sent. The token is the only credential and is read from
MONDAY_API_TOKEN. With it unset, is_configured() is False and callers skip the
lookup. Every other failure raises MondayError, which callers record against
the project rather than failing the request that triggered it.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.monday.com/v2"
# monday versions its API quarterly. Pinning means a platform release cannot
# silently reshape payloads; a deprecated pin is served the maintenance version
# rather than refused, so this degrades instead of breaking when it ages.
DEFAULT_API_VERSION = "2026-07"
DEFAULT_ACCOUNT_URL = "https://castillope.monday.com"
# "PMO - Project Management" -> Portfolio. Same default as PMO 360's sync.
DEFAULT_PORTFOLIO_BOARD_ID = "18403099969"

COL_PROJECT_ID = "Project ID"
COL_CLIENT = "Client Name"
COL_STATUS = "Contract Status"
COL_TASK_LINKS = "Project Tasks Links"

_RATE_LIMIT_CODES = {
    "COMPLEXITY_BUDGET_EXHAUSTED", "RATE_LIMIT_EXCEEDED", "DAILY_LIMIT_EXCEEDED",
}
_AUTH_CODES = {"UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN", "NOT_AUTHENTICATED"}
# Lookups run inside a user's request, so the retry budget is small: one
# engineer waiting on a save matters more than squeezing out a slow reply.
_MAX_RETRIES = 1
_MAX_WAIT_S = 3.0


class MondayError(RuntimeError):
    """Any monday.com failure. The message is safe to show to a user."""


class MondayAuthError(MondayError):
    """Token missing, rejected, or unable to read the board."""


class MondayRateLimitError(MondayError):
    """Complexity budget or request rate exhausted."""


# ── configuration (read per call so tests and redeploys see changes) ──────

def _token() -> str:
    return (os.getenv("MONDAY_API_TOKEN") or "").strip()


def is_configured() -> bool:
    return bool(_token())


def portfolio_board_id() -> str:
    return (os.getenv("MONDAY_PORTFOLIO_BOARD_ID") or DEFAULT_PORTFOLIO_BOARD_ID).strip()


def account_url() -> str:
    return (os.getenv("MONDAY_ACCOUNT_URL") or DEFAULT_ACCOUNT_URL).strip().rstrip("/")


def board_url(board_id: str | None) -> str | None:
    return f"{account_url()}/boards/{board_id}" if board_id else None


def item_url(item_id: str | None, board_id: str | None = None) -> str | None:
    if not item_id:
        return None
    return f"{account_url()}/boards/{board_id or portfolio_board_id()}/pulses/{item_id}"


def _cache_ttl_s() -> float:
    try:
        return float(os.getenv("MONDAY_CACHE_TTL_SECONDS", "600"))
    except ValueError:
        return 600.0


# ── Project IDs ────────────────────────────────────────────────────────────

# Hyphen look-alikes that arrive by copy-paste from Word, Outlook and PDFs.
_DASHES = re.compile("[‐-―−﹣－]")


def normalize_project_id(raw: str | None) -> str | None:
    """Clean a typed or pasted Project ID without interpreting it.

    The value is OPAQUE, as it is in PMO 360: two formats are in circulation
    (NNN-NNN and YYMM-NNN) and the scheme has already changed once, so it is
    never parsed, split or validated against a pattern. Only presentation
    noise is removed: surrounding whitespace, look-alike dashes, and spaces
    around a dash.
    """
    if raw is None:
        return None
    s = _DASHES.sub("-", str(raw))
    s = re.sub(r"\s*-\s*", "-", s)
    s = " ".join(s.split())
    return s or None


def project_id_key(raw: str | None) -> str:
    """Comparison key: normalised and case-folded. Never stored or shown."""
    return (normalize_project_id(raw) or "").casefold()


# ── transport ──────────────────────────────────────────────────────────────

def _post(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict, Any]:
    """One HTTP POST -> (status, headers, parsed JSON or None).

    The only function that touches the network; tests replace it.
    """
    import httpx

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise MondayError(f"Could not reach monday.com ({type(exc).__name__}).") from exc
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, dict(resp.headers), body


def _classify(errors: list) -> MondayError:
    codes: set[str] = set()
    messages: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            messages.append(str(err))
            continue
        messages.append(str(err.get("message") or err))
        ext = err.get("extensions") or {}
        code = ext.get("code") or err.get("error_code")
        if code:
            codes.add(str(code).upper())
        if ext.get("status_code") in (401, 403):
            codes.add("FORBIDDEN")
    detail = "; ".join(messages)[:300] or "unknown monday.com error"
    if codes & _RATE_LIMIT_CODES:
        return MondayRateLimitError(f"monday.com rate limit: {detail}")
    if codes & _AUTH_CODES:
        return MondayAuthError(f"monday.com refused access: {detail}")
    return MondayError(f"monday.com query failed: {detail}")


def execute(query: str, variables: dict | None = None) -> dict:
    """Run a read-only GraphQL document and return its ``data``.

    monday reports GraphQL failures inside an HTTP 200, so a body with an
    ``errors`` array is an error, never an empty result. Only rate limits and
    5xx responses are retried.
    """
    if re.search(r"\bmutation\b", query, re.IGNORECASE):
        raise MondayError("Refusing to send a mutation: the monday.com link is read-only.")
    token = _token()
    if not token:
        raise MondayAuthError("MONDAY_API_TOKEN is not set.")

    url = (os.getenv("MONDAY_API_URL") or DEFAULT_API_URL).strip()
    headers = {
        "Authorization": token,
        "API-Version": (os.getenv("MONDAY_API_VERSION") or DEFAULT_API_VERSION).strip(),
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables or {}}

    # An engineer is waiting on a save while this runs, and a hung monday
    # should cost them seconds, not the default minute.
    try:
        timeout = float(os.getenv("MONDAY_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10.0

    attempt = 0
    while True:
        status, resp_headers, body = _post(url, payload, headers, timeout)
        if status in (401, 403):
            raise MondayAuthError(
                f"monday.com rejected the API token (HTTP {status}). "
                "Check MONDAY_API_TOKEN and that it can read the Portfolio board.")

        err: MondayError
        if status == 429:
            err = MondayRateLimitError("monday.com rate limit (HTTP 429).")
        elif status >= 500:
            err = MondayError(f"monday.com server error (HTTP {status}).")
        elif not isinstance(body, dict):
            raise MondayError(f"monday.com returned an unreadable response (HTTP {status}).")
        elif body.get("errors") or body.get("error_message"):
            errors = body.get("errors") or [{"message": body.get("error_message")}]
            err = _classify(errors if isinstance(errors, list) else [errors])
            if not isinstance(err, MondayRateLimitError):
                raise err
        elif body.get("data") is None:
            raise MondayError("monday.com response contained neither data nor errors.")
        else:
            return body["data"]

        if attempt >= _MAX_RETRIES:
            raise err
        try:
            wait = float((resp_headers or {}).get("Retry-After") or 0)
        except (TypeError, ValueError):
            wait = 0.0
        wait = min(_MAX_WAIT_S, wait or 2.0 ** attempt)
        log.warning("monday.com call failed (%s); retrying in %.1fs", err, wait)
        time.sleep(wait)
        attempt += 1


# ── Portfolio ──────────────────────────────────────────────────────────────
#
# The cache is shared by every request in the process. Two rules keep a slow
# or failing monday from stalling the app:
#
#   * No lock is held across a network call. _lock guards the dictionaries;
#     _fetch_lock lets one fetch run at a time, and a caller that already has
#     a copy is not made to wait for another request's fetch.
#   * A failed fetch starts a short back-off. Until it expires, automatic
#     lookups get the cached copy (lists) or an immediate error (lookups that
#     need current data) instead of each trying monday again. Only an explicit
#     refresh -- the Retry button, the bulk refresh -- goes back to monday
#     during a back-off.
#
# A refresh that was asked for (force=True) never answers with the old copy:
# resolving a Project ID against stale data would record a confident
# "not on monday" for a project added since.

_lock = threading.Lock()
_fetch_lock = threading.Lock()
_columns: dict[str, dict[str, str]] = {}          # board id -> lower(title) -> column id
_portfolio: dict[str, Any] = {
    "items": None, "fetched": 0.0, "board": None,
    "failed_at": 0.0, "failed_board": None, "error": None,
}
_FAILURE_BACKOFF_S = 30.0


def _column_ids(board_id: str, *, force: bool = False) -> dict[str, str]:
    with _lock:
        cached = _columns.get(board_id)
    if cached is not None and not force:
        return cached
    data = execute(
        "query ($ids: [ID!]) { boards(ids: $ids) { name columns { id title } } }",
        {"ids": [board_id]},
    )
    boards = data.get("boards") or []
    if not boards:
        raise MondayAuthError(
            f"monday.com board {board_id} is not visible to this token.")
    fresh = {
        (c.get("title") or "").strip().lower(): c["id"]
        for c in boards[0].get("columns") or [] if c.get("id")
    }
    with _lock:
        _columns[board_id] = fresh
    return fresh


_ITEMS_FIRST = """
query ($ids: [ID!], $cols: [String!]) {
  boards(ids: $ids) {
    items_page(limit: 100) {
      cursor
      items { id name column_values(ids: $cols) { id text } }
    }
  }
}"""

_ITEMS_NEXT = """
query ($cursor: String!, $cols: [String!]) {
  next_items_page(cursor: $cursor, limit: 100) {
    cursor
    items { id name column_values(ids: $cols) { id text } }
  }
}"""


def _fetch_portfolio(board_id: str, *, refresh_columns: bool = False) -> list[dict]:
    wanted = {"castillo_project_id": COL_PROJECT_ID, "client": COL_CLIENT, "status": COL_STATUS}
    for attempt in (0, 1):
        cols = _column_ids(board_id, force=refresh_columns or attempt == 1)
        ids = {key: cols.get(title.lower()) for key, title in wanted.items()}
        if not ids["castillo_project_id"]:
            if attempt == 0:
                continue                         # the column may have been renamed or recreated
            raise MondayError(
                f'The Portfolio board has no column titled "{COL_PROJECT_ID}".')
        by_col = {cid: key for key, cid in ids.items() if cid}
        items = _fetch_items(board_id, by_col)
        # monday silently drops a column id it no longer knows, so a column
        # recreated under the same title reads as "every item is blank".
        if (attempt == 0 and not refresh_columns and items
                and not any(i["castillo_project_id"] for i in items)):
            continue
        return items


def _fetch_items(board_id: str, by_col: dict[str, str]) -> list[dict]:
    raw: list[dict] = []
    page = (execute(_ITEMS_FIRST, {"ids": [board_id], "cols": list(by_col)})
            .get("boards") or [{}])[0].get("items_page") or {}
    raw.extend(page.get("items") or [])
    cursor = page.get("cursor")
    pages = 1
    while cursor and pages < 50:                   # 5,000 projects is not a real board
        page = execute(_ITEMS_NEXT, {"cursor": cursor, "cols": list(by_col)}).get(
            "next_items_page") or {}
        raw.extend(page.get("items") or [])
        cursor = page.get("cursor")
        pages += 1

    out = []
    for it in raw:
        entry = {"item_id": str(it.get("id")), "name": (it.get("name") or "").strip(),
                 "castillo_project_id": None, "client": None, "status": None}
        for cv in it.get("column_values") or []:
            key = by_col.get(cv.get("id"))
            if key:
                text = (cv.get("text") or "").strip() or None
                entry[key] = normalize_project_id(text) if key == "castillo_project_id" else text
        out.append(entry)
    return out


def portfolio(*, force: bool = False, during_backoff: bool = False) -> list[dict]:
    """Every Portfolio item: {item_id, name, castillo_project_id, client, status}.

    Cached for MONDAY_CACHE_TTL_SECONDS. ``force`` fetches now and raises on
    failure rather than answering from the cache. ``during_backoff`` lets an
    explicit refresh reach monday even shortly after a failed fetch.
    Unforced callers are served the cached copy when a refresh fails, or while
    another request is already fetching -- a slightly old list of projects is
    more useful to someone picking a Project ID than a wait or an error.
    """
    board_id = portfolio_board_id()
    started = time.monotonic()
    with _lock:
        cached = _portfolio["items"] if _portfolio["board"] == board_id else None
        fresh = cached is not None and started - _portfolio["fetched"] < _cache_ttl_s()
        backing_off = (_portfolio["failed_board"] == board_id
                       and started - _portfolio["failed_at"] < _FAILURE_BACKOFF_S)
        last_error = _portfolio["error"]

    if fresh and not force:
        return list(cached)
    if backing_off and not during_backoff:
        if cached is not None and not force:
            return list(cached)
        raise MondayError(last_error or "monday.com is unavailable; try again shortly.")

    # One fetch at a time. A caller that can make do with the cached copy does
    # not queue behind someone else's fetch.
    if not _fetch_lock.acquire(blocking=force or cached is None):
        return list(cached)
    try:
        with _lock:
            # Someone else's fetch finished while this call waited: use it.
            if _portfolio["board"] == board_id and _portfolio["fetched"] >= started:
                return list(_portfolio["items"])
        try:
            items = _fetch_portfolio(board_id, refresh_columns=force)
        except MondayError as exc:
            with _lock:
                _portfolio.update(failed_at=time.monotonic(), failed_board=board_id,
                                  error=str(exc))
            if force or cached is None:
                raise
            log.warning("monday.com Portfolio refresh failed (%s); serving the cached copy", exc)
            return list(cached)
        with _lock:
            _portfolio.update(items=items, fetched=time.monotonic(), board=board_id,
                              failed_at=0.0, failed_board=None, error=None)
        return list(items)
    finally:
        _fetch_lock.release()


def find_by_project_id(
    castillo_project_id: str | None, *, force: bool = False, during_backoff: bool = False,
) -> list[dict]:
    """Portfolio items carrying this Project ID. Normally zero or one."""
    key = project_id_key(castillo_project_id)
    if not key:
        return []
    return [p for p in portfolio(force=force, during_backoff=during_backoff)
            if project_id_key(p.get("castillo_project_id")) == key]


def board_for_item(item_id: str) -> dict | None:
    """The project schedule board behind a Portfolio item, or None if unlinked.

    Every task linked from one Portfolio item sits on the same board, so any
    one of them identifies it.
    """
    board_id = portfolio_board_id()
    linked: list[str] = []
    for attempt in (0, 1):
        # A recreated "Project Tasks Links" column keeps its title with a new
        # id, and monday answers the old id with nothing -- so an empty answer
        # re-reads the column ids once before concluding there is no board.
        link_col = _column_ids(board_id, force=attempt == 1).get(COL_TASK_LINKS.lower())
        if not link_col:
            continue
        data = execute(
            """query ($ids: [ID!], $cols: [String!]) {
                 items(ids: $ids) {
                   column_values(ids: $cols) { ... on BoardRelationValue { linked_item_ids } }
                 }
               }""",
            {"ids": [item_id], "cols": [link_col]},
        )
        for it in data.get("items") or []:
            for cv in it.get("column_values") or []:
                linked.extend(str(i) for i in (cv.get("linked_item_ids") or []))
        if linked:
            break
    if not linked:
        return None
    data = execute(
        "query ($ids: [ID!]) { items(ids: $ids) { id board { id name } } }",
        {"ids": linked[:1]},
    )
    for it in data.get("items") or []:
        board = it.get("board") or {}
        if board.get("id"):
            return {"board_id": str(board["id"]), "board_name": board.get("name")}
    return None


def reset_cache() -> None:
    """Forget cached columns, Portfolio items and any back-off. For tests."""
    with _lock:
        _columns.clear()
        _portfolio.update(items=None, fetched=0.0, board=None,
                          failed_at=0.0, failed_board=None, error=None)
