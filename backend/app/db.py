from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "planset_qc.sqlite3"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
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
    ]:
        try:
            cur.execute(f"SELECT {col} FROM issues LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute(ddl)
    conn.commit()
    conn.close()


def insert_run(run: dict[str, Any], issues: Iterable[dict[str, Any]]) -> None:
    conn = get_conn()
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
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                issue["created_at"],
                issue["updated_at"],
            )
            for issue in issues
        ],
    )
    conn.commit()
    conn.close()


def row_to_issue(row: sqlite3.Row) -> dict[str, Any]:
    issue = dict(row)
    issue["bbox"] = json.loads(issue["bbox_json"]) if issue.get("bbox_json") else None
    issue.pop("bbox_json", None)
    return issue


def get_run(run_id: str) -> dict[str, Any] | None:
    conn = get_conn()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        conn.close()
        return None
    issues = conn.execute(
        "SELECT * FROM issues WHERE run_id = ? ORDER BY category, page_number, title",
        (run_id,),
    ).fetchall()
    conn.close()

    run_dict = dict(run)
    run_dict["summary"] = json.loads(run_dict.pop("summary_json"))
    run_dict["status_counts"] = json.loads(run_dict.pop("status_counts_json"))
    run_dict["categories"] = json.loads(run_dict.pop("categories_json"))
    pd_json = run_dict.pop("project_details_json", None)
    run_dict["project_details"] = json.loads(pd_json) if pd_json else None
    run_dict["issues"] = [row_to_issue(row) for row in issues]
    return run_dict


def list_runs() -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
    conn.close()
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
    conn = get_conn()
    conn.execute(
        "UPDATE issues SET status = ?, override_comment = ?, updated_at = datetime('now') WHERE id = ?",
        (status, override_comment, issue_id),
    )
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    conn.commit()
    conn.close()
    return row_to_issue(row) if row else None


def delete_run(run_id: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute("DELETE FROM issues WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    conn.commit()
    conn.close()
    return True


def insert_manual_issue(issue: dict[str, Any]) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO issues (
            id, run_id, category, item_key, title, description, severity, status, auto_status,
            page_number, bbox_json, snippet_path, page_preview_path, evidence, confidence,
            override_comment, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            issue["created_at"],
            issue["updated_at"],
        ),
    )
    conn.commit()
    conn.close()
