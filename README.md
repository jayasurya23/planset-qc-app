# Castillo Planset QC - Starter App

This is a local-first starter application for QCing Castillo plansets.

## What this version does

- Upload a planset PDF
- Parse machine-generated AutoCAD PDFs locally
- Check the drawing index against the actual sheets in the PDF
- Verify sheet presence, order, and no extras
- Parse title block sheet number and sheet title
- Run a first pass on core checklist categories
- Generate cropped issue snippets and full-page highlighted previews
- Let the QC engineer manually change status or add manual issues
- Save runs locally in SQLite
- Export the checklist to Excel

## Current limitations

This is a strong starter, not the finished production version.

It is strongest for:

- machine-generated PDFs
- sheet index consistency checks
- sheet existence checks
- basic keyword-driven checklist checks

It does **not** yet include:

- OpenAI reasoning for fuzzy checklist items
- PVSyst upload and comparison
- multi-file project linking
- user authentication / role separation
- PDF report export
- packaged EXE build

## Recommended final architecture

- **Frontend:** React
- **Backend:** FastAPI (Python)
- **PDF engine:** PyMuPDF + pdfplumber
- **Storage:** SQLite for V1
- **AI layer later:** OpenAI API for ambiguous checks only
- **Windows packaging later:** PyInstaller for backend, and either:
  - run frontend in browser locally, or
  - wrap frontend + backend into a desktop shell like Tauri/Electron later

For your use case, I would still keep the core extraction/check logic in Python.

---

## Folder structure

```text
planset-qc-app/
  backend/
    app/
      analyzer.py
      checklist.py
      db.py
      exporter.py
      main.py
    requirements.txt
  frontend/
    src/
      App.tsx
      main.tsx
      styles.css
      types.ts
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

## How to test with your sample PDF

1. Launch backend and frontend.
2. Open the frontend.
3. Enter a project name like `Girard Solar - 90%`.
4. Upload `Girard - 90% Sealed.pdf`.
5. Wait for analysis.
6. Review the category list and issue cards.
7. Click an issue snippet to open the full highlighted page image.
8. Change statuses as needed.
9. Add manual issues if the automation missed anything.
10. Click **Export Excel** to download the checklist workbook.

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

## What to improve next

### Best next engineering steps

1. Add a proper rule registry so each checklist item has:
   - rule id
   - category
   - auto-check function
   - confidence
   - evidence
   - optional handbook reference

2. Add OpenAI for fuzzy checks only
   - note quality
   - does a callout satisfy intent
   - does the sheet appear to contain the expected design element

3. Add PVSyst upload support
   - upload report PDF or XLSX
   - compare module, inverter, DC/AC, albedo, losses, tracker settings

4. Add project grouping
   - one project name
   - separate runs for 30%, 60%, IFC

5. Add packaging for internal engineers
   - bundle backend with PyInstaller
   - run React as static build served by FastAPI

---

## Packaging suggestion for later

For internal Windows users, I recommend:

### Good practical option

- build React frontend with `npm run build`
- serve the built frontend from FastAPI
- package the Python backend with PyInstaller
- launch the app locally with a small starter script

This keeps your heavy logic in Python and is easier to maintain than trying to move the parsing logic into JavaScript.

---

## Important note about this starter

This version is designed to be easy to extend.

It already includes the workflow shell you asked for:

- upload PDF
- auto-check
- issue snippets
- highlighted page preview
- manual overrides
- saved runs
- Excel export

The next step is improving the actual rule coverage category by category.
