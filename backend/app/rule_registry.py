"""Rule Registry – loads QC rules from rules.yaml and provides lookup helpers.

Replaces the hardcoded ``CHECK_RULES`` list in ``checklist.py``.  The YAML
file is loaded once at import time and cached.  Every rule is exposed as a
:class:`Rule` dataclass for type-safe access throughout the codebase.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_DEFAULT_RULES_FILE = "rules.yaml"
_STAGE_OVERRIDES_FILE = "stage_overrides.yaml"

# Ordered from earliest to latest; anything left of the chosen stage also fires.
STAGE_ORDER: tuple[str, ...] = ("30", "60", "90", "IFC", "AsBuilt")


def stage_index(stage: str | None) -> int:
    """Return the position of ``stage`` in STAGE_ORDER, or -1 if unknown/None.

    An unknown value (including None) is treated as "no gating" — every rule
    fires — by returning a value >= len(STAGE_ORDER) so comparisons succeed.
    """
    if not stage:
        return len(STAGE_ORDER)  # no stage selected = don't gate anything
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return len(STAGE_ORDER)


def _resolve_rules_path() -> Path:
    """Pick which rules file to load.

    Priority:
      1. ``RULES_FILE`` env var — absolute, or relative to ``backend/app/``.
      2. ``rules.yaml`` next to this module (default).

    Lets us A/B test rule sets without code changes:
        RULES_FILE=rules_v4_draft.yaml  python -m uvicorn app.main:app …
    """
    app_dir = Path(__file__).resolve().parent
    env_value = os.getenv("RULES_FILE", "").strip()
    if env_value:
        p = Path(env_value)
        if not p.is_absolute():
            p = app_dir / p
        if p.exists():
            return p
        log.warning(
            "RULES_FILE=%s does not exist — falling back to default %s",
            env_value, _DEFAULT_RULES_FILE,
        )
    return app_dir / _DEFAULT_RULES_FILE


@dataclass(frozen=True)
class Rule:
    """Single QC check rule loaded from YAML."""

    key: str
    category: str
    title: str
    description: str
    severity: str = "medium"
    check_type: str = "manual"
    confidence: float = 0.75
    nec_ref: str | None = None
    handbook_ref: str | None = None

    # V4 additions
    source: str | None = None
    v4_status: str | None = None
    v4_row: int | None = None

    # Stage-gating: earliest design stage at which this rule should fire.
    # Populated from stage_overrides.yaml (category default + per-rule override).
    min_stage: str | None = None

    # True when ``min_stage`` came from an explicit per-rule override (or was
    # set in the rule entry itself), False when it was inherited from the
    # category default. The V4 engine uses this to decide whether the lenient
    # "if the sheet is in the PDF, run anyway" bypass applies — explicit
    # rule overrides are treated as authoritative and bypass-resistant.
    min_stage_explicit: bool = False

    # keyword check fields
    keywords: list[Any] = field(default_factory=list)
    min_matches: int = 1
    search_scope: str | None = None
    title_match: list[str] = field(default_factory=list)
    exclude_title: list[str] = field(default_factory=list)

    # sheet_existence check fields
    title_keywords: list[str] = field(default_factory=list)

    # electrical_calc check fields
    calc_function: str | None = None
    inputs: list[str] = field(default_factory=list)


@functools.lru_cache(maxsize=1)
def _load_raw() -> dict:
    path = _resolve_rules_path()
    log.info("Loading rules from %s", path)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@functools.lru_cache(maxsize=1)
def _load_stage_overrides() -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Return (category_min_stage, rule_overrides, disabled_rules).

    Reads ``stage_overrides.yaml``. Missing file is not an error — stage gating
    becomes a no-op and no rules are disabled.
    """
    path = Path(__file__).resolve().parent / _STAGE_OVERRIDES_FILE
    if not path.exists():
        log.info("stage_overrides.yaml not present — stage gating disabled")
        return {}, {}, set()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cat = {str(k): str(v) for k, v in (raw.get("category_min_stage") or {}).items()}
    rule = {str(k): str(v) for k, v in (raw.get("rule_overrides") or {}).items()}
    disabled = {str(k) for k in (raw.get("disabled_rules") or [])}
    log.info(
        "Loaded overrides: %d categor%s, %d rule-level, %d disabled",
        len(cat), "y" if len(cat) == 1 else "ies",
        len(rule), len(disabled),
    )
    return cat, rule, disabled


