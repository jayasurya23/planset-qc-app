"""Vision-based diagram-consistency pass for solar plansets.

Much of a planset's engineering content lives in the *rasterized drawings* —
equipment tags with ratings, dimension callouts (pole/pile spacing, row
pitch), ratings printed inside one-line symbols, schedule cells — and never
makes it into the PDF text layer or the ~30 predefined spec fields. This
module reads those values off the DRAWING IMAGES with a single vision call per
diagram-heavy sheet and flags where the SAME labeled value disagrees between
sheets.

It reuses the already-built cross-sheet infrastructure in
:mod:`app.consistency`:

  * Confidently-recognized annotation labels are mapped onto the existing
    predefined provenance field names and *fed back into the analyzer's
    provenance dict*. The DETERMINISTIC comparator
    (``consistency.compare_provenance``) then catches any conflict — this is
    the de-dup mechanism (a value handled deterministically is never also
    cross-checked here).
  * Un-recognized labels are grouped by a normalized label key and compared
    here with ``consistency.normalize_value`` so unit/format differences do
    NOT trigger false positives. Genuine disagreements become findings that
    REUSE the exact multi-location shape ``consistency._build_finding``
    produces.

Public surface:

  * ``select_diagram_pages(pages) -> list[PageInfo]``
  * ``extract_diagram_annotations(page, *, render, ai_call) -> list[dict]``
  * ``map_label_to_field(label, category) -> str | None``
  * ``cross_check_annotations(annotations) -> list[dict]``
  * ``run_diagram_consistency(pages, provenance, *, render, ai_call) -> list[dict]``

Every entry point is DEFENSIVE: on any AI / parse / render error it logs and
degrades to an empty result — a diagram-pass failure must never crash the
analysis.
"""

from __future__ import annotations

import json
import logging
import re

from . import consistency

log = logging.getLogger(__name__)


def _extract_json_list(text: str) -> list:
    """Best-effort extraction of a JSON array from a model response.

    Mirrors ``gemini_analyzer._extract_json`` but is self-contained so this
    module — which is imported BY ``analyzer`` — never has to import the heavy
    ``gemini_analyzer``/``analyzer`` chain back (avoids an import cycle and the
    optional-dependency import cost). Returns ``[]`` on any failure.
    """
    if not text:
        return []
    # ```json ... ``` fenced array first.
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1))
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            pass
    # Whole response.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("annotations"), list):
            return parsed["annotations"]
    except json.JSONDecodeError:
        pass
    # Last resort — outermost [ ... ].
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            pass
    log.warning("diagram-consistency: could not parse JSON list from response")
    return []


# ── Page selection ──────────────────────────────────────────────────────────
# Diagram-heavy sheets we read with vision. Three families:
#   1. one-line / three-line (SLD) electrical diagrams,
#   2. array / site / civil layout, pile / racking / stringing plans,
#   3. schedules (conductor / equipment / panel schedules).
# Plus key detail sheets. Capped to keep the added vision-call count bounded.

_MAX_DIAGRAM_PAGES = 10

# One-line / three-line electrical diagrams.
_ONELINE_TITLE_KW = (
    "ONE LINE", "ONE-LINE", "SINGLE LINE", "SINGLE-LINE",
    "THREE LINE", "THREE-LINE", "3-LINE", "3 LINE", "SLD",
)
_ONELINE_SHEET_CODES = {"E-100", "E-101", "E-103", "E-104"}

# Array / site / civil layout, pile / racking / stringing plans.
_LAYOUT_TITLE_KW = (
    "LAYOUT", "ARRAY", "SITE PLAN", "RACKING", "PILE", "PIER",
    "STRINGING", "CIVIL", "FOUNDATION", "ROW", "TRACKER",
)

# Schedules.
_SCHEDULE_TITLE_KW = ("SCHEDULE",)
_SCHEDULE_SHEET_PREFIX = "E-3"   # E-300 family

# Key detail sheets (lighter weight — only used to top up if room remains).
_DETAIL_TITLE_KW = ("DETAIL", "DETAILS")


def _page_title(p) -> str:
    return (getattr(p, "sheet_title", None) or "").upper()


def _page_sheet(p) -> str:
    return (getattr(p, "sheet_number", None) or "").upper()


def _is_oneline(p) -> bool:
    title = _page_title(p)
    if any(kw in title for kw in _ONELINE_TITLE_KW):
        return True
    snum = _page_sheet(p)
    return bool(snum) and snum in _ONELINE_SHEET_CODES


