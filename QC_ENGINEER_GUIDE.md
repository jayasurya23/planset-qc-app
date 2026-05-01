# Planset QC — Engineer Guide

This is a beta tool for QCing solar PV plansets. It runs ~400 NEC checks
across the planset using a mix of deterministic math, keyword scans, and
AI vision review, then hands you a triage list. **You are the QC engineer
of record — the tool's results are a starting point, not the final word.**

Your honest feedback during this initial test decides which rules stay,
which get tuned, and which get deleted. Read the **Feedback** section
below — it's the most important part of this doc.

---

## Getting in

- **URL:** `http://192.168.1.150:5173/`
- Open it in any browser on the office Wi-Fi. No login.
- The server runs on Jay's machine — if the page doesn't load, ping Jay
  on Teams and check that his laptop is on.

---

## Day-to-day workflow

### 1. Put your name in
Top of the sidebar: **"Your name (QC engineer)"** field. Type it once;
the browser remembers it. Every run you launch is tagged with your name,
so we know whose feedback is whose.

### 2. Drop the planset PDF
Drag the planset PDF into the dropzone, or click to browse. Works best
with **machine-generated AutoCAD PDFs** — scanned/raster PDFs miss text
checks.

### 3. (Optional but recommended) Drop supporting documents
The **Supporting documents** box accepts:

| What to drop | Why it helps |
| --- | --- |
| Module / inverter / transformer **datasheets** | Auto-fills project details (Voc, Vmp, kVA, Z%, BIL, etc.) so the math checks have real values |
| **CESIR / impact study / interconnection survey** | Cross-checks against site limits and feeder ratings |
| **PVSyst report** | DC-side numbers, module count, expected yield |
| **Owner BOD / tech spec / Exhibit-X** | Comparison source for "matches owner spec" checks |
| **Ampacity table** | Verifies cable sizing against the project's reference table |
| **Relay protection study** | Cross-checks relay & inverter settings on E-110 |

Drop multiples at once. The AI tags each by type and threads the
extracted specs into the relevant rules.

### 4. (Optional) Project Details
Click **"Fill Project Details"** if you want to fix any owner /
equipment / utility info up front. The tool will compare these against
the planset and flag mismatches. **Easier path:** drop a recent email or
the interconnection agreement into the auto-fill dropzone — the AI
extracts what it can, you correct what's wrong.

### 5. Pick the design stage
**30% / 60% (IFP) / 90% / IFC / As-Built.** This matters: rules that
don't apply at your stage are deferred (rendered as N/A) instead of
flagged as Fail. Default is "All stages (no gating)" — only use that
if you genuinely want every rule run.

### 6. Click **Analyze PDF**
Takes 5–10 minutes for a typical planset, longer for IFC. You can
queue more analyses while one is running, but don't pile up too many —
Jay's machine is doing the work.

### 7. Triage the findings

Each finding has a status, a category, a page number, and a snippet
preview pointing at the location in the PDF.

| Status | Meaning |
| --- | --- |
| **Pass** ✓ | Tool is confident the requirement is met. **Spot-check, don't blindly trust.** |
| **Fail** ✗ | Tool found a clear violation. Verify, then keep, override, or escalate. |
| **Needs Review** ? | Tool is unsure. Most of your time goes here. |
| **Deferred** N/A | Skipped — sheet not in the PDF, design stage hasn't reached this rule, or referenced external doc not uploaded. |
| **Overridden** | You manually corrected the AI's call. |

Click the **eye icon** to see the highlighted region in the PDF.
Click the **pencil icon** to change status / leave a comment.
Use **j / k** to step through findings; **?** shows all shortcuts.

### 8. Add manual findings for anything missed
Big plus button under the finding list. Anything you'd flag that the
tool didn't — type it in. **These are gold for tuning.**

### 9. Export
Top of the run view → **Export to Excel.** Format matches the columns
you use for the EOR handoff.

---

## Feedback — what we need from you

The whole point of this initial test is to find where the AI is wrong
so we can fix it. Three lightweight asks:

### 1. When you override, *write why*

Every override has a comment box. Use this format:

> `wrong because: <reason>. actually: <truth>.`

Examples:

- *"wrong because: cited E-110 but the relay table is on E-100. actually: top-right of E-100."*
- *"wrong because: said TBD but the field reads N/A which is acceptable for software equipment. actually: pass."*
- *"wrong because: missed that this is a tracker project. actually: flexible stranding note IS present on E-002."*

Three-second comment, massive feedback signal. Comments without a "why"
tell us nothing — please don't skip the reason.

### 2. Add manual findings for anything you catch by hand

If you'd flag it during a normal QC and the tool didn't surface it,
add it manually with a clear title and the page number. Each manual
issue is a candidate for becoming a real rule.

### 3. At the end of each run, tell Jay one thing

Reply to the daily thread (or grab Jay) with one of:

- **"Saved time"** — and roughly how much, and on what (cover sheet? equipment list? cross-checks?)
- **"Cost time"** — what fought you. The override volume, the false passes, the missing checks?
- **"About even"** — same.

This is the single most important data point. We're optimizing for
*net time saved*, not feature count.

---

## Tips that aren't obvious

- **Group similar findings** — if the same rule fires on 14 pages
  (e.g. "TBD in equipment list"), they collapse behind one header.
  Click the header to expand and override per-row, or override the
  group header to apply once.
- **Compare two runs** — if you re-analyze the same planset, the URL
  picks up `?compare=<old-run-id>` and you get a diff view.
- **Source-doc citations** — when a finding says "per CESIR p.4," click
  through; the cited sentence is highlighted in the supporting doc.
- **"Show full-page preview"** — for findings without a tight bbox,
  the page render is collapsed by default to save scrolling. Expand
  if you need it.

---

## Things to be aware of

- **It's beta.** Expect ~10–20% of findings to be wrong in some way.
  That's the whole reason you're testing it — your overrides are how
  it gets better.
- **Don't trust Pass blindly.** If a Pass result feels suspicious,
  override and tell us. False passes are the most dangerous failure
  mode — they're invisible.
- **Don't share the URL externally.** It's office-LAN-only by design.
- **Two people on the same run = last write wins.** If you and a
  colleague are looking at the same run simultaneously, coordinate.
  Status changes don't lock.
- **No automatic backups yet.** Don't rely on this as your only QC
  record — keep your normal handoff process in parallel until we've
  proven it.

---

## When something breaks

| Symptom | What to do |
| --- | --- |
| Page won't load at all | Server's down. Ping Jay. |
| Analysis says "Failed" | Click Re-analyze once. If it fails again, screenshot + send to Jay with the run name. |
| A finding is comically wrong (wrong page, wrong sheet, garbled text) | Override with a comment, then mention it to Jay so he can pull the rule. |
| A whole category is missing or all Deferred | Probably stage gating — check the design stage you picked. If that's not it, tell Jay. |
| Results disappeared from the sidebar | They didn't — the run list shows everyone's runs. Filter by your name, or scroll. If genuinely gone, tell Jay. |

**Contact:** Jay Bhaskar — Teams / `castillopeit@gmail.com`

---

## What's coming, what isn't (yet)

Not in this build, but on the roadmap depending on your feedback:

- Per-engineer filtering of the run list
- "Report a problem" button distinct from override (for cases where
  the finding is right but you want to flag the rule itself)
- Shared backup of the SQLite DB so a server restart doesn't lose runs
- Production build (no hot-reload during work hours)
- Login / per-user override history

Tell us which of these you actually want. We'll prioritize from your
real usage, not guesses.
