from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import fitz
from PIL import Image, ImageDraw

from .rule_registry import Rule, get_category_order, get_rules, get_rules_by_check_type
from .electrical_calcs import CALC_FUNCTIONS, run_calc

# Match common engineering sheet number formats:
# E-001, E-100A, C-001, S-1, EE-01, SP-001  (prefix-dash-digits)
# E1.0, E1.01, C1.1  (prefix-dot-digits)
#
# Known sheet prefixes in solar plansets:
#   E (Electrical), C (Civil), S (Structural), G (General), M (Mechanical),
#   SP (Site Plan), EE (Electrical Engineering), EL (Elevation), GD (Grounding),
#   FP (Feeder Plan), CP (Comm Plan), DC (DC Diagram), PD (Pole Detail),
#   TD (Trench Detail), LB (Labels), A (Architecture)
#
# Exclude false positives like: USE-2 (wire type), FS-7535 (fuse spec),
#   CT-5 (CT ratio), VT-3 (VT ratio), UL-1741 (standard)

# Prefixes that are NOT sheet numbers — product specs, wire types, standards
_NOT_SHEET_PREFIXES = {
    "USE", "RHW", "RHH", "XHH", "THW", "THWN", "XHHW",  # wire insulation
    "FS", "FRN", "FRS", "LPN", "LPS",                       # fuse specs
    "CT", "VT", "PT",                                        # instrument ratios
    "UL", "NEC", "IEEE", "NFPA", "IEC", "ANSI",             # standards
    "AWG", "MCM", "PV", "MV", "LV", "HV", "AC", "DC",      # electrical abbrevs
    "HP", "KW", "KVA", "MVA",                                # unit prefixes
    "TR", "AL", "CU",                                        # material/cable type
}

_SHEET_TOKEN_RE = re.compile(
    r"[A-Z]{1,3}-\d{1,4}[A-Z]?"   # E-001, C-001, EE-01, SP-001A
)
# Text-search pattern: uses \b for dash-style, lookahead for dot-style
SHEET_RE = re.compile(
    r"\b[A-Z]{1,3}-\d{1,4}[A-Z]?\b"    # dash-separated with word boundaries
    r"|(?<![A-Za-z])[A-Z]{1,3}\.\d{1,2}(?:\.\d{1,2})?(?!\w)"  # dot-separated
)


def _is_valid_sheet_number(token: str) -> bool:
    """Return True if *token* looks like a real sheet number, not a product spec."""
    m = _SHEET_TOKEN_RE.fullmatch(token)
    if not m:
        return False
    prefix = token.split("-")[0]
    if prefix in _NOT_SHEET_PREFIXES:
        return False
    parts = token.split("-", 1)
    digits_part = parts[1].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") if len(parts) > 1 else ""
    # 3-letter prefix + single digit → almost always a product spec (USE-2, AWG-4)
    if len(prefix) == 3 and len(digits_part) <= 1:
        return False
    # Real sheet numbers have at least 2 digits after the hyphen (E-01, C-001, SP-001)
    # Single digit like A-2, S-1, E-1 are too ambiguous — could be references
    if len(digits_part) < 2:
        return False
    return True
COORD_RE = re.compile(r"\([-+]?\d{1,3}\.\d+\s*,\s*[-+]?\d{1,3}\.\d+\)")


@dataclass
class PageInfo:
    number: int
    text: str
    sheet_number: str | None
    sheet_title: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def first_sheet_number(page: fitz.Page, text: str) -> str | None:
    words = page.get_text("words")
    candidates = []
    for x0, y0, x1, y1, word, *_ in words:
        token = str(word).strip()
        if _is_valid_sheet_number(token):
            candidates.append((x0, y0, x1, y1, token))
    if candidates:
        footer_candidates = [c for c in candidates if c[1] >= page.rect.height * 0.65]
        target = footer_candidates or candidates
        target.sort(key=lambda c: (c[1], c[0]))
        return target[-1][4]

    matches = [m for m in SHEET_RE.findall(text) if _is_valid_sheet_number(m)]
    return matches[-1] if matches else None


_TITLE_SKIP = {
    "SHEET NAME", "SHEET SIZE", "SHEET NUMBER", "SHEET NO",
    "SHEET NO.", "SCALE", "DATE", "DRAWN", "CHECKED", "APPROVED",
}


def guess_sheet_title(page: fitz.Page, text: str, sheet_number: str | None) -> str | None:
    if not sheet_number:
        return None

    footer = default_footer_bbox(page)
    footer_text = page.get_textbox(footer) or text
    lines = [normalize_spaces(line) for line in footer_text.splitlines() if normalize_spaces(line)]

    # Strategy 1: look for the sheet number on its own line, title is adjacent
    for idx, line in enumerate(lines):
        if line == sheet_number and idx > 0:
            prev_line = normalize_spaces(lines[idx - 1])
            if prev_line and prev_line.upper() not in _TITLE_SKIP:
                return prev_line
        if line == sheet_number and idx + 1 < len(lines):
            nxt = normalize_spaces(lines[idx + 1])
            if nxt and nxt.upper() not in _TITLE_SKIP:
                return nxt

    # Strategy 2: sheet number is embedded in a longer line (e.g. "E-001 SITE PLAN")
    for line in lines:
        if sheet_number in line and line != sheet_number:
            remainder = line.replace(sheet_number, "").strip(" -–—:")
            if remainder and remainder.upper() not in _TITLE_SKIP:
                return remainder

    # Strategy 3: search full page text (not just footer)
    all_lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(all_lines):
        if line == sheet_number and idx + 1 < len(all_lines):
            nxt = normalize_spaces(all_lines[idx + 1])
            if nxt and nxt.upper() not in _TITLE_SKIP:
                return nxt

    # Strategy 4: look for common sheet title keywords near the footer
    # (the title block often has a dedicated SHEET NAME field)
    for idx, line in enumerate(lines):
        upper = line.upper()
        if upper in {"SHEET NAME", "SHEET NAME:"}:
            if idx + 1 < len(lines):
                nxt = normalize_spaces(lines[idx + 1])
                if nxt and nxt.upper() not in _TITLE_SKIP and not _SHEET_TOKEN_RE.fullmatch(nxt):
                    return nxt
    return None


def extract_pages(doc: fitz.Document) -> list[PageInfo]:
    pages: list[PageInfo] = []
    for i in range(doc.page_count):
        page = doc[i]
        text = page.get_text("text")
        number = first_sheet_number(page, text)
        title = guess_sheet_title(page, text, number)
        pages.append(PageInfo(number=i + 1, text=text, sheet_number=number, sheet_title=title))
    return pages


def parse_drawing_index(cover_text: str) -> list[tuple[str, str]]:
    if "DRAWING INDEX" not in cover_text.upper():
        return []
    lines = [normalize_spaces(line) for line in cover_text.splitlines() if normalize_spaces(line)]
    start = None
    for idx, line in enumerate(lines):
        if "DRAWING INDEX" in line.upper():
            start = idx + 1
            break
    if start is None:
        return []

    # Stop phrases that indicate the drawing index section has ended
    _STOP_PHRASES = {
        "DIGITALLY", "DATE:", "PROJECT INFORMATION", "SYSTEM INFORMATION",
        "OWNER", "EPC", "ENGINEER", "CONTRACTOR", "NOTES:", "GENERAL NOTES",
        "BUILDING CODE", "CODE COMPLIANCE", "VICINITY MAP", "COUNTY MAP",
        "ARRAY VIEW", "SITE COORDINATES", "LATITUDE", "LONGITUDE",
        "MODULE", "INVERTER", "RACKING", "TOTAL DC", "TOTAL AC",
        "DC/AC RATIO", "PROPERTY", "CLIENT", "DESCRIPTION",
    }

    cleaned: list[str] = []
    consecutive_non_sheet = 0
    for line in lines[start:]:
        upper = line.upper()
        if upper in {"SHEET NUMBER", "SHEET NAME", "SHEET NO.", "SHEET NO",
                      "NO.", "DESCRIPTION", "SHEET DESCRIPTION"}:
            continue
        # Stop if we hit a known non-index section
        if any(phrase in upper for phrase in _STOP_PHRASES):
            break
        # Stop if we've seen too many consecutive non-sheet lines
        has_sheet = _SHEET_TOKEN_RE.search(line) and _is_valid_sheet_number(
            _SHEET_TOKEN_RE.search(line).group(0)  # type: ignore[union-attr]
        )
        if not has_sheet:
            consecutive_non_sheet += 1
            if consecutive_non_sheet > 3:
                break
        else:
            consecutive_non_sheet = 0
        cleaned.append(line)

    results: list[tuple[str, str]] = []
    idx = 0
    while idx < len(cleaned):
        line = cleaned[idx]
        # Case 1: Sheet number on its own line, title on next line
        if _is_valid_sheet_number(line):
            title = cleaned[idx + 1] if idx + 1 < len(cleaned) and not _is_valid_sheet_number(cleaned[idx + 1]) else "(title not parsed)"
            results.append((line, title))
            idx += 2
            continue
        # Case 2: Sheet number embedded in line (e.g. "E-001 SITE PLAN")
        m = _SHEET_TOKEN_RE.search(line)
        if m and _is_valid_sheet_number(m.group(0)):
            sheet_num = m.group(0)
            remainder = line[:m.start()].strip() + " " + line[m.end():].strip()
            title = remainder.strip() or "(title not parsed)"
            results.append((sheet_num, title))
            idx += 1
            continue
        idx += 1
    return results


