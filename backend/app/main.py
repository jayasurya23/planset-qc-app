from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analyzer import analyze_pdf, make_issue, utc_now
from .auth import current_user
from .db import (
    DATA_DIR, chat_thread_stats, delete_run, get_chat_messages,
    get_issue_run_id, get_or_create_project,
    get_project, get_project_detail, get_run, get_run_versions, init_db,
    insert_chat_message, insert_issue_feedback, insert_manual_issue, insert_run,
    insert_run_feedback, latest_run_feedback, list_projects, list_runs,
    save_run_version, update_issue, update_run_name,
)
from . import chat as qc_chat
from .exporter import export_due_diligence, export_run_to_excel
from .progress import clear_progress, get_progress, set_progress
from . import jobs

APP_DIR = Path(__file__).resolve().parents[1]

# ── File logging ──────────────────────────────────────────────────────────
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_log_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Rotating file: 5 MB per file, keep last 5 files
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "planset_qc.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)
_file_handler.setLevel(logging.DEBUG)

# Apply to root logger so all modules (analyzer, gemini_client, etc.) log to file
logging.getLogger().addHandler(_file_handler)
logging.getLogger().setLevel(logging.INFO)
RUNS_DIR = DATA_DIR / "runs"
EXPORTS_DIR = DATA_DIR / "exports"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Castillo Planset QC API", version="0.3.0")
# CORS: explicit allowlist, not a wildcard. In production the SPA is served
# same-origin by this app (behind EasyAuth), so CORS is only exercised by the
# local Vite dev server; extra origins can be added via CORS_ORIGINS.
_cors_origins = [
    o.strip() for o in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/artifacts", StaticFiles(directory=DATA_DIR), name="artifacts")

_req_log = logging.getLogger("access")


@app.middleware("http")
async def log_requests(request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    response = await call_next(request)
    # Skip noisy polling endpoints
    path = request.url.path
    if not path.startswith("/api/progress/") and not path.startswith("/artifacts/"):
        _req_log.info("%s %s %s → %d", client_ip, request.method, path, response.status_code)
    return response


@app.on_event("startup")
def on_startup() -> None:
    init_db()


class IssueUpdate(BaseModel):
    status: str
    override_comment: str | None = None


class RunRename(BaseModel):
    run_name: str | None = None


class ManualIssueCreate(BaseModel):
    run_id: str
    category: str
    title: str
    description: str
    severity: str = "medium"
    status: str = "Needs Review"
    page_number: int | None = None
    evidence: str | None = None


class IssueFeedbackCreate(BaseModel):
    tags: list[str] = []
    comment: str | None = None
    engineer_name: str | None = None


class RunFeedbackCreate(BaseModel):
    rating: str  # "saved_time" | "even" | "cost_time"
    comment: str | None = None
    engineer_name: str | None = None


_VALID_RATINGS = {"saved_time", "even", "cost_time"}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


_PARSE_DOCS_PROMPT = """\
You are an assistant that extracts solar PV project details from documents, emails, \
and reports. The user has uploaded one or more files related to a solar PV project. \
Extract ANY project details you can find and return them as a JSON object.

Use EXACTLY these field names (omit any field you cannot find):

```json
{
  "project_name": "",
  "project_address": "",
  "site_coordinates": "(lat, long)",
  "county": "",
  "state": "",
  "parcel_id": "",
  "building_codes": "",
  "der_number": "",
  "owner_name": "",
  "owner_address": "",
  "owner_phone": "",
  "epc_name": "",
  "epc_address": "",
  "epc_phone": "",
  "eor_name": "",
  "eor_license": "",
  "eor_state": "",
  "checker_name": "",
  "designer_name": "",
  "module_make": "",
  "module_model": "",
  "module_stc_watts": "",
  "module_voc": "",
  "module_vmp": "",
  "module_isc": "",
  "module_imp": "",
  "module_temp_coeff_voc": "",
  "module_temp_coeff_isc": "",
  "is_bifacial": "yes/no",
  "string_size": "",
  "string_quantity": "",
  "total_dc_kw": "",
  "inverter_make": "",
  "inverter_model": "",
  "inverter_kva": "",
  "inverter_kw": "",
  "inverter_max_vdc": "",
  "inverter_mppt_range": "",
  "inverter_quantity": "",
  "total_ac_kva": "",
  "dc_ac_ratio": "",
  "racking_make": "",
  "racking_model": "",
  "racking_type": "fixed/tracker",
  "pitch": "",
  "interrow_spacing": "",
  "gcr": "",
  "tilt_angle": "",
  "azimuth": "",
  "poi_voltage": "",
  "feeder_grounding": "",
  "fault_current": "",
  "transformer_kva": "",
  "transformer_primary_voltage": "",
  "transformer_secondary_voltage": "",
  "transformer_winding_config": "",
  "transformer_impedance": "",
  "transformer_bil": "",
  "recloser_make": "",
  "recloser_continuous_a": "",
  "recloser_interrupting_ka": "",
  "ct_ratio": "",
  "vt_ratio": "",
  "meter_accuracy_class": "",
  "surge_arrestor_mcov": "",
  "utility_name": "",
  "utility_feeder": "",
  "is_ngrid": "yes/no",
  "ieee_category": "I/II/III",
  "design_temp_low_c": "",
  "design_temp_high_c": "",
  "ambient_temp_c": "",
  "special_notes": ""
}
```

Return ONLY the JSON object with fields you found. Do not include fields with empty values. \
If you find a value, include it as a string even if it's a number. \
Look for these details in interconnection agreements, CESIR documents, impact studies, \
equipment submittals, client emails, project specs, and any other project documentation.

Only return the JSON object, no other text.
"""

_MIME_MAP = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".eml": "text/plain",
    ".msg": "text/plain",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
}


@app.post("/api/parse-project-details")
async def api_parse_project_details(
    files: list[UploadFile] = File(...),
) -> dict:
    """Upload documents/emails and extract project details using AI."""
    import logging
    import fitz
    from .gemini_client import analyze_multiple_images, analyze_text

    log = logging.getLogger(__name__)
    all_extracted: dict = {}

    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        data = await f.read()

        try:
            if ext in (".txt", ".eml", ".msg", ".csv"):
                # Text files — send as text prompt
                text_content = data.decode("utf-8", errors="replace")[:50000]
                prompt = (
                    f"The following is the content of a file named '{f.filename}':\n\n"
                    f"{text_content}\n\n{_PARSE_DOCS_PROMPT}"
                )
                raw = analyze_text(prompt)

            elif ext == ".pdf":
                # PDF — extract text + render first few pages as images for AI
                doc = fitz.open(stream=data, filetype="pdf")
                # Extract text from all pages
                text_parts = []
                for i in range(min(doc.page_count, 20)):
                    text_parts.append(doc[i].get_text("text"))
                pdf_text = "\n---PAGE BREAK---\n".join(text_parts)

                # Also render first 3 pages as images (for tables/diagrams)
                page_images: list[bytes] = []
                for i in range(min(doc.page_count, 3)):
                    pix = doc[i].get_pixmap(
                        matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                    page_images.append(pix.tobytes("png"))
                doc.close()

                # Send both text and images to AI
                prompt = (
                    f"The following is extracted text from a PDF named '{f.filename}':\n\n"
                    f"{pdf_text[:40000]}\n\n{_PARSE_DOCS_PROMPT}"
                )
                if page_images:
                    raw = analyze_multiple_images(
                        page_images, prompt, "image/png")
                else:
                    raw = analyze_text(prompt)

            elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                # Images — send as image to AI
                mime = _MIME_MAP.get(ext, "image/png")
                from .gemini_client import analyze_page_image
                raw = analyze_page_image(data, _PARSE_DOCS_PROMPT, mime)

            else:
                # Other files — try sending text extraction or skip
                text_content = data.decode("utf-8", errors="replace")[:50000]
                prompt = (
                    f"The following is the content of a file named '{f.filename}':\n\n"
                    f"{text_content}\n\n{_PARSE_DOCS_PROMPT}"
                )
                raw = analyze_text(prompt)

        except Exception:
            log.exception("Failed to parse file %s", f.filename)
            continue

        # Parse the JSON response
        parsed = _parse_extracted_json(raw)
        # Merge: later files overwrite earlier ones for the same field
        for k, v in parsed.items():
            if v and str(v).strip():
                all_extracted[k] = str(v).strip()

    return {"project_details": all_extracted}


def _parse_extracted_json(text: str) -> dict:
    """Extract a JSON object from Gemini's response."""
    import re as _re
    # Try ```json block
    m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try whole response
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    # Find outermost { }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {}


@app.post("/api/parse-supporting-docs")
async def api_parse_supporting_docs(
    files: list[UploadFile] = File(...),
) -> dict:
    """Ingest supporting engineering documents (CESIR, PVSyst, ampacity, …).

    Each file is classified and sent through a type-specific extractor.
    Returns a list of SupportingDoc dicts that the frontend can preview before
    attaching to an analyze call.
    """
    from .supporting_docs import process_document

    out: list[dict] = []
    for f in files:
        if not f.filename:
            continue
        data = await f.read()
        try:
            doc = process_document(f.filename, data)
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to ingest supporting doc %s", f.filename)
            continue
        out.append(doc.to_dict())
    return {"supporting_docs": out}


@app.get("/api/me")
def api_me(user: dict = Depends(current_user)) -> dict:
    """The signed-in engineer (from EasyAuth, or DEV_USER_EMAIL locally)."""
    return user


@app.get("/api/runs")
def api_list_runs() -> list[dict]:
    return list_runs()


@app.get("/api/projects")
def api_list_projects() -> list[dict]:
    """Projects with a per-stage summary, newest activity first."""
    return list_projects()


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: str) -> dict:
    proj = get_project_detail(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str) -> dict:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs/{run_id}/versions")
def api_run_versions(run_id: str) -> list[dict]:
    """All rerun versions in this run's lineage, oldest → newest."""
    return get_run_versions(run_id)


@app.patch("/api/runs/{run_id}")
def api_rename_run(run_id: str, payload: RunRename) -> dict:
    """Set or clear a run's human-friendly name."""
    updated = update_run_name(run_id, payload.run_name)
    if not updated:
        raise HTTPException(status_code=404, detail="Run not found")
    return updated


@app.get("/api/progress/{upload_id}")
def api_progress(upload_id: str) -> dict:
    p = get_progress(upload_id)
    return p or {"step": "waiting", "detail": "Waiting...", "pct": 0}


@app.get("/api/jobs")
def api_jobs() -> dict:
    """Shared activity feed: every analysis in process (queued/running) plus
    recently finished ones, across all engineers. Drives the Activity panel."""
    return {"jobs": jobs.list_jobs(), **jobs.stats()}


import threading

# Track background analysis results: upload_id -> run_data or error
_analysis_results: dict[str, dict | None] = {}
_analysis_lock = threading.Lock()


def _run_analysis_bg(
    upload_id: str, pdf_path: Path, project_name: str | None,
    original_filename: str, pd: dict | None, use_deep: bool = True,
    supporting_docs: list[dict] | None = None,
    design_stage: str | None = None,
    extra_meta: dict | None = None,
) -> dict | None:
    """Run analysis in a background thread, updating progress along the way.

    Returns the saved run dict on success (carries ``id``) or ``{"error": ...}``
    on failure, so the job queue can record the outcome.

    ``extra_meta`` carries orchestration fields stamped onto the run before it
    is saved — attribution (engineer_name/created_by/created_by_id), project
    linkage (project_id), and rerun versioning (parent_run_id/root_run_id/
    version/is_latest). After a successful save, the predecessor identified by
    parent_run_id (if any) is marked superseded — done here, not at request
    time, so a failed reanalysis leaves the prior version intact and current.
    """
    try:
        def on_progress(step: str, detail: str, pct: int) -> None:
            set_progress(upload_id, step, detail, pct)

        run, issues = analyze_pdf(
            pdf_path=pdf_path,
            project_name=project_name,
            original_filename=original_filename,
            progress_cb=on_progress,
            project_details=pd,
            use_deep=use_deep,
            supporting_docs=supporting_docs,
            design_stage=design_stage,
        )
        for key, val in (extra_meta or {}).items():
            if val is not None:
                run[key] = val

        set_progress(upload_id, "saving", "Saving results...", 95)
        # A rerun (predecessor present) is versioned atomically from the
        # lineage's current state; a fresh analysis is a plain insert.
        predecessor_run_id = (extra_meta or {}).get("parent_run_id")
        if predecessor_run_id:
            save_run_version(run, issues, predecessor_run_id)
        else:
            insert_run(run, issues)
        saved = get_run(run["id"])

        set_progress(upload_id, "done", "Complete!", 100)
        with _analysis_lock:
            _analysis_results[upload_id] = saved
        return saved
    except Exception as exc:
        logging.getLogger(__name__).exception("Analysis failed for %s", upload_id)
        set_progress(upload_id, "error", f"Analysis failed: {exc}", 0)
        with _analysis_lock:
            _analysis_results[upload_id] = {"error": str(exc)}
        return {"error": str(exc)}


_VALID_STAGES = {"30", "60", "90", "IFC", "AsBuilt"}


@app.post("/api/analyze")
async def api_analyze(
    project_name: str | None = Form(None),
    project_id: str | None = Form(None),
    run_name: str | None = Form(None),
    project_details: str | None = Form(None),
    use_deep: str | None = Form("true"),
    supporting_docs: str | None = Form(None),
    design_stage: str | None = Form(None),
    engineer_name: str | None = Form(None),
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file")

    pd = None
    if project_details:
        try:
            pd = json.loads(project_details)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="Invalid project_details JSON")

    sd: list[dict] | None = None
    if supporting_docs:
        try:
            sd_raw = json.loads(supporting_docs)
            if isinstance(sd_raw, list):
                sd = [d for d in sd_raw if isinstance(d, dict)]
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="Invalid supporting_docs JSON")

    # Auto-merge datasheet-extracted specs into project_details. User-entered
    # values WIN — this only fills gaps. Lets calc rules (validate_stringing,
    # validate_fuse_sizing, validate_transformer, …) stop deferring when the
    # module/inverter/transformer datasheets are uploaded but the user hasn't
    # manually typed every spec.
    if sd:
        from .supporting_docs import project_details_from_supporting_docs
        auto_pd = project_details_from_supporting_docs(sd)
        if auto_pd:
            merged = dict(auto_pd)
            if pd:
                # user's own values override the auto-extract — but a blank box
                # is not a value. The form posts every field it renders, so
                # letting "" through would erase a datasheet-extracted spec and
                # silently defer the calc that needed it.
                merged.update({
                    k: v for k, v in pd.items()
                    if v is not None and str(v).strip() != ""
                })
            pd = merged
            logging.getLogger(__name__).info(
                "Auto-merged %d project_details fields from datasheet uploads "
                "(%d kept from user input).",
                len(auto_pd), len(set(pd.keys()) & set(auto_pd.keys())),
            )

    upload_id = str(uuid.uuid4())
    run_dir = RUNS_DIR / upload_id
    run_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = run_dir / file.filename

    set_progress(upload_id, "upload", "Saving PDF...", 5)
    with pdf_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    set_progress(upload_id, "analyze", "Starting analysis...", 10)

    deep_flag = (use_deep or "true").strip().lower() not in ("false", "0", "no", "off")

    stage = (design_stage or "").strip() or None
    if stage and stage not in _VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid design_stage {stage!r}; expected one of {sorted(_VALID_STAGES)}",
        )

    eng = (engineer_name or "").strip() or None

    # Identity: prefer the signed-in user (trustworthy behind EasyAuth in prod);
    # the engineer_name form field is only a display fallback for local dev.
    created_by = user.get("email") or eng
    created_by_id = user.get("user_id")
    eng_display = eng or user.get("name") or user.get("email")

    # Link every run to a project so the project browser works even before the
    # explicit picker (Phase 2). Honor a valid explicit project_id, else match
    # or create one from the (cleaned) project name.
    proj_name = (project_name or "").strip() or Path(file.filename).stem
    if project_id and get_project(project_id):
        resolved_project_id = project_id
    else:
        resolved_project_id = get_or_create_project(proj_name, created_by)

    extra_meta = {
        "engineer_name": eng_display,
        "created_by": created_by,
        "created_by_id": created_by_id,
        "project_id": resolved_project_id,
        "run_name": (run_name or "").strip() or None,
    }

    # Enqueue on the bounded analysis queue — returns the upload_id immediately;
    # the job runs when a slot is free (status 'queued' until then).
    jobs.submit(
        upload_id, "analyze",
        {
            "project_name": proj_name,
            "run_name": extra_meta["run_name"],
            "started_by": eng_display,
            "created_by": created_by,
        },
        lambda: _run_analysis_bg(
            upload_id, pdf_path, proj_name, file.filename, pd, deep_flag, sd, stage, extra_meta
        ),
    )

    return {
        "upload_id": upload_id, "status": "queued",
        "deep_mode": deep_flag, "design_stage": stage,
        "project_id": resolved_project_id,
    }