@functools.lru_cache(maxsize=1)
def load_rules() -> tuple[list[Rule], list[str]]:
    """Return (rules_list, category_order) from the YAML registry.

    Stage gating: every rule receives a ``min_stage`` from the rule-level
    override if present, else the category default, else None (= always fires).
    """
    raw = _load_raw()
    category_order: list[str] = raw.get("category_order", [])
    cat_stage, rule_stage, disabled = _load_stage_overrides()
    skipped = 0
    rules: list[Rule] = []
    for entry in raw.get("rules", []):
        fields = {k: v for k, v in entry.items() if k in Rule.__dataclass_fields__}
        if fields.get("key") in disabled:
            skipped += 1
            continue
        # min_stage precedence:
        #   1. value already on the rule entry (rare; treat as explicit)
        #   2. per-rule override in stage_overrides.yaml (explicit)
        #   3. category default (NOT explicit — lenient bypass applies)
        if fields.get("min_stage") is not None:
            fields["min_stage_explicit"] = True
        else:
            key = fields.get("key") or ""
            cat = fields.get("category") or ""
            override = rule_stage.get(key)
            if override is not None:
                fields["min_stage"] = override
                fields["min_stage_explicit"] = True
            else:
                fields["min_stage"] = cat_stage.get(cat)
                fields["min_stage_explicit"] = False
        rules.append(Rule(**fields))
    if skipped:
        log.info("Registry: suppressed %d disabled rule(s)", skipped)
    return rules, category_order


def active_rules_path() -> Path:
    """Which file is currently being loaded — useful for logging / UI."""
    return _resolve_rules_path()


@functools.lru_cache(maxsize=1)
def rules_fingerprint() -> dict[str, str]:
    """Name + content hash of the active rules file.

    Stamped onto each run's summary at analysis time so that chat-time rule
    lookups can detect that the ruleset has changed since the run was graded
    (the registry is mutable and unversioned — without this, an explanation
    could cite a rule definition that did not produce the verdict).
    """
    path = _resolve_rules_path()
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


# ---------------------------------------------------------------------------
# Finding-source resolution
#
# Only ~21% of findings carry an item_key that exists in the registry (the
# keyword / sheet_existence / electrical_calc rules). The rest are dynamic
# keys minted by the vision prompts in gemini_analyzer (measured on run
# d8a0b104: 78% of 212 findings, all prefixed ai_<family>_). This map lets
# the chat layer answer "what check produced this finding?" for those too,
# by pointing at the prompt that generated it.
# ---------------------------------------------------------------------------

