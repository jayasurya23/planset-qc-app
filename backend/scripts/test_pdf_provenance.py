"""Record what wrote each planset, and say so when it is not a CAD export.

Prompted by a team considering a move from Adobe Acrobat to Bluebeam. The
viewer itself is irrelevant — the tool reads PDF bytes and never invokes
either application — but the question exposed something worth recording.

All 40 plansets in the corpus are direct CAD exports:

    Creator   AutoCAD 2025 / 2026 / 2027
    Producer  pdfplot17 / pdfplot18   (AutoCAD's PDF driver)

That matters because sheet numbers, sheet titles and every text-anchored
evidence highlight are read out of the text layer CAD writes. A file re-saved
by another application can carry a rewritten text layer, and a FLATTENED file
additionally merges reviewer markup into the page content, where the title
extractor and the highlight search would treat it as drawing content.

Un-flattened annotations are harmless, and this was checked rather than
assumed: on a 205-markup review copy, the markup phrase "make sure it is
correct" does NOT appear in the extracted text. PyMuPDF reads the page content
stream; annotation appearance streams are not part of it. An earlier reading
that markup text "leaked" was wrong — those matches were coincidental
substrings such as a reviewer's name, which also appears legitimately in the
title block.

Run: PYTHONPATH=backend python backend/scripts/test_pdf_provenance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from app.analyzer import pdf_provenance  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def doc_with(producer: str = "", creator: str = "", annots: int = 0):
    doc = fitz.open()
    page = doc.new_page(width=1728, height=1120)
    page.insert_text((200, 200), "E-100 SINGLE LINE DIAGRAM", fontsize=12)
    for i in range(annots):
        page.add_rect_annot(fitz.Rect(300 + i * 20, 300, 340 + i * 20, 340))
    meta = doc.metadata or {}
    meta.update({"producer": producer, "creator": creator})
    doc.set_metadata(meta)
    return doc


print("A direct CAD export is recognised:")
for producer, creator in (
    ("pdfplot18.hdi 18.00.060.00000", "AutoCAD 2027 - English 2027 (26.0)"),
    ("pdfplot17.hdi 17.01.060.00000", "AutoCAD 2025 - English 2025 (25.0s"),
    ("", "AutoCAD 2026"),
    ("Autodesk DWG to PDF", ""),
):
    p = pdf_provenance(doc_with(producer, creator))
    check(f"{(producer or creator)[:34]!r} -> CAD export", p["is_cad_export"])

print("A file re-saved by something else is NOT:")
for producer, creator in (
    ("Bluebeam Revu 21", "Bluebeam Revu"),
    ("Adobe Acrobat Pro 24.0", "Adobe Acrobat"),
    ("Ghostscript 10.0", ""),
    ("", ""),
):
    p = pdf_provenance(doc_with(producer, creator))
    check(f"{(producer or 'no metadata')[:34]!r} -> not a CAD export",
          not p["is_cad_export"])

print("Provenance is reported faithfully:")
d = doc_with("pdfplot18.hdi", "AutoCAD 2027")
p = pdf_provenance(d)
check("producer preserved", p["producer"] == "pdfplot18.hdi")
check("creator preserved", p["creator"] == "AutoCAD 2027")
d.close()
p = pdf_provenance(doc_with("", ""))
check("absent metadata reports None, not empty string",
      p["producer"] is None and p["creator"] is None)

print("Markups are counted but do not disqualify a file:")
p = pdf_provenance(doc_with("pdfplot18.hdi", "AutoCAD 2027", annots=5))
check("annotations counted", p["annotation_count"] == 5)
check("a marked-up CAD export is still a CAD export", p["is_cad_export"])

print("Un-flattened markup stays out of the text layer:")
# The property the whole recommendation rests on: reviewers can mark up a
# planset in any tool and the extractor will not read their comments.
d = fitz.open()
page = d.new_page(width=1728, height=1120)
page.insert_text((200, 200), "E-100 SINGLE LINE DIAGRAM", fontsize=12)
a = page.add_text_annot(fitz.Point(400, 400), "make sure it is correct")
a.update()
text = page.get_text("text")
check("drawing text is extracted", "SINGLE LINE DIAGRAM" in text)
check("markup text is NOT extracted", "make sure it is correct" not in text)
d.close()

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL PDF-PROVENANCE CHECKS PASSED")
