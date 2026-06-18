# Rules-engine audit — cross-engine duplicate findings

*Why the same engineering concept gets flagged 2–4 times, the complete inventory,
and a coverage-safe plan to stop it. Guiding constraint: **no check coverage may
be lost** — only duplicate instances of a concept are removed, never a concept.*

---

## 1. Root cause: five accreted check layers

The QC pipeline grew by accretion — newer check engines were added without
retiring the ones they overlap. Five layers now run, each independently
re-checking many of the same concepts:

| Layer | item_key prefix | Defined in | Nature |
|---|---|---|---|
| Per-category AI vision | `ai_*` | `gemini_analyzer.py` | Reads the drawing |
| Multi-page "deep" AI | `ai_*_deep` | `gemini_analyzer.py` | **Re-runs the same prompt** over several pages |
| Deterministic keyword/content | `cover` `title` `gnd` `elec` `electrical` `three` `sld` `pad` `relay` `pole` … | `analyzer.py` | Exact, free text-layer match |
| V4 rule engine + cross-ref + stage | `v4_*`, `xref`, `stage` | `v4_engine.py` + `rules*.yaml` | Structured rules (only when a V4 ruleset is active) |
| Math checks | `calc_*` | `rules_v4_draft.yaml` (`electrical_calc`) | Deterministic arithmetic |

Empirically (mined across 13 runs) **almost every category is touched by 2–4 of
these layers.**

---

## 2. Complete cross-engine duplicate inventory

| Category | Concept(s) duplicated | Layers that overlap |
|---|---|---|
| **Grounding Diagram** | main bonding jumper (250.102), GEC (250.66), EGC (250.122), ground rods, transformer grounding (primary/secondary), grounding ring, code references, rack/CAB grounding | `ai_gnd` + `ai_gnd_deep` + `gnd` |
| **System Information (E-001)** | module count; total DC capacity; total AC capacity; DC/AC ratio | `calc_*` + `v4_e` + `xref` |
| **Cover Sheet** | project name/address; owner name/address/phone; EPC name/address/phone | `ai_cover` + `cover` (+ `ai_tb` title-block re-read) |
| **Electrical Sheet** | voltage-drop summary; conduit fill; schedule ampacity/FLA/OCPD | `ai_elec` + `ai_elec_deep` + `elec`/`electrical` |
| **Three-Line Diagram** | CT/VT arrangement; transformer grounding; equipment name/rating vs SLD | `ai_3ld` + `three` (+ `ai_consistency`) |
| **Relay & Inverter Settings** | IEEE-1547 trip settings | `ai_relay` + `ai_relay_deep` (identical prompt) |
| **E-900** | module datasheet current/approved; inverter datasheet current/approved | `v4_e` (two variants) + `xref` |
| **E-002 / E-011** | DC conductor color convention; GOAB / ESB pole sequence | `v4_e` (two variants each) + `xref` |

---

## 3. Remediation — two tracks, by coverage-risk

### Track A — Output consolidation (shipped, provably lossless)
`dedup_findings.consolidate_overlapping_findings` (wired in `analyzer.py` after
the cross-sheet filter) merges findings in the same category that share a concept
signature **and** come from ≥2 different engine families. It keeps the **most
severe** status (a real Fail is never hidden) plus the richest evidence, and notes
the consolidation.

**Coverage guarantee (verified on all 13 runs):** the set of distinct
*(category, concept)* coverage units is **identical before and after** — zero
concepts, categories, or Fails lost; only duplicate *instances* are removed.
Currently covers: all grounding NEC items, CT/VT, meter accuracy, module count,
DC/AC ratio, total DC/AC, voltage drop, conduit fill, module/inverter datasheet.
This is the safe net for every overlap where the engines are equally uncertain.

### Track B — Source-level retirement (recommended; each gated on a coverage check)
These stop the duplicates from being *generated* (and save AI calls), but each
risks coverage if done blindly, so each must pass the same
*concepts-before == concepts-after* gate on real runs before shipping.

1. **Make single-page vs deep AI checks mutually exclusive.** `ai_gnd`/`ai_gnd_deep`
   and `ai_relay`/`ai_relay_deep` send the *identical* prompt; `ai_elec`/`ai_elec_deep`
   cover the same sections. The single-page check runs on a page already inside the
   deep multi-page set, so gating the single-page **off when the deep runs** is
   lossless and saves an AI call. *(gemini_analyzer.py: deep dispatch ~:2787/:2802/:2814.)*
2. **Let `calc_*` own the System-Info math** (module count, total DC/AC, DC/AC ratio)
   and retire the `v4_e_001_*` vision variants via the existing `disabled_rules`
   list in `stage_overrides.yaml` — calc is deterministic and exact. *(Confirm the
   active ruleset is a V4 set first; the default `rules.yaml` has no `v4_*`.)*
3. **Collapse within-V4 variant pairs** (E-900 datasheet current vs current-approved;
   E-002 color; E-011 pole sequence) — keep one rule per concept via `disabled_rules`.
4. **Drop the `*_legacy` supplement prompts** (`ai_*_legacy`) when a V4 ruleset is
   active — V4 already covers those sheets.
5. **Cover Sheet / Title Block:** the deterministic layer reads title-block text
   *exactly and for free*, so it should own field *presence*; trim the `ai_cover`
   prompt to only what needs the drawing read. (Do **not** simply delete the
   deterministic checks — that exact text read is unique coverage.)
6. **Dead code:** `_SYSTEM_INFO_PROMPT` is defined but never dispatched — remove.

### What each layer uniquely contributes (so we never retire unique coverage)
- **Deterministic keyword/procedural** — exact, free, hallucination-proof reads of
  sheet numbers, titles, drawing-index, and presence-of-field. **Keep.**
- **AI vision** — actually reads symbols/values off the diagram. Owns diagram concepts.
- **`calc_*`** — exact arithmetic on extracted specs. Owns the math.
- **`xref` / cross-sheet** — value *agreement across sheets* (a thing no single-sheet
  check sees). **Keep** for cross-sheet, retire only where it merely re-states a calc.

---

## 4. Status
- **Track A: live** — grounding + the system-info/electrical/datasheet concepts are
  consolidated on every analysis, proven lossless. 24 unit tests in
  `scripts/test_dedup_findings.py`.
- **Track B: backlog** — the source retirements above, each to be shipped only after
  its coverage-equivalence check passes on the run corpus. Item 1 (single/deep
  mutual exclusion) is the highest-value, lowest-risk next step.