@app.get("/api/result/{upload_id}")
def api_get_result(upload_id: str) -> dict:
    """Get the analysis result once it's done. Returns 202 if still running."""
    with _analysis_lock:
        result = _analysis_results.get(upload_id)
    if result is None:
        p = get_progress(upload_id)
        if p and p.get("step") == "error":
            raise HTTPException(status_code=500, detail=p.get("detail", "Analysis failed"))
        raise HTTPException(status_code=202, detail="Still processing")
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    # Clean up
    with _analysis_lock:
        _analysis_results.pop(upload_id, None)
    clear_progress(upload_id)
    return {"upload_id": upload_id, **result}


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: str) -> dict:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    # Clean up run artifacts on disk — the directory is the parent of the
    # stored pdf_path (not RUNS_DIR/run_id, since the dir uses upload_id)
    pdf_path = Path(run["pdf_path"])
    run_dir = pdf_path.parent if pdf_path.exists() else RUNS_DIR / run_id
    delete_run(run_id)
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    return {"ok": True, "deleted": run_id}


@app.post("/api/runs/{run_id}/reanalyze")
async def api_reanalyze(
    run_id: str,
    use_deep: str | None = Form("true"),
    engineer_name: str | None = Form(None),
    user: dict = Depends(current_user),
) -> dict:
    old_run = get_run(run_id)
    if not old_run:
        raise HTTPException(status_code=404, detail="Run not found")
    old_pdf_path = Path(old_run["pdf_path"])
    if not old_pdf_path.exists():
        raise HTTPException(
            status_code=400, detail="Original PDF no longer exists on disk")

    project_name = old_run["project_name"]
    original_filename = old_run["original_filename"]

    # Non-destructive rerun: the new run is the next VERSION in the same lineage.
    # The old run and its on-disk artifacts are preserved; it is flagged
    # not-latest only after the new version saves (in _run_analysis_bg), so a
    # failed reanalysis leaves the prior version intact and current.
    upload_id = str(uuid.uuid4())
    new_run_dir = RUNS_DIR / upload_id
    new_run_dir.mkdir(parents=True, exist_ok=True)
    new_pdf_path = new_run_dir / Path(original_filename).name
    shutil.copy2(old_pdf_path, new_pdf_path)

    set_progress(upload_id, "analyze", "Re-running analysis...", 10)

    deep_flag = (use_deep or "true").strip().lower() not in ("false", "0", "no", "off")

    eng = (engineer_name or "").strip() or None
    created_by = user.get("email") or eng or old_run.get("created_by")
    created_by_id = user.get("user_id") or old_run.get("created_by_id")
    eng_display = (eng or user.get("name") or user.get("email")
                   or old_run.get("engineer_name"))

    project_id = old_run.get("project_id") or get_or_create_project(
        project_name or original_filename, created_by)
    # Force the prior stage so a rerun stays the same stage submission rather
    # than risking a different auto-detection.
    old_stage = old_run.get("design_stage")

    # parent_run_id signals "this is a rerun" + identifies the lineage; the
    # actual version number, root and predecessor are resolved atomically from
    # the lineage's current tip in db.save_run_version at save time.
    extra_meta = {
        "engineer_name": eng_display,
        "created_by": created_by,
        "created_by_id": created_by_id,
        "project_id": project_id,
        "parent_run_id": run_id,
        "run_name": old_run.get("run_name"),
    }

    jobs.submit(
        upload_id, "reanalyze",
        {
            "project_name": project_name or original_filename,
            "run_name": old_run.get("run_name"),
            "started_by": eng_display,
            "created_by": created_by,
        },
        lambda: _run_analysis_bg(
            upload_id, new_pdf_path, project_name, original_filename, None,
            deep_flag, None, old_stage, extra_meta
        ),
    )

    return {
        "upload_id": upload_id, "status": "queued",
        "deep_mode": deep_flag, "parent_run_id": run_id,
    }


