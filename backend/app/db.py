from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

BASE_DIR = Path(__file__).resolve().parents[1]
# DATA_DIR is overridable so cloud deploys can point it at persistent storage
# (e.g. an Azure Files mount). Falls back to the in-repo ./data for local dev.
DATA_DIR = Path(os.getenv("PLANSET_DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "planset_qc.sqlite3"

# The DB file may live on a network share (e.g. an Azure Files / SMB mount in the
# cloud) that doesn't support the POSIX byte-range locks SQLite relies on — there
# it fails with "database is locked" even for a single writer. We open with
# nolock=1 (the unix-none VFS) to skip OS file locking and serialise every access
# through _DB_LOCK instead. Correct because the app runs as a single replica /
# single process; to scale beyond one instance, move to a networked DB server.
_DB_LOCK = threading.RLock()
_CONN_URI = f"file:{DB_PATH}?nolock=1"

# Version of the item_key minting convention. Runs are compared to each other
# by item_key, so a change to how keys are built makes every finding in an
# older run look new. Bump this whenever key format changes; the compare view
# then declines to diff across the boundary instead of reporting hundreds of
# phantom "new"/"removed" rows that no reviewer can act on.
#
#   1  original — free-form model check name appended to a family prefix.
#      Runs written before this column existed report NULL and are treated as 1.
#   2  multi-page findings that a model attributed to more than one sheet keep
#      one row per sheet, suffixed "__p<page>", instead of collapsing to one.
#   3  enumerated families (check_ids.yaml) key off the registry id rather than
#      the model's wording, and un-enumerated families fold case/punctuation —
#      the model was emitting "date"/"Date" and "sov"/"SOV" for one check on
#      one sheet, which halved that family's stability by capitalisation alone.
KEY_SCHEMA_VERSION = 3


# Stage ordering for sorting stage submissions within a project. Mirrors
# rule_registry.STAGE_ORDER; kept local to avoid importing the rule engine here.
_STAGE_SORT = {"30": 0, "60": 1, "90": 2, "IFC": 3, "AsBuilt": 4}


def _stage_sort_key(stage: str | None) -> int:
    return _STAGE_SORT.get(stage or "", 99)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _norm_project_name(name: str | None) -> str:
    """Case/whitespace-insensitive key used to match runs to the same project."""
    return " ".join((name or "").split()).strip().lower()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    with _DB_LOCK:
        conn = sqlite3.connect(
            _CONN_URI, uri=True, timeout=30, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def init_db() -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                project_name TEXT,
                original_filename TEXT,
                created_at TEXT NOT NULL,
                pdf_path TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                summary_json TEXT NOT NULL,
                status_counts_json TEXT NOT NULL,
                categories_json TEXT NOT NULL,
                project_details_json TEXT,
                engineer_name TEXT
            )
            """
        )
        # Migration: add project_details_json if missing from older DB
        try:
            cur.execute("SELECT project_details_json FROM runs LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE runs ADD COLUMN project_details_json TEXT")
        try:
            cur.execute("SELECT engineer_name FROM runs LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE runs ADD COLUMN engineer_name TEXT")
        # Projects / stage-tagging / multi-user attribution / rerun-versioning.
        # All additive TEXT/INTEGER columns (SQLite can't ALTER-add a real FK, so
        # project_id / parent_run_id / root_run_id are plain TEXT enforced in app
        # code). design_stage is promoted out of summary_json so it is queryable.
        for col, ddl in [
            ("project_id",    "ALTER TABLE runs ADD COLUMN project_id TEXT"),
            ("design_stage",  "ALTER TABLE runs ADD COLUMN design_stage TEXT"),
            ("created_by",    "ALTER TABLE runs ADD COLUMN created_by TEXT"),
            ("created_by_id", "ALTER TABLE runs ADD COLUMN created_by_id TEXT"),
            ("parent_run_id", "ALTER TABLE runs ADD COLUMN parent_run_id TEXT"),
            ("root_run_id",   "ALTER TABLE runs ADD COLUMN root_run_id TEXT"),
            ("version",       "ALTER TABLE runs ADD COLUMN version INTEGER"),
            ("is_latest",     "ALTER TABLE runs ADD COLUMN is_latest INTEGER"),
            # Optional human-friendly label for a run (falls back to the PDF
            # filename in the UI when unset).
            ("run_name",      "ALTER TABLE runs ADD COLUMN run_name TEXT"),
            # Which item_key convention this run was written under. Findings
            # are matched between runs by item_key, so any change to how keys
            # are minted makes an older run look like it churned 100% of its
            # findings. The compare refuses to diff across a boundary rather
            # than reporting phantom "new" and "removed" rows. NULL = pre-stamp
            # (schema 1). See KEY_SCHEMA_VERSION.
            ("key_schema_version",
             "ALTER TABLE runs ADD COLUMN key_schema_version INTEGER"),
        ]:
            try:
                cur.execute(f"SELECT {col} FROM runs LIMIT 1")
            except sqlite3.OperationalError:
                cur.execute(ddl)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS issues (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                category TEXT NOT NULL,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                auto_status TEXT NOT NULL,
                page_number INTEGER,
                bbox_json TEXT,
                snippet_path TEXT,
                page_preview_path TEXT,
                evidence TEXT,
                confidence REAL NOT NULL,
                override_comment TEXT,
                source_doc_filename TEXT,
                source_doc_page INTEGER,
                source_doc_excerpt TEXT,
                nec_ref TEXT,
                calc_computed_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            )
            """
        )
        # Migration: add source_doc_* columns if missing on older DBs.
        for col, ddl in [
            ("source_doc_filename", "ALTER TABLE issues ADD COLUMN source_doc_filename TEXT"),
            ("source_doc_page",     "ALTER TABLE issues ADD COLUMN source_doc_page INTEGER"),
            ("source_doc_excerpt",  "ALTER TABLE issues ADD COLUMN source_doc_excerpt TEXT"),
            # Multi-location findings (cross-sheet consistency conflicts) store
            # their per-location list here as JSON (or NULL for normal findings).
            ("locations_json",      "ALTER TABLE issues ADD COLUMN locations_json TEXT"),
            # Chat-copilot provenance: governing NEC article from the rule
            # definition, and the calc's intermediate values (CalcResult.computed)
            # for electrical_calc findings. NULL on older rows and non-calc rows.
            ("nec_ref",             "ALTER TABLE issues ADD COLUMN nec_ref TEXT"),
            ("calc_computed_json",  "ALTER TABLE issues ADD COLUMN calc_computed_json TEXT"),
        ]:
            try:
                cur.execute(f"SELECT {col} FROM issues LIMIT 1")
            except sqlite3.OperationalError:
                cur.execute(ddl)
        # Per-finding feedback distinct from override status — captures "the
        # call was right but the location/citation/reason/category was wrong"
        # signal that override comments alone can't capture.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_feedback (
                id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                engineer_name TEXT,
                tags_json TEXT,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (issue_id) REFERENCES issues(id)
            )
            """
        )
        # Per-run "how was this run" rating: saved_time / even / cost_time
        # plus optional comment. Insert-only so the rating history is preserved
        # if engineers change their mind after more triage.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS run_feedback (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                engineer_name TEXT,
                rating TEXT NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            )
            """
        )
        # Projects group multiple runs (different design stages + reruns) of the
        # same physical project. Created lazily on upload and during backfill.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                number TEXT,
                created_at TEXT NOT NULL,
                created_by TEXT
            )
            """
        )
        # Chat copilot: one thread per run. Read-only grounding in Phase 1 —
        # chat NEVER writes issue status; the exported checklist stays the
        # system of record and chat content is excluded from the export.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_by TEXT,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            )
            """
        )
        _backfill_projects_and_versions(cur)
        conn.commit()


