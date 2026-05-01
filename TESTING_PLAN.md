# Planset QC — Initial Testing Plan

You're one of a small group of QC engineers helping us test an internal
beta of an AI-assisted planset QC tool. This document explains what
we're testing, what we need from you, and how the next 2–3 weeks will
run. **Read this once before you start.** It will save us both time.

Pair this with `QC_ENGINEER_GUIDE.md` (the day-to-day how-to). This
doc is the *program*; that one is the *manual*.

---

## What we're testing

Three questions, in priority order:

1. **Does it save you time net?** Sum of (time saved by not running
   checks manually) − (time spent fighting wrong findings, false
   passes, missing checks). If the answer is "no," nothing else
   matters.
2. **Where is it confidently wrong?** False passes are the dangerous
   ones — the AI says ✓ but the planset has the issue. We need every
   one you catch.
3. **What's it missing entirely?** Things you'd flag during a normal
   review that the tool doesn't surface at all.

## What we are *not* testing yet

- Production readiness — this is beta, expect ~10–20% of findings to
  be wrong in some way.
- Edge cases on unusual projects (microgrids, BESS-only, retrofits).
  Stick with mainstream solar PV plansets if you can.
- Performance with everyone hammering it at once. If runs feel slow,
  it's likely Jay's laptop and the Gemini API, not a real bug.

Your job isn't to stress-test it. **Use it normally, on real work,
and tell us where it lets you down.**

---

## The 2–3 week test plan

### Week 1 — Calibration

**Goal:** find out where it agrees and disagrees with you, on plansets
you already know the answer to.

Pick **2 plansets you QC'd in the last 30 days** — ideally one
straightforward and one that had real issues. Run each through the
tool. For each finding:

- Was the call (Pass/Fail/NR) right?
- Was the location/page/bbox right?
- Was the reason / cited source right?
- Anything you flagged manually that the tool missed?

Override anything wrong, with a `wrong because: ___. actually: ___.`
comment. Add manual issues for anything missed.

**Don't change your real workflow this week.** Run your normal QC in
parallel — we want a clean comparison.

If you can, jot rough times: *"normal review 90 min, with-tool review
60 min."* Even ballpark numbers are gold.

### Week 2 — Real use

**Goal:** use it as part of actual production QC and tell us how it
felt.

Use the tool on new plansets in your queue. **Don't** ship anything
based on the tool alone — keep your normal handoff process running in
parallel until we agree it's earned trust.

End of each day, post one line in the testing Teams thread:

> *"Friday — ran 2 plansets. Saved ~25min on cover sheet & equipment list. Lost ~10min on E-110 false NRs (overrode 6). Net +15min. Big false-pass on E-002 stranding note — rule fired twice for same check, both with conflicting status."*

That's all. Two minutes of writing per day.

### Week 3 (if we extend) — Stress + edge cases

If the first two weeks go well, we'll ask you to throw harder
plansets at it: trackers, large central inverters, multi-transformer
projects, weird utility requirements. We'll set this up after seeing
week-1 and week-2 data.

---

## What to report and how

Three channels, in priority order:

### 1. Override comments (highest signal)

Every override should have a comment. Format:

> `wrong because: <reason>. actually: <truth>.`

Examples:

- *"wrong because: cited E-110, relay table is on E-100. actually: E-100 top-right."*
- *"wrong because: 'TBD' is acceptable for software equipment per project spec. actually: pass."*
- *"wrong because: missed that this is a tracker project. actually: flexible stranding note IS present."*

A blank override comment tells us nothing. A 10-second specific
comment tells us everything. **This is the single most important thing
you can do.**

### 2. Manual issues (false negatives)

Anything you'd flag and the tool didn't — add as a manual finding
with a clear title, page number, and category. Each one is a candidate
for a new rule.

### 3. End-of-day note in the testing thread

One line. Saved time / even / cost time, plus one specific thing that
broke or worked.

---

## What good feedback looks like vs vague

| ✓ Good (specific, actionable) | ✗ Vague (un-actionable) |
| --- | --- |
| "Wire stranding rule fires twice on E-002 with conflicting status — same check, two findings" | "Some duplicates" |
| "On Wellington IFP p.4, said XFMR Z% missing. Actually on row 4 of the equipment table — bbox pointed at row 5" | "Bbox is wrong sometimes" |
| "Tool said cover sheet code year missing. The 2023 NEC reference is in the project address block, not the standard location — rule needs to look in title block too" | "Code year check failed" |
| "Override volume on E-110 was painful — 8 of 11 NRs were the same false 'relay setting absent' pattern" | "Too many NRs" |

Specifics turn into rule fixes. Generalities turn into more meetings.

---

## Weekly check-in

**15 minutes with Jay, in person, end of each week.** Bring nothing.
Jay will pull up your override list and your manual issues on screen
and walk through a sample. The conversation is the feedback —
you don't need to prep.

If 15 minutes isn't enough, we'll book more. If you've got nothing to
say, we cancel. Don't grind on a written report.

---

## How we'll know it's working

Beta ends — and we make this an everyday tool — when:

- **Net time savings** (rough average across testers) is consistently > 25%.
- **False-pass rate** (engineer flips Pass → Fail/NR after review) is below 5%.
- Each tester answers "yes" to: *"would I use this on routine work without parallel manual QC?"*

We'll track all three weekly and share the numbers back to the group.
If things plateau before we hit those bars, we'll discuss whether to
keep tuning, change scope, or pull the plug.

---

## Ground rules

- **Don't ship plansets based on the tool alone yet.** Keep your normal
  manual QC running in parallel for the entire test period.
- **Don't share the tool's outputs with the EOR or client** before we
  graduate from beta. The findings have known false positives — your
  reputation goes with them.
- **Don't upload anything confidential** — NDA-restricted plansets,
  client BODs marked confidential, etc. PDFs sit on Jay's machine in
  plaintext.
- **Don't share the URL outside the office.** It's only on the LAN
  anyway, but flag if you see external traffic in the run list.
- **If something's clearly broken** (page won't load, all runs fail),
  ping Jay before retrying — saves wasted time.

---

## Questions / contact

- **Day-to-day:** Jay Bhaskar — Teams or `castillopeit@gmail.com`
- **Tool feedback:** override comments + Teams thread (don't email these
  — we'll lose them)
- **Bugs / outages:** Teams DM to Jay
- **Privacy concerns about uploading a specific planset:** ask Jay
  *before* uploading

---

## TL;DR

| | |
| --- | --- |
| Week 1 | Run 2 old plansets, compare to your manual review, override with reasons |
| Week 2 | Use it on real work, post one-line daily summary |
| Always | Override comments use `wrong because: ___. actually: ___.` |
| Always | Add manual issues for things the tool missed |
| Weekly | 15-min in-person check-in with Jay |
| Bar to clear | Net time saved > 25%, false-pass rate < 5% |

Thanks for testing. Honest feedback is what makes this thing eventually
useful — please don't soften it.
