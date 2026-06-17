"""Functional tests for the projects / stage / multi-user / rerun-versioning
data layer (db.py) and the EasyAuth identity resolver (auth.py).

Runs WITHOUT any AI call against a throwaway temp SQLite DB. Exercises:
  - one-time backfill of a legacy (pre-feature) runs row;
  - get_or_create_project case/space-insensitive matching;
  - insert_run persisting the new columns;
  - non-destructive rerun versioning (lineage, is_latest flip, ordering);
  - list_projects per-stage summary + multi-stage sorting;
  - auth.resolve_identity header / base64-claims / dev-fallback paths.

Usage (from backend/):
    python scripts/test_projects_versioning.py
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Point db at a fresh temp dir BEFORE importing it (DB_PATH is import-time).
_TMP = tempfile.mkdtemp(prefix="planset_test_db_")
os.environ["PLANSET_DATA_DIR"] = _TMP
os.environ["DEV_USER_EMAIL"] = ""  # clean slate for header-only auth cases

from app import auth, db  # noqa: E402


_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {extra}")


def _make_run(rid, project_id, *, stage="60", version=1, is_latest=1,
              parent=None, root=None, created_at="2026-02-01T00:00:00+00:00",
              created_by="eng@co.com"):
    return {
        "id": rid, "project_name": "Acme Solar", "original_filename": "acme.pdf",
        "created_at": created_at, "pdf_path": f"/tmp/{rid}.pdf", "page_count": 12,
        "summary": {"design_stage": stage}, "status_counts": {"Pass": 3, "Fail": 1},
        "categories": [], "project_details": None,
        "project_id": project_id, "design_stage": stage, "created_by": created_by,
        "created_by_id": "oid-1", "version": version, "is_latest": is_latest,
        "parent_run_id": parent, "root_run_id": root or rid,
    }


# ── 1. Legacy backfill: build an OLD-schema row, then migrate ───────────────
LEGACY_ID = str(uuid.uuid4())
conn = sqlite3.connect(str(db.DB_PATH))
conn.execute(
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY, project_name TEXT, original_filename TEXT,
        created_at TEXT NOT NULL, pdf_path TEXT NOT NULL, page_count INTEGER NOT NULL,
        summary_json TEXT NOT NULL, status_counts_json TEXT NOT NULL,
        categories_json TEXT NOT NULL, project_details_json TEXT, engineer_name TEXT)"""
)
conn.execute(
    "INSERT INTO runs (id, project_name, original_filename, created_at, pdf_path, "
    "page_count, summary_json, status_counts_json, categories_json, engineer_name) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)",
    (LEGACY_ID, "Test Project", "test.pdf", "2026-01-01T00:00:00+00:00",
     "/tmp/test.pdf", 10, '{"design_stage":"60"}', '{"Pass":5}', "[]", "Jay"),
)
conn.commit()
conn.close()

db.init_db()  # adds columns + projects table + backfill

run = db.get_run(LEGACY_ID)
check("legacy design_stage promoted from summary_json", run["design_stage"] == "60",
      str(run.get("design_stage")))
check("legacy created_by seeded from engineer_name", run["created_by"] == "Jay")
check("legacy version defaulted to 1", run["version"] == 1)
check("legacy is_latest defaulted to 1", run["is_latest"] == 1)
check("legacy root_run_id self-references", run["root_run_id"] == LEGACY_ID)
check("legacy project_id assigned", bool(run["project_id"]))

projs = db.list_projects()
check("backfill created exactly one project", len(projs) == 1, str(len(projs)))
check("backfill preserved project name", projs and projs[0]["name"] == "Test Project")

# init_db is idempotent — running again must not duplicate or error.
db.init_db()
check("init_db idempotent (still one project)", len(db.list_projects()) == 1)


# ── 2. get_or_create_project matching ───────────────────────────────────────
p1 = db.get_or_create_project("Acme Solar", "a@x.com")
p2 = db.get_or_create_project("  acme   solar ", "b@y.com")
check("project match is case/space-insensitive", p1 == p2)
p3 = db.get_or_create_project("Different Project")
check("distinct name -> distinct project", p3 != p1)


# ── 3. Fresh run insert (v1) ────────────────────────────────────────────────
V1 = str(uuid.uuid4())
db.insert_run(_make_run(V1, p1, version=1), [])
r1 = db.get_run(V1)
check("fresh run stores project_id", r1["project_id"] == p1)
check("fresh run version=1", r1["version"] == 1)
check("fresh run is_latest=1", r1["is_latest"] == 1)
check("fresh run design_stage stored", r1["design_stage"] == "60")
check("fresh run created_by stored", r1["created_by"] == "eng@co.com")