_DRAWING_INDEX_AI_PROMPT = """\
You are an expert engineer reading a solar PV planset cover sheet. \
Extract the DRAWING INDEX table from this page.

The drawing index is a table that lists ALL sheets in the planset with their \
sheet numbers (like E-001, C-001, SP-001, EE-01, EL-01, GD-01) and titles.

IMPORTANT:
- Only extract entries from the DRAWING INDEX table itself.
- Do NOT extract equipment specs, fuse models, wire types, NEC references, \
  or any other alphanumeric codes that are NOT sheet numbers.
- Real sheet numbers follow the pattern: 1-2 letter prefix + dash + 2-3 digits, \
  optionally followed by a single letter suffix (e.g. E-001, SP-001A, C-01).
- If you cannot find a drawing index table, return an empty array.

Return ONLY a JSON array of objects:
```json
[
  {"sheet_number": "E-001", "title": "COVER SHEET"},
  {"sheet_number": "E-002", "title": "GENERAL NOTES"}
]
```
Return ONLY the JSON array, no other text.
"""


def parse_drawing_index_ai(
    doc: fitz.Document,
    cover_page_numbers: list[int] | None = None,
) -> list[tuple[str, str]]:
    """Use AI vision to extract the drawing index from cover page(s).

    Returns a list of (sheet_number, title) tuples, same as parse_drawing_index().
    Returns an empty list if the AI provider is unavailable or fails.
    """
    import logging
    log = logging.getLogger(__name__)

    try:
        from .gemini_client import analyze_multiple_images
    except Exception:
        return []

    page_nums = cover_page_numbers or [1]
    images: list[bytes] = []
    for pn in page_nums:
        if pn < 1 or pn > doc.page_count:
            continue
        page = doc[pn - 1]
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        images.append(pix.tobytes("png"))

    if not images:
        return []

    try:
        raw = analyze_multiple_images(images, _DRAWING_INDEX_AI_PROMPT)
    except Exception:
        log.exception("AI drawing index extraction failed")
        return []

    # Parse JSON response
    results: list[tuple[str, str]] = []
    try:
        import re as _re
        m = _re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, _re.DOTALL)
        text = m.group(1) if m else raw
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end <= start:
            return []
        parsed = json.loads(text[start:end + 1])
        if not isinstance(parsed, list):
            return []
        for entry in parsed:
            snum = str(entry.get("sheet_number", "")).strip()
            title = str(entry.get("title", "")).strip()
            if snum and _is_valid_sheet_number(snum):
                results.append((snum, title or "(title not parsed)"))
    except (json.JSONDecodeError, Exception):
        log.exception("Failed to parse AI drawing index JSON")
        return []

    log.info("AI extracted %d drawing index entries", len(results))
    return results


def ensure_dirs(run_dir: Path) -> tuple[Path, Path]:
    snippets_dir = run_dir / "snippets"
    previews_dir = run_dir / "page_previews"
    snippets_dir.mkdir(parents=True, exist_ok=True)
    previews_dir.mkdir(parents=True, exist_ok=True)
    return snippets_dir, previews_dir


def expanded_rect(rect: fitz.Rect, page_rect: fitz.Rect, pad: float = 24) -> fitz.Rect:
    return fitz.Rect(
        max(page_rect.x0, rect.x0 - pad),
        max(page_rect.y0, rect.y0 - pad),
        min(page_rect.x1, rect.x1 + pad),
        min(page_rect.y1, rect.y1 + pad),
    )


def default_footer_bbox(page: fitz.Page) -> fitz.Rect:
    r = page.rect
    return fitz.Rect(r.width * 0.62, r.height * 0.78, r.width * 0.98, r.height * 0.98)


def rect_to_dict(rect: fitz.Rect | None) -> dict | None:
    if rect is None:
        return None
    return {"x0": rect.x0, "y0": rect.y0, "x1": rect.x1, "y1": rect.y1}


def parse_ai_bbox(value: Any, page: fitz.Page) -> fitz.Rect | None:
    """Convert an AI-supplied normalized bounding box to a PDF-coordinate
    fitz.Rect.

    Accepts either a list ``[y0, x0, y1, x1]`` (the order Gemini 2.0 returns)
    or a dict-shaped ``{"y0": ..., "x0": ..., "y1": ..., "x1": ...}``. Values
    are accepted as either 0-1000 integers (Gemini default) or 0-1.0 floats.
    Returns ``None`` for missing, malformed, zero-area, or out-of-bounds
    inputs — caller falls back to other anchoring.

    Used as the bbox source-of-last-resort: when ``location_text`` doesn't
    match the PDF text layer (paraphrase, scanned PDF, mismatched table-cell
    layout) AND the value is missing from the drawing entirely (so there is
    no string to search for), this gives a focused highlight pointing at
    where the AI thinks the relevant region is — including where a missing
    item *should* have been drawn.
    """
    if not value:
        return None
    if isinstance(value, dict):
        try:
            coords = [
                float(value["y0"]), float(value["x0"]),
                float(value["y1"]), float(value["x1"]),
            ]
        except (KeyError, ValueError, TypeError):
            return None
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            coords = [float(c) for c in value]
        except (TypeError, ValueError):
            return None
    else:
        return None

    # Normalize to 0-1.0 floats. Gemini returns 0-1000 ints by default; some
    # SDKs return 0-1.0 floats. Detect by max value: anything >1.0 is treated
    # as the 0-1000 range. Anything obviously out of range bails.
    if any(c < 0 for c in coords):
        return None
    max_c = max(coords)
    if max_c <= 1.0:
        scale = 1.0
    elif max_c <= 1000.0:
        scale = 1000.0
    elif max_c <= 100.0:
        # 0-100 percent scale (some prompts return this); rare but cheap to handle
        scale = 100.0
    else:
        return None
    norm = [c / scale for c in coords]

    page_w = page.rect.width
    page_h = page.rect.height
    y0, x0, y1, x1 = norm
    # Clamp to [0, 1] in case the model rounded slightly outside.
    x0c = max(0.0, min(1.0, x0))
    y0c = max(0.0, min(1.0, y0))
    x1c = max(0.0, min(1.0, x1))
    y1c = max(0.0, min(1.0, y1))
    rect = fitz.Rect(
        x0c * page_w, y0c * page_h,
        x1c * page_w, y1c * page_h,
    )
    rect.normalize()  # fix any (x0 > x1) / (y0 > y1)
    if rect.width < 1 or rect.height < 1:
        return None  # zero-area — useless
    return rect


_NUM_WITH_UNIT = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*"
    r"(?:kV|V|kA|A|MVA|kVA|kW|MWp|kWp|MW|W|ft|in|°|deg|%|Hz|VA)",
    re.IGNORECASE,
)


def _search_variants(needle: str) -> list[str]:
    """Generate fuzzy search variants of a needle so PDF text-layer quirks
    (comma/space differences, trailing units) don't cause silent misses.
    Order matters — we try the most specific variants first.
    """
    needle = (needle or "").strip().strip('"\'')
    if not needle:
        return []
    variants: list[str] = []

    def _push(s: str) -> None:
        s = s.strip()
        if s and len(s) >= 3 and s not in variants:
            variants.append(s)

    # Raw needle
    _push(needle)
    # No thousands-separator commas — PDF text layer often has "3078" not "3,078"
    _push(needle.replace(",", ""))
    # Collapse whitespace
    _push(re.sub(r"\s+", " ", needle))
    # Drop trailing punctuation
    _push(needle.rstrip(".,;:"))
    # Leading substrings (for long needles)
    if len(needle) > 24:
        _push(needle[:40])
        _push(needle[:24])
    # Stand-alone numeric+unit tokens inside the needle ("3078 kWp", "380 A")
    for m in _NUM_WITH_UNIT.finditer(needle):
        _push(m.group(0))
        # Also try the normalized number alone
        _push(re.sub(r"[,\s]+", "", m.group(0)))
    return variants


_TOKEN_SPLIT_RE = re.compile(r"[\s,;:()/\\\[\]{}\"']+")
# Tokens that are too generic to highlight on their own (would match every row).
_TOKEN_STOPLIST = {
    "the", "and", "or", "of", "to", "for", "with", "on", "at", "in",
    "row", "column", "table", "list", "see", "shown", "is", "are",
    "value", "field", "cell", "number", "label", "text",
}


def _split_into_tokens(needle: str) -> list[str]:
    """Break a needle like 'Row 12 (PT)' into ['Row 12', 'PT', '12'].

    Used as a last-ditch fallback when the full needle doesn't match the PDF
    text layer. Returns tokens long enough to be useful and rare enough not
    to flood the page with matches. Original phrase is NOT included — caller
    has already tried it.
    """
    raw_tokens = [t for t in _TOKEN_SPLIT_RE.split(needle) if t]
    tokens: list[str] = []
    seen: set[str] = set()
    # Pair of adjacent tokens first (e.g. "Row 12") — more specific than singles.
    for i in range(len(raw_tokens) - 1):
        pair = f"{raw_tokens[i]} {raw_tokens[i+1]}"
        if 3 <= len(pair) <= 30 and pair not in seen:
            seen.add(pair)
            tokens.append(pair)
    # Then single tokens, skipping stoplist + too-short.
    for t in raw_tokens:
        tl = t.strip().lower()
        if not tl or tl in _TOKEN_STOPLIST:
            continue
        if len(t) < 2 or len(t) > 30:
            continue
        if t in seen:
            continue
        seen.add(t)
        tokens.append(t)
    return tokens


def _search_page_multi(page: fitz.Page, needles: list[str]) -> list[fitz.Rect]:
    """Search a page for each needle, return all match rectangles (deduped).

    Uses several fuzzy variants per needle so tiny text-layer differences
    (commas, unit spacing) don't cause silent misses. When NO full-needle
    match is found anywhere, falls back to token-level matching: pairs of
    adjacent words first ("Row 12"), then individual tokens (skipping
    generic stoplist words like "the"/"row"/"value"). The token fallback
    keeps Fail/NR findings visually anchored to *some* spot on the page
    even when the AI returned a paraphrase rather than a verbatim excerpt.
    """
    rects: list[fitz.Rect] = []
    seen: set[tuple[float, float, float, float]] = set()

    def _add_matches(term: str) -> bool:
        """Append all matches for ``term`` (deduped). Return True if any added."""
        try:
            matches = page.search_for(term, quads=False)
        except Exception:
            matches = []
        added = False
        for r in matches:
            key = (round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1))
            if key in seen:
                continue
            seen.add(key)
            rects.append(r)
            added = True
        return added

    # Pass 1 — full needle + fuzzy variants. First match wins per needle.
    for needle in needles:
        for term in _search_variants(needle):
            if _add_matches(term):
                break

    # Pass 2 — token fallback, only if pass 1 found nothing across all needles.
    # Cap to ~4 tokens per needle to avoid flooding the page with generic
    # matches when the AI's hint is verbose. Stop as soon as ANY token
    # produces a hit so we don't accumulate scattered noise.
    if not rects:
        for needle in needles:
            for token in _split_into_tokens(needle)[:4]:
                if _add_matches(token):
                    break
            if rects:
                break

    return rects


