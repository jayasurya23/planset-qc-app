# Production QC Audit — False-Positive Root-Cause Analysis

**Scope:** All 26 production runs, May 28 – Aug 3, 2026 (`D:/tmp/prod_audit_dump_dl.json`, 5,250 findings, 11 distinct latest projects), cross-referenced against source at `backend/app/` (analyzer.py, gemini_analyzer.py, electrical_calcs.py, v4_engine.py, rules.yaml, stage_overrides.yaml) and the firm's QC checklist and checker's handbook references.

---

## 1. Executive summary

Of 418 hard Fails across 26 production runs, **at least ~185 (44%) are confirmed mechanical false positives** — findings a checker would discard in the first minute — and the central estimate including probable render artifacts is **~50%**. The top three causes by measured volume are: **(1) page misrouting** — a `find_pages` keyword collision ("EQUIPMENT PAD" in both the PAD and EAF lists, both taking match `[0]`) that ran structural-pad and feeder-plan checks on inverter zone maps, ~112–128 Fail+NR findings; **(2) nine "repeat-offender" rules** that fire on nearly every project by construction — stub calc functions that return Needs Review unconditionally, a `found:false→Fail` mapping with no not-applicable channel (DER number, SOV field: 58 hard Fails on fields the plansets legitimately omit), and an invented "no cable sizes on the 3LD" requirement that fails 7/7 professionally stamped 3LDs — ~150 Fail+NR; **(3) absence claims at zoom-1.5 render resolution**, ~129 findings flagging fine-print values (%Z, BIL, kcmil) the model plausibly could not see, 25 of which say "not legible" outright and were still counted. The Wave-1 prompt fixes (MCOV/BIL/KNAN) and the rule-9b decimal guard are verified working in the Aug 3 Bagby runs (v2 cleared the 4,779.000 kW false Fail). The `_volts()` bare-kV fix is deployed — verified live in the production container before the v3 re-run — but **has never been exercised by a production run**: v2 predates it (its AC schedule passed at 5,113 A per-inverter FLA — 34.5-volt arithmetic on a 34.5 kV system), and v3's extraction failed to capture `poi_voltage` at all, so every voltage-dependent calc deferred and the corrected path never executed. The extraction flicker is masking the fix. Zero engineer overrides and zero feedback rows across all 5,250 findings confirm the cost: engineers ran each project once and never came back. **The single most important next step is to ship the built-but-unreleased fixes to the production container and verify with a Bagby re-run** — every one of the 24 pre-August runs predates every fix, and two of the three top causes are one-file changes that are already written or scoped.

---

## 2. The numbers

### Corpus

| Metric | Value |
|---|---|
| Runs | 26 (May 28 – Aug 3, 2026) |
| Distinct latest projects | 11 |
| Total findings | 5,250 |
| Pass / Needs Review / Deferred / Fail | 3,503 / 718 / 611 / 418 |
| Fail + NR (the "bad list" engineers see) | 1,136 |
| Findings with absence-claim evidence (broad pattern; narrow "not shown/missing/not visible" gloss alone = 163) | ~306 (27% of Fail+NR) |
| Engineer overrides (status ≠ auto_status) | **0 of 5,250** |
| issue_feedback / run_feedback rows | **0 / 0** |

### Fail+NR problem-rate by family (all runs)

| Family | Fail+NR / total | Rate | Rate after removing confirmed wrong-page + resolution artifacts |
|---|---|---|---|
| ai_pad | 56/63 | 88% | **29%** |
| elec_ (keyword) | 36/40 | 90% | — (family is structurally NR; see §3.4) |
| ai_gnd | 22/28 | 78% | — |
| relay_ (keyword) | 20/30 | 66% | — |
| gnd_ (keyword) | 23/40 | 57% | — |
| ai_3ld | 66/120 | 55% | 35% |
| ai_eaf | 59/114 | 51% | ~17% (10/59 on-page) |
| ai_gnd_deep | 38/107 | 35% | — |
| electrical (calcs) | 49/155 | 31% | — (93% of family verifies nothing; §3.4) |
| v4 | 259/955 | 27% | — |
| ai_sld | 185/711 | 26% | 16% |
| ai_relay | 46/174 | 26% | — |
| ai_tb | 45/448 | 10% | — |

