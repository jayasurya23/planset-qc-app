"""Fixed check-ID registries for the vision families that used to self-name.

WHY THIS EXISTS. Four vision families let the model choose a name for every
finding it returned. Across 26 production runs that produced 623 distinct check
names for 711 ai_sld findings — roughly one name per finding. On triplicate runs
of a single unchanged document those families scored 0% key stability, while
every family presenting a FIXED numbered checklist scored 100%. The same defect
came back under a new name each run, which made run-to-run comparison, per-item
suppression, and any feedback loop impossible. On one project five runs of the
identical PDF produced problem lists whose consecutive overlap fell to 14%, and
the engineers stopped using the tool.

The model's JUDGMENT was never the problem: on keys that did persist, status
agreed 85% of the time. This is an identity problem, so it gets an identity fix.

HOW IT WORKS. Each family owns an ordered list of check IDs (check_ids.yaml).
``checklist_block()`` renders them into the prompt the same way v4_engine does
for the registry rules that are already 100% stable, and ``canonical_key()``
maps whatever the model answers back onto the registry ID — so the item_key is
ultimately chosen by this file, not by the model.

COVERAGE IS NOT TRADED FOR STABILITY. A registry narrower than what the model
actually finds would improve every stability metric while quietly dropping real
defects — the worst outcome for a tool whose false negatives reach construction
sites. Two protections:
  * the registries are derived from the check names production actually
    produced, so nothing already caught loses a home; and
  * every enumerated prompt keeps an ``open_findings`` escape hatch. Those
    findings are content-hash keyed into ``<family>_open_<sha8>``, kept out of
    the key-stability denominator, and COUNTED — if the exploratory rate falls
    to zero after enumeration, that is a regression to investigate, not a win.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent / "check_ids.yaml"

# item_key prefix -> registry family. Several prompts are dispatched twice, once
# single-page and once multi-page (ai_gnd/ai_gnd_deep, ai_relay/ai_relay_deep),
# and BOTH must resolve to the same registry — otherwise the same check mints a
# different key depending on how many matching sheets the planset happened to
# have, which is exactly the churn this file exists to remove.
_PREFIX_TO_FAMILY = {
    "ai_sld": "sld",
    "ai_relay": "relay",
    "ai_relay_deep": "relay",
    "ai_consistency": "consistency",
    "ai_gnd": "grounding",
    "ai_gnd_deep": "grounding",
}

# Canonical key prefix per family. Deliberately NOT the dispatch prefix: only
# one of the single/deep passes usually runs (the deep pass supersedes the
# single one), so keying off the dispatch would make a finding's identity depend
# on sheet count. The dispatch is still recorded in vision_dispatch.
_FAMILY_KEY_PREFIX = {
    "sld": "ai_sld",
    "relay": "ai_relay",
    "consistency": "ai_consistency",
    "grounding": "ai_gnd",
}


def _slug(value: object, limit: int = 24) -> str:
    """Short stable token for an instance discriminator (e.g. 'XFMR-1')."""
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text[:limit]


# What a MISSING subject means for a given check. Without this the model gets a
# circular instruction: "answer Deferred when the subject is not present" is
# exactly wrong for a check whose entire purpose is to notice that something is
# not present (no ground rods drawn, no GEC callout).
#
# The wording is SCOPED on purpose. An unscoped "absence is the defect" would
# override the careful status ladder each instruction already states — a check
# whose parent equipment is simply not on the supplied sheets would be Failed
# for the drawings not containing something they never claimed to show. The
# rendered line therefore defers to the Verify text whenever the parent subject
# is absent, and only bites when the parent IS depicted.
_ABSENCE_RULE = {
    "defect": ("Where the parent equipment or circuit IS depicted on these "
               "sheets, the absence of this item is the defect — report Fail, "
               "not Deferred. Where the parent equipment is not depicted at "
               "all, follow the status ladder in the Verify text above."),
    "not_applicable": ("If this design genuinely has no such equipment, report "
                       "Deferred and say which equipment is absent. Where the "
                       "check's own Verify text states a different Deferred "
                       "trigger (for example that the supplied sheets carry no "
                       "content of this kind at all), that text governs."),
    "unknown": ("If you cannot tell whether the item is missing or merely not "
                "shown on these sheets, report Needs Review and name the "
                "sheets you searched."),
}


@dataclass(frozen=True)
class CheckId:
    id: str
    title: str
    instruction: str
    nec_ref: str | None = None
    aliases: tuple[str, ...] = ()
    absence_is: str = "unknown"


def _normalize(name: str) -> str:
    """Fold a check name to its comparison form.

    Case, punctuation, ordinal prefixes ("5. ") and filler words all vary
    between runs without changing meaning, so none of them may affect identity.
    """
    text = str(name or "").strip().lower()
    text = re.sub(r"^\s*\d+[\.\)]\s*", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


@functools.lru_cache(maxsize=1)
def _load_guidance() -> dict[str, str]:
    """Per-family open_findings guidance from the YAML `guidance:` block.

    Without this the family's own escape-hatch wording — which names the kinds
    of defect the enumerated list cannot anticipate — is compiled away and the
    model only ever sees the generic sentence. The exploratory rate is the
    release gate for enumeration, so weakening its instructions quietly
    weakens the one protection against silent narrowing.
    """
    if not _REGISTRY_PATH.exists():
        return {}
    try:
        raw = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return {k: str(v).strip() for k, v in (raw.get("guidance") or {}).items() if v}


@functools.lru_cache(maxsize=1)
def _load() -> dict[str, tuple[CheckId, ...]]:
    if not _REGISTRY_PATH.exists():
        return {}
    try:
        raw = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — a broken registry must not stop a run
        logger.exception("check_ids.yaml failed to parse — falling back to "
                         "free-form check names")
        return {}
    out: dict[str, tuple[CheckId, ...]] = {}
    for family, entries in (raw.get("families") or {}).items():
        checks: list[CheckId] = []
        for e in entries or []:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            checks.append(CheckId(
                id=str(e["id"]).strip(),
                title=str(e.get("title") or e["id"]).strip(),
                instruction=str(e.get("instruction") or "").strip(),
                nec_ref=(str(e["nec_ref"]).strip() if e.get("nec_ref") else None),
                aliases=tuple(str(a) for a in (e.get("aliases") or [])),
                absence_is=str(e.get("absence_is") or "unknown"),
            ))
        if checks:
            out[family] = tuple(checks)
    return out


@functools.lru_cache(maxsize=8)
def _lookup(family: str) -> dict[str, str]:
    """Normalized name (id + aliases) -> canonical id."""
    table: dict[str, str] = {}
    for c in _load().get(family, ()):  # aliases first so an id always wins
        for alias in c.aliases:
            table.setdefault(_normalize(alias), c.id)
    for c in _load().get(family, ()):
        table[_normalize(c.id)] = c.id
        table.setdefault(_normalize(c.title), c.id)
    return table


def _clear_caches() -> None:
    """Drop every memoized view of the registry (tests swap the file)."""
    _load.cache_clear()
    _lookup.cache_clear()
    _load_guidance.cache_clear()


def family_for_prefix(prefix: str) -> str | None:
    """Registry family owning an item_key prefix, or None if not enumerated."""
    fam = _PREFIX_TO_FAMILY.get(prefix)
    return fam if fam and fam in _load() else None


def checks_for(family: str) -> tuple[CheckId, ...]:
    return _load().get(family, ())


def checklist_block(family: str) -> str:
    """The enumerated checklist to splice into a prompt.

    Mirrors v4_engine's format, which is the one already producing 100% stable
    keys: a numbered line carrying the literal id the model must echo back.
    """
    checks = checks_for(family)
    if not checks:
        return ""
    lines = [
        "=== CHECKS ===",
        "Answer EVERY check below, exactly once, using its check_id verbatim in",
        'the "check" field. Do NOT invent, rename, re-case, pluralise or',
        "paraphrase a check_id. Never skip a check and never stay silent about",
        "one: a reader cannot tell an unanswered check from a clean sheet.",
        "Each check states below what its ABSENCE means — follow that, not a",
        "general rule.",
        "If one check finds several offenders (three transformers, two",
        'feeders), return one finding PER offender with an "instance" field',
        "naming it. Do not merge them into one row — they get fixed separately.",
        "",
    ]
    for i, c in enumerate(checks, 1):
        lines.append(f"{i}. [check_id: {c.id}] {c.title}")
        if c.instruction:
            lines.append(f"   Verify: {c.instruction}")
        if c.nec_ref:
            lines.append(f"   Reference: {c.nec_ref}")
        rule = _ABSENCE_RULE.get(c.absence_is)
        if rule:
            lines.append(f"   If absent: {rule}")
    lines += [
        "",
        "=== ANYTHING ELSE YOU FIND ===",
        "The checks above are not the limit of your review. If you see a real",
        "defect that NO check_id above covers, report it in a SEPARATE",
        '"open_findings" array alongside "findings", using the same object',
        'shape with a short descriptive "check" name. Use it whenever the',
        "drawing shows something wrong that the list did not anticipate —",
        "an unlabelled bus, a stale revision, a value contradicting itself.",
        "Do not force such a defect into an unrelated check_id, and do not",
        "leave it out. Reporting it is required, not optional.",
    ]
    # The family's own escape-hatch wording names the defect classes its
    # enumerated list cannot anticipate. Dropping it would leave only the
    # generic sentence above and weaken the exploratory rate, which is the
    # release gate against enumeration silently narrowing coverage.
    guidance = _load_guidance().get(family)
    if guidance:
        lines += ["", guidance]
    return "\n".join(lines)


_RESPONSE_BLOCK = '''
Return ONE JSON object with two arrays:

```json
{
  "findings": [
    {
      "check": "the check_id, copied exactly from the list above",
      "instance": "which transformer/feeder/circuit this is about, or \\"\\"",
      "status": "Pass|Fail|Needs Review|Deferred",
      "value": "the value you read on the drawing (short)",
      "evidence": "the lookup or comparison you performed, with the numbers",
      "location": "sheet, table and row where you read it",
      "location_text": "short literal searchable excerpt (3-30 chars)",
      "severity": "low|medium|high"
    }
  ],
  "open_findings": [
    {
      "check": "short descriptive name for a defect no check_id above covers",
      "status": "Fail|Needs Review",
      "value": "", "evidence": "", "location": "", "location_text": "",
      "severity": "low|medium|high"
    }
  ]
}
```

Return "open_findings": [] when you found nothing outside the list. Return
both keys always. Return only the JSON object.'''


def compose_prompt(family: str, header: str) -> str | None:
    """Header + enumerated checklist + response schema, or None if unregistered.

    Returning None lets each dispatch keep its hand-written prompt until its
    family is enumerated, so the rollout is per-family and reversible by
    emptying one YAML section.
    """
    block = checklist_block(family)
    if not block:
        return None
    return f"{header.strip()}\n\n{block}\n{_RESPONSE_BLOCK}"


def canonical_id(prefix: str, check_name: str) -> str | None:
    """Registry id a model-returned check name resolves to, ignoring instances."""
    family = family_for_prefix(prefix)
    if family is None:
        return None
    return _lookup(family).get(_normalize(check_name))


def fanout_ids(prefix: str, findings: list[dict]) -> set[str]:
    """Canonical ids the model reported MORE THAN ONCE in one response.

    Only these get an instance suffix. Measured on the grounding pilot: the
    model labels the same subject differently between runs — "XFMR-1" one run,
    "TRANSFORMER_1" the next, "MV_TRANSFORMER" the run after — so suffixing
    unconditionally reintroduced exactly the churn enumeration removes, just
    moved from the check name into the instance label. Suffixing only genuine
    fan-out keeps two co-occurring defects distinct (the reason the
    discriminator exists) while leaving the common single-instance case with a
    bare, reproducible key.
    """
    counts: dict[str, int] = {}
    for idx, f in enumerate(findings):
        if f.get("_exploratory"):
            continue
        name = (f.get("check") or f.get("field") or f.get("title")
                or f"item_{idx}")
        cid = canonical_id(prefix, name)
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    return {cid for cid, n in counts.items() if n > 1}


def canonical_key(prefix: str, check_name: str, finding: dict[str, Any] | None = None,
                  fanout: set[str] | None = None) -> tuple[str, bool]:
    """Map a model-returned check name onto a stable item_key.

    Returns ``(item_key, is_open)``. ``is_open`` marks a finding that no
    enumerated id covered: it keeps a content-derived key so it stays stable
    for as long as its evidence does, and is reported separately rather than
    being silently dropped or forced into an unrelated id.

    Families without a registry keep the previous free-form behaviour, so
    enumeration can be rolled out one family at a time.
    """
    family = family_for_prefix(prefix)
    if family is None:
        # Un-enumerated family: the model still chooses the name, but it should
        # not also choose the CASING. Measured on Bagby v8 vs v9, ai_tb emitted
        # "date"/"Date", "designer_name"/"Designer name", "sov"/"SOV" for the
        # same checks on the same sheet, halving that family's key stability on
        # its own. Folding case and punctuation costs nothing and collapses
        # those pairs; the model's original wording is kept for the title.
        name = str(check_name or "")
        if name.startswith(prefix):
            name = name[len(prefix):].lstrip("_")
        folded = _normalize(name)
        return f"{prefix}_{folded}" if folded else f"{prefix}_{name}", False

    key_prefix = _FAMILY_KEY_PREFIX.get(family, prefix)
    exploratory = bool((finding or {}).get("_exploratory"))
    if not exploratory:
        canonical = _lookup(family).get(_normalize(check_name))
        if canonical:
            # One check id legitimately fires more than once on a sheet — two
            # different feeders can both be undersized. Without a discriminator
            # the per-call dedup would keep the first and silently discard the
            # rest, turning an enumeration into a finding-loss mechanism.
            #
            # But ONLY for genuine fan-out. The model's instance labels are not
            # stable between runs ("XFMR-1" / "TRANSFORMER_1" / "MV_TRANSFORMER"
            # for one subject), so suffixing a single-instance finding just
            # relocates the churn from the check name into the label. When
            # `fanout` is not supplied we cannot tell, so we stay conservative
            # and suffix — a duplicate key would silently drop a finding, which
            # is the worse failure.
            instance = _slug((finding or {}).get("instance"))
            suffix = ""
            if instance and (fanout is None or canonical in fanout):
                suffix = f"__{instance}"
            return f"{key_prefix}_{canonical}{suffix}", False

    # Unrecognised id, or an explicit open_findings entry. Key off the content
    # so the same observation keeps the same key across runs; the raw name is
    # deliberately NOT used, since that is the thing that churns.
    src = finding or {}
    basis = "|".join(str(src.get(k) or "") for k in
                     ("evidence", "value", "location", "location_text")) or str(check_name)
    digest = hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()[:8]
    if not exploratory:
        logger.info(
            "Unrecognised check id %r for family %s — routed to open bucket "
            "(%s_open_%s). If this recurs it belongs in check_ids.yaml.",
            check_name, family, key_prefix, digest,
        )
    return f"{key_prefix}_open_{digest}", True


def key_prefix_for(prefix: str) -> str:
    """Canonical key prefix for a dispatch prefix (identity when unregistered)."""
    family = family_for_prefix(prefix)
    return _FAMILY_KEY_PREFIX.get(family, prefix) if family else prefix


def title_for(prefix: str, check_name: str) -> str | None:
    """Report-ready title for an enumerated check, if it is one."""
    family = family_for_prefix(prefix)
    if family is None:
        return None
    canonical = _lookup(family).get(_normalize(check_name))
    if not canonical:
        return None
    for c in checks_for(family):
        if c.id == canonical:
            return c.title
    return None


def registry_fingerprint() -> dict[str, Any]:
    """Per-family id counts + a content hash, stamped onto each run.

    A registry change alters item_keys, so a run must record which registry
    graded it or a later comparison would report phantom churn.
    """
    if not _REGISTRY_PATH.exists():
        return {"families": {}, "sha256": None}
    return {
        "families": {f: len(c) for f, c in _load().items()},
        "sha256": hashlib.sha256(
            _REGISTRY_PATH.read_bytes()).hexdigest(),
    }