def _backfill_projects_and_versions(cur: sqlite3.Cursor) -> None:
    """One-time, idempotent backfill for pre-feature rows.

    Only touches rows whose new columns are still NULL, so it is safe to run on
    every startup. Promotes design_stage out of summary_json, seeds attribution
    from engineer_name, marks every legacy run as a standalone v1/latest, and
    synthesizes a project per distinct project name, linking runs to it.
    """
    # 1. Promote design_stage from summary_json (Python-side to avoid relying on
    #    the SQLite JSON1 extension being compiled in).
    for r in cur.execute(
        "SELECT id, summary_json FROM runs WHERE design_stage IS NULL"
    ).fetchall():
        try:
            stage = (json.loads(r["summary_json"]) or {}).get("design_stage")
        except Exception:
            stage = None
        if stage:
            cur.execute("UPDATE runs SET design_stage = ? WHERE id = ?", (stage, r["id"]))

    # 2. Seed attribution from the old free-text engineer_name where empty.
    cur.execute(
        "UPDATE runs SET created_by = engineer_name "
        "WHERE created_by IS NULL AND engineer_name IS NOT NULL AND TRIM(engineer_name) != ''"
    )

    # 3. Treat every legacy run as a standalone first version that is current.
    cur.execute("UPDATE runs SET version = 1 WHERE version IS NULL")
    cur.execute("UPDATE runs SET is_latest = 1 WHERE is_latest IS NULL")
    cur.execute(
        "UPDATE runs SET root_run_id = id WHERE root_run_id IS NULL OR root_run_id = ''"
    )

    # 4. Synthesize projects from distinct (normalized) names and link runs.
    #    Pre-group so a project takes its EARLIEST run's created_at but the
    #    FIRST NON-NULL created_by across its runs — an earlier run with no
    #    engineer must not stamp the project owner NULL forever when a later
    #    run for the same project does have one.
    unassigned = cur.execute(
        "SELECT id, project_name, original_filename, created_at, created_by "
        "FROM runs WHERE project_id IS NULL OR project_id = '' ORDER BY created_at ASC"
    ).fetchall()
    if not unassigned:
        return
    existing: dict[str, str] = {
        _norm_project_name(p["name"]): p["id"]
        for p in cur.execute("SELECT id, name FROM projects").fetchall()
    }
    groups: dict[str, dict[str, Any]] = {}
    for r in unassigned:  # already ordered created_at ASC
        raw = (r["project_name"] or "").strip() \
            or Path(r["original_filename"] or "").stem.strip() \
            or "Untitled Project"
        key = _norm_project_name(raw)
        g = groups.get(key)
        if g is None:
            g = {"raw": raw, "created_at": r["created_at"] or _now_iso(),
                 "created_by": None, "ids": []}
            groups[key] = g
        g["ids"].append(r["id"])
        if not g["created_by"] and r["created_by"]:
            g["created_by"] = r["created_by"]
    for key, g in groups.items():
        pid = existing.get(key)
        if not pid:
            pid = _new_id()
            cur.execute(
                "INSERT INTO projects (id, name, number, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, g["raw"], None, g["created_at"], g["created_by"]),
            )
            existing[key] = pid
        for rid in g["ids"]:
            cur.execute("UPDATE runs SET project_id = ? WHERE id = ?", (pid, rid))