# ── 4. Non-destructive versioning via save_run_version (reanalyze path) ──────
# version / root / is_latest / parent are computed atomically — the values in
# the dict are placeholders that save_run_version must override.
V2 = str(uuid.uuid4())
db.save_run_version(_make_run(V2, p1, created_at="2026-02-02T00:00:00+00:00"), [], V1)

check("save_run_version assigned version 2", db.get_run(V2)["version"] == 2)
check("new version is latest", db.get_run(V2)["is_latest"] == 1)
check("predecessor demoted to not-latest", db.get_run(V1)["is_latest"] == 0)
check("predecessor preserved (non-destructive)", db.get_run(V1) is not None)
check("new version parented to the tip (V1)", db.get_run(V2)["parent_run_id"] == V1)
check("versions share root_run_id",
      db.get_run(V1)["root_run_id"] == db.get_run(V2)["root_run_id"] == V1)
versions = db.get_run_versions(V2)
check("lineage has 2 versions", len(versions) == 2, str(len(versions)))
check("versions ordered v1,v2", [v["version"] for v in versions] == [1, 2])
check("get_run_versions works from any member", len(db.get_run_versions(V1)) == 2)


# ── 4b. Reanalyzing a NON-LATEST version advances the tip (review HIGH fix) ──
# Lineage: V1(v1, not latest) -> V2(v2, latest). Reanalyze the OLD V1.
V3 = str(uuid.uuid4())
db.save_run_version(_make_run(V3, p1, created_at="2026-02-03T00:00:00+00:00"), [], V1)
check("non-latest rerun advances to version 3", db.get_run(V3)["version"] == 3)
lineage = db.get_run_versions(V3)
latest_rows = [v for v in lineage if v["is_latest"]]
check("exactly one latest in lineage", len(latest_rows) == 1, str(len(latest_rows)))
check("the latest is the newest version V3",
      latest_rows and latest_rows[0]["id"] == V3)
check("all version numbers unique",
      len({v["version"] for v in lineage}) == len(lineage))
check("non-latest rerun parented to the tip V2 (not the clicked V1)",
      db.get_run(V3)["parent_run_id"] == V2)
check("lineage now has 3 versions", len(lineage) == 3, str(len(lineage)))


# ── 5. list_projects per-stage summary ──────────────────────────────────────
acme = {p["id"]: p for p in db.list_projects()}[p1]
check("project run_count counts all versions", acme["run_count"] == 3, str(acme["run_count"]))
stage60 = [s for s in acme["stages"] if s["stage"] == "60"]
check("project has one stage-60 summary", len(stage60) == 1)
check("stage summary points at LATEST version (V3)",
      stage60 and stage60[0]["run_id"] == V3 and stage60[0]["version"] == 3)
detail = db.get_project_detail(p1)
check("project detail lists all runs", detail and len(detail["runs"]) == 3)


# ── 6. Multiple stages under one project, sorted ────────────────────────────
S30 = str(uuid.uuid4())
db.insert_run(_make_run(S30, p1, stage="30", version=1,
                        created_at="2026-01-15T00:00:00+00:00"), [])
acme2 = {p["id"]: p for p in db.list_projects()}[p1]
check("project now reports 2 stages", acme2["stage_count"] == 2, str(acme2["stage_count"]))
order = [s["stage"] for s in acme2["stages"]]
check("stages sorted 30 before 60", order.index("30") < order.index("60"), str(order))


# ── 7. auth.resolve_identity ────────────────────────────────────────────────
i = auth.resolve_identity("jay@castillo.com", "oid-123", None)
check("auth: header name -> email", i["email"] == "jay@castillo.com")
check("auth: header id -> user_id", i["user_id"] == "oid-123")

blob = base64.b64encode(json.dumps({"claims": [
    {"typ": "preferred_username", "val": "sam@castillo.com"},
    {"typ": "name", "val": "Sam Engineer"},
    {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": "oid-xyz"},
]}).encode()).decode()
i2 = auth.resolve_identity(None, None, blob)
check("auth: base64 blob email fallback", i2["email"] == "sam@castillo.com")
check("auth: base64 blob name claim", i2["name"] == "Sam Engineer")
check("auth: base64 blob oid fallback", i2["user_id"] == "oid-xyz")

os.environ["DEV_USER_EMAIL"] = "dev@local"
check("auth: DEV_USER_EMAIL fallback", auth.resolve_identity(None, None, None)["email"] == "dev@local")
os.environ["DEV_USER_EMAIL"] = ""
i4 = auth.resolve_identity(None, None, "not-valid-base64!!")
check("auth: garbage blob is safe", i4["email"] is None and i4["user_id"] is None)


print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
