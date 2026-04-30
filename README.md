# Castillo Planset QC

A local-first application for QCing Castillo solar PV plansets. Combines
deterministic NEC math, page-level keyword checks, and Gemini vision-model
review to produce a triaged Pass / Fail / Needs Review / Deferred report
that a QC engineer can open, adjust, and export to Excel.

## What it does

- Upload a planset PDF (machine-generated AutoCAD PDFs work best)
- Cross-check the drawing index against the actual sheets present
- Verify sheet presence, order, and detect extras
- Run the **V4 dynamic rule engine** — ~400 rules across 20+ categories
  (E-001 cover, E-050 sub-station, E-100 AC SLD, E-103/4 three-line,
  E-110 relay, E-120 stringing, E-300 schedules, E-500 grounding, etc.)
- Dispatch each category to Gemini vision with category-specific prompts
- Run **deterministic electrical calcs** for math-heavy rules:
  - String Voc(cold), Vmp(cold), Vmp(hot) per NEC 690.7 — three
    rule-specific calcs sharing a temperature-corrected helper
  - System-info math (Module Qty = String Size × String Qty,
    Total DC = Module Qty × STC Watts, Total AC = Inv Qty × Inv kVA,
    DC/AC ratio range)
  - Multi-transformer sizing (per-unit kVA × count or explicit total)
  - Fuse, ampacity (DC / AC / MV), EGC / GEC sizing, voltage drop,
    NEC 110.26 clearances
- Generate cropped issue snippets and full-page highlighted previews
  pointing at the literal text excerpt that triggered each finding
- Manual overrides and free-form issues from the UI
- Save runs locally in SQLite + JSON under `backend/data/`
- Export the checklist to Excel with Pass / Fail / Needs Review /
  Deferred / Overridden columns

## Design-stage gating

Pick one of **30%**, **60% / IFP**, **90%**, **IFC**, or **As-Built** in
the upload sidebar. Rules whose `min_stage` is later than the selected
stage are deferred — but **only if** the target sheet isn't actually in
the PDF. If you drew it, it gets checked. The list of stage thresholds
lives in `backend/app/stage_overrides.yaml` (category-level defaults
with per-rule overrides plus a `disabled_rules` list).

Stage gating runs alongside a **cross-reference filter** that defers
rules whose title/description names sheets or external documents not
present in the upload — these used to come back as "Needs Review: other
sheet not in view" and dominated the NR bucket.

## Deferred status

A fourth status alongside Pass / Fail / Needs Review. Findings flagged
"Deferred" are rendered as N/A in the UI with a precise "Requires X"
reason: missing sheet code, missing external document type
(submittal / CESIR / PVSyst / BOD / ampacity table / …), or stage
not yet reached. Evidence-less Needs Review findings (no location,
value, or notes returned) are also auto-demoted to Deferred — an
unreviewable NR was always noise.

## Supporting documents

Drop optional engineering documents into the **Supporting Documents**
panel and they're parsed and made available as evidence to the rule
engine:

| Type | What it provides |
| --- | --- |
| `module_datasheet` | module Voc / Vmp / Isc / Imp / Pmax / temp coeffs |
| `inverter_datasheet` | inverter kVA / kW / max Vdc / MPPT range |
| `transformer_datasheet` | transformer kVA / Z% / BIL / windings |
| `cesr` | interconnection survey / CESIR / impact study |
| `pvsyst` | PVSyst yield report |
| `ampacity` | cable ampacity / Neher–McGrath table |
| `relay` | protection coordination study |
| `structural` | wind / snow / seismic / pile calcs |
| `bod` | owner BOD / project tech spec / Exhibit-X criteria |
| `datasheet` / `generic` | other cut sheets, kept as free-text evidence |

Datasheet uploads are **auto-merged into project_details** — extracted
specs fill any field the user didn't fill in by hand. User-entered
values always win; this only fills gaps. PDF, XLSX, and DOCX inputs
are all supported (DOCX paragraphs and table cells, in document order).

## Architecture

- **Frontend:** React + Vite + TypeScript
- **Backend:** FastAPI (Python 3.11+)
- **PDF engine:** PyMuPDF (fitz) + pdfplumber
- **AI:** Google Gemini (vision + text) for category dispatch and
  AUX-SLD review; per-rule prompts contain explicit HARD SKIP
  TRIGGERS so the model omits findings rather than emitting "cannot
  be verified from this sheet alone"
- **Storage:** SQLite + JSON sidecar files under `backend/data/`
- **Windows packaging plan:** PyInstaller for backend + `npm run build`
  for frontend served by FastAPI, launched via a small starter script