def _is_layout(p) -> bool:
    return any(kw in _page_title(p) for kw in _LAYOUT_TITLE_KW)


def _is_schedule(p) -> bool:
    title = _page_title(p)
    if any(kw in title for kw in _SCHEDULE_TITLE_KW):
        return True
    snum = _page_sheet(p)
    return bool(snum) and snum.startswith(_SCHEDULE_SHEET_PREFIX)


def _is_detail(p) -> bool:
    return any(kw in _page_title(p) for kw in _DETAIL_TITLE_KW)


def select_diagram_pages(pages) -> list:
    """Pick the diagram-heavy sheets worth a vision read, capped at
    ``_MAX_DIAGRAM_PAGES``.

    Priority order: one-line/three-line, layout/plan, schedules, then key
    detail sheets to top up. Selection is de-duplicated by page number and the
    number of dropped pages is logged.
    """
    if not pages:
        return []

    selected: list = []
    seen: set[int] = set()

    def _add(group: list) -> None:
        for p in group:
            num = getattr(p, "number", None)
            if num is None or num in seen:
                continue
            seen.add(num)
            selected.append(p)

    oneline = [p for p in pages if _is_oneline(p)]
    layout = [p for p in pages if _is_layout(p)]
    schedule = [p for p in pages if _is_schedule(p)]
    detail = [p for p in pages if _is_detail(p)]

    # Priority tiers — earlier tiers fill the cap first.
    _add(oneline)
    _add(layout)
    _add(schedule)
    _add(detail)

    if len(selected) > _MAX_DIAGRAM_PAGES:
        dropped = selected[_MAX_DIAGRAM_PAGES:]
        kept = selected[:_MAX_DIAGRAM_PAGES]
        log.info(
            "diagram-consistency: %d candidate diagram sheets, capped at %d; "
            "dropped %d (%s)",
            len(selected), _MAX_DIAGRAM_PAGES, len(dropped),
            ", ".join(_page_sheet(p) or f"p{getattr(p, 'number', '?')}"
                      for p in dropped),
        )
        selected = kept
    else:
        log.info(
            "diagram-consistency: selected %d diagram sheets "
            "(%d one-line, %d layout, %d schedule, %d detail) within cap of %d",
            len(selected), len(oneline), len(layout), len(schedule),
            len(detail), _MAX_DIAGRAM_PAGES,
        )
    return selected


# ── Vision extraction ───────────────────────────────────────────────────────

_DIAGRAM_EXTRACT_PROMPT = """\
You are a senior solar PV professional engineer reading ONE rasterized planset
DRAWING (an electrical one-line/three-line, an array/site/civil layout, a
pile/racking/stringing plan, a schedule, or a detail sheet).

LIST every labeled engineering value you can READ ON THE DRAWING with high
confidence. Include, wherever clearly drawn:
  - equipment tags WITH their rating (e.g. "Transformer T1 = 2500 kVA",
    "Inverter INV-1 = 3800 kW", "Recloser R1 = 630 A"),
  - dimension callouts (pole spacing, pile spacing, row pitch, clearances —
    e.g. "Pile spacing = 18'-0\\""),
  - voltages (e.g. "480Y/277V", "34.5 kV", "12470Y/7200V"),
  - counts (module count, string count, pile count — e.g. "Module count = 1320"),
  - schedule entries (conductor size, OCPD rating, panel rows),
  - other clearly-labeled parameters (GCR, tilt, azimuth).

STRICT RULES:
  - ONLY include values you can actually READ on the image with confidence.
  - DO NOT infer, calculate, or guess. If a value is unreadable, OMIT it.
  - Each item's "label" must name the entity/parameter exactly as drawn so the
    SAME parameter on another sheet gets the SAME label (e.g. "Transformer T1
    kVA", "Row pitch", "Pile spacing", "Inverter INV-1 kW", "Module count").
  - Each item's "value" is the value AS DRAWN, keeping units (e.g. "2500 kVA",
    "18'-0\\"", "480Y/277V", "1320").

Return ONLY a STRICT JSON list, no prose, exactly this shape:
```json
[
  {"label": "Transformer T1 kVA", "value": "2500 kVA", "category": "equipment_rating"},
  {"label": "Pile spacing",       "value": "18'-0\\"",  "category": "dimension"},
  {"label": "Secondary voltage",  "value": "480Y/277V","category": "voltage"},
  {"label": "Module count",       "value": "1320",      "category": "count"}
]
```
"category" MUST be one of: equipment_rating, dimension, voltage, count,
schedule, label, other. Return ONLY the JSON list.
"""