@app.patch("/api/issues/{issue_id}")
def api_update_issue(issue_id: str, payload: IssueUpdate) -> dict:
    updated = update_issue(issue_id, payload.status, payload.override_comment)
    if not updated:
        raise HTTPException(status_code=404, detail="Issue not found")
    return updated


@app.post("/api/issues/manual")
def api_create_manual_issue(payload: ManualIssueCreate) -> dict:
    run = get_run(payload.run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    issue = make_issue(
        run_id=payload.run_id,
        item_key=f"manual_{uuid.uuid4().hex[:8]}",
        category=payload.category,
        title=payload.title,
        description=payload.description,
        status=payload.status,
        severity=payload.severity,
        page_number=payload.page_number,
        evidence=payload.evidence,
        confidence=1.0,
    )
    insert_manual_issue(issue)
    return issue


@app.post("/api/issues/{issue_id}/feedback")
def api_create_issue_feedback(issue_id: str, payload: IssueFeedbackCreate) -> dict:
    if not (payload.tags or (payload.comment or "").strip()):
        raise HTTPException(
            status_code=400, detail="Provide at least one tag or a comment")
    fb = {
        "id": str(uuid.uuid4()),
        "issue_id": issue_id,
        "run_id": "",
        "engineer_name": (payload.engineer_name or "").strip() or None,
        "tags": [t for t in payload.tags if t and isinstance(t, str)],
        "comment": (payload.comment or "").strip() or None,
        "created_at": utc_now(),
    }
    run_id = get_issue_run_id(issue_id)
    if not run_id:
        raise HTTPException(status_code=404, detail="Issue not found")
    fb["run_id"] = run_id
    insert_issue_feedback(fb)
    return fb


@app.post("/api/runs/{run_id}/feedback")
def api_create_run_feedback(run_id: str, payload: RunFeedbackCreate) -> dict:
    if payload.rating not in _VALID_RATINGS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid rating {payload.rating!r}; expected one of {sorted(_VALID_RATINGS)}",
        )
    if not get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    fb = {
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "engineer_name": (payload.engineer_name or "").strip() or None,
        "rating": payload.rating,
        "comment": (payload.comment or "").strip() or None,
        "created_at": utc_now(),
    }
    insert_run_feedback(fb)
    return fb