def _union_rects(rects: list[fitz.Rect]) -> fitz.Rect | None:
    if not rects:
        return None
    x0 = min(r.x0 for r in rects)
    y0 = min(r.y0 for r in rects)
    x1 = max(r.x1 for r in rects)
    y1 = max(r.y1 for r in rects)
    return fitz.Rect(x0, y0, x1, y1)


def _cached_table_cells(page: fitz.Page) -> list[fitz.Rect]:
    """All non-trivial table cells on the page. Computed once per page
    (attached to the Page object) and reused across every issue rendered
    from that page — table detection is expensive on big plansets.
    """
    cached = getattr(page, "_qc_cell_cache", None)
    if cached is not None:
        return cached
    cells_out: list[fitz.Rect] = []
    try:
        finder = page.find_tables()
        tables = getattr(finder, "tables", None) or []
        for table in tables:
            cells = getattr(table, "cells", None) or []
            for cell in cells:
                if not cell:
                    continue
                try:
                    cell_rect = fitz.Rect(cell)
                except Exception:
                    continue
                # Only accept reasonable cell sizes — skip full-page or
                # full-width wrapper cells.
                if cell_rect.width < page.rect.width * 0.95 and cell_rect.height < page.rect.height * 0.5:
                    cells_out.append(cell_rect)
    except Exception:
        pass
    try:
        setattr(page, "_qc_cell_cache", cells_out)
    except Exception:
        pass
    return cells_out


def _cached_text_lines(page: fitz.Page) -> list[fitz.Rect]:
    """All line-level bounding rects on the page (cached)."""
    cached = getattr(page, "_qc_line_cache", None)
    if cached is not None:
        return cached
    lines: list[fitz.Rect] = []
    try:
        td = page.get_text("dict")
        for block in td.get("blocks", []):
            for line in block.get("lines", []):
                bbox = line.get("bbox")
                if not bbox:
                    continue
                try:
                    lines.append(fitz.Rect(bbox))
                except Exception:
                    continue
    except Exception:
        pass
    try:
        setattr(page, "_qc_line_cache", lines)
    except Exception:
        pass
    return lines


def _cached_page_image(page: fitz.Page, zoom: float = 2.0) -> Image.Image:
    """Render the page to a PIL image once per analysis and reuse it."""
    cache_key = f"_qc_image_{zoom}"
    cached = getattr(page, cache_key, None)
    if cached is not None:
        return cached
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    try:
        setattr(page, cache_key, image)
    except Exception:
        pass
    return image


def _cell_for_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect | None:
    for cell_rect in _cached_table_cells(page):
        if cell_rect.contains(rect) or cell_rect.intersects(rect):
            return cell_rect
    return None


def _line_for_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect | None:
    for line_rect in _cached_text_lines(page):
        if line_rect.intersects(rect):
            overlap = line_rect & rect
            if overlap.get_area() > 0.3 * rect.get_area():
                return line_rect
    return None