_VALID_CATEGORIES = {
    "equipment_rating", "dimension", "voltage", "count",
    "schedule", "label", "other",
}


def extract_diagram_annotations(page, *, render, ai_call) -> list[dict]:
    """Render *page* to a high-res PNG and make ONE vision call that lists the
    labeled values drawn on it.

    *render* renders the page to PNG bytes (e.g. a closure over
    ``gemini_analyzer.render_page_to_bytes(doc, page.number, zoom=2.0)``).
    *ai_call* sends the image bytes + prompt to the vision model and returns
    the raw text response (e.g. a closure over
    ``gemini_client.analyze_page_image``).

    Returns a list of ``{label, value, category, page_number, sheet_number}``
    dicts. DEFENSIVE: on any render / AI / parse error returns ``[]`` and logs
    — never raises.
    """
    page_number = getattr(page, "number", None)
    sheet_number = getattr(page, "sheet_number", None)

    try:
        image = render(page)
    except Exception:
        log.exception("diagram-consistency: render failed for page %s", page_number)
        return []
    if not image:
        log.warning("diagram-consistency: empty render for page %s", page_number)
        return []

    try:
        raw = ai_call(image, _DIAGRAM_EXTRACT_PROMPT)
    except Exception:
        log.exception("diagram-consistency: AI call failed for page %s", page_number)
        return []

    try:
        parsed = _extract_json_list(raw or "")
    except Exception:
        log.exception("diagram-consistency: JSON parse failed for page %s", page_number)
        return []

    if not isinstance(parsed, list):
        log.warning("diagram-consistency: page %s returned non-list JSON", page_number)
        return []

    annotations: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        value = item.get("value")
        if label is None or value is None:
            continue
        label_s = str(label).strip()
        value_s = str(value).strip()
        if not label_s or not value_s:
            continue
        # Skip obviously-empty / placeholder values.
        if value_s.lower() in ("not shown", "n/a", "na", "tbd", "null", "none", "-"):
            continue
        category = str(item.get("category") or "other").strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "other"
        annotations.append({
            "label": label_s,
            "value": value_s,
            "category": category,
            "page_number": page_number,
            "sheet_number": sheet_number,
        })

    log.info(
        "diagram-consistency: page %s (sheet %s) → %d annotation(s)",
        page_number, sheet_number or "?", len(annotations),
    )
    return annotations


# ── Label → known-field mapping ─────────────────────────────────────────────
# Confidently-recognized labels are mapped onto the EXISTING predefined
# provenance field names so the deterministic comparator handles them. Each
# rule is (compiled-regex-on-normalized-label, field). The first match wins.
# Normalized label = uppercase, single-spaced (see _norm_label).

_LABEL_FIELD_RULES: list[tuple[re.Pattern, str]] = [
    # Transformer ratings / voltages / winding.
    (re.compile(r"TRANSFORMER.*KVA|TRANSFORMER.*MVA|\bXFMR.*KVA|\bT\d+.*KVA"),
     "transformer_kva"),
    (re.compile(r"TRANSFORMER.*PRIMARY.*VOLT|PRIMARY.*VOLTAGE"),
     "transformer_primary_voltage"),
    (re.compile(r"TRANSFORMER.*SECONDARY.*VOLT|SECONDARY.*VOLTAGE"),
     "transformer_secondary_voltage"),
    (re.compile(r"WINDING|VECTOR\s*GROUP|DELTA.*WYE|WYE.*DELTA"),
     "transformer_winding_config"),
    # Inverter rating / voltage.
    (re.compile(r"INVERTER.*\bKW\b|INVERTER.*\bMW\b|\bINV[- ]?\d+.*\bKW\b"),
     "inverter_kw"),
    (re.compile(r"INVERTER.*AC.*VOLT|INVERTER.*OUTPUT.*VOLT"),
     "inverter_ac_voltage"),
    (re.compile(r"INVERTER.*MAX.*VDC|MAX(IMUM)?\s*DC\s*VOLT|MAX\s*VDC"),
     "inverter_max_vdc"),
    # Geometry / civil.
    (re.compile(r"\bPOLE\b.*SPAC|SPAC.*\bPOLE\b"), "pole_spacing"),
    (re.compile(r"\bPILE\b.*SPAC|\bPIER\b.*SPAC|SPAC.*\bPILE\b"), "pile_spacing"),
    (re.compile(r"ROW\s*PITCH|ROW[- ]TO[- ]ROW\s*PITCH"), "row_pitch"),
    (re.compile(r"INTER[- ]?ROW|ROW\s*SPACING|ROW\s*GAP"), "interrow_spacing"),
    (re.compile(r"\bGCR\b|GROUND\s*COVERAGE\s*RATIO"), "gcr"),
    (re.compile(r"TILT\s*ANGLE|MODULE\s*TILT|ARRAY\s*TILT"), "tilt_angle"),
    (re.compile(r"\bAZIMUTH\b"), "azimuth"),
    (re.compile(r"TRACKER\s*ROTATION|MAX.*TRACKER.*ANGLE"), "tracker_rotation"),
    # POI / utility voltage.
    (re.compile(r"\bPOI\b.*VOLT|POINT\s*OF\s*INTERCONNECT.*VOLT"), "poi_voltage"),
    # Ratios.
    (re.compile(r"DC[\s/]*AC\s*RATIO|DC[- ]TO[- ]AC"), "dc_ac_ratio"),
]