---

## Folder structure

```text
planset-qc-app/
  backend/
    app/
      analyzer.py            # PDF parsing, page detection, regex checks
      electrical_calcs.py    # NEC math: stringing, ampacity, transformer, …
      gemini_analyzer.py     # Gemini vision orchestration + AUX SLD prompt
      v4_engine.py           # Dynamic rule engine + stage gating + xref filter
      rule_registry.py       # YAML loader, stage_overrides, Rule dataclass
      rules_v4_draft.yaml    # ~400 rules (workbook + manual supplements)
      stage_overrides.yaml   # min_stage by category, per-rule overrides
      supporting_docs.py     # CESIR / PVSyst / datasheet / BOD ingest
      training_docs.py       # Castillo SOP and reference docs
      xlsx_template_sniffer.py  # spec workbook layout heuristics
      checklist.py           # legacy V3 categories
      exporter.py            # Excel export (Pass/Fail/NR/Deferred/Override)
      db.py                  # SQLite I/O
      main.py                # FastAPI routes, design-stage param plumbing
    requirements.txt
  frontend/
    src/
      App.tsx                # main UI: stage dropdown, status tiles, cards
      main.tsx
      styles.css             # Pass/Fail/Review/Deferred theming
      types.ts               # DesignStage, Status, RunSummary, etc.
    package.json
    vite.config.ts
  README.md
```

---

## Step-by-step setup

## 1) Install Python

Use Python **3.11 or 3.12** on Windows.

Verify:

```powershell
python --version
```

## 2) Install Node.js

Use Node **18+**.

Verify:

```powershell
node -v
npm -v
```

## 3) Open two terminals

One terminal will run the backend.
One terminal will run the frontend.

---

## Backend setup

### 4) Go to the backend folder

```powershell
cd path\to\planset-qc-app\backend
```

### 5) Create a virtual environment

```powershell
python -m venv .venv
```

### 6) Activate it

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks it, run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

### 7) Install backend packages

```powershell
pip install -r requirements.txt
```

### 8) Start the backend

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

When it starts, the API will be available at:

```text
http://127.0.0.1:8000
```

Optional API docs:

```text
http://127.0.0.1:8000/docs
```

---

## Frontend setup

### 9) Open another terminal and go to the frontend folder

```powershell
cd path\to\planset-qc-app\frontend
```

### 10) Install frontend packages

```powershell
npm install
```

### 11) Start the frontend

```powershell
npm run dev
```

Vite will show a local URL, usually:

```text
http://127.0.0.1:5173
```

Open that in the browser.

---

## How to run a QC pass

1. Launch backend and frontend.
2. Open the frontend.
3. Enter a project name like `Girard Solar — 90%`.
4. Pick the **Design Stage** that matches the planset (30 / 60 / 90
   / IFC / As-Built). Leave on "All stages (no gating)" to run every
   rule regardless of stage.
5. (Optional) Drop module / inverter / transformer datasheets, CESIR,
   PVSyst, or BOD documents into the **Supporting Documents** panel.
   Specs from datasheets auto-merge into project_details — anything
   you typed manually still wins.
6. Upload the planset PDF.
7. Wait for analysis. Per-call timing is shown under "timing details"
   in the run header so you can see which categories are slow.
8. Review the category list and issue cards. Filter by status using
   the chips at the top: Pass / Fail / Needs Review / Deferred /
   Overridden.
9. Click an issue snippet to open the full highlighted page image.
10. Change statuses or add manual issues as needed.
11. Click **Export Excel** to download the checklist workbook.

---

## Where data is saved

The backend saves data locally in:

```text
backend/data/
```

That includes:

- SQLite database
- uploaded PDFs
- generated snippets
- page preview images
- exported Excel files

---

## What's still on the roadmap

- **Multi-file project linking** — one project, multiple stages
  (30% / 60% / 90% / IFC) tracked as a sequence with diffs between
  revisions
- **User authentication / role separation** — engineer vs. reviewer vs.
  approver
- **PDF report export** alongside the Excel workbook
- **Packaged Windows EXE** — PyInstaller backend + static React build
  served by FastAPI, launched via a starter script
- **PVSyst comparison** beyond the current evidence-block ingest —
  side-by-side module / inverter / DC-AC / albedo / loss / tracker
  diffing
- **Per-category rule expansion** — Cross-Sheet, E-200 series, civil
  / structural cross-refs are still thinner than the electrical-side
  coverage