def _expand_hit(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    """Expand a narrow word-level match to its enclosing table cell, or
    failing that, its full text line. This makes the highlight point at
    the value *and* its surrounding context (row label, units, etc.)
    instead of a cramped 40-pixel box."""
    cell = _cell_for_rect(page, rect)
    if cell is not None:
        return cell
    line = _line_for_rect(page, rect)
    if line is not None:
        return line
    return rect


def _cluster_hits(rects: list[fitz.Rect], page: fitz.Page) -> list[fitz.Rect]:
    """When many hits exist across the page (common for repeated tokens
    like "500 kcmil AL"), keep only the densest cluster — the one whose
    members are physically close to each other. Prevents the classic
    "highlight every occurrence across the drawing" mistake.

    Heuristic: if hits span > 30% of the page diagonally, keep only the
    tightest grouping (one-step DBSCAN with eps = 15% of page diagonal).
    """
    if len(rects) <= 1:
        return rects
    diag = (page.rect.width ** 2 + page.rect.height ** 2) ** 0.5
    span_x = max(r.x1 for r in rects) - min(r.x0 for r in rects)
    span_y = max(r.y1 for r in rects) - min(r.y0 for r in rects)
    if (span_x ** 2 + span_y ** 2) ** 0.5 < 0.30 * diag:
        return rects  # already compact
    eps = 0.15 * diag

    def _center(r: fitz.Rect) -> tuple[float, float]:
        return ((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)

    # Build clusters by greedy expansion
    unassigned = list(range(len(rects)))
    clusters: list[list[int]] = []
    while unassigned:
        seed = unassigned.pop(0)
        cluster = [seed]
        changed = True
        while changed:
            changed = False
            for i in list(unassigned):
                cx, cy = _center(rects[i])
                if any(
                    abs(cx - _center(rects[j])[0]) < eps and
                    abs(cy - _center(rects[j])[1]) < eps
                    for j in cluster
                ):
                    cluster.append(i)
                    unassigned.remove(i)
                    changed = True
        clusters.append(cluster)
    # Return the densest (largest) cluster
    best = max(clusters, key=len)
    return [rects[i] for i in best]


def render_issue_artifacts(
    doc: fitz.Document,
    issue_id: str,
    page_number: int | None,
    run_dir: Path,
    target_text: str | None = None,
    fallback_bbox: fitz.Rect | None = None,
    target_texts: list[str] | None = None,
) -> tuple[str | None, str | None, dict | None]:
    """Render a highlighted page preview + cropped snippet for an issue.

    If any of the target texts is found on the page, every match is drawn
    individually on the preview and the snippet is cropped to the union of
    matches. If no target texts are found, we do NOT stamp the page with a
    misleading fallback rectangle — we return the plain page image and a
    null bbox so the UI shows the page without a fake highlight. This
    prevents unrelated findings on the same page from sharing identical
    red boxes.

    An explicit ``fallback_bbox`` is still honored (used by the drawing-index
    and sheet-existence checks that genuinely target the footer).
    """
    if not page_number:
        return None, None, None
    snippets_dir, previews_dir = ensure_dirs(run_dir)
    page = doc[page_number - 1]

    needles: list[str] = []
    if target_texts:
        needles.extend([t for t in target_texts if t])
    if target_text and target_text not in needles:
        needles.append(target_text)

    raw_hits = _search_page_multi(page, needles) if needles else []

    # Drop far-apart stray matches (same token occurring elsewhere on the
    # drawing). Keeps only the densest cluster when hits are scattered.
    clustered = _cluster_hits(raw_hits, page) if raw_hits else []

    # Expand each hit to its table cell or text line so we highlight the
    # whole row / cell instead of a cramped word-level box.
    hit_rects: list[fitz.Rect] = []
    seen_expanded: set[tuple[float, float, float, float]] = set()
    for r in clustered:
        expanded = _expand_hit(page, r)
        key = (
            round(expanded.x0, 1), round(expanded.y0, 1),
            round(expanded.x1, 1), round(expanded.y1, 1),
        )
        if key in seen_expanded:
            continue
        seen_expanded.add(key)
        hit_rects.append(expanded)

    union = _union_rects(hit_rects) if hit_rects else None

    # Choose a bbox. Priority: matched needles > explicit fallback > no bbox.
    # We deliberately do NOT default to ``default_footer_bbox`` anymore —
    # that caused every unlocated finding on a page to share the same red box.
    bbox = union or fallback_bbox

    if bbox is not None:
        crop_bbox = expanded_rect(bbox, page.rect, pad=24)
    else:
        crop_bbox = page.rect  # whole page for snippet fallback
    crop_bbox = fitz.Rect(
        min(crop_bbox.x0, crop_bbox.x1),
        min(crop_bbox.y0, crop_bbox.y1),
        max(crop_bbox.x0, crop_bbox.x1),
        max(crop_bbox.y0, crop_bbox.y1),
    )

    image = _cached_page_image(page, zoom=2.0)

    preview = image.copy()
    draw = ImageDraw.Draw(preview)
    scale = 2

    def _draw_rect(r: fitz.Rect, outline: str, width: int, pad: float = 4.0) -> None:
        padded = fitz.Rect(
            max(page.rect.x0, r.x0 - pad),
            max(page.rect.y0, r.y0 - pad),
            min(page.rect.x1, r.x1 + pad),
            min(page.rect.y1, r.y1 + pad),
        )
        coords = [padded.x0 * scale, padded.y0 * scale, padded.x1 * scale, padded.y1 * scale]
        coords = [
            min(coords[0], coords[2]), min(coords[1], coords[3]),
            max(coords[0], coords[2]), max(coords[1], coords[3]),
        ]
        draw.rectangle(coords, outline=outline, width=width)

    if hit_rects:
        # Highlight each individual match in red; draw a lighter union box so
        # the viewer can see both the specific hits and the overall region.
        for r in hit_rects:
            _draw_rect(r, outline="#e12a3f", width=4, pad=2)
        if len(hit_rects) > 1 and union is not None:
            _draw_rect(union, outline="#ffb020", width=3, pad=10)
    elif fallback_bbox is not None:
        # Only the caller's explicit fallback gets a box (drawing-index /
        # sheet-existence checks that truly target the footer).
        _draw_rect(fallback_bbox, outline="#e12a3f", width=6, pad=0)
    # else: no highlight drawn — the preview is the plain page image.

    preview_path = previews_dir / f"{issue_id}.png"
    preview.save(preview_path)

    scaled = [
        crop_bbox.x0 * scale, crop_bbox.y0 * scale,
        crop_bbox.x1 * scale, crop_bbox.y1 * scale,
    ]
    scaled = [
        min(scaled[0], scaled[2]), min(scaled[1], scaled[3]),
        max(scaled[0], scaled[2]), max(scaled[1], scaled[3]),
    ]
    snippet = image.crop(tuple(int(v) for v in scaled))
    snippet_path = snippets_dir / f"{issue_id}.png"
    snippet.save(snippet_path)

    final_bbox = union if union is not None else (fallback_bbox if fallback_bbox is not None else None)
    return str(snippet_path), str(preview_path), rect_to_dict(final_bbox) if final_bbox else None


def make_issue(
    run_id: str,
    item_key: str,
    category: str,
    title: str,
    description: str,
    status: str,
    severity: str = "medium",
    page_number: int | None = None,
    evidence: str | None = None,
    confidence: float = 0.75,
    snippet_path: str | None = None,
    page_preview_path: str | None = None,
    bbox: dict | None = None,
) -> dict:
    now = utc_now()
    return {
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "category": category,
        "item_key": item_key,
        "title": title,
        "description": description,
        "severity": severity,
        "status": status,
        "auto_status": status,
        "page_number": page_number,
        "bbox": bbox,
        "snippet_path": snippet_path,
        "page_preview_path": page_preview_path,
        "evidence": evidence,
        "confidence": confidence,
        "override_comment": None,
        "created_at": now,
        "updated_at": now,
    }


def has_keywords(text: str, keywords: Iterable[str]) -> tuple[bool, list[str]]:
    upper = text.upper()
    found = [word for word in keywords if word.upper() in upper]
    return len(found) == len(list(keywords)), found


def _first_keyword_str(keywords: list) -> str | None:
    """Extract a plain string from a keywords list (which may be nested)."""
    if not keywords:
        return None
    first = keywords[0]
    while isinstance(first, list):
        if not first:
            return None
        first = first[0]
    return str(first) if first else None


_EXTRACT_SPECS_PROMPT_TEMPLATE = """\
You are an expert solar PV engineer. Extract electrical specifications from this
planset cover sheet. The page typically has a SYSTEM INFORMATION TABLE that is
the authoritative source of truth for the project values.

{stage_clause}
=== EXTRACTION RULES (read carefully) ===

1. SISTER-PROJECT DISAMBIGUATION
   Many Castillo plansets share a cover with multiple sibling projects (e.g.
   "Raven", "Waxwing", "Gonzo" listed side by side, each with its own column
   in the system info table). The project name is in the title block. Extract
   ONLY the values from the column or row matching THIS project's name. If
   you cannot tell which row belongs to this project, OMIT the field rather
   than guess.

2. MULTI-REVISION COVERS
   The cover may show a revision history with multiple stage blocks (30%,
   60%, 90%, IFC). When there are multiple system-info-table snapshots in
   the page, use the LATEST revision (highest %) — that's the as-designed
   value. Older blocks are stale.

3. INVERTER MODEL DESIGNATION ≠ MPPT RANGE
   "XGI 1500 250/250-600" or "XGI 1500 225/250-600" is a MODEL NAME meaning
   "XGI 1500-platform, 250 kW (or 225 kW) nameplate, 250 or 600 V AC output."
   The "250-600" here is AC output voltage options, NOT inverter_mppt_range.
   * MPPT range is the DC INPUT tracking window — typically 800-1500 V on a
     1500V-architecture inverter, 200-1000 V on a 1000V system. Only emit
     inverter_mppt_range if you see a label that explicitly says "MPPT range",
     "Operating DC voltage", or similar. If unsure, OMIT — better to defer
     than to report a wrong value.
   * inverter_max_vdc is the absolute MAX DC input (the upper Voc limit) —
     typically 1500 V for XGI 1500. Often stated near "Voc max" / "Max DC".

4. MIXED-MODEL INVERTER QUANTITY
   When inverter_quantity is shown as "X/Y" (e.g. "19/1") it means X of one
   model plus Y of another (e.g. 19 × 250 kW + 1 × 225 kW). In that case:
     - inverter_quantity = X + Y (the SUM, e.g. 20)
     - inverter_kva = the WEIGHTED kVA if obvious; otherwise OMIT and let
       the calc rules use total_ac_kva as the source of truth.

5. ADJACENT-LABEL TRAPS
   String size (modules per string) is typically labeled "MODULES/STRING" or
   "STRING SIZE" — a small integer 18-30. Do NOT read it from row-spacing
   or pitch dimensions like "23'-6\"" (those are FEET-INCHES tilts/pitches).
   Module quantity is labeled "MODULE QUANTITY" / "MODULE QTY" — typically
   a 4-5 digit integer. Don't pick it from inverter quantity, string count,
   or sister-project totals.

6. STC WATTAGE
   module_stc_watts is the W rating shown on the cover (e.g. "615W") next
   to the module model number. If the page has a side-by-side table for
   60% rev (610W) and 90% rev (615W), use the 90%/latest. Datasheet's
   nameplate may differ — TRUST THE COVER for as-designed wattage.

7. TRANSFORMER %Z AND X/R RATIO (REQUIRED EVEN AT 30% — common omission)
   The step-up transformer's percent impedance (%Z) AND X/R ratio are
   essential design values that should be on the cover system-info table
   (or at least on the E-100 SLD) — Joe Jancauskas explicitly flags these
   as a "really bad trend" of recent omissions.
   * transformer_impedance is the %Z value, typically 5.5–5.75 % for
     750–2500 kVA distribution transformers. Look for labels like "Z%",
     "Impedance", "%Z", "Imp.", or "Zps" near the transformer specs.
     Common values: 5.0, 5.5, 5.75, 6.0, 6.5 (percent).
   * transformer_xr_ratio is the X-to-R ratio, typically 4–10 for
     distribution-class transformers. Look for "X/R", "XR ratio". This
     value rarely appears on the nameplate — the cover sheet/SLD is
     usually the only place it shows up. Omit if not visible; the
     downstream calc rule will flag the gap.
   * transformer_bil is the Basic Insulation Level in kV — for 15 kV
     class typically 95 or 110 kV BIL; for 25 kV class 125 or 150 kV;
     for 35 kV class 150 or 200 kV.
   If any of these three are absent from the cover, the calc rule will
   surface the omission separately — your job is to extract them faithfully
   when they ARE present, not to flag missing ones at the extraction step.

Return ONLY a JSON object with the values you can find. OMIT fields you
cannot find with high confidence. Use EXACTLY these field names:

```json
{
  "module_voc": "open circuit voltage in V",
  "module_vmp": "max power voltage in V",
  "module_isc": "short circuit current in A",
  "module_imp": "max power current in A",
  "module_stc_watts": "STC rating in W",
  "module_temp_coeff_voc": "temp coefficient of Voc in %/C (negative number)",
  "module_temp_coeff_isc": "temp coefficient of Isc in %/C",
  "string_size": "modules per string",
  "string_quantity": "total number of strings",
  "total_dc_kw": "total DC in kW",
  "inverter_make": "manufacturer",
  "inverter_model": "model number",
  "inverter_kva": "kVA rating",
  "inverter_kw": "kW rating",
  "inverter_max_vdc": "max DC voltage in V",
  "inverter_mppt_range": "MPPT range like 860-1300",
  "inverter_quantity": "number of inverters",
  "total_ac_kva": "total AC in kVA",
  "dc_ac_ratio": "DC/AC ratio",
  "transformer_kva": "transformer kVA per unit (one transformer's nameplate)",
  "transformer_quantity": "number of step-up transformers in this project (e.g. '(2) 2500 kVA' → 2). Defaults to 1 if not stated.",
  "transformer_total_kva": "total transformer capacity if stated explicitly (e.g. 'Total: 5000 kVA'). Otherwise omit and the calc will multiply per-unit × count.",
  "transformer_primary_voltage": "primary voltage in V",
  "transformer_secondary_voltage": "secondary voltage in V",
  "transformer_impedance": "Z percent (typical 5.5-5.75 for 750-2500 kVA)",
  "transformer_xr_ratio": "X/R ratio (typical 4-10 for distribution class)",
  "transformer_bil": "BIL in kV (15 kV class: 95 or 110; 25 kV: 125 or 150; 35 kV: 150 or 200)",
  "poi_voltage": "POI voltage in V",
  "design_temp_low_c": "lowest design temp in C",
  "design_temp_high_c": "highest design temp in C"
}
```
Return only numeric values as strings. Return ONLY the JSON object, no other text.
"""


_PLANSET_DATASHEET_PROMPT = """\
You are reading a vendor equipment datasheet that has been included as a
page within a solar PV planset PDF. The page is a rasterized cut-sheet
from the manufacturer.

STEP 1 — Identify what equipment this page describes. Pick ONE:
  module        — PV solar module / panel
  inverter      — string or central inverter
  transformer   — pad-mount / step-up MV transformer
  combiner      — DC combiner box
  recloser      — utility recloser / fault interrupter
  arrester      — surge arrester
  other         — anything else (warning labels, monitoring, etc. — extract nothing)

If you cannot identify the equipment, return equipment_type="other" and
no spec fields.

STEP 2 — Extract ONLY fields appropriate to that equipment type, using
the project_details schema keys verbatim so they auto-merge. Omit any
field whose value isn't legible or would require guessing. Numeric
fields should be the number only (no unit suffix).

For module:
  module_make, module_model, module_stc_watts, module_voc, module_vmp,
  module_isc, module_imp, module_temp_coeff_voc, module_temp_coeff_isc,
  module_temp_coeff_pmax, is_bifacial

For inverter:
  inverter_make, inverter_model, inverter_kva, inverter_kw,
  inverter_max_vdc, inverter_mppt_range (as 'min-max V'),
  inverter_max_dc_input_isc, inverter_ac_voltage,
  inverter_max_short_circuit_amps, inverter_efficiency_pct

For transformer:
  transformer_kva, transformer_quantity (count of identical step-ups in
  the project — defaults to 1), transformer_total_kva (only if explicitly
  stated), transformer_primary_voltage, transformer_secondary_voltage,
  transformer_winding_config, transformer_impedance, transformer_bil,
  transformer_cooling_class

Return ONLY a JSON object. Always include "equipment_type" and a one-sentence
"summary". Do not include the schema description or comments.

```json
{
  "equipment_type": "module|inverter|transformer|combiner|recloser|arrester|other",
  "summary": "one sentence",
  "module_voc": "37.5",
  "module_vmp": "31.2",
  "...": "..."
}
```
"""


def _parse_extract_json(raw: str) -> dict:
    """Pull the JSON object out of a (possibly fenced) AI response."""
    try:
        import re as _re
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, _re.DOTALL)
        text = m.group(1) if m else raw
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return {}
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, Exception):
        return {}