# prefix -> (category, prompt constant name in gemini_analyzer)
_VISION_FAMILIES: dict[str, tuple[str, str]] = {
    "ai_sld_": ("AC Single Line Diagram", "_SLD_PROMPT"),
    "ai_3ld_": ("Three Line Diagram", "_THREE_LINE_PROMPT"),
    "ai_tb_": ("Title Block", "_TITLE_BLOCK_PROMPT"),
    "ai_consistency_": ("Cross-Sheet Consistency", "_CROSS_SHEET_CONSISTENCY_PROMPT"),
    "ai_cover_": ("Cover Sheet", "_COVER_SHEET_PROMPT"),
    "ai_eaf_": ("Equipment Area Feeder Plan", "_EQUIP_AREA_FEEDER_PROMPT"),
    "ai_pole_": ("Pole Line Up", "_POLE_LINEUP_PROMPT"),
    "ai_poledet_": ("Pole Details", "_POLE_DETAILS_PROMPT"),
    "ai_cab_": ("CAB or Cable Hanger Details", "_CAB_DETAILS_PROMPT"),
    "ai_pad_": ("PAD / Slab Details", "_PAD_SLAB_PROMPT"),
    "ai_gnd_deep_": ("Grounding Diagram", "_GROUNDING_PROMPT"),
    "ai_gnd_": ("Grounding Diagram", "_GROUNDING_PROMPT"),
    "ai_gndplan_": ("Overall Site Grounding Plan", "_GROUNDING_PLAN_PROMPT"),
    "ai_support_docs_": ("Supporting Document Consistency", "_CROSS_SHEET_CONSISTENCY_PROMPT"),
    "ai_aux_": ("AUX SLD", "_AUX_SLD_PROMPT"),
    "ai_elev_": ("Elevation Details", "_ELEVATION_PROMPT"),
    "ai_elec_deep_": ("Electrical Sheet", "_ELECTRICAL_SHEET_DEEP_PROMPT"),
    "ai_elec_": ("Electrical Sheet", "_ELECTRICAL_SHEET_PROMPT"),
    "ai_relay_deep_": ("Relay and Inverter Settings", "_RELAY_SETTINGS_PROMPT"),
    "ai_relay_": ("Relay and Inverter Settings", "_RELAY_SETTINGS_PROMPT"),
    "ai_dc_": ("DC Line Diagram", "_DC_DIAGRAM_PROMPT"),
    "ai_comm_": ("Communication Diagram", "_COMM_DIAGRAM_PROMPT"),
    "ai_cfp_": ("Communication Feeder Plan", "_COMM_FEEDER_PLAN_PROMPT"),
    "ai_feeder_": ("Feeder Plan", "_FEEDER_PLAN_PROMPT"),
    "ai_equip_": ("Engineered Equipment List", "_EQUIPMENT_LIST_PROMPT"),
    "ai_labels_": ("Labels", "_LABELS_PROMPT"),
    "ai_siteplan_": ("Site Plan", "_SITE_PLAN_PROMPT"),
}

# Longest prefix first so ai_gnd_deep_ resolves before ai_gnd_, etc.
_VISION_FAMILIES_ORDERED = sorted(
    _VISION_FAMILIES.items(), key=lambda kv: -len(kv[0]))


def resolve_finding_source(item_key: str) -> dict[str, Any]:
    """Explain which check produced a finding, for every kind of item_key.

    Returns one of:
      {"kind": "rule", "rule": Rule}                       — registry rule
      {"kind": "vision_family", "family", "category",
       "prompt_attr"}                                      — dynamic ai_* key;
        prompt_attr names the prompt constant in gemini_analyzer (resolve
        lazily with getattr to avoid importing the vision module here)
      {"kind": "unknown"}                                  — nothing matched
    """
    rule = get_rule(item_key)
    if rule is not None:
        return {"kind": "rule", "rule": rule}
    if item_key.startswith("ai_"):
        for prefix, (category, prompt_attr) in _VISION_FAMILIES_ORDERED:
            if item_key.startswith(prefix):
                return {
                    "kind": "vision_family",
                    "family": prefix.rstrip("_"),
                    "category": category,
                    "prompt_attr": prompt_attr,
                }
    return {"kind": "unknown"}


def get_rules() -> list[Rule]:
    """All rules as a flat list."""
    return load_rules()[0]


def get_category_order() -> list[str]:
    """Ordered list of category names for display."""
    return load_rules()[1]


def get_rules_by_category() -> dict[str, list[Rule]]:
    """Rules grouped by category name."""
    by_cat: dict[str, list[Rule]] = {}
    for rule in get_rules():
        by_cat.setdefault(rule.category, []).append(rule)
    return by_cat


def get_rule(key: str) -> Rule | None:
    """Lookup a single rule by key."""
    for rule in get_rules():
        if rule.key == key:
            return rule
    return None


def get_rules_by_check_type(check_type: str) -> list[Rule]:
    """All rules with a given check_type."""
    return [r for r in get_rules() if r.check_type == check_type]


# ---------------------------------------------------------------------------
# Backwards-compatible exports so existing code can import from here
# instead of checklist.py with minimal changes.
# ---------------------------------------------------------------------------

def get_check_rules_dicts() -> list[dict]:
    """Return rules as plain dicts (same shape as old CHECK_RULES)."""
    return [
        {"key": r.key, "category": r.category,
         "title": r.title, "description": r.description}
        for r in get_rules()
    ]


