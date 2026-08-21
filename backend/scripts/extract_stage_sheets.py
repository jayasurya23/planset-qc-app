"""Extract sheet index + titles from sample plansets at each design stage.

Purpose: figure out which sheet numbers (E-001, E-100, etc.) exist at 30/60/90/IFC so
we can gate QC rules to the correct milestone.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import fitz  # PyMuPDF

SAMPLES = {
    "30%_HILLSBORO": r"d:\code\QAQC\planset-qc-app\backend\data\runs\1677ef30-4309-44c9-88ac-d00c6d87eb1d\HILLSBORO - 30%.pdf",
    "30%_WELLINGTON": r"d:\code\QAQC\planset-qc-app\backend\data\runs\2ea9e3f6-e936-45a5-988f-69f03d3f931c\WELLINGTON - 30%.pdf",
    "IFP_HILLSBORO": r"d:\code\QAQC\planset-qc-app\backend\data\runs\822efdfd-f24f-47b9-8b44-813e95e9e3c8\252-284-Hillsoboro IFP.pdf",
    "90%_GIRARD": r"d:\code\QAQC\planset-qc-app\backend\data\runs\475c53f8-898d-4966-955b-68fe04d34db0\Girard - 90%.pdf",
    "90%_BISHOP": r"d:\code\QAQC\planset-qc-app\backend\data\runs\4c0d71c3-99f8-4f75-9ec5-df1ad729a39b\bishop_90.pdf",
    "IFC_ZIA_HILLS": r"d:\code\QAQC\planset-qc-app\backend\data\runs\154278c0-a94e-42c9-aa77-e5f5b9520e25\247-169- Zia Hills IFC Rev1.pdf",
}

SHEET_RE = re.compile(r"\b([GCEM][-\s]?\d{2,4}(?:[A-Z]|\.\d+)?)\b")


def sheet_codes_on_page(page: fitz.Page) -> set[str]:
    text = page.get_text("text") or ""
    codes = set()
    for m in SHEET_RE.finditer(text):
        code = m.group(1).replace(" ", "").replace("--", "-")
        if not code.startswith(("G-", "C-", "E-", "M-", "G", "C", "E", "M")):
            continue
        if "-" not in code and len(code) >= 4:
            code = code[0] + "-" + code[1:]
        codes.add(code.upper())
    return codes


def titleblock_sheet_number(page: fitz.Page) -> str | None:
    """Grab the likely sheet number from the bottom-right title block.

    The clip is described in DISPLAY space, which is where the title block
    visibly is, but get_textbox() clips in the page's AUTHORED space. On a
    /Rotate 270 sheet the two are transposed and the box lands off the
    authored page, so this returned None on 471 of 473 corpus pages -- the
    two that worked were the corpus's only unrotated pages. De-rotating it
    recovers a code on 470 of 473.
    """
    rect = page.rect
    tb = fitz.Rect(rect.x1 - rect.width * 0.25, rect.y1 - rect.height * 0.25, rect.x1, rect.y1)
    tb = tb * page.derotation_matrix
    tb.normalize()  # a 90/270 transform can invert x0/x1 or y0/y1
    text = page.get_textbox(tb) or ""
    codes = sheet_codes_on_page_text(text)
    if codes:
        return sorted(codes)[0]
    return None


def sheet_codes_on_page_text(text: str) -> set[str]:
    codes = set()
    for m in SHEET_RE.finditer(text):
        code = m.group(1).replace(" ", "")
        if len(code) >= 3 and code[0] in "GCEM" and (code[1] == "-" or code[1].isdigit()):
            if "-" not in code:
                code = code[0] + "-" + code[1:]
            codes.add(code.upper())
    return codes


def analyze(tag: str, path: str) -> None:
    p = Path(path)
    if not p.exists():
        print(f"{tag}: MISSING {path}", file=sys.stderr)
        return
    doc = fitz.open(p)
    print(f"\n=== {tag} ({p.name}, {len(doc)} pages) ===")

    # First, try to parse the sheet index page (usually first 2-3 pages)
    index_codes: set[str] = set()
    for i in range(min(3, len(doc))):
        index_codes |= sheet_codes_on_page(doc[i])

    # Then, walk every page and grab each page's own sheet code from title block
    per_page: list[tuple[int, str | None]] = []
    for i, page in enumerate(doc):
        per_page.append((i + 1, titleblock_sheet_number(page)))

    distinct = sorted({c for _, c in per_page if c})
    print(f"Distinct sheet codes from title blocks ({len(distinct)}):")
    for c in distinct:
        print(f"  {c}")

    print(f"Codes mentioned on index pages ({len(index_codes)}):")
    for c in sorted(index_codes):
        print(f"  {c}")

    doc.close()


if __name__ == "__main__":
    for tag, path in SAMPLES.items():
        try:
            analyze(tag, path)
        except Exception as e:
            print(f"{tag}: ERROR {e}", file=sys.stderr)