def insert_chat_message(msg: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (
                id, run_id, role, content, created_by, model,
                prompt_tokens, completion_tokens, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg["id"], msg["run_id"], msg["role"], msg["content"],
                msg.get("created_by"), msg.get("model"),
                msg.get("prompt_tokens"), msg.get("completion_tokens"),
                msg.get("created_at") or _now_iso(),
            ),
        )
        conn.commit()


def get_chat_messages(run_id: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE run_id = ? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def chat_thread_stats(run_id: str) -> dict[str, int]:
    """Turn count + total completion tokens — drives the server-side ceilings."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
            "FROM chat_messages WHERE run_id = ? AND role = 'assistant'",
            (run_id,),
        ).fetchone()
    return {"assistant_turns": row["n"], "completion_tokens": row["completion_tokens"]}


def insert_issue_feedback(fb: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO issue_feedback (
                id, issue_id, run_id, engineer_name, tags_json, comment, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fb["id"], fb["issue_id"], fb["run_id"], fb.get("engineer_name"),
                json.dumps(fb.get("tags") or [], ensure_ascii=False),
                fb.get("comment"),
                fb["created_at"],
            ),
        )
        conn.commit()


def insert_run_feedback(fb: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO run_feedback (
                id, run_id, engineer_name, rating, comment, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fb["id"], fb["run_id"], fb.get("engineer_name"),
                fb["rating"], fb.get("comment"),
                fb["created_at"],
            ),
        )
        conn.commit()


def get_issue_run_id(issue_id: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT run_id FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return row["run_id"] if row else None


def latest_run_feedback(run_id: str, engineer_name: str | None) -> dict[str, Any] | None:
    """Most recent rating for this run by this engineer (or anonymous if name is None)."""
    with _conn() as conn:
        if engineer_name:
            row = conn.execute(
                "SELECT * FROM run_feedback WHERE run_id = ? AND engineer_name = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id, engineer_name),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM run_feedback WHERE run_id = ? AND engineer_name IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
    return dict(row) if row else None


def insert_run(run: dict[str, Any], issues: Iterable[dict[str, Any]]) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO runs (
                id, project_name, original_filename, created_at,
                pdf_path, page_count, summary_json,
                status_counts_json, categories_json,
                project_details_json, engineer_name,
                project_id, design_stage, created_by, created_by_id,
                parent_run_id, root_run_id, version, is_latest, run_name,
                key_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["id"],
                run.get("project_name"),
                run["original_filename"],
                run["created_at"],
                run["pdf_path"],
                run["page_count"],
                json.dumps(run["summary"], ensure_ascii=False),
                json.dumps(run["status_counts"], ensure_ascii=False),
                json.dumps(run["categories"], ensure_ascii=False),
                json.dumps(run.get("project_details"),
                           ensure_ascii=False)
                if run.get("project_details") else None,
                run.get("engineer_name"),
                run.get("project_id"),
                run.get("design_stage") or (run.get("summary") or {}).get("design_stage"),
                run.get("created_by"),
                run.get("created_by_id"),
                run.get("parent_run_id"),
                run.get("root_run_id") or run["id"],
                run.get("version") or 1,
                1 if run.get("is_latest", 1) else 0,
                (run.get("run_name") or "").strip() or None,
                run.get("key_schema_version") or KEY_SCHEMA_VERSION,
            ),
        )
        cur.executemany(
            """
            INSERT INTO issues (
                id, run_id, category, item_key, title, description, severity, status, auto_status,
                page_number, bbox_json, snippet_path, page_preview_path, evidence, confidence,
                override_comment, source_doc_filename, source_doc_page, source_doc_excerpt,
                locations_json, nec_ref, calc_computed_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    issue["id"],
                    issue["run_id"],
                    issue["category"],
                    issue["item_key"],
                    issue["title"],
                    issue["description"],
                    issue["severity"],
                    issue["status"],
                    issue["auto_status"],
                    issue.get("page_number"),
                    json.dumps(issue.get("bbox")) if issue.get("bbox") else None,
                    issue.get("snippet_path"),
                    issue.get("page_preview_path"),
                    issue.get("evidence"),
                    issue.get("confidence", 0.0),
                    issue.get("override_comment"),
                    issue.get("source_doc_filename"),
                    issue.get("source_doc_page"),
                    issue.get("source_doc_excerpt"),
                    json.dumps(issue.get("locations"), ensure_ascii=False)
                    if issue.get("locations") else None,
                    issue.get("nec_ref"),
                    json.dumps(issue.get("calc_computed"), ensure_ascii=False)
                    if issue.get("calc_computed") else None,
                    issue["created_at"],
                    issue["updated_at"],
                )
                for issue in issues
            ],
        )
        conn.commit()