def map_label_to_field(label: str, category: str | None = None) -> str | None:
    """Map a drawing-annotation *label* onto an existing predefined provenance
    field name, or ``None`` if it isn't confidently recognized.

    Recognized labels become provenance contributions (handled by the
    deterministic comparator); un-recognized ones flow to the open-ended
    cross-check. *category* is accepted for future disambiguation but the rule
    table is label-driven today.
    """
    if not label:
        return None
    norm = _norm_label(label)
    if not norm:
        return None
    for rx, field in _LABEL_FIELD_RULES:
        if rx.search(norm):
            return field
    return None


# ── Open-ended cross-check ──────────────────────────────────────────────────

# Words stripped from a label before grouping, so "Transformer T1 kVA" and
# "Transformer T1 (kVA)" collapse to the same key.
_LABEL_STOPWORD_RE = re.compile(
    r"\b(THE|A|AN|OF|FOR|RATED|RATING|VALUE|TOTAL|NOM(?:INAL)?|APPROX(?:IMATELY)?)\b"
)
_LABEL_PUNCT_RE = re.compile(r"[^A-Z0-9 ]+")
_MULTISPACE_RE = re.compile(r"\s+")


def _norm_label(label: str) -> str:
    """Uppercase, strip punctuation + filler words, collapse whitespace."""
    s = (label or "").upper()
    s = _LABEL_PUNCT_RE.sub(" ", s)
    s = _LABEL_STOPWORD_RE.sub(" ", s)
    s = _MULTISPACE_RE.sub(" ", s).strip()
    return s


def _is_garbage_label(norm: str) -> bool:
    """Reject single-character / digit-only / empty labels that would group
    unrelated values together and produce false positives."""
    if not norm or len(norm) < 3:
        return True
    if not re.search(r"[A-Z]", norm):   # must contain a letter
        return True
    return False


# Title-block / sheet-identity / administrative labels are NOT cross-sheet
# value-consistency targets — sheet name/number/title, revision, date, scale,
# drawn/checked-by, project/engineer/EPC/logo, SOV, etc. legitimately DIFFER
# from sheet to sheet (or are covered by the dedicated Title Block category).
# Excluding them removes a whole class of false positives (e.g. flagging the
# title-block "SHEET NAME" for differing across sheets).
_METADATA_LABEL_RE = re.compile(
    r"\b(SHEET|SHT|DWG|DRAWING|REV|REVISION|DATE|SCALE|NTS|DRAWN|CHECKED|"
    r"APPROVED|DESIGNED|DESIGNER|ENGINEER|PROJECT|CLIENT|OWNER|EPC|LOGO|"
    r"STAMP|SEAL|FILE\s*NAME|FILENAME|PAGE|SOV|TITLE\s*BLOCK)\b"
)


def _is_metadata_label(norm: str) -> bool:
    """True for title-block / administrative labels that are not cross-sheet
    value-consistency targets (they differ by design)."""
    return bool(_METADATA_LABEL_RE.search(norm))


