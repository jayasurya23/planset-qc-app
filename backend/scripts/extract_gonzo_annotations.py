"""Extract reviewer annotations from the QC-engineer-marked Gonzo planset.

Output: structured JSON with one record per annotation — page, type, author,
comment text, bounding box, and (when reachable) the sheet code from the
title block on that page.

Usage from backend/:
    .venv/Scripts/python scripts/extract_gonzo_annotations.py

Output lands at:
    backend/data/gonzo_qc_annotations.json
    backend/data/gonzo_client_annotations.json

This is the labeled "ground truth" we'll bucket and compare against the
app's findings, with the overfitting guardrails we agreed on.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
APP = BACKEND / "app"
sys.path.insert(0, str(BACKEND))

# Reuse the planset's sheet-number extractor so each annotation gets a
# best-effort sheet code (E-100, E-200, etc.) for bucketing.
from app.analyzer import first_sheet_number  # noqa: E402

ROOT = Path(r"d:\code\QAQC\planset-qc-app\projects\Gonzo")
SOURCES = [
    ("qc_engineer", ROOT / "Planset - QC Engineer comments" / "Gonzo w comments.pdf"),
    ("client_elight", ROOT / "Planset - Client Comments" / "Gonzo 60% Electrical Drawings - E Light Comments.pdf"),
]
OUT_DIR = BACKEND / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# PDF annotation types we care about. PyMuPDF uses an integer + a name.
# Reviewer comments are typically Text/FreeText/Highlight/Underline/StrikeOut/
# Squiggly/Caret/Square/Circle/Polygon. We capture all and let the bucketing
# step decide what's a defect vs a stamp.
def _annot_type(annot: fitz.Annot) -> str:
    try:
        return annot.type[1]
    except Exception:
        return str(annot.type)


def _bbox(annot: fitz.Annot) -> dict | None:
    try:
        r = annot.rect
        return {"x0": round(r.x0, 2), "y0": round(r.y0, 2),
                "x1": round(r.x1, 2), "y1": round(r.y1, 2)}
    except Exception:
        return None


def _info_field(info: dict | None, key: str) -> str | None:
    if not info:
        return None
    val = info.get(key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _surrounding_text(page: fitz.Page, rect: fitz.Rect, pad: float = 30.0) -> str:
    """Read text from a slightly-padded box around the annotation — gives
    bucketing the actual planset content the reviewer was commenting on."""
    try:
        expanded = fitz.Rect(
            max(rect.x0 - pad, 0),
            max(rect.y0 - pad, 0),
            min(rect.x1 + pad, page.rect.x1),
            min(rect.y1 + pad, page.rect.y1),
        )
        txt = page.get_textbox(expanded) or ""
        # Collapse whitespace
        return " ".join(txt.split())[:300]
    except Exception:
        return ""


def extract_one(pdf_path: Path, source_label: str) -> list[dict]:
    if not pdf_path.exists():
        print(f"  MISSING: {pdf_path}", file=sys.stderr)
        return []
    doc = fitz.open(pdf_path)
    out: list[dict] = []
    for page_index in range(doc.page_count):
        page = doc[page_index]
        page_no = page_index + 1

        # Best-effort sheet code from this page's title block — same logic
        # the analyzer uses, so the labels match the app's categorization.
        try:
            sheet_code = first_sheet_number(page, page.get_text("text"))
        except Exception:
            sheet_code = None

        annots = list(page.annots() or [])
        if not annots:
            continue
        for a in annots:
            info = a.info or {}
            content = (a.info.get("content") if a.info else "") or ""
            content = content.strip()
            # Ignore "stamp" / pure markup annotations with no comment text;
            # those are usually approval/checkmark stamps, not defects.
            atype = _annot_type(a)
            if not content and atype not in ("Highlight", "Square", "Circle", "Polygon", "FreeText"):
                # Skip pure markups (StrikeOut/Underline/Squiggly with no text)
                # unless the type itself implies a distinct defect call-out.
                pass

            out.append({
                "source": source_label,
                "annotation_id": len(out) + 1,
                "page": page_no,
                "sheet_code": sheet_code,
                "type": atype,
                "author": _info_field(info, "title") or _info_field(info, "creator"),
                "subject": _info_field(info, "subject"),
                "content": content,
                "rect": _bbox(a),
                "surrounding_text": _surrounding_text(page, a.rect),
            })
    doc.close()
    return out


def main() -> int:
    for label, path in SOURCES:
        print(f"Extracting from {path.name}…")
        records = extract_one(path, label)
        print(f"  {len(records)} annotations")

        # Pages reached
        pages_with_annots = sorted({r["page"] for r in records})
        print(f"  pages with annotations: {len(pages_with_annots)} "
              f"(of {fitz.open(path).page_count})")

        # Sheet-code spread
        from collections import Counter
        codes = Counter(r["sheet_code"] or "(unread)" for r in records)
        print(f"  by sheet code: {dict(codes.most_common(8))}")

        # Annotation types
        types = Counter(r["type"] for r in records)
        print(f"  by type: {dict(types)}")

        # Save
        out_file = OUT_DIR / f"gonzo_{label}_annotations.json"
        out_file.write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"  → {out_file.relative_to(BACKEND.parent)}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