def row_to_issue(row: sqlite3.Row) -> dict[str, Any]:
    issue = dict(row)
    issue["bbox"] = json.loads(issue["bbox_json"]) if issue.get("bbox_json") else None
    issue.pop("bbox_json", None)
    # Decode the multi-location list (cross-sheet conflicts). Older rows /
    # normal findings have it NULL → locations stays None.
    loc_json = issue.pop("locations_json", None)
    issue["locations"] = json.loads(loc_json) if loc_json else None
    # Decode calc provenance (electrical_calc findings only; NULL elsewhere).
    calc_json = issue.pop("calc_computed_json", None)
    issue["calc_computed"] = json.loads(calc_json) if calc_json else None
    return issue


def get_run(run_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return None
        issues = conn.execute(
            "SELECT * FROM issues WHERE run_id = ? ORDER BY category, page_number, title",
            (run_id,),
        ).fetchall()

    run_dict = dict(run)
    run_dict["summary"] = json.loads(run_dict.pop("summary_json"))
    run_dict["status_counts"] = json.loads(run_dict.pop("status_counts_json"))
    run_dict["categories"] = json.loads(run_dict.pop("categories_json"))
    pd_json = run_dict.pop("project_details_json", None)
    run_dict["project_details"] = json.loads(pd_json) if pd_json else None
    run_dict["issues"] = [row_to_issue(row) for row in issues]
    return run_dict


def list_runs() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["summary"] = json.loads(item.pop("summary_json"))
        item["status_counts"] = json.loads(item.pop("status_counts_json"))
        item["categories"] = json.loads(item.pop("categories_json"))
        pd_json = item.pop("project_details_json", None)
        item["project_details"] = json.loads(pd_json) if pd_json else None
        out.append(item)
    return out


def update_issue(issue_id: str, status: str, override_comment: str | None) -> dict[str, Any] | None:
    with _conn() as conn:
        conn.execute(
            "UPDATE issues SET status = ?, override_comment = ?, updated_at = datetime('now') WHERE id = ?",
            (status, override_comment, issue_id),
        )
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        conn.commit()
    return row_to_issue(row) if row else None


def delete_run(run_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM issues WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM chat_messages WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.commit()
    return True


def insert_manual_issue(issue: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO issues (
                id, run_id, category, item_key, title, description, severity, status, auto_status,
                page_number, bbox_json, snippet_path, page_preview_path, evidence, confidence,
                override_comment, locations_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue["id"],
                issue["run_id"],
                issue["category"],
                issue["item_key"],
                issue["title"],
                issue["description"],
                issue["severity"],
                issue["status"],
                issue["auto_status"],
                issue.get("page_number"),
                json.dumps(issue.get("bbox")) if issue.get("bbox") else None,
                issue.get("snippet_path"),
                issue.get("page_preview_path"),
                issue.get("evidence"),
                issue.get("confidence", 0.0),
                issue.get("override_comment"),
                json.dumps(issue.get("locations"), ensure_ascii=False)
                if issue.get("locations") else None,
                issue["created_at"],
                issue["updated_at"],
            ),
        )
        conn.commit()


# ── Projects & rerun-version helpers ────────────────────────────────────────

def get_or_create_project(
    name: str, created_by: str | None = None, number: str | None = None
) -> str:
    """Return the id of the project matching ``name`` (case/space-insensitive),
    creating it if none exists. Used at upload time so every run is linked to a
    project even before the explicit project picker (Phase 2) exists.
    """
    raw = (name or "").strip() or "Untitled Project"
    norm = _norm_project_name(raw)
    with _conn() as conn:
        for p in conn.execute("SELECT id, name FROM projects").fetchall():
            if _norm_project_name(p["name"]) == norm:
                return p["id"]
        pid = _new_id()
        conn.execute(
            "INSERT INTO projects (id, name, number, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, raw, number, _now_iso(), created_by),
        )
        conn.commit()
    return pid


def get_project(project_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def _stage_summary(stage_runs: list[sqlite3.Row]) -> dict[str, Any]:
    """Pick the current run for a stage (is_latest first, then newest)."""
    chosen = None
    for r in stage_runs:
        if chosen is None:
            chosen = r
            continue
        better = (
            (r["is_latest"] and not chosen["is_latest"])
            or (bool(r["is_latest"]) == bool(chosen["is_latest"])
                and (r["created_at"] or "") > (chosen["created_at"] or ""))
        )
        if better:
            chosen = r
    return {
        "stage": chosen["design_stage"] or None,
        "run_id": chosen["id"],
        "version": chosen["version"],
        "is_latest": bool(chosen["is_latest"]),
        "created_at": chosen["created_at"],
        "status_counts": json.loads(chosen["status_counts_json"])
        if chosen["status_counts_json"] else {},
        "run_count": len(stage_runs),
    }


def list_projects() -> list[dict[str, Any]]:
    """Projects with one summary row per design stage, newest activity first."""
    with _conn() as conn:
        projects = conn.execute(
            "SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        runs = conn.execute(
            "SELECT id, project_id, design_stage, version, is_latest, created_at, "
            "created_by, engineer_name, status_counts_json FROM runs"
        ).fetchall()
    by_proj: dict[str, list[sqlite3.Row]] = {}
    for r in runs:
        by_proj.setdefault(r["project_id"], []).append(r)
    out: list[dict[str, Any]] = []
    for p in projects:
        pruns = by_proj.get(p["id"], [])
        by_stage: dict[str, list[sqlite3.Row]] = {}
        last_activity = None
        for r in pruns:
            by_stage.setdefault(r["design_stage"] or "", []).append(r)
            if last_activity is None or (r["created_at"] or "") > last_activity:
                last_activity = r["created_at"]
        stages = [_stage_summary(rs) for rs in by_stage.values()]
        stages.sort(key=lambda s: _stage_sort_key(s["stage"]))
        out.append({
            **dict(p),
            "run_count": len(pruns),
            "stage_count": len(by_stage),
            "stages": stages,
            "last_activity": last_activity,
        })
    out.sort(key=lambda x: (x["last_activity"] or ""), reverse=True)
    return out


def _run_card(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["status_counts"] = json.loads(d.pop("status_counts_json")) \
        if d.get("status_counts_json") else {}
    return d


_RUN_CARD_COLS = (
    "id, project_id, project_name, original_filename, run_name, design_stage, "
    "version, is_latest, parent_run_id, root_run_id, created_at, created_by, "
    "engineer_name, page_count, status_counts_json"
)


def update_run_name(run_id: str, run_name: str | None) -> dict[str, Any] | None:
    """Set (or clear, when blank) a run's human-friendly name."""
    name = (run_name or "").strip() or None
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE runs SET run_name = ? WHERE id = ?", (name, run_id))
        if cur.rowcount == 0:
            return None
        conn.commit()
    return get_run(run_id)


def get_project_detail(project_id: str) -> dict[str, Any] | None:
    proj = get_project(project_id)
    if not proj:
        return None
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_RUN_CARD_COLS} FROM runs WHERE project_id = ? "
            "ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return {**proj, "runs": [_run_card(r) for r in rows]}


def save_run_version(
    run: dict[str, Any], issues: Iterable[dict[str, Any]], predecessor_run_id: str
) -> None:
    """Insert ``run`` as the next version in ``predecessor_run_id``'s lineage.

    Version, root_run_id and parent_run_id are derived from the lineage's
    CURRENT state (its highest-version run, the tip), not from the predecessor
    passed in — so reanalyzing an *older* version still advances the tip rather
    than minting a duplicate version. Every other run in the lineage is then
    demoted, guaranteeing exactly one ``is_latest`` run. The whole
    read-compute-insert-demote runs while holding the reentrant ``_DB_LOCK``, so
    two concurrent reruns are serialized and can't both claim the same version
    or both stay latest.
    """
    with _DB_LOCK:
        with _conn() as conn:
            pred = conn.execute(
                "SELECT root_run_id FROM runs WHERE id = ?", (predecessor_run_id,)
            ).fetchone()
            root = (pred["root_run_id"] if pred and pred["root_run_id"]
                    else predecessor_run_id)
            tip = conn.execute(
                "SELECT id, version FROM runs WHERE root_run_id = ? "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (root,),
            ).fetchone()
        run["version"] = ((tip["version"] if tip else 0) or 0) + 1
        run["root_run_id"] = root
        run["is_latest"] = 1
        run["parent_run_id"] = tip["id"] if tip else predecessor_run_id
        insert_run(run, issues)
        with _conn() as conn:
            conn.execute(
                "UPDATE runs SET is_latest = 0 WHERE root_run_id = ? AND id != ?",
                (root, run["id"]),
            )
            conn.commit()


def get_run_versions(run_id: str) -> list[dict[str, Any]]:
    """All versions in the same lineage as ``run_id``, oldest → newest."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT root_run_id, id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return []
        root = row["root_run_id"] or row["id"]
        rows = conn.execute(
            f"SELECT {_RUN_CARD_COLS} FROM runs WHERE root_run_id = ? OR id = ? "
            "ORDER BY version ASC, created_at ASC",
            (root, root),
        ).fetchall()
    # De-dup in case root matches both predicates.
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(_run_card(r))
    return out