# A category → pseudo-field used so the right tolerance / normalizer applies in
# consistency.normalize_value (which keys behavior off the field name).
def _pseudo_field(category: str) -> str:
    if category == "dimension":
        return "row_pitch"            # ft/in architectural tolerance
    if category == "voltage":
        return "transformer_secondary_voltage"  # voltage normalizer
    if category == "equipment_rating":
        return "transformer_kva"      # power normalizer (kVA/MVA/kW)
    return "_diagram_generic"         # generic numeric/text path


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    return s or "value"


def cross_check_annotations(annotations: list[dict]) -> list[dict]:
    """Cross-check the UN-mapped diagram annotations and return conflict
    findings in the same multi-location shape ``consistency._build_finding``
    produces.

    Annotations are grouped by normalized label. For any label seen on >= 2
    DIFFERENT pages, values are compared with ``consistency.normalize_value``
    (using a category-appropriate pseudo-field so unit/format differences do
    not trigger). If the values fall into more than one normalized group it is
    a CONFLICT. Clear numeric-magnitude conflicts → ``Fail``; genuinely
    ambiguous label/text conflicts are routed to ``consistency.reconcile_text``
    and become ``Fail`` (confidence >= 0.6) or ``Needs Review``.
    """
    findings: list[dict] = []
    if not annotations:
        return findings

    # Group by (normalized label). Carry the dominant category for tolerance.
    by_label: dict[str, dict] = {}
    for ann in annotations:
        norm = _norm_label(ann.get("label", ""))
        if _is_garbage_label(norm) or _is_metadata_label(norm):
            continue
        slot = by_label.setdefault(
            norm, {"sightings": [], "categories": {}, "display": ann.get("label")}
        )
        slot["sightings"].append({
            "value": ann.get("value"),
            "page_number": ann.get("page_number"),
            "sheet_number": ann.get("sheet_number"),
            "source": (
                f"{ann.get('sheet_number') or ('p' + str(ann.get('page_number')))} drawing"
            ),
        })
        cat = ann.get("category") or "other"
        slot["categories"][cat] = slot["categories"].get(cat, 0) + 1

    for norm, slot in by_label.items():
        sightings = slot["sightings"]
        # Need the SAME label read on >= 2 DIFFERENT pages.
        pages_seen = {s.get("page_number") for s in sightings if s.get("page_number") is not None}
        if len(pages_seen) < 2:
            continue

        category = max(slot["categories"].items(), key=lambda kv: kv[1])[0]
        pseudo = _pseudo_field(category)

        # Multi-instance guard: a label carrying MULTIPLE distinct values on the
        # SAME page is a per-instance label (e.g. FLA for different equipment, or
        # several breaker ratings) — not one cross-sheet value. Skip it.
        per_page: dict = {}
        for s in sightings:
            pn = s.get("page_number")
            if pn is None:
                continue
            k, _m = consistency.normalize_value(pseudo, s.get("value"))
            per_page.setdefault(pn, set()).add(k)
        if any(len(vals) >= 2 for vals in per_page.values()):
            log.info(
                "diagram-consistency: '%s' skipped — multi-instance label "
                "(multiple values on one sheet)", slot["display"] or norm,
            )
            continue

        groups = consistency._group_sightings(pseudo, sightings)
        if len(groups) < 2:
            continue  # all readings agree once normalized

        display_label = slot["display"] or norm
        item_key = f"diagram_{_slug(display_label)}"
        all_numeric = all(g["magnitude"] is not None for g in groups)

        if all_numeric:
            # Clear numeric magnitude conflict.
            finding = consistency._build_finding(
                pseudo, groups,
                status="Fail", auto_status="Fail",
                confidence=0.9, reference=None,
            )
        else:
            # Ambiguous label/text — defer to the AI reconciler.
            verdict = consistency.reconcile_text(pseudo, sightings)
            if verdict.get("verdict") == "consistent":
                log.info(
                    "diagram-consistency: '%s' reconciled consistent — no finding",
                    display_label,
                )
                continue
            try:
                conf = float(verdict.get("confidence", 0.3) or 0.3)
            except (TypeError, ValueError):
                conf = 0.3
            status = "Fail" if conf >= consistency._AI_CONFLICT_CONFIDENCE else "Needs Review"
            finding = consistency._build_finding(
                pseudo, groups,
                status=status, auto_status=status,
                confidence=conf, reference=None,
            )
            reason = verdict.get("reason")
            if reason:
                finding["description"] += f" AI assessment: {reason}"
                finding["evidence"] += f" AI: {reason} (confidence {conf:.2f})."

        # Re-key + re-title around the AS-DRAWN label rather than the pseudo
        # field, and mark locations as drawing sightings.
        finding["item_key"] = item_key
        finding["title"] = f"Cross-sheet diagram mismatch: {display_label}"
        finding["description"] = (
            f"The drawing value for '{display_label}' disagrees across planset "
            f"sheets. " + finding["description"].split(". ", 1)[-1]
        )
        for loc in finding.get("locations") or []:
            sheet = loc.get("sheet_number")
            pn = loc.get("page_number")
            loc["source_label"] = (
                f"{sheet} drawing" if sheet else f"p{pn} drawing"
            )
        findings.append(finding)
        log.info(
            "diagram-consistency: CONFLICT on '%s' (%s) across %d group(s) → %s",
            display_label, category, len(groups), finding["status"],
        )

    return findings


