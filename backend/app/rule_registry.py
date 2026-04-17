"""Rule Registry – loads QC rules from rules.yaml and provides lookup helpers.

Replaces the hardcoded ``CHECK_RULES`` list in ``checklist.py``.  The YAML
file is loaded once at import time and cached.  Every rule is exposed as a
:class:`Rule` dataclass for type-safe access throughout the codebase.
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_DEFAULT_RULES_FILE = "rules.yaml"


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
def load_rules() -> tuple[list[Rule], list[str]]:
    """Return (rules_list, category_order) from the YAML registry."""
    raw = _load_raw()
    category_order: list[str] = raw.get("category_order", [])
    rules: list[Rule] = []
    for entry in raw.get("rules", []):
        rules.append(Rule(**{
            k: v for k, v in entry.items()
            if k in Rule.__dataclass_fields__
        }))
    return rules, category_order


def active_rules_path() -> Path:
    """Which file is currently being loaded — useful for logging / UI."""
    return _resolve_rules_path()


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


