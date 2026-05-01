from __future__ import annotations

import json
import logging
import logging.handlers
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analyzer import analyze_pdf, make_issue, utc_now
from .db import DATA_DIR, delete_run, get_run, init_db, insert_manual_issue, insert_run, list_runs, update_issue
from .exporter import export_due_diligence, export_run_to_excel
from .progress import clear_progress, get_progress, set_progress

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


class ManualIssueCreate(BaseModel):
    run_id: str
    category: str
    title: str
    description: str
    severity: str = "medium"
    status: str = "Needs Review"
    page_number: int | None = None
    evidence: str | None = None


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


@app.get("/api/runs")
def api_list_runs() -> list[dict]:
    return list_runs()


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str) -> dict:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/progress/{upload_id}")
def api_progress(upload_id: str) -> dict:
    p = get_progress(upload_id)
    return p or {"step": "waiting", "detail": "Waiting...", "pct": 0}


import threading

# Track background analysis results: upload_id -> run_data or error
_analysis_results: dict[str, dict | None] = {}
_analysis_lock = threading.Lock()


def _run_analysis_bg(
    upload_id: str, pdf_path: Path, project_name: str | None,
    original_filename: str, pd: dict | None, use_deep: bool = True,
    supporting_docs: list[dict] | None = None,
    design_stage: str | None = None,
    engineer_name: str | None = None,
) -> None:
    """Run analysis in a background thread, updating progress along the way."""
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
        if engineer_name:
            run["engineer_name"] = engineer_name

        set_progress(upload_id, "saving", "Saving results...", 95)
        insert_run(run, issues)
        saved = get_run(run["id"])

        set_progress(upload_id, "done", "Complete!", 100)
        with _analysis_lock:
            _analysis_results[upload_id] = saved
    except Exception as exc:
        logging.getLogger(__name__).exception("Analysis failed for %s", upload_id)
        set_progress(upload_id, "error", f"Analysis failed: {exc}", 0)
        with _analysis_lock:
            _analysis_results[upload_id] = {"error": str(exc)}


_VALID_STAGES = {"30", "60", "90", "IFC", "AsBuilt"}


@app.post("/api/analyze")
async def api_analyze(
    project_name: str | None = Form(None),
    project_details: str | None = Form(None),
    use_deep: str | None = Form("true"),
    supporting_docs: str | None = Form(None),
    design_stage: str | None = Form(None),
    engineer_name: str | None = Form(None),
    file: UploadFile = File(...),
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
                merged.update(pd)  # user's own values override the auto-extract
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

    # Launch in background thread — return upload_id immediately
    t = threading.Thread(
        target=_run_analysis_bg,
        args=(upload_id, pdf_path, project_name, file.filename, pd, deep_flag, sd, stage, eng),
        daemon=True,
    )
    t.start()

    return {
        "upload_id": upload_id, "status": "running",
        "deep_mode": deep_flag, "design_stage": stage,
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

    upload_id = str(uuid.uuid4())
    new_run_dir = RUNS_DIR / upload_id
    new_run_dir.mkdir(parents=True, exist_ok=True)
    new_pdf_path = new_run_dir / Path(original_filename).name
    shutil.copy2(old_pdf_path, new_pdf_path)

    old_run_dir = old_pdf_path.parent
    delete_run(run_id)
    if old_run_dir.exists() and old_run_dir != new_run_dir:
        shutil.rmtree(old_run_dir, ignore_errors=True)

    set_progress(upload_id, "analyze", "Re-running analysis...", 10)

    deep_flag = (use_deep or "true").strip().lower() not in ("false", "0", "no", "off")

    eng = (engineer_name or "").strip() or old_run.get("engineer_name")

    t = threading.Thread(
        target=_run_analysis_bg,
        args=(upload_id, new_pdf_path, project_name, original_filename, None,
              deep_flag, None, None, eng),
        daemon=True,
    )
    t.start()

    return {"upload_id": upload_id, "status": "running", "deep_mode": deep_flag}


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