# ── Orchestrator ────────────────────────────────────────────────────────────

def run_diagram_consistency(pages, provenance: dict, *, render, ai_call) -> list[dict]:
    """Run the full diagram-consistency pass.

    1. Select the diagram-heavy sheets (capped).
    2. Extract labeled annotations from each (one vision call per sheet,
       optionally parallel via ``AI_PARALLELISM``).
    3. MAPPED annotations (recognized labels) are MUTATED into the caller's
       *provenance* dict so the existing deterministic comparator catches any
       conflict — this is the de-dup mechanism.
    4. UN-MAPPED annotations go to :func:`cross_check_annotations`; its
       open-ended findings are returned for the caller to render + emit.

    DEFENSIVE: any per-page failure is isolated; a total failure returns ``[]``.
    The number of vision calls issued is logged.
    """
    if provenance is None:
        provenance = {}

    selected = select_diagram_pages(pages)
    if not selected:
        log.info("diagram-consistency: no diagram sheets selected — skipping")
        return []

    # Extract annotations per page. Parallelize the (IO-bound) vision calls,
    # mirroring the analyzer's AI_PARALLELISM convention. Fall back to serial
    # if the pool can't be created.
    all_annotations: list[dict] = []
    vision_calls = 0
    try:
        import concurrent.futures as _cf
        import os as _os
        max_workers = max(1, int(_os.getenv("AI_PARALLELISM", "6")))
        with _cf.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="qc-diagram"
        ) as pool:
            fmap = {
                pool.submit(
                    extract_diagram_annotations, p, render=render, ai_call=ai_call
                ): p
                for p in selected
            }
            for fut in _cf.as_completed(fmap):
                vision_calls += 1
                try:
                    all_annotations.extend(fut.result() or [])
                except Exception:
                    log.exception(
                        "diagram-consistency: extraction future failed for page %s",
                        getattr(fmap[fut], "number", "?"),
                    )
    except Exception:
        log.exception(
            "diagram-consistency: parallel extraction unavailable — serial fallback"
        )
        for p in selected:
            vision_calls += 1
            all_annotations.extend(
                extract_diagram_annotations(p, render=render, ai_call=ai_call) or []
            )

    log.info(
        "diagram-consistency: %d vision call(s) over %d sheet(s) → %d annotation(s)",
        vision_calls, len(selected), len(all_annotations),
    )

    # Split: mapped → provenance (deterministic comparator); unmapped → cross-check.
    unmapped: list[dict] = []
    mapped_count = 0
    for ann in all_annotations:
        field = map_label_to_field(ann.get("label"), ann.get("category"))
        if field:
            # Skip an exact duplicate (same field/value/page) already present
            # so we don't inflate the sighting list.
            sightings = provenance.setdefault(field, [])
            sv = str(ann.get("value")).strip()
            pn = ann.get("page_number")
            if any(
                isinstance(s, dict)
                and str(s.get("value")).strip() == sv
                and s.get("page_number") == pn
                for s in sightings
            ):
                continue
            sightings.append({
                "value": sv,
                "source": (
                    f"{ann.get('sheet_number') or ('p' + str(pn))} drawing"
                ),
                "page_number": pn,
                "sheet_number": ann.get("sheet_number"),
            })
            mapped_count += 1
        else:
            unmapped.append(ann)

    log.info(
        "diagram-consistency: %d annotation(s) mapped to known fields "
        "(→ provenance), %d unmapped (→ cross-check)",
        mapped_count, len(unmapped),
    )

    return cross_check_annotations(unmapped)
