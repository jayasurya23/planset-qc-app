"""Backwards-compatible shim – imports from the new rule registry.

Existing code that does ``from .checklist import CHECK_RULES, CATEGORY_ORDER``
continues to work without changes.  New code should import from
``rule_registry`` directly.
"""

from __future__ import annotations

from .rule_registry import get_category_order, get_check_rules_dicts

CHECK_RULES: list[dict] = get_check_rules_dicts()
CATEGORY_ORDER: list[str] = get_category_order()