# Datasheet-page detection: a page that looks like a vendor cut-sheet
# rather than a designed schematic / plan. Three signals: title says so,
# sheet number is in the typical datasheet range (E-7xx through E-9xx),
# OR the page text is mostly the title-block boilerplate (a strong
# indicator that the actual content is embedded as a raster/vector
# image, which is how vendor PDFs commonly land in plansets).
_DATASHEET_TITLE_KW = (
    "DATASHEET", "DATA SHEET", "CUT SHEET", "SUBMITTAL",
    "SPEC SHEET", "SPECIFICATION SHEET", "NAMEPLATE",
)
# Negative — pages in the datasheet sheet-range that AREN'T datasheets.
# Avoids spending tokens on warning-label sheets, legends, etc.
_DATASHEET_TITLE_NEGATIVE = (
    "WARNING LABEL", "WARNING SIGN", "PLACARD",
    "LEGEND", "ABBREVIATION", "SYMBOL", "INDEX",
    "BILL OF MATERIAL", "BOM",
    "NOTES", "GENERAL NOTE",
    "REVISION", "REV",
    "TRENCH", "GROUND", "CONDUIT", "CABLE TRAY",
)
_DATASHEET_SHEET_RANGE_RE = re.compile(r"^E-?(7|8|9)\d{2}", re.I)


# Stage markers found in title-block revision boxes. The string forms map
# directly to ``design_stage`` values the API accepts.
_STAGE_MARKERS: list[tuple[re.Pattern, str]] = [
    # Order matters: more-specific patterns first.
    (re.compile(r"\bAS[\s-]?BUILT\b|\bRECORD\s+DRAWING", re.I), "AsBuilt"),
    (re.compile(r"\bIFC\b|\bISSUED\s+FOR\s+CONSTRUCTION\b|\bFOR\s+CONSTRUCTION\b", re.I), "IFC"),
    (re.compile(r"\b(?:IFP|ISSUED\s+FOR\s+PERMIT)\b", re.I), "60"),
    (re.compile(r"\b90\s?%", re.I), "90"),
    (re.compile(r"\b60\s?%", re.I), "60"),
    (re.compile(r"\b30\s?%", re.I), "30"),
]


def detect_design_stage(doc: fitz.Document, max_pages: int = 3) -> str | None:
    """Best-effort scan of the first few pages' title-block text for a
    revision/stage marker. Returns "30" / "60" / "90" / "IFC" / "AsBuilt"
    or None if no marker found.

    Strategy:
      Plansets often list a deliverable SCHEDULE on the cover ("30%, 60%,
      90%, IFC, As-Built") that mentions every stage. To avoid confusing
      that schedule with the CURRENT stage, we use a priority-based
      decision:

        1. EXPLICIT issue-type markers (As-Built / IFC / IFP) win first.
           Designers stamp these specifically when issuing — they are not
           part of a generic deliverable list.
        2. If no explicit marker, fall back to the highest %% pattern seen.

      This means a "Wellington IFP" planset whose cover happens to list
      "30%/60%/90%" as future deliverables correctly resolves to 60 (IFP),
      not 90 (most-recent %% marker).
    """
    found: set[str] = set()
    for i in range(min(max_pages, doc.page_count)):
        text = doc[i].get_text("text") or ""
        for rx, stage in _STAGE_MARKERS:
            if rx.search(text):
                found.add(stage)
    if not found:
        return None

    # Priority 1 — explicit issue-type markers. AsBuilt > IFC > IFP-as-60.
    # We use the "60" marker only when it came from "IFP" / "ISSUED FOR
    # PERMIT", not from a "60%" %% reference; the regex emits "60" for
    # IFP, but a bare "60%" also emits "60". To disambiguate explicit-IFP
    # from %%-only, re-scan with the IFP-specific pattern.
    if "AsBuilt" in found:
        return "AsBuilt"
    if "IFC" in found:
        return "IFC"
    # Re-test for explicit IFP/Issued-For-Permit (separate from "60%" %%).
    ifp_re = re.compile(r"\b(?:IFP|ISSUED\s+FOR\s+PERMIT)\b", re.I)
    for i in range(min(max_pages, doc.page_count)):
        if ifp_re.search(doc[i].get_text("text") or ""):
            return "60"

    # Priority 2 — highest %% marker seen.
    for stage in ("90", "60", "30"):
        if stage in found:
            return stage
    return None


def _looks_like_datasheet_page(p: PageInfo) -> bool:
    """Detect pages that are likely vendor cut-sheets embedded in the planset.

    Vendor PDFs are typically rasterized when imported, so the page's text
    extraction shows mostly the title-block boilerplate — equipment specs
    aren't in the text layer. We can't rely on text keywords; instead we
    use sheet-number range + a negative-title filter to weed out obvious
    non-datasheets like warning-label pages.
    """
    title = (p.sheet_title or "").upper()
    # Explicit datasheet keyword wins immediately.
    if any(kw in title for kw in _DATASHEET_TITLE_KW):
        return True
    # In the typical datasheet sheet-number range — unless the title
    # explicitly says it's something else.
    if p.sheet_number and _DATASHEET_SHEET_RANGE_RE.match(p.sheet_number):
        if any(neg in title for neg in _DATASHEET_TITLE_NEGATIVE):
            return False
        return True
    return False


def extract_specs_from_pages(
    doc: fitz.Document,
    pages: list[PageInfo],
    design_stage: str | None = None,
    project_name: str | None = None,
) -> dict:
    """Auto-extract electrical specs from the planset itself.

    Two passes:
      1. SYSTEM-INFO PASS — page 1 (cover) for the system-info table that
         carries totals (string size, total DC, MPPT range stated, etc.).
      2. DATASHEET PASS — every page whose title/sheet-code marks it as a
         vendor datasheet. Sub-classified into module / inverter /
         transformer and processed with the matching extractor prompt
         from ``supporting_docs``.

    Returns the merged dict keyed by ``project_details`` field names.
    User-supplied project_details still wins downstream — this only fills
    gaps so calc rules stop deferring on missing inputs.
    """
    import logging
    log = logging.getLogger(__name__)

    try:
        from .gemini_client import analyze_multiple_images, analyze_page_image
    except Exception:
        log.exception("extract_specs_from_pages: imports failed")
        return {}

    merged: dict[str, str] = {}

    # ── Pass 1: cover-sheet system info ──────────────────────────────────
    # Render at 2x — system-info tables on solar cover sheets are dense and
    # 1.5x rendering loses the fine numeric digits that disambiguate
    # "615W" vs "610W" or "23" vs "24" modules-per-string.
    cover_images: list[bytes] = []
    cover_pages = [1]
    for pn in cover_pages:
        if 1 <= pn <= len(pages):
            page = doc[pn - 1]
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            cover_images.append(pix.tobytes("png"))
    if cover_images:
        try:
            # Bake stage + project hints into the prompt so the model knows
            # which revision block / sister-project column to extract from.
            stage_clause = ""
            if design_stage:
                stage_clause = (
                    f"=== STAGE CONTEXT ===\n"
                    f"This planset is at the {design_stage}% / {design_stage} stage. "
                    f"If multiple revision blocks (30%/60%/90%/IFC) appear on the cover, "
                    f"use the {design_stage} block.\n\n"
                )
            if project_name:
                stage_clause += (
                    f"=== PROJECT CONTEXT ===\n"
                    f"Project name: '{project_name}'. If the cover lists multiple sibling "
                    f"projects in a side-by-side system info table, extract values from the "
                    f"row/column matching '{project_name}' only.\n\n"
                )
            cover_prompt = _EXTRACT_SPECS_PROMPT_TEMPLATE.replace(
                "{stage_clause}", stage_clause,
            )
            raw = analyze_multiple_images(cover_images, cover_prompt)
            sysinfo = _parse_extract_json(raw)
            for k, v in sysinfo.items():
                if v is None:
                    continue
                sv = str(v).strip()
                if sv:
                    merged[k] = sv
            log.info("cover-sheet specs: %d fields (%s)",
                     len(merged), list(merged.keys()))
        except Exception:
            log.exception("cover-sheet spec extraction failed")

    # ── Pass 2: datasheet pages — unified vision extractor ───────────────
    # Vendor datasheets are typically rasterized when imported into a
    # planset, so the text layer is just title-block boilerplate. We send
    # the page IMAGE to a single prompt that figures out what equipment
    # it's looking at and emits project_details-schema fields accordingly.
    candidate_pages = [p for p in pages if _looks_like_datasheet_page(p)]
    # Cap at 8 candidate pages to keep cost bounded on huge plansets;
    # plansets usually have ≤4 datasheet pages anyway.
    #
    # PRECEDENCE NOTE: cover-sheet system info wins over a datasheet's
    # nameplate value when they disagree. The cover reflects what was
    # actually designed; vendor datasheets are generic and may describe
    # a different module/inverter variant than what's procured. We use
    # ``setdefault`` (not direct assignment) so cover-extracted values
    # are preserved.
    #
    # MULTI-EQUIPMENT COUNT: count distinct datasheet pages by equipment
    # type. When the planset has e.g. two transformer pages (E-901 and
    # E-902), that strongly suggests transformer_quantity >= 2 even when
    # the cover sheet didn't list it explicitly.
    equipment_page_counts: dict[str, int] = {}
    for p in candidate_pages[:8]:
        try:
            page = doc[p.number - 1]
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            img = pix.tobytes("png")
            raw = analyze_page_image(img, _PLANSET_DATASHEET_PROMPT, "image/png")
            specs = _parse_extract_json(raw)
            n_kept = 0
            for k, v in specs.items():
                if k in ("doc_type", "equipment_type", "summary", "source_pages"):
                    continue
                if v is None:
                    continue
                sv = str(v).strip()
                if not sv or sv.lower() in ("not shown", "n/a", "tbd", "null"):
                    continue
                # Cover wins on overlap. Datasheet only fills gaps.
                if merged.setdefault(k, sv) is sv:
                    n_kept += 1
            equip = specs.get("equipment_type") or "unknown"
            if equip in ("module", "inverter", "transformer"):
                equipment_page_counts[equip] = equipment_page_counts.get(equip, 0) + 1
            log.info("datasheet page %d (sheet %s, equipment=%s): %d new fields extracted",
                     p.number, p.sheet_number or "?", equip, n_kept)
        except Exception:
            log.exception("datasheet extraction failed for page %d", p.number)

    # Backfill {equipment}_quantity from page counts when cover didn't say.
    # Two transformer datasheet pages = transformer_quantity ≥ 2 etc. We
    # only set when the cover-extracted value is missing OR equals 1
    # (the implicit default the model emits when uncertain).
    for equip, n in equipment_page_counts.items():
        if n < 2:
            continue  # 1 page is the default — no override needed
        key = f"{equip}_quantity"
        existing = merged.get(key)
        try:
            existing_int = int(float(existing)) if existing else None
        except (ValueError, TypeError):
            existing_int = None
        if existing_int is None or existing_int < n:
            merged[key] = str(n)
            log.info("Inferred %s=%d from %d %s datasheet pages "
                     "(was %r)", key, n, n, equip, existing)

    log.info("extract_specs_from_pages: %d total fields → %s",
             len(merged), sorted(merged.keys()))
    return merged