### False-positive fraction among the 418 hard Fails

**Method:** sum distinct, evidence-confirmed mechanical failure classes (no double counting across classes); treat cannot-determine as *not* FP.

| Class | Fails | Basis |
|---|---|---|
| Wrong-page routing (ai_pad 42, ai_eaf 49, ai_elev 22) | ~113 | Evidence self-identifies wrong sheet ("this is an inverter zone map only" → Fail) |
| SOV title-block field (disabled rule, prompt still asks) | 42 | Rule disabled in rules.yaml:127-134 since initial commit; free-named vision findings bypass it |
| DER number (model-acknowledged skip → Fail) | 16 | Evidence literally says "this check is skipped" / "no finding is emitted" |
| 3LD "no cable sizes" (invented requirement) | 15 | Contradicts firm's own checklist; fails 7/7 stamped 3LDs |
| **Confirmed FP floor** | **~186 (44%)** | |
| + probable resolution-driven absence Fails | +20 | Fine-print values at zoom 1.5 (~3px text) |
| + stage-inappropriate Fails on 60% sets | +7 | Fault current, pile depth, PE stamp before IFC |
| **Central estimate** | **~213 (51%)** | |

**Uncertainty:** the remaining ~205 Fails include 75 "plausibly genuine" absence Fails (class 4, §3.3) that have never been adjudicated by a human — zero ground truth exists. So Fail precision is **at most ~50–56% and unverified below that**. The 718-NR pool is worse in a different way: at least ~270 NRs (stub calcs 49, keyword families ~79, single-sheet cross-questions 36, resolution-driven 109) are structurally incapable of resolving and convey no information about the drawing.

---

## 3. Root causes, ranked by measured FP volume

### 3.1 Repeat-offender rules that fire by construction — ~165 findings, ~150 Fail+NR (13% of the entire bad pool) — **OPEN**

Three mechanisms, none touched by any deployed fix (Bagby d8ed57b7, Aug 3, newest code in prod, exhibits every one):

**(a) Stub calc functions that cannot pass.** `validate_conduit_fill` (electrical_calcs.py:1013-1022) reads nothing from the planset and unconditionally returns NR. `validate_voltage_drop` (electrical_calcs.py:972-1010) formats system size into a template and returns NR — and rules.yaml binds it to **two** item_keys (`electrical_voltage_drop`, rules.yaml:1573; `electrical_vd_client_criteria`, rules.yaml:1662), so every run gets the identical NR twice. Result: `electrical_conduit_fill_40pct` flags **11/11 projects** (19 findings, byte-identical evidence), the VD pair flags 9/11 and 8/11 (30 findings). Sample, run d8ed57b7:

> "Conduit fill must be < 40% for 3+ conductors per NEC Chapter 9 Table 1. Verify from electrical schedule that all conduit runs show fill percentage below 40%."

That is the checklist item restated at the reviewer, not a finding. Note also the code basis: NEC 210.19(A) IN No. 4 / 215.2(A)(4) IN 2 are **informational** — VD is a design criterion, not an enforceable code limit, and no "client criteria" input field exists anywhere in the system.