@app.get("/api/runs/{run_id}/feedback")
def api_get_run_feedback(run_id: str, engineer_name: str | None = None) -> dict:
    """Most recent rating for this run by this engineer (so the UI can avoid
    re-prompting people who've already rated)."""
    fb = latest_run_feedback(run_id, (engineer_name or "").strip() or None)
    return {"feedback": fb}


# ── QC copilot chat (Phase 1: read-only, grounded) ────────────────────────


class ChatMessageCreate(BaseModel):
    message: str


@app.get("/api/runs/{run_id}/chat")
def api_chat_history(run_id: str, user: dict = Depends(current_user)) -> dict:
    run = get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return {
        "messages": get_chat_messages(run_id),
        "config": qc_chat.chat_config(),
        "citations": qc_chat.citation_map(run),
        "stats": chat_thread_stats(run_id),
    }


@app.post("/api/runs/{run_id}/chat")
def api_chat_send(
    run_id: str,
    payload: ChatMessageCreate,
    user: dict = Depends(current_user),
):
    """Send one chat turn; the assistant reply streams back as SSE.

    Read-only by design: the model has no tool that writes issue status, and
    nothing persisted here is ever read by the Excel exporter — the checklist
    remains the system of record.
    """
    run = get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    text = (payload.message or "").strip()
    if not text:
        raise HTTPException(400, "Empty message")
    refusal = qc_chat.check_ceilings(run_id, text)
    if refusal:
        raise HTTPException(429, refusal)

    history = get_chat_messages(run_id)  # before this turn
    insert_chat_message({
        "id": str(uuid.uuid4()), "run_id": run_id, "role": "user",
        "content": text, "created_by": user.get("email"),
        "created_at": utc_now(),
    })

    def event_stream():
        chunks: list[str] = []
        usage: dict = {}
        model = None
        try:
            for ev in qc_chat.stream_reply(run, history, text):
                if ev["type"] == "delta":
                    chunks.append(ev["text"])
                    yield f"data: {json.dumps(ev)}\n\n"
                elif ev["type"] == "done":
                    usage = ev.get("usage") or {}
                    model = ev.get("model")
        except Exception as exc:  # noqa: BLE001 — surface, don't hang the stream
            logging.getLogger(__name__).exception("chat stream failed")
            yield "data: " + json.dumps(
                {"type": "error",
                 "message": f"Model call failed ({type(exc).__name__}); "
                            "please retry."}) + "\n\n"
        full = "".join(chunks)
        message_id = None
        if full:
            message_id = str(uuid.uuid4())
            insert_chat_message({
                "id": message_id, "run_id": run_id, "role": "assistant",
                "content": full, "model": model,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "created_at": utc_now(),
            })
        yield "data: " + json.dumps({
            "type": "done", "message_id": message_id, "model": model,
            "usage": usage, "citations": qc_chat.citation_map(run),
        }) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/due-diligence-template")
def api_due_diligence_template():
    out_path = EXPORTS_DIR / "Castillo_Due_Diligence_Template.xlsx"
    export_due_diligence(out_path)
    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="Castillo_Due_Diligence_Template.xlsx",
    )


@app.get("/api/export/{run_id}")
def api_export_run(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    safe_project = "".join(
        c for c in run["project_name"] if c.isalnum() or c in ("-", "_", " ")
    ).strip() or "planset"
    out_path = EXPORTS_DIR / f"{safe_project}_{run_id[:8]}_qc.xlsx"
    export_run_to_excel(run, out_path)
    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=out_path.name,
    )


# ── Serve the built React SPA (single-container deployment) ─────────────────
# When FRONTEND_DIST points at a built frontend (set in the Docker image), mount
# it at "/" so one container serves both the API and the UI. Mounted last so all
# /api and /artifacts routes above take precedence. Unset for local dev, where
# Vite serves the UI on :5173 and talks to this API on :8000.
_frontend_dist = os.getenv("FRONTEND_DIST")
if _frontend_dist and Path(_frontend_dist).is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
