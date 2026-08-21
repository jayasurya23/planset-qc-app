"""Evidence snippets embedded in the XLSX must keep their aspect ratio.

The XLSX is the deliverable a QC engineer opens, and column L carries a
cropped image of the drawing region each finding refers to.

The bug: the embedded height was computed from img.width AFTER img.width had
already been overwritten with the target, so the ratio was always 1.0 and the
height stayed at the source pixel height.

    img.width  = 180
    img.height = int(img.height * (180 / img.width))   # 180/180 == 1.0

Every snippet came out stretched by exactly (source_width / 180): 4.5x on a
focused crop, and 28.8x on a full-page render, which embeds a 5184x3456 image
at 180x3456 instead of 180x120 and buries the findings table under it.

Run: PYTHONPATH=backend python backend/scripts/test_excel_snippets.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.exporter import SNIPPET_WIDTH_PX, build_workbook  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


TMP = Path(tempfile.mkdtemp())


def snippet(name: str, w: int, h: int) -> str:
    path = TMP / f"{name}.png"
    Image.new("RGB", (w, h), "white").save(path)
    return str(path)


# Shapes the pipeline actually produces: a whole page rendered at zoom 2, a
# region crop expanded to its table cell, and a wide single-line note.
SHAPES = [
    ("full_page", 5184, 3456),
    ("focused_crop", 816, 494),
    ("wide_note", 980, 192),
    ("tall_narrow", 240, 900),
]


def run_with(paths: list[str]) -> dict:
    return {
        "project_name": "Test", "original_filename": "t.pdf", "created_at": "-",
        "summary": {"pdf_page_count": 1, "indexed_sheet_count": 1, "actual_sheet_count": 1},
        "status_counts": {"Pass": 0, "Fail": len(paths), "Needs Review": 0, "Deferred": 0},
        "categories": [],
        "issues": [
            {
                "category": "Grounding", "title": f"check_{i}",
                "check_name": f"check_{i}", "status": "Fail", "auto_status": "Fail",
                "severity": "high", "page_number": 1, "confidence": 0.9,
                "evidence": "-", "description": "-", "override_comment": None,
                "snippet_path": p,
            }
            for i, p in enumerate(paths)
        ],
    }


print("Every embedded snippet keeps its source aspect ratio:")
paths = [snippet(n, w, h) for n, w, h in SHAPES]
ws = build_workbook(run_with(paths))["Issues"]
images = list(ws._images)
check(f"all {len(SHAPES)} snippets embedded", len(images) == len(SHAPES))

for (name, w, h), img in zip(SHAPES, images):
    want = round(h * SNIPPET_WIDTH_PX / w)
    got = img.height
    # The old code returned the source height verbatim for every one of these.
    check(f"{name} {w}x{h} -> {SNIPPET_WIDTH_PX}x{want} (got {got})", abs(got - want) <= 1)
    check(f"{name} is not embedded at its source height", got != h or h == want)

print("Width is pinned to the column, whatever the source:")
for (name, _w, _h), img in zip(SHAPES, images):
    check(f"{name} width == {SNIPPET_WIDTH_PX}", img.width == SNIPPET_WIDTH_PX)

print("The row is tall enough to show the snippet it holds:")
ws2 = build_workbook(run_with([snippet("tall", 240, 900)]))["Issues"]
img = list(ws2._images)[0]
# Data starts on row 2; row height is points, image height pixels (1px = .75pt).
height_pt = ws2.row_dimensions[2].height
check(f"row height {height_pt} pt covers a {img.height} px image",
      height_pt is not None and height_pt >= img.height * 0.75)

print("A tiny snippet does not collapse the row below the old floor:")
ws3 = build_workbook(run_with([snippet("small", 900, 40)]))["Issues"]
check("row keeps the 110 pt minimum", (ws3.row_dimensions[2].height or 0) >= 110)

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL EXCEL-SNIPPET CHECKS PASSED")