**(b) `found:false → Fail` with no N/A channel** (gemini_analyzer.py:1849-1852). The cover prompt says a missing DER number is conditional ("DO NOT emit a finding"); the model emits `found:false` with a note that it is skipping, and the mapper converts the model's own "not a defect" into a medium-severity Fail. 16 Fails across 10/11 projects. Run 8599cfe3 (Rock Run): status=**Fail**, evidence: *"No DER number is shown on the cover sheet, so this check is skipped."* Same defect drives the SOV field: `title_block_sov_date_designer` is **disabled** in rules.yaml:127-134 ("not all plansets use this field") — but the disable is a no-op because _TITLE_BLOCK_PROMPT item 1 (gemini_analyzer.py:638) still asks, and vision findings free-name (`ai_tb_p1_SOV`) past the registry. 42 Fails of 56 SOV findings, 11/11 projects, and the Passes are hallucinated rationalizations (Coal City: "Value: CASTILLO PROJECT ID"; Bagby: "Value: APPENDIX SHEET 3"). A DER number is a utility-assigned interconnection ID and SOV is a title-block template field — but both ARE line items in the firm's own SOP-001 checklist, so the right disposition is not to drop them: it is to stage-gate them (informational/NR at 30/60%, genuine check at IFC when the interconnection data exists) and to fix the skip→Fail mapping so the model's own "not applicable" stops being converted into a defect.

**(c) Cross-sheet questions in single-sheet context.** The 3LD prompt asks "do ratings match the SLD?" but the dispatch (gemini_analyzer.py:2856-2864) sends only `tld[:3]` — the SLD is never in the call. 14/15 findings are the model correctly saying it cannot compare: guaranteed NR, 7/7 projects. Same wiring in `_LABELS_PROMPT` vs `lb[0]`-only dispatch (gemini_analyzer.py:2992): "match the calculation sheet?" with no calc sheet → 22 perpetual NRs across 6 projects. And criterion 2 of the 3LD prompt (gemini_analyzer.py:892) — *"The 3LD should NOT show cable sizes or FLA"* — is an **invented requirement**: no NEC provision prohibits information on a drawing, the Castillo checklist says the opposite ("Three Line Diagram: all SLD items apply", which include cable sizing), and every stamped 3LD in production shows FLA. 15 Fails, 7/7 projects, 100% FP rate. The Wave-1 commit acknowledged this contradiction but fixed only the keyword rule's title, not the vision prompt.

### 3.2 Page misrouting (`find_pages` keyword collision + `[0]`-taking) — 112 confirmed, ceiling 128 Fail+NR (~10-11% of pool) — **PARTIALLY FIXED, insufficient**

"EQUIPMENT PAD" appears in both the ai_pad list (gemini_analyzer.py:3022) and the ai_eaf list (:2998); both take `[0]`. On Rock Run, **both families ran on page 13, the E-200 Inverter Zone Map** — all seven pad checks (rebar, PSI, anchors, edge, drainage…) and the EAF checks failed for content that sheet type can never show. Same on E1300 (E-209 feeder plan) and Coal City (E-218). In all 9 runs where both families emitted, they reviewed the **identical page** — structurally at most one can be on-topic. Run c7bf5d98:

> ai_pad_legacy_Rebar Schedule: "Location: Sheet E-200 (Inverter Zone Map)… No pad/slab detail or rebar schedule is shown on this sheet." — **Fail**

