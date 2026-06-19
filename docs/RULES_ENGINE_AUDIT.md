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

1. **✅ SHIPPED (PR #21) — Make single-page vs deep AI checks mutually exclusive.**
   `ai_gnd`/`ai_gnd_deep` and `ai_relay`/`ai_relay_deep` send the *identical* prompt;
   `ai_elec`/`ai_elec_deep` cover the same sections. The single-page check runs on a
   page already inside the deep multi-page set, so gating the single-page **off when
   the deep runs** is lossless and saves an AI call. Implemented as
   `_deep_supersedes_single(single, deep, cap)` — skip the single **only** when the
   deep will run (its set has >1 page) AND the single's page ∈ `deep[:cap]`; otherwise
   the single is kept, so coverage can never be lost. *(gemini_analyzer.py: gates at
   `ai_elec`/`ai_gnd`/`ai_relay` dispatch.)*
   **Empirical proof (re-analyzed run `8345e356` → `9bd51774`):** Grounding collapsed
   from 26 findings across 4 engines (`ai_gnd`:9, `ai_gnd_deep`:10, `gnd`:5,
   `grounding`:2) to **10** (`ai_gnd_deep`:9, `grounding`:1) with **0 grounding
   concepts lost**; Electrical & Relay categories lost 0 concepts; run total 221→189
   (−32 duplicate instances). The 5-test gate suite + full suite are green.
2. **❌ REJECTED on coverage-safety review — do NOT retire `v4_e_001_*` in favor of
   `calc_*`.** On close reading the two are *not* equivalent. `calc_*` checks only
   internal arithmetic on extracted specs (`calc_total_dc_math`: DC kW = Module Qty ×
   STC W within 2%; `calc_module_count_consistency`: count = String Size × String Qty).
   The `v4_e_001_*` vision rules bundle **cross-source agreement and limit checks the
   math does not perform**: `total_module_count` requires *five* sources to agree
   (stringing calc / PVCase / E-050 / PVSyst); `dc_ac_ratio` adds ≤ inverter-warranty
   limit, ≤ client-BOD limit, and cross-set consistency; `total_ac_capacity` adds
   = E-050 and interconnection-agreement match. Disabling them would **lose** that
   coverage. The correct, lossless handler for this overlap is the already-shipped
   **Track-A consolidation** (its keyword groups already cover `module_count`,
   `dc_ac_ratio`, `total_dc_capacity`, `total_ac_capacity` — it keeps most-severe +
   richest evidence, so the union of coverage survives in one finding). *Also note this
   overlap is **V4-only**: in production `RULES_FILE` is unset → `rules.yaml` (legacy,
   not a V4 set) → `calc_*`/`v4_*`/`xref` never dispatch, so there is no live duplicate.*
3. **Collapse within-V4 variant pairs** (E-900 / E-002 / E-011) — **largely already
   done**: `stage_overrides.yaml`'s `disabled_rules` has a curated "V3-vs-V4-ENH
   duplicate sweep" plus targeted entries that already retire the within-V4 variant
   pairs (Jaccard-similarity sweep, keeping the V4-ENH variant). V4-only and inert in
   production. Re-audit only if a new ingest re-introduces variants.
4. **Drop the `*_legacy` supplement prompts** — **moot in production**: no `ai_*_legacy`
   findings appear in live runs (V4 ruleset not active). Revisit only if/when a V4 set
   is enabled.
5. **Cover Sheet / Title Block — DEFERRED (granular-vs-bundle, ~0 NR impact).** The
   overlap is real (`ai_cover` per-field vs deterministic `cover_sheet_*` field
   *bundles*; `ai_tb` per-page vs `title_block_*` "every page" checks) but it is **not**
   a clean 1:1 same-concept duplicate: the deterministic checks *bundle* fields the AI
   checks individually (`cover_sheet_epc_info` = "EPC Name, Address, Telephone" vs three
   separate `ai_cover` fields). A signature-merge would over-merge distinct fields and
   *reduce* granularity — a coverage loss. It is also almost entirely **Pass** findings
   (in the audited run: 45 Pass / 1 NR / 3 Fail across both categories), so it does not
   drive the Needs-Review inflation this audit targets. Leave to a future
   *subsumption* pass (drop the redundant bundle only when all its granular AI fields
   are present), not the merge mechanism.
6. **✅ DONE — Dead code removed.** `_SYSTEM_INFO_PROMPT` (a ~150-line system-info vision
   prompt defined but never dispatched) deleted from `gemini_analyzer.py`; imports +
   full suite green.

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
- **Track B item 1: ✅ live** — single/deep AI mutual exclusion shipped (PR #21) and
  empirically proven lossless on a real re-analysis (grounding 26→10 findings, 0
  concepts lost; run 221→189). 5 gate unit tests in `scripts/test_page_targeting.py`.
- **Track B item 2: ❌ rejected** — retiring `v4_e_001_*` for `calc_*` is *not*
  lossless (v4 adds 5-source agreement + warranty/BOD limits + interconnection match);
  Track-A consolidation already handles this overlap. V4-only / inert in prod anyway.
- **Track B item 3: ✅ already handled** by the existing `disabled_rules` V3-vs-V4-ENH
  sweep (V4-only).
- **Track B item 4: moot** — no `*_legacy` prompts fire in production (V4 not active).
- **Track B item 5: deferred** — Cover/Title overlap is granular-vs-bundle (over-merge
  would lose granularity) and ~0 NR impact; needs a subsumption pass, not a merge.
- **Track B item 6: ✅ done** — dead `_SYSTEM_INFO_PROMPT` removed.

## 5. Production outcome (default `rules.yaml`, legacy AI + deterministic engines)
After item 1 + Track A, the live re-analysis (`9bd51774`) shows the cross-engine
duplicate problem is **resolved for the production configuration**: **28 Needs-Review,
every one a *distinct* item from a *single* engine — zero residual same-concept
multi-engine duplicates** (down from 41 NR on the original `8345e356`). The remaining
NRs (SLD 10, 3LD 8, Grounding 4, Electrical 3, Labels 2, Cover 1) are genuine review
items, not duplicates. Items 2–4 only ever applied to the non-default V4 ruleset
(`RULES_FILE=rules_v4_draft.yaml`), which production does not use.

> **Verification note:** the coverage check compares the *concept set* of a finding
> using `concept_signature`. For AI vision engines (e.g. `ai_sld`) the model phrases
> findings differently on each run and does not always reprint a literal "NEC 250.x"
> article number, so a per-run concept-set diff can show spurious "losses" in
> *untouched* categories from ordinary model non-determinism. Scope the
> coverage check to the categories the change actually gates (and confirm the concept
> is still covered under a different phrasing) rather than reading a raw global diff.
