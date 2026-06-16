from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
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
        conn.commit()


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
                project_details_json, engineer_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        cur.executemany(
            """
            INSERT INTO issues (
                id, run_id, category, item_key, title, description, severity, status, auto_status,
                page_number, bbox_json, snippet_path, page_preview_path, evidence, confidence,
                override_comment, source_doc_filename, source_doc_page, source_doc_excerpt,
                locations_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