The same pattern hits `ai_elev` (bare "ELEVATION" grabs CAB-hanger and MV-equipment elevations, then fails array-elevation items; note the prompt side was made scope-aware on Jun 23 (d22678d, deployed) — the surviving defect is the routing keyword only — 22 Fails on Coal City/Trigo, including two findings whose own evidence concludes *"the shown clearance dimensions far exceed the NEC 110.26(A)(1) minimums… no deficiency is present"* yet ship as **Fail** — a status-mapper inversion) and `ai_gnd` (gnd[0] = Overall Site Grounding plan, NR'ing NEC 250.66/250.102(C)(1) items that `ai_gnd_deep` simultaneously *found* on E-501 in the same run — contradictory findings in one report).

**Status:** the `exclude=("GROUNDING",)` fix in sha d9703001 does not touch this collision — both post-fix Bagby runs (ceb87623, d8ed57b7) still show ai_eaf misrouted onto the page-26 pad structural detail (8 wrong-page Fails post-fix). The `_deep_supersedes_single` guard (gemini_analyzer.py:137-152) has been deployed since June 19 (PR #21) and was in the Aug 3 container — yet the ai_gnd/ai_gnd_deep contradiction still occurs, so the guard's gate (the single-page check is skipped only when gnd[0] falls within the deep pass's first 3 pages) does not cover this case. The fix is repairing the gate condition, not releasing the guard. Double damage: the hijacked sheet gets false Fails **and** the true pad-detail sheet, if Rock Run has one, was never opened in any of its 5 runs — real defects there are invisible.

### 3.3 Absence claims at render resolution, and absence-as-Fail policy — 306 absence findings; 129 resolution-driven, 147 absence-Fails at ≤51% precision — **OPEN**

Multi-page vision calls rasterize E-size sheets at zoom 1.5 (gemini_analyzer.py:2029-31) vs 2.0 single-page; API-side downsampling leaves 3/32" text ~3px tall. All 129 class-2 findings are absences of *fine-print* values (%Z, X/R, BIL, kcmil, MPPT ranges, tempco, CT ratios) in multi-page zoom-1.5 families — 25 say so outright (*"maximum DC input voltage is not legible on this image"*, Coal City b3402a42) and were still counted as NR/Fail. The fingerprint is unambiguous: ai_sld 58, ai_3ld 20, ai_dc 12 (all multi-page) vs near-zero in single-page zoom-2.0 families, and the same evidence strings read the large text fine while only the small ratings block goes "missing."

The policy question: 147 of 418 Fails (35%) rest on absence evidence from a model with a demonstrated *presence*-misread record (the "4,779.000 kW" decimal case; the bare-kV corruption). An affirmative misread and a negative claim are epistemically different — the negative asserts the model exhausted the sheet at a zoom where it demonstrably cannot read dimension text. Classes 1–3 alone (72 artifact-Fails) cap absence-Fail precision at ~51% before the 97 "plausibly genuine" class-4 findings are even audited. A human checker marks "not found — confirm," never a red-line, until a second look. Removing classes 1–2 cuts the worst-run Fail count (Rock Run cc55b1e0) from 33 to 22 and drops ai_pad from 89% to 29%, ai_dc from 36% to 10% — **most of the tool's reputation damage is these two mechanical classes, not model judgment.**

### 3.4 Deterministic calc/keyword layer verifies almost nothing — 144/155 electrical findings + 79 keyword NRs — **OPEN, plus one false-negative hazard**

Of 155 `electrical_*` findings: 49 NR (100% unconditional boilerplate, §3.1a), 58 Deferred (100% extraction gaps — `module_isc`, inverter kVA, transformer voltages; the Bagby lineage proves the values are on the drawings: v2 extracted them, v1 and v3 deferred on the *same PDF*), and **37 unconditional Passes**: `validate_ac_ampacity` (electrical_calcs.py:822), `validate_mv_ampacity` (:862-868), `validate_egc_sizing` (:908-913) compute a reference minimum and return Pass without ever reading the drawing's actual conductor/OCPD/EGC. **Zero Fails in 26 runs** — believable only because the checks cannot fail. The hazard is real: Bagby ceb87623 (Aug 3) **Passed** `electrical_ac_schedule` with *"Per-inverter FLA = 5113.4A; total FLA = 71587.7A; min OCPD = 89485A; min wire = #600 CU (224 parallel sets)"* — 34.5-volts arithmetic on a 34.5 kV system, ~1000× wrong, plus a silent table-overflow: the corrupted OCPD (≈6,392 A) exceeds Table 250.122's largest row (6,000 A → 800 kcmil Cu), and the fallback at :900 clamps to that largest row instead of erroring — Table 250.122 simply does not apply above 6,000 A, so the check should raise Needs Review, not fabricate a minimum. A Pass that verified nothing is worse than a wrong Fail.

The keyword families (`elec_` 36/40 NR, `gnd_` 23/40, `relay_` 20/30) are token counters miscalibrated against Castillo's own sheet vocabulary: `elec_pv_params_content` NRs 7 projects with "Found: IMP, STC" (needs 3 hits, but Voc/Vmp/Isc live on the SLD module table, outside the rule's page scope); `gnd_gec_content` is NR 8/9 with "Found: none" because grounding one-lines call out "#4/0 BARE CU," not the literal token "GEC" — while `ai_gnd` on the same sheets independently validated GEC/MBJ sizing with real callouts. Five professional firms' plansets do not all lack PV parameters; the grep is wrong. Each keyword rule is also redundant with a calc twin and a vision twin (VD has **four** overlapping checks).

### 3.5 Stage-inappropriate demands on 60% submittals — ~103 clearly premature Fail+NR; 394 of 462 (85%) of 60%-run findings come from ungated families — **OPEN**

Stage gating exists and works — but only for v4 rules (v4_engine.py:846-875; 123 stage-deferrals on 60% runs, 0 on IFC). Every legacy family ignores `design_stage` entirely (rules.yaml has zero `min_stage` keys). So a 60% permit set gets Fails for ultimate fault current on the pole lineup (utility impact-study data, post-60%), pad rebar schedules (structural package, IFC per the firm's own stage_overrides.yaml: E-214–E-217 → IFC), arc-flash label values (final short-circuit study), and a PE stamp (IFC-only). ai_3ld runs 75% Fail+NR at 60% vs 25% at IFC. The firm already encoded the policy in stage_overrides.yaml; the legacy dispatch just never reads it. Caveat: stage labels themselves are unreliable — Trigo's 60% markup set ran twice as IFC (bare "FOR CONSTRUCTION" regex, analyzer.py:1149), and both Coal City runs are labeled 60 on a project named "Coal city-1 90%."

### 3.6 Extraction corruption and model misreads — small count, outsized blast radius — **FIXED IN SOURCE, UNVERIFIED IN PROD**

Three confirmed modes for `poi_voltage` alone: missing (11/16 VD evidence strings read "?V POI"), bare-kV ("34.5" treated as volts → NEC 110.26 clearances computed as 3/3/3 ft instead of 5/6/9 for Condition 3 at 34.5 kV, BIL 95 instead of 150, MV FLA 41,837 A instead of 41.8 A), and mis-attribution (Aurora 2: "1100.0V POI" — an inverter DC/LV figure; the `_volts()` <100→kV heuristic correctly leaves 1100 alone, so only provenance-aware extraction catches it). The decimal misread guard (rule 9b) is deployed and verified — Bagby v2 cleared the "4,779.000 kW" false Fail. `_volts()` normalization (electrical_calcs.py:178-203) is deployed and was verified live in the production container (in-container check returned the correct 5/6/9 ft clearances) — but no production run has exercised it yet: v2 predates it, and v3's extraction missed `poi_voltage`, deferring every dependent calc. (Precision note: above 1,000 V the governing working-space article is NEC 110.34, not 110.26 — Table 110.34(A) Condition 3 at 34.5 kV, 19.9 kV to ground, gives the 5/6/9 ft values cited.)

### 3.7 Free-naming key churn — 98% of run-to-run key instability — **OPEN**

Four prompt families (ai_sld, ai_relay, ai_consistency, ai_gnd_deep) mint item_keys freely. Post-fix Bagby v2 vs v3 (18 minutes apart, same file, same sha): judgments agree, but the transformer-cooling check appears as `ai_sld_transformer_1_cooling_fluid_class_validation` vs `ai_sld_transformer_cooling_class_and_fluid_consistency`. Churn is orthogonal to the prompt fixes and defeats any compare/suppress/feedback feature built on item_key identity. Also: `ai_tb_p{N}_` bakes page numbers into keys, and confidence is decorative — 3,690 findings carry exactly 0.72 (hardcoded, gemini_analyzer.py:1963, 2187), so the report cannot be sorted by believability.

---

## 4. What the engineers' behavior tells us

**Zero of 5,250 findings ever overridden; zero feedback rows.** Not low engagement — *no* engagement, including from the developer. Roughly 10 of 26 runs were launched by actual QC engineers (Manjil ×3, Kyler ×2, Manjil Puri ×2, Sam, Pradeep ×3); every engineer ran each project exactly once and never returned. No project except dev-driven Nessler/Bagby has an engineer-initiated rerun.

**Nessler v1–v5 is the trust story in miniature.** Five runs of the identical 32-page PDF produced 52/50/37/38/44 bad findings with **no convergence**; consecutive-run Jaccard overlap of bad item_keys: 0.32, 0.19, 0.36, **0.14**. Of 134 distinct bad keys, 88 (66%) appear in exactly one run. The only 9 keys stable across all five runs are the known false-positive floor (the VD/conduit stubs, labels cross-sheet NRs, pole fault-current). So the reproducible part of the report is the noise, and the real-looking part churns. An engineer who fixed everything from run N would see a mostly new list in run N+1 and rationally conclude the list is random.

**The one piece of ground truth arrived out-of-band.** The E1300 engineer's MCOV/BIL/KNAN feedback — detailed, correct, PE-grade — came as a message to the developer, bypassing the in-app mechanism entirely. Engineers *will* give ground truth through channels they already use. They will not do uncompensated per-item data entry into a report they distrust, with no visible payoff. Every finding classification in this audit is the only ground-truthing these 5,250 findings have ever received.

---

## 5. What is already fixed — and where it actually lives

This distinction is the crux: **all 24 pre-August runs predate every fix.** Only the two Aug 3 Bagby runs (ceb87623, d8ed57b7) carry rules_sha d9703001, and rules_sha stamps rules.yaml, not calc code.

| Fix | Where it lives | Verified in production? |
|---|---|---|
| Wave-1 prompts: MCOV grounding (7.65 kV MCOV correct for 9 kV arrester on 12.47Y/7.2 kV per IEEE C62.22), BIL {95,110} both standard for 15 kV class, KNAN = correct IEC 60076-2 ester designation | Deployed (live by Bagby v1, Jul 30) | **YES** — all three E1300 FPs now Pass with correct reasoning on Bagby ("FR-3 is a K-class ester fluid, and KNAN is the correct ester-equivalent cooling designation") |
| Rule 9b decimal misread guard | Deployed | **YES** — Bagby v2 (ceb87623) cleared the 4,779.000 kW false Fail |
| `_volts()` bare-kV normalization (electrical_calcs.py:178-203) | Deployed (verified live in-container before the v3 re-run) | **NO — deployed but never exercised:** v2 (ceb87623) ran pre-fix (its FLA=5,113.4 A Pass back-solves to 34.5-volt arithmetic); v3 (d8ed57b7) had the fix but extraction missed `poi_voltage`, so every voltage calc deferred. The Phase 2 extraction retry (uncommitted) is what makes this path reliably reachable. |
| ai_pad `exclude=("GROUNDING",)` | Deployed (d9703001) | Partially — does not fix the EQUIPMENT PAD collision; ai_eaf still misrouted on both post-fix Bagby runs |
| `_deep_supersedes_single` guard (ai_gnd vs deep duplication) | Deployed since Jun 19 (PR #21, in the Aug 3 container) | Gate has a hole — ai_gnd/ai_gnd_deep contradiction still observed (Rock Run); repair the gate condition |
| Phase 2 instrumentation: title parser, page-aware dedup, compare gating, extraction retry, dispatch accounting, key-schema stamp | **Uncommitted, this branch** (`claude/condescending-meninsky-122b6c`) | NO — zero production runs have any of it |
| `extraction_incomplete` meta-finding (analyzer.py:2489-2510) | Source, postdates all runs | NO — engineers saw unexplained Deferred rows for the entire pilot |
| calc_inputs provenance persistence | Deployed late | Only 2 of 26 runs have non-empty calc_inputs |

Also note one true positive the fixes did not blind: d8ed57b7 `ai_consistency_recloser_bil_inconsistency_same_sheet` = Fail for conflicting recloser callouts (630 A/170 kV BIL vs 300 A/150 kV BIL) at the same pole — a legitimate same-sheet ambiguity a checker would flag.

---

## 6. Next steps, ranked

### Horizon A — this week

| # | What | Why (measured volume) | Effort | Risk |
|---|---|---|---|---|
| A1 | **Ship it:** commit + deploy the Phase 2 branch (extraction retry, dispatch accounting, page-aware dedup, compare gating, key-schema stamp), then re-run Bagby and confirm (a) `poi_voltage` survives extraction (the retry targets exactly its failure mode) and (b) the voltage calcs compute double-digit amps (≈42 A per 2,500 kVA transformer at 34.5 kV), not 54,388 A | The bare-kV fix is deployed but starved of input — v3 deferred every voltage calc because extraction missed `poi_voltage`; the uncommitted retry is the missing piece; and a garbage-Pass shipped on Aug 3 | Hours | Low — code exists and is tested (235 checks); the risk is *not* shipping |
| A2 | Fix the page router: disjoint keyword sets (drop "EQUIPMENT PAD" from ai_pad; exclude FEEDER/ZONE/MAP/PLAN from ai_pad, DETAIL/SLAB/FOUNDATION from ai_eaf; exclude CAB/MV from ai_elev; OVERALL/SITE from ai_gnd), assert `pad[0] != eaf[0]`, and emit one Deferred "target sheet not found" instead of 7 content-Fails | ~112–128 Fail+NR (≥10% of pool); the single largest confirmed-Fail mechanism | 1 day | Low — keyword-list edits; add the dispatch-record log (family, matched pages/titles, chosen page) so the fix is verifiable from prod data |
| A3 | Kill the worst 3 rules: merge the VD pair + conduit-fill stub into one Deferred "manual verification" item (the Deferred lane exists, analyzer.py:2544-2549); delete 3LD criterion 2 (invented no-cable-sizes rule); map DER/SOV `found:false` to a stage-gated NR ("not present — required at IFC") instead of Fail, and drop `ai_tb` findings for registry keys that are explicitly disabled | ~100 Fail+NR removed, including 73 hard Fails (DER 16 + SOV 42 + 3LD 15), all at ~100% FP rate | 1 day | Low — deletions and a post-filter; the 3LD change should become "if FLA shown, verify it MATCHES the SLD" later (B) |

### Horizon B — this month

| # | What | Why | Effort | Risk |
|---|---|---|---|---|
| B1 | Enumerated check-ID pilot: fixed sub-check registries for the four free-naming families; strip `p{N}` from ai_tb keys | 98% of key churn; prerequisite for compare view, suppression, and any feedback loop; Nessler Jaccard 0.14 is disqualifying | ~1 wk | Medium — prompt-schema change needs a rerun-agreement gate (target >90% bad-key Jaccard on identical input) |
| B2 | Absence-claim policy: absence evidence → NR ceiling, never Fail, unless confirmed by zoom-4 region re-render (extend `rescue_by_page`, gemini_analyzer.py:2050) or deterministic text-layer search; report absence-NRs in a separate "could not verify" section; replace the flat 0.72 with per-family measured precision from this audit | 147 absence-Fails at ≤51% precision; 129 probable render artifacts; the constant confidence makes triage impossible | ~1 wk | Low-medium — extra vision calls on flagged regions only |
| B3 | Stage-gate legacy families: map each to its stage_overrides.yaml category, route through the v4 `stage_index` machinery, cap bypass findings at NR with "preliminary at this stage"; surface a banner when user-selected stage contradicts `detect_design_stage`, and tighten the bare "FOR CONSTRUCTION" regex | 394 of 462 Fail+NR on 60% runs come from ungated families; ~103 clearly premature; the policy is already encoded, only v4 reads it | 2-3 days ("one afternoon of plumbing" plus tests) | Medium — depends on stage-label reliability, hence the banner first |
| B4 | Feedback at point of read: single Confirm / Not-an-issue toggle on Fail+NR rows only, with per-item_key dismissals honored (visibly) on the next run of the same project; plus a paste-an-email box mapping out-of-band feedback to item_keys | Zero in-app feedback ever; the E1300 case proves engineers give PE-grade ground truth via channels they already use; payoff must be visible next run | ~1 wk (needs B1 for key stability) | Low |

### Horizon C — this quarter

| # | What | Why | Effort | Risk |
|---|---|---|---|---|
| C1 | Regression corpus with engineer-labeled ground truth: start with one PE adjudicating the 75 class-4 absence-Fails (~30 s each, under an hour) plus the E1300 before/after re-run sent to that engineer — the cheapest adoption lever available | Converts this audit's plausibility bounds into measured precision; only external ground truth to date arrived out-of-band | PE hours + harness | Low |
| C2 | Per-check precision tracking as CI: same-hash rerun Jaccard, per-family FP rate, deferral rate as the extractor's scorecard; block prompt changes that regress them | Nessler showed the tool cannot currently prove two runs of one PDF agree | 1-2 wks | Low |
| C3 | Retire/merge redundant families and build the real comparators: delete the `elec_`/`gnd_`/`relay_` keyword NR-generators (keep `gnd_egc_content` and `relay_settings_exist` as Pass-only annotations); rename compute-only calc "Pass" to "Reference computed" until the drawing-side comparator exists; then extract schedule wire/OCPD/EGC and diff against computed NEC 310.16/240.6/250.122 minimums, and feed ai_gnd's vision-extracted conductor sizes into the deterministic 250.122/250.66/250.102(C)(1) tables | ~130 findings of pure duplication; 37 Passes that verify nothing (a false-*negative* hazard); "vision reads, code checks" is the highest-value architecture available | 3-4 wks | Medium — the comparator is new capability; plausibility interlocks (per-inverter LV FLA > 2,000 A → force NR) are the cheap first slice |

Not recommended because already done: Wave-1 prompt corrections (verified), Deferred-lane design (works — it is the *inputs* that are missing), v4 stage gating (works — extend it, don't rebuild it), `extraction_incomplete` meta-finding (ship it), calc-provenance persistence (ship it).

---

## 7. Limitations

- **No engineer-labeled ground truth.** With zero overrides and zero feedback rows, every FP/legitimate classification here rests on evidence-text analysis, source-code mechanics, and NEC/checklist judgment — not on human adjudication. The 97 "plausibly genuine" class-4 findings (75 Fails) are unaudited; the true Fail-precision floor could be lower than the ~50% central estimate.
- **Absence verdicts are unverifiable from the dump alone.** The dump stores 420-char evidence strings, not renders; whether a value was on the sheet but illegible versus genuinely absent cannot be resolved without re-rendering the PDFs. The 129 "resolution-driven" findings are a bounded estimate (floor: 25 explicit-legibility; ceiling includes genuine 60/90% omissions).
- **The dump lacks a per-page sheet index**, so the `[0]`/`[:N]` coverage blind spot — how many matching sheets were never reviewed — is unmeasurable from production data as logged. The proposed dispatch record closes this.
- **Single-firm corpus, 11 projects, ~10 engineer-initiated runs.** Family FP rates carry wide confidence intervals (ai_gnd's 78% rests on 2 projects); "11/11 projects" claims are strong, "4/8" claims are not.
- **Stage labels are contaminated** (4 of 26 runs carry contradictory metadata: Trigo 60%-as-IFC ×2, Coal City 90%-as-60), so 60-vs-IFC comparisons in §3.5 have a few percent of slippage.
- **Deployment provenance is incomplete**: rules_sha stamps rules.yaml only; calc_inputs is empty for 24/26 runs; the Aug 3 container's actual electrical_calcs.py version is inferred from output arithmetic, not from an image digest.