def analyze_pdf(
    pdf_path: Path,
    project_name: str | None,
    original_filename: str,
    progress_cb: object = None,
    project_details: dict | None = None,
    use_deep: bool = True,
    supporting_docs: list[dict] | None = None,
    design_stage: str | None = None,
) -> tuple[dict, list[dict]]:
    def _progress(step: str, detail: str, pct: int) -> None:
        if progress_cb and callable(progress_cb):
            progress_cb(step, detail, pct)

    _analysis_start = time.monotonic()
    run_id = str(uuid.uuid4())
    run_dir = pdf_path.parent
    doc = fitz.open(pdf_path)

    _progress("parse", "Extracting pages and text...", 15)
    pages = extract_pages(doc)
    # Drawing index may span multiple cover pages — combine first few pages
    cover_pages_text = "\n".join(p.text for p in pages[:3]) if pages else ""
    cover_text = pages[0].text if pages else ""

    # Auto-detect design stage from title block when caller didn't specify.
    # Reading "60% / 90% / IFC" from the revision block beats forcing the
    # user to set it manually and prevents the "ran with wrong stage"
    # failure mode we hit during ground-truth measurement.
    if not design_stage:
        detected = detect_design_stage(doc)
        if detected:
            import logging
            logging.getLogger(__name__).info(
                "Auto-detected design_stage=%s from title block", detected,
            )
            design_stage = detected

    _progress("index", "Parsing drawing index...", 20)
    indexed_sheets = parse_drawing_index(cover_pages_text)
    actual_numbers = [page.sheet_number for page in pages if page.sheet_number]

    # If regex parser got poor results, fall back to AI vision
    regex_coverage = len(indexed_sheets) / max(len(actual_numbers), 1)
    if regex_coverage < 0.5 or len(indexed_sheets) == 0:
        _progress("index", "Using AI to read drawing index...", 22)
        ai_sheets = parse_drawing_index_ai(doc, cover_page_numbers=[1, 2])
        if len(ai_sheets) > len(indexed_sheets):
            import logging
            logging.getLogger(__name__).info(
                "AI drawing index (%d sheets) replacing regex result (%d sheets)",
                len(ai_sheets), len(indexed_sheets),
            )
            indexed_sheets = ai_sheets

    indexed_numbers = [num for num, _title in indexed_sheets]

    _progress("regex", "Running regex checks...", 25)
    issues: list[dict] = []

    # Drawing index page exists
    if indexed_sheets:
        issues.append(
            make_issue(
                run_id,
                "drawing_index_page_exists",
                "Drawing Index",
                "Drawing index exists",
                "Cover sheet includes a drawing index.",
                "Pass",
                page_number=1,
                evidence=f"Parsed {len(indexed_sheets)} indexed sheets from the cover sheet.",
                confidence=0.98,
            )
        )
    else:
        snippet_path, preview_path, bbox = render_issue_artifacts(doc, str(uuid.uuid4()), 1, run_dir, target_text="DRAWING INDEX")
        issues.append(
            make_issue(
                run_id,
                "drawing_index_page_exists",
                "Drawing Index",
                "Drawing index exists",
                "Cover sheet should include a drawing index.",
                "Fail",
                severity="high",
                page_number=1,
                evidence="Unable to parse a drawing index from the cover sheet.",
                confidence=0.85,
                snippet_path=snippet_path,
                page_preview_path=preview_path,
                bbox=bbox,
            )
        )

    missing_from_pdf = [num for num in indexed_numbers if num not in actual_numbers]
    if missing_from_pdf:
        page_no = 1
        target = missing_from_pdf[0]
        snippet_path, preview_path, bbox = render_issue_artifacts(doc, str(uuid.uuid4()), page_no, run_dir, target_text=target)
        issues.append(
            make_issue(
                run_id,
                "drawing_index_sheet_match",
                "Drawing Index",
                "All indexed sheets are present",
                "Every sheet listed in the drawing index should be present in the PDF.",
                "Fail",
                severity="high",
                page_number=page_no,
                evidence=f"Missing from PDF: {', '.join(missing_from_pdf)}",
                confidence=0.97,
                snippet_path=snippet_path,
                page_preview_path=preview_path,
                bbox=bbox,
            )
        )
    else:
        issues.append(
            make_issue(
                run_id,
                "drawing_index_sheet_match",
                "Drawing Index",
                "All indexed sheets are present",
                "Every sheet listed in the drawing index should be present in the PDF.",
                "Pass",
                page_number=1,
                evidence=f"Indexed sheet count matches actual extracted sheets ({len(indexed_numbers)}).",
                confidence=0.98,
            )
        )

    extra_sheets = list(dict.fromkeys(
        num for num in actual_numbers if indexed_numbers and num not in indexed_numbers
    ))

    # If the parser only captured a small fraction of actual sheets, the drawing
    # index was probably a table format the regex couldn't handle.  Don't trust
    # the "extra" list in that case — it's a parsing limitation, not real extras.
    parser_coverage = len(indexed_numbers) / max(len(actual_numbers), 1)
    # Also check if the "extra" sheet numbers appear somewhere in the first
    # few pages' text (drawing index table text the parser couldn't structure).
    truly_extra = [s for s in extra_sheets if s not in cover_pages_text]

    if extra_sheets and parser_coverage < 0.6:
        # Parser got less than 60% of sheets — don't trust the extras list
        issues.append(
            make_issue(
                run_id,
                "drawing_index_no_extras",
                "Drawing Index",
                "No extra sheets outside the drawing index",
                "PDF should not include extra sheets that are not listed in the drawing index.",
                "Needs Review",
                severity="low",
                page_number=1,
                evidence=(
                    f"Drawing index parser only extracted {len(indexed_numbers)} of "
                    f"{len(actual_numbers)} sheets ({parser_coverage:.0%} coverage). "
                    f"{len(extra_sheets)} sheets appear unindexed but this is likely "
                    f"a parsing limitation. AI vision check should verify."
                ),
                confidence=0.45,
            )
        )
    elif truly_extra:
        page_match = next((p for p in pages if p.sheet_number == truly_extra[0]), None)
        snippet_path, preview_path, bbox = render_issue_artifacts(
            doc,
            str(uuid.uuid4()),
            page_match.number if page_match else None,
            run_dir,
            target_text=truly_extra[0],
        )
        issues.append(
            make_issue(
                run_id,
                "drawing_index_no_extras",
                "Drawing Index",
                "No extra sheets outside the drawing index",
                "PDF should not include extra sheets that are not listed in the drawing index.",
                "Fail",
                severity="medium",
                page_number=page_match.number if page_match else None,
                evidence=f"Extra sheets found: {', '.join(truly_extra)}",
                confidence=0.90,
                snippet_path=snippet_path,
                page_preview_path=preview_path,
                bbox=bbox,
            )
        )
    elif extra_sheets:
        # Sheets appear in cover text but weren't parsed from the drawing index
        # table — likely a parsing limitation, not a real issue
        issues.append(
            make_issue(
                run_id,
                "drawing_index_no_extras",
                "Drawing Index",
                "No extra sheets outside the drawing index",
                "PDF should not include extra sheets that are not listed in the drawing index.",
                "Pass",
                severity="low",
                page_number=1,
                evidence=(
                    f"Sheets {', '.join(extra_sheets)} were not parsed from the "
                    f"drawing index table but do appear in the cover sheet text."
                ),
                confidence=0.85,
            )
        )
    else:
        issues.append(
            make_issue(
                run_id,
                "drawing_index_no_extras",
                "Drawing Index",
                "No extra sheets outside the drawing index",
                "PDF should not include extra sheets that are not listed in the drawing index.",
                "Pass",
                page_number=1,
                evidence="No extra sheets were detected beyond the drawing index.",
                confidence=0.97,
            )
        )

    # Order check: only compare sheets that appear in BOTH the index and the PDF.
    # The PDF may have extra sheets (appendices) or the index parser may have
    # missed some — that's handled by other checks. Here we only care about
    # whether the sheets that ARE indexed appear in the correct relative order.
    indexed_set = set(indexed_numbers)
    actual_in_index_order = [s for s in actual_numbers if s in indexed_set]

    if not indexed_numbers or len(indexed_numbers) < 2:
        # Not enough data to check order
        issues.append(
            make_issue(
                run_id,
                "drawing_index_order",
                "Drawing Index",
                "Sheet order matches drawing index",
                "Actual sheet order should follow the drawing index order.",
                "Needs Review",
                severity="low",
                page_number=1,
                evidence=f"Only {len(indexed_numbers)} indexed sheet(s) found — not enough to verify order.",
                confidence=0.40,
            )
        )
    elif actual_in_index_order == indexed_numbers:
        issues.append(
            make_issue(
                run_id,
                "drawing_index_order",
                "Drawing Index",
                "Sheet order matches drawing index",
                "Actual sheet order should follow the drawing index order.",
                "Pass",
                page_number=1,
                evidence=f"All {len(indexed_numbers)} indexed sheets appear in the correct order.",
                confidence=0.97,
            )
        )
    elif parser_coverage < 0.5:
        issues.append(
            make_issue(
                run_id,
                "drawing_index_order",
                "Drawing Index",
                "Sheet order matches drawing index",
                "Actual sheet order should follow the drawing index order.",
                "Needs Review",
                severity="low",
                page_number=1,
                evidence=(
                    f"Drawing index parser only extracted {len(indexed_numbers)} "
                    f"of {len(actual_numbers)} sheets — order comparison unreliable."
                ),
                confidence=0.40,
            )
        )
    else:
        # Find first mismatch
        mismatch_sheet = None
        for a, b in zip(actual_in_index_order, indexed_numbers):
            if a != b:
                mismatch_sheet = a
                break
        if not mismatch_sheet:
            # Lists differ in length but matching prefix — extra sheets at end
            issues.append(
                make_issue(
                    run_id,
                    "drawing_index_order",
                    "Drawing Index",
                    "Sheet order matches drawing index",
                    "Actual sheet order should follow the drawing index order.",
                    "Pass",
                    page_number=1,
                    evidence=f"Indexed sheets appear in correct relative order.",
                    confidence=0.90,
                )
            )
        else:
            page_match = next((p for p in pages if p.sheet_number == mismatch_sheet), None)
            target_page = page_match.number if page_match else 1
            snippet_path, preview_path, bbox = render_issue_artifacts(
                doc, str(uuid.uuid4()), target_page, run_dir, target_text=mismatch_sheet,
            )
            issues.append(
                make_issue(
                    run_id,
                    "drawing_index_order",
                    "Drawing Index",
                    "Sheet order matches drawing index",
                    "Actual sheet order should follow the drawing index order.",
                    "Fail",
                    severity="medium",
                    page_number=target_page,
                    evidence=(
                        f"Sheet {mismatch_sheet} appears out of order. "
                        f"Index order: {', '.join(indexed_numbers[:5])}{'...' if len(indexed_numbers) > 5 else ''}. "
                        f"PDF order: {', '.join(actual_in_index_order[:5])}{'...' if len(actual_in_index_order) > 5 else ''}."
                    ),
                    confidence=0.90,
                    snippet_path=snippet_path,
                    page_preview_path=preview_path,
                    bbox=bbox,
                )
            )

    # Title block checks — tolerate page 1 (cover sheet) which often
    # has different formatting, and don't hard-fail for a small number
    # of pages where regex couldn't parse a sheet number.
    pages_to_check = [p for p in pages if p.number > 1]  # skip cover sheet
    missing_sheet_num_pages = [p.number for p in pages_to_check if not p.sheet_number]
    total_checked = len(pages_to_check)
    missing_ratio = len(missing_sheet_num_pages) / max(total_checked, 1)

    if not missing_sheet_num_pages:
        issues.append(
            make_issue(
                run_id,
                "title_block_sheet_number",
                "Title Block",
                "Each page has a sheet number",
                "Every sheet should show a valid sheet number in the title block.",
                "Pass",
                evidence=f"Found sheet numbers on all {len(pages)} pages.",
                confidence=0.96,
            )
        )
    elif missing_ratio <= 0.15:
        # A few pages missing — likely appendices or non-standard pages
        page_number = missing_sheet_num_pages[0]
        snippet_path, preview_path, bbox = render_issue_artifacts(doc, str(uuid.uuid4()), page_number, run_dir)
        issues.append(
            make_issue(
                run_id,
                "title_block_sheet_number",
                "Title Block",
                "Each page has a sheet number",
                "Every sheet should show a valid sheet number in the title block.",
                "Needs Review",
                severity="low",
                page_number=page_number,
                evidence=f"Could not parse sheet number on {len(missing_sheet_num_pages)} of {total_checked} pages (pages: {', '.join(map(str, missing_sheet_num_pages[:10]))}). These may be appendices or use non-standard numbering.",
                confidence=0.72,
                snippet_path=snippet_path,
                page_preview_path=preview_path,
                bbox=bbox,
            )
        )
    else:
        page_number = missing_sheet_num_pages[0]
        snippet_path, preview_path, bbox = render_issue_artifacts(doc, str(uuid.uuid4()), page_number, run_dir)
        issues.append(
            make_issue(
                run_id,
                "title_block_sheet_number",
                "Title Block",
                "Each page has a sheet number",
                "Every sheet should show a valid sheet number in the title block.",
                "Fail",
                severity="high",
                page_number=page_number,
                evidence=f"Missing sheet number on {len(missing_sheet_num_pages)} of {total_checked} pages: {', '.join(map(str, missing_sheet_num_pages[:10]))}",
                confidence=0.92,
                snippet_path=snippet_path,
                page_preview_path=preview_path,
                bbox=bbox,
            )
        )

    missing_sheet_title_pages = [p.number for p in pages_to_check if not p.sheet_title]
    title_missing_ratio = len(missing_sheet_title_pages) / max(total_checked, 1)

    if not missing_sheet_title_pages:
        issues.append(
            make_issue(
                run_id,
                "title_block_sheet_name",
                "Title Block",
                "Each page has a sheet title",
                "Every sheet should show a readable sheet title in the title block.",
                "Pass",
                evidence="Sheet titles were parsed on every page.",
                confidence=0.89,
            )
        )
    else:
        # Title parsing is inherently fragile due to PDF text extraction
        # limitations. Failure to parse ≠ title is absent. Always mark as
        # Needs Review so the AI vision check (which actually reads the
        # page image) can provide the authoritative verdict.
        page_number = missing_sheet_title_pages[0]
        snippet_path, preview_path, bbox = render_issue_artifacts(doc, str(uuid.uuid4()), page_number, run_dir)
        issues.append(
            make_issue(
                run_id,
                "title_block_sheet_name",
                "Title Block",
                "Each page has a sheet title",
                "Every sheet should show a readable sheet title in the title block.",
                "Needs Review",
                severity="low",
                page_number=page_number,
                evidence=f"Could not parse sheet title on {len(missing_sheet_title_pages)} of {total_checked} pages (pages: {', '.join(map(str, missing_sheet_title_pages[:10]))}). Title block layout may differ — AI vision check should verify.",
                confidence=0.55,
                snippet_path=snippet_path,
                page_preview_path=preview_path,
                bbox=bbox,
            )
        )

    # ── Registry-driven checks ─────────────────────────────────────────
    # These replace the old hardcoded keyword, sheet_existence, and
    # electrical_calc checks.  Drawing-index and title-block checks above
    # remain procedural because they need bespoke logic.

    actual_set = set(actual_numbers)
    page_map = {page.sheet_number: page for page in pages if page.sheet_number}

    indexed_title_map: dict[str, str] = {}
    for snum, stitle in indexed_sheets:
        indexed_title_map[snum] = stitle

    page_title_map: dict[str, str] = {}
    for p in pages:
        if p.sheet_number and p.sheet_title:
            page_title_map[p.sheet_number] = p.sheet_title

    def _find_sheets_by_title(keywords: list[str]) -> tuple[list[str], list[str]]:
        found_in_pdf: list[str] = []
        for snum in actual_numbers:
            title_text = (
                page_title_map.get(snum, "") + " " +
                indexed_title_map.get(snum, "")
            ).upper()
            if any(kw.upper() in title_text for kw in keywords):
                found_in_pdf.append(snum)
        indexed_but_missing: list[str] = []
        for snum, ititle in indexed_sheets:
            if snum not in actual_set:
                if any(kw.upper() in ititle.upper() for kw in keywords):
                    indexed_but_missing.append(snum)
        return found_in_pdf, indexed_but_missing

    def _find_all_pages_by_title(*keywords: str) -> list[PageInfo]:
        out = []
        for p in pages:
            title = (p.sheet_title or "").upper()
            if any(kw.upper() in title for kw in keywords):
                out.append(p)
        return out

    def _get_scope_text(rule: Rule) -> tuple[str | None, int | None]:
        """Return (combined_upper_text, first_page_number) for a rule's
        search_scope.

        - ``search_scope: cover``   → cover text only.
        - ``search_scope: all``     → every page joined.
        - ``title_match`` provided  → pages whose sheet title contains the
                                      given keywords.
        - None of the above         → default to ALL pages joined (V4 rules
                                      frequently have no narrowing scope).
        """
        scope = rule.search_scope
        if scope == "cover":
            return cover_text.upper(), 1
        if scope == "all":
            combined = "\n".join(p.text for p in pages).upper()
            return combined, (pages[0].number if pages else 1)
        title_kws = rule.title_match
        if title_kws:
            exclude = [e.upper() for e in rule.exclude_title]
            matched = _find_all_pages_by_title(*title_kws)
            if exclude:
                matched = [
                    p for p in matched
                    if not any(ex in (p.sheet_title or "").upper() for ex in exclude)
                ]
            if not matched:
                return None, None
            combined = "\n".join(p.text for p in matched).upper()
            return combined, matched[0].number
        # Fallback: search all pages (default for V4 keyword rules).
        combined = "\n".join(p.text for p in pages).upper()
        return combined, (pages[0].number if pages else 1)

    # --- sheet_existence rules ---
    for rule in get_rules_by_check_type("sheet_existence"):
        if not rule.title_keywords:
            continue
        found, missing = _find_sheets_by_title(rule.title_keywords)
        if found:
            page_no = page_map[found[0]].number
            sp, pp, bbox_d = render_issue_artifacts(
                doc, str(uuid.uuid4()), page_no, run_dir,
            )
            issues.append(make_issue(
                run_id, rule.key, rule.category, rule.title, rule.description,
                "Pass", severity=rule.severity,
                page_number=page_no,
                evidence=f"Found: {', '.join(found)}",
                confidence=0.96,
                snippet_path=sp, page_preview_path=pp, bbox=bbox_d,
            ))
        elif missing:
            sp, pp, bbox_d = render_issue_artifacts(
                doc, str(uuid.uuid4()), 1, run_dir,
                target_text=missing[0],
            )
            issues.append(make_issue(
                run_id, rule.key, rule.category, rule.title, rule.description,
                "Fail", severity="high", page_number=1,
                evidence=(
                    f"Drawing index lists {', '.join(missing)} "
                    f"but they are missing from the PDF."
                ),
                confidence=0.97,
                snippet_path=sp, page_preview_path=pp, bbox=bbox_d,
            ))
        # else: not in scope → skip

    # --- keyword rules ---
    for rule in get_rules_by_check_type("keyword"):
        scope_text, page_no = _get_scope_text(rule)
        if scope_text is None:
            continue  # scope pages not found → skip
        keywords = rule.keywords
        # Support grouped keywords (list-of-lists → match any within group)
        if keywords and isinstance(keywords[0], list):
            found_kws: list[str] = []
            for group in keywords:
                matched = [kw for kw in group if kw.upper() in scope_text]
                if matched:
                    found_kws.append(matched[0])
        else:
            found_kws = [kw for kw in keywords if kw.upper() in scope_text]

        status = "Pass" if len(found_kws) >= rule.min_matches else "Needs Review"
        sp, pp, bbox_d = (None, None, None)
        if page_no:
            # Highlight every matched keyword (plus a fallback to the first
            # candidate keyword so even a Pass shows where the hits landed).
            highlight_terms = list(found_kws)
            first_kw = _first_keyword_str(keywords)
            if first_kw and first_kw not in highlight_terms:
                highlight_terms.append(first_kw)
            sp, pp, bbox_d = render_issue_artifacts(
                doc, str(uuid.uuid4()), page_no, run_dir,
                target_texts=highlight_terms,
            )
        issues.append(make_issue(
            run_id, rule.key, rule.category, rule.title, rule.description,
            status, severity=rule.severity, page_number=page_no,
            evidence=f"Found: {', '.join(found_kws) or 'none'}",
            confidence=rule.confidence if status == "Pass" else rule.confidence * 0.85,
            snippet_path=sp, page_preview_path=pp, bbox=bbox_d,
        ))

    # --- Auto-extract specs from planset pages for electrical calcs ---
    _progress("calcs", "Extracting specs from planset...", 33)
    auto_specs = extract_specs_from_pages(
        doc, pages,
        design_stage=design_stage,
        project_name=project_name,
    )

    # Merge: user-provided project_details override auto-extracted values
    pd = {**auto_specs, **(project_details or {})}

    # --- electrical_calc rules ---
    _progress("calcs", "Running electrical calculations...", 35)

    # Build a lookup: category → first page number, so calc results
    # can link to the relevant sheet.
    _cat_page: dict[str, int] = {}
    for iss in issues:
        if iss.get("page_number") and iss["category"] not in _cat_page:
            _cat_page[iss["category"]] = iss["page_number"]

    for rule in get_rules_by_check_type("electrical_calc"):
        if not rule.calc_function:
            continue
        result = run_calc(rule.calc_function, pd)
        if result is None:
            continue
        nec_note = f" (Ref: {rule.nec_ref})" if rule.nec_ref else ""
        # Tuning (demo prep): a calc that returns "Needs Review" because its
        # inputs weren't extracted from the planset is a deferred check, not
        # a real finding. Emitting NR here creates false-positive EXTRA noise
        # on pilots where the extractor hasn't been written yet (E-120
        # stringing, DC/AC/MV ampacity, etc.). Flip to Deferred so it shows
        # in the UI as "not run - missing inputs" and doesn't count against
        # precision in the regression scorer.
        result_status = result.status
        result_evidence = result.evidence + nec_note
        # A calc that returns "Needs Review" because its inputs weren't
        # extracted is a deferred check, not a real finding. Flip to Deferred
        # so the UI shows "not run — missing inputs" instead of polluting NR.
        # Matches any evidence starting with "Missing " — covers all calc
        # functions (validate_stringing says "Missing project details: X",
        # the others say "Missing module_isc", "Missing voltage", etc.).
        if result.status == "Needs Review" and result.evidence.startswith("Missing "):
            result_status = "Deferred"
            result_evidence = (
                f"Deferred: calc inputs not yet extracted from planset "
                f"({result.evidence})."
            )
        # Try to link to the relevant sheet page
        page_no = _cat_page.get(rule.category)
        sp, pp, bbox_d = (None, None, None)
        if page_no:
            sp, pp, bbox_d = render_issue_artifacts(
                doc, str(uuid.uuid4()), page_no, run_dir,
            )
        issues.append(make_issue(
            run_id, rule.key, rule.category, rule.title,
            rule.description,
            result_status,
            severity=result.severity,
            page_number=page_no,
            evidence=result_evidence,
            confidence=result.confidence,
            snippet_path=sp,
            page_preview_path=pp,
            bbox=bbox_d,
        ))

    # PVSyst - external document. Emit as Deferred (not Needs Review) so it
    # appears in the UI as "not yet implemented / awaiting extractor" without
    # inflating the EXTRA false-positive count in regression scoring. The
    # PVSyst extractor is a follow-on task; until it lands, this is deferred
    # work, not a finding.
    issues.append(
        make_issue(
            run_id,
            "pvsyst_summary_available",
            "PVSyst Analysis Summary",
            "PVSyst summary available",
            "PVSyst report is external. Upload support can be added later.",
            "Deferred",
            severity="low",
            evidence=(
                "Deferred: PVSyst extractor not yet implemented - upload "
                "support coming in a follow-on task."
            ),
            confidence=1.0,
        )
    )

    # ── Gemini-powered deep checks ──────────────────────────────────────
    _progress("gemini", "Running AI vision checks (Gemini Flash)...", 40)
    gemini_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "api_calls": 0}
    call_timings: list[dict] = []
    try:
        from .gemini_client import reset_usage, get_usage
        from .gemini_analyzer import run_gemini_checks

        reset_usage()

        # Forward per-check progress from run_gemini_checks so the UI
        # bar doesn't sit at 40% for the whole Gemini phase.
        def _gem_progress(detail: str, pct: int) -> None:
            _progress("gemini", detail, pct)

        gemini_issues = run_gemini_checks(
            doc=doc,
            pages=pages,
            page_map=page_map,
            run_id=run_id,
            run_dir=run_dir,
            actual_numbers=actual_numbers,
            project_details=project_details,
            use_deep=use_deep,
            supporting_docs=supporting_docs,
            design_stage=design_stage,
            progress_cb=_gem_progress,
            out_timings=call_timings,
        )
        issues.extend(gemini_issues)
        gemini_usage = get_usage()
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Gemini checks failed – continuing with regex-only results"
        )

    _progress("summary", "Building summary...", 90)
    status_counts = Counter(issue["status"] for issue in issues)
    by_category = defaultdict(lambda: {"total": 0, "Pass": 0, "Fail": 0, "Needs Review": 0, "Overridden / Accepted by QC Engineer": 0})
    for issue in issues:
        cat = by_category[issue["category"]]
        cat["total"] += 1
        cat[issue["status"]] = cat.get(issue["status"], 0) + 1

    ordered_categories = []
    for category in get_category_order():
        if category in by_category:
            data = by_category[category]
            ordered_categories.append({"name": category, **data})

    # Build page_number → sheet_number map for frontend PDF links
    page_sheet_map = {
        page.number: page.sheet_number
        for page in pages
        if page.sheet_number
    }

    duration_seconds = round(time.monotonic() - _analysis_start, 2)

    # Per-dispatch wall time (parallel — these overlap). Used by the UI to
    # spot outlier categories when total duration spikes.
    call_timings_sorted = sorted(
        call_timings, key=lambda t: t.get("duration_s", 0), reverse=True,
    )

    summary = {
        "indexed_sheet_count": len(indexed_numbers),
        "actual_sheet_count": len(actual_numbers),
        "pdf_page_count": doc.page_count,
        "missing_from_pdf": missing_from_pdf,
        "extra_sheets": extra_sheets,
        "gemini_usage": gemini_usage,
        "page_sheet_map": page_sheet_map,
        "duration_seconds": duration_seconds,
        "deep_mode": bool(use_deep),
        "design_stage": design_stage,
        "supporting_docs": supporting_docs or [],
        "call_timings": call_timings_sorted,
    }

    run = {
        "id": run_id,
        "project_name": project_name or pdf_path.stem,
        "original_filename": original_filename,
        "created_at": utc_now(),
        "pdf_path": str(pdf_path),
        "page_count": doc.page_count,
        "summary": summary,
        "status_counts": dict(status_counts),
        "categories": ordered_categories,
        "project_details": project_details,
        "duration_seconds": duration_seconds,
        "deep_mode": bool(use_deep),
    }
    doc.close()
    return run, issues
