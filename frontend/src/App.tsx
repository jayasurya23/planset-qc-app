import { useEffect, useMemo, useState, useCallback } from "react";
import type {
  Issue,
  RunData,
  Status,
  CategorySummary,
  GeminiUsage,
  ProjectDetails,
} from "./types";

const API = `http://${window.location.hostname}:8000`;
const STATUSES: Status[] = [
  "Pass",
  "Fail",
  "Needs Review",
  "Overridden / Accepted by QC Engineer",
];
const CATEGORIES = [
  "Drawing Index",
  "Title Block",
  "Cover Sheet",
  "System Information Table",
  "General Notes",
  "Site Plan",
  "Pole Line Up",
  "Engineered Equipment List",
  "AC Single Line Diagram",
  "DC Line Diagram",
  "Three Line Diagram",
  "Relay and Inverter Settings",
  "AUX SLD",
  "Communication Diagram",
  "Feeder Plan",
  "Communication Feeder Plan",
  "Equipment Area Feeder Plan",
  "Electrical Sheet",
  "Inverter Zone Map",
  "Elevation Details",
  "Trenching Details",
  "Overall Site Grounding Plan",
  "Grounding Diagram",
  "CAB or Cable Hanger Details",
  "PAD / Slab Details",
  "Pole Details",
  "Labels",
  "PVSyst Analysis Summary",
  "Cross-Sheet Consistency",
];

function artifactUrl(p?: string | null) {
  if (!p) return "";
  const s = p.replace(/\\/g, "/");
  const i = s.indexOf("/data/");
  return i >= 0
    ? `${API}/artifacts/${s.slice(i + 6)}`
    : `${API}/artifacts/${s}`;
}
function pdfPageUrl(run: RunData, pg?: number | null) {
  return pg ? `${artifactUrl(run.pdf_path)}#page=${pg}` : null;
}

// ── Tiny helpers ──
const SL: Record<string, string> = {
  Pass: "Pass",
  Fail: "Fail",
  "Needs Review": "Review",
  "Overridden / Accepted by QC Engineer": "Accepted",
};
const SV: Record<string, string> = { high: "HIGH", medium: "MED", low: "LOW" };
function catHealth(c: CategorySummary): "pass" | "fail" | "review" {
  if ((c.Fail ?? 0) > 0) return "fail";
  if ((c["Needs Review"] ?? 0) > 0) return "review";
  return "pass";
}
function catPct(c: CategorySummary) {
  const t = c.total || 1;
  return Math.round(
    (((c.Pass ?? 0) + (c["Overridden / Accepted by QC Engineer"] ?? 0)) / t) *
      100,
  );
}

// ── Components ──

function GeminiBar({ u }: { u?: GeminiUsage }) {
  if (!u || !u.api_calls) return null;
  return (
    <div className="gem">
      <span className="gem-dot" />
      <span className="gem-text">{u.api_calls} AI calls</span>
      <span className="gem-sep">&middot;</span>
      <span className="gem-text">
        {(u.total_tokens / 1000).toFixed(1)}k tokens
      </span>
      <span className="gem-sep">&middot;</span>
      <span className="gem-cost">
        ~${((u.total_tokens / 1e6) * 0.1).toFixed(4)}
      </span>
    </div>
  );
}

function StatusBtn({
  current,
  target,
  label,
  color,
  onClick,
}: {
  current: string;
  target: Status;
  label: string;
  color: string;
  onClick: () => void;
}) {
  const on = current === target;
  return (
    <button
      className={`qb ${on ? "qb-on" : ""}`}
      style={{ "--qc": color, "--qcbg": `${color}18` } as React.CSSProperties}
      onClick={onClick}
      title={target}
    >
      {label}
    </button>
  );
}

export default function App() {
  const [runs, setRuns] = useState<RunData[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [cat, setCat] = useState("All");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [sel, setSel] = useState<Issue | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState("");
  const [projName, setProjName] = useState("");
  const [editId, setEditId] = useState<string | null>(null);
  const [editStatus, setEditStatus] = useState<Status>("Pass");
  const [editComment, setEditComment] = useState("");
  const [saving, setSaving] = useState(false);
  const [manual, setManual] = useState({
    category: CATEGORIES[0],
    title: "",
    description: "",
    page_number: "",
    severity: "medium",
    evidence: "",
  });
  const [showManual, setShowManual] = useState(false);
  const [progressPct, setProgressPct] = useState(0);
  const [expandedEv, setExpandedEv] = useState<string | null>(null);
  const [sideOpen, setSideOpen] = useState(true);
  const [issuesOnly, setIssuesOnly] = useState(false);
  const [showProjDetails, setShowProjDetails] = useState(false);
  const [pd, setPd] = useState<Partial<ProjectDetails>>({});
  const pdSet = (k: keyof ProjectDetails, v: string) =>
    setPd((p) => ({ ...p, [k]: v }));
  const pdHasValues = Object.values(pd).some((v) => v && String(v).trim());
  const [parsing, setParsing] = useState(false);
  const [parseMsg, setParseMsg] = useState("");

  const parseDocuments = async (fileList: FileList) => {
    setParsing(true);
    setParseMsg("AI is reading documents...");
    const fd = new FormData();
    for (let i = 0; i < fileList.length; i++) fd.append("files", fileList[i]);
    try {
      const r = await fetch(`${API}/api/parse-project-details`, {
        method: "POST",
        body: fd,
      });
      if (!r.ok) {
        setParseMsg(`Error: ${await r.text()}`);
        return;
      }
      const d = await r.json();
      const extracted = d.project_details as Partial<ProjectDetails>;
      const count = Object.keys(extracted).length;
      // Merge into existing pd — only overwrite empty fields
      setPd((prev) => {
        const merged = { ...prev };
        for (const [k, v] of Object.entries(extracted)) {
          if (
            v &&
            (!prev[k as keyof ProjectDetails] ||
              !String(prev[k as keyof ProjectDetails]).trim())
          ) {
            (merged as Record<string, string>)[k] = String(v);
          }
        }
        return merged;
      });
      setParseMsg(
        count > 0
          ? `Extracted ${count} field${count > 1 ? "s" : ""} from ${fileList.length} file${fileList.length > 1 ? "s" : ""}`
          : "No project details found in the uploaded files",
      );
      setTimeout(() => setParseMsg(""), 5000);
    } catch {
      setParseMsg("Failed to parse documents");
    } finally {
      setParsing(false);
    }
  };

  // ── Data loading ──
  const load = useCallback(async () => {
    const r = await fetch(`${API}/api/runs`);
    const d = await r.json();
    setRuns(d);
    if (d.length && !runId) {
      const first = d[0];
      setRunId(first.id);
    }
  }, [runId]);

  const refresh = useCallback(async (id: string) => {
    const r = await fetch(`${API}/api/runs/${id}`);
    const d = await r.json();
    setRuns((p) => [d, ...p.filter((x: RunData) => x.id !== id)]);
    setRunId(id);
  }, []);

  useEffect(() => {
    void load();
  }, []);

  const run = useMemo(
    () => runs.find((r) => r.id === runId) ?? null,
    [runs, runId],
  );

  const issues = useMemo(() => {
    if (!run) return [];
    let list = run.issues ?? [];
    if (cat !== "All") list = list.filter((i) => i.category === cat);
    if (statusFilter === "fail") list = list.filter((i) => i.status === "Fail");
    else if (statusFilter === "review")
      list = list.filter((i) => i.status === "Needs Review");
    else if (statusFilter === "pass")
      list = list.filter((i) => i.status === "Pass");
    else if (statusFilter === "override")
      list = list.filter(
        (i) => i.status === "Overridden / Accepted by QC Engineer",
      );
    if (issuesOnly)
      list = list.filter(
        (i) =>
          i.status !== "Pass" &&
          i.status !== "Overridden / Accepted by QC Engineer",
      );
    return list;
  }, [run, cat, statusFilter, issuesOnly]);

  // Live status counts computed from actual issues (updates when statuses change)
  const liveStatusCounts = useMemo(() => {
    if (!run)
      return {
        Pass: 0,
        Fail: 0,
        "Needs Review": 0,
        "Overridden / Accepted by QC Engineer": 0,
      };
    const all = run.issues ?? [];
    return {
      Pass: all.filter((i) => i.status === "Pass").length,
      Fail: all.filter((i) => i.status === "Fail").length,
      "Needs Review": all.filter((i) => i.status === "Needs Review").length,
      "Overridden / Accepted by QC Engineer": all.filter(
        (i) => i.status === "Overridden / Accepted by QC Engineer",
      ).length,
    };
  }, [run]);

  // Live category summaries computed from actual issues
  const liveCategories = useMemo(() => {
    if (!run) return [] as CategorySummary[];
    const all = run.issues ?? [];
    const byCat: Record<string, CategorySummary> = {};
    for (const issue of all) {
      if (!byCat[issue.category]) {
        byCat[issue.category] = {
          name: issue.category,
          total: 0,
          Pass: 0,
          Fail: 0,
          "Needs Review": 0,
          "Overridden / Accepted by QC Engineer": 0,
        };
      }
      const c = byCat[issue.category];
      c.total += 1;
      if (issue.status === "Pass") c.Pass = (c.Pass ?? 0) + 1;
      else if (issue.status === "Fail") c.Fail = (c.Fail ?? 0) + 1;
      else if (issue.status === "Needs Review")
        c["Needs Review"] = (c["Needs Review"] ?? 0) + 1;
      else if (issue.status === "Overridden / Accepted by QC Engineer")
        c["Overridden / Accepted by QC Engineer"] =
          (c["Overridden / Accepted by QC Engineer"] ?? 0) + 1;
    }
    // Preserve original category order
    return (run.categories ?? []).map((orig) => byCat[orig.name] ?? orig);
  }, [run]);

  const counts = useMemo(() => {
    if (!run) return { p: 0, f: 0, r: 0, o: 0, t: 0 };
    const all =
      cat === "All"
        ? (run.issues ?? [])
        : (run.issues ?? []).filter((i) => i.category === cat);
    return {
      p: all.filter((i) => i.status === "Pass").length,
      f: all.filter((i) => i.status === "Fail").length,
      r: all.filter((i) => i.status === "Needs Review").length,
      o: all.filter((i) => i.status === "Overridden / Accepted by QC Engineer")
        .length,
      t: all.length,
    };
  }, [run, cat]);

  // ── Polling helper ──
  const pollProgress = useCallback(
    (uploadId: string): Promise<RunData> =>
      new Promise((resolve, reject) => {
        const iv = setInterval(async () => {
          try {
            const r = await fetch(`${API}/api/progress/${uploadId}`);
            if (!r.ok) {
              clearInterval(iv);
              reject(new Error(`Progress check failed: ${r.status}`));
              return;
            }
            const p = await r.json();
            setProgress(p.detail ?? p.step);
            setProgressPct(p.pct ?? 0);
            if (p.step === "done") {
              clearInterval(iv);
              const res = await fetch(`${API}/api/result/${uploadId}`);
              if (!res.ok) {
                reject(new Error(`Fetching result failed: ${res.status}`));
                return;
              }
              resolve(await res.json());
            } else if (p.step === "error") {
              clearInterval(iv);
              reject(new Error(p.detail ?? "Analysis failed"));
            }
          } catch (err) {
            clearInterval(iv);
            reject(err);
          }
        }, 1500);
      }),
    [],
  );

  // ── Actions ──
  const upload = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = e.currentTarget;
    const inp = form.elements.namedItem("pdf") as HTMLInputElement | null;
    if (!inp?.files?.[0]) return;
    const fd = new FormData();
    fd.append("file", inp.files[0]);
    if (projName.trim()) fd.append("project_name", projName.trim());
    if (pdHasValues) fd.append("project_details", JSON.stringify(pd));
    setUploading(true);
    setProgress("Uploading...");
    setProgressPct(5);
    try {
      const r = await fetch(`${API}/api/analyze`, { method: "POST", body: fd });
      if (!r.ok) {
        setProgress(`Error: ${await r.text()}`);
        setProgressPct(0);
        return;
      }
      const { upload_id } = await r.json();
      setProgress("Analysis started...");
      setProgressPct(10);
      const d = await pollProgress(upload_id);
      setProgressPct(100);
      setProgress("Done!");
      setRuns((p) => [d, ...p.filter((x) => x.id !== d.id)]);
      setRunId(d.id);
      setCat("All");
      setStatusFilter("all");
      form.reset();
      setProjName("");
      setTimeout(() => {
        setProgress("");
        setProgressPct(0);
      }, 1200);
    } catch (err) {
      setProgress(err instanceof Error ? err.message : "Failed. Check logs.");
      setProgressPct(0);
    } finally {
      setUploading(false);
    }
  };

  const deleteRun = async (id: string) => {
    if (!confirm("Delete this run and all its results? This cannot be undone."))
      return;
    await fetch(`${API}/api/runs/${id}`, { method: "DELETE" });
    setRuns((p) => p.filter((x) => x.id !== id));
    if (runId === id) {
      setRunId(null);
      setCat("All");
      setStatusFilter("all");
    }
  };

  const reanalyze = async (id: string) => {
    if (
      !confirm(
        "Re-run analysis on this PDF? The current results will be replaced.",
      )
    )
      return;
    setUploading(true);
    setProgress("Re-analyzing...");
    setProgressPct(10);
    try {
      const r = await fetch(`${API}/api/runs/${id}/reanalyze`, {
        method: "POST",
      });
      if (!r.ok) {
        setProgress(`Error: ${await r.text()}`);
        setProgressPct(0);
        return;
      }
      const { upload_id } = await r.json();
      setProgress("Re-analysis started...");
      setProgressPct(15);
      const d = await pollProgress(upload_id);
      setProgressPct(100);
      setProgress("Done!");
      setRuns((p) => [d, ...p.filter((x) => x.id !== id && x.id !== d.id)]);
      setRunId(d.id);
      setCat("All");
      setStatusFilter("all");
      setTimeout(() => {
        setProgress("");
        setProgressPct(0);
      }, 1200);
    } catch (err) {
      setProgress(err instanceof Error ? err.message : "Re-analysis failed.");
      setProgressPct(0);
    } finally {
      setUploading(false);
    }
  };

  const quickStatus = async (issue: Issue, status: Status) => {
    if (!run) return;
    await fetch(`${API}/api/issues/${issue.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        status,
        override_comment:
          status === "Overridden / Accepted by QC Engineer"
            ? issue.override_comment || "Accepted by QC"
            : issue.override_comment,
      }),
    });
    await refresh(run.id);
  };

  const saveIssue = async () => {
    if (!editId || !run) return;
    setSaving(true);
    await fetch(`${API}/api/issues/${editId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        status: editStatus,
        override_comment: editComment || null,
      }),
    });
    setSaving(false);
    setEditId(null);
    await refresh(run.id);
  };

  const addManual = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!run) return;
    await fetch(`${API}/api/issues/manual`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_id: run.id,
        category: manual.category,
        title: manual.title,
        description: manual.description,
        severity: manual.severity,
        page_number: manual.page_number ? Number(manual.page_number) : null,
        evidence: manual.evidence || null,
      }),
    });
    setManual({
      category: CATEGORIES[0],
      title: "",
      description: "",
      page_number: "",
      severity: "medium",
      evidence: "",
    });
    setShowManual(false);
    await refresh(run.id);
  };

  // ── Render ──
  return (
    <div className="app">
      {/* ── Sidebar ── */}
      <aside className={`side ${sideOpen ? "" : "collapsed"}`}>
        <div className="side-head">
          <div className="brand">
            <div className="brand-mark">CE</div>
            <div className="brand-text">
              <span className="brand-sub">Castillo Engineering</span>
              <span className="brand-name">Planset QC <span style={{fontSize:"0.6em",opacity:0.5,fontWeight:400}}>v0.2.0</span></span>
            </div>
          </div>
          <button
            className="side-toggle"
            onClick={() => setSideOpen(!sideOpen)}
          >
            {sideOpen ? "\u2039" : "\u203A"}
          </button>
        </div>

        {sideOpen && (
          <>
            <form className="upload-form" onSubmit={upload}>
              <input
                value={projName}
                onChange={(e) => setProjName(e.target.value)}
                placeholder="Project name"
                className="si"
              />
              <input
                name="pdf"
                type="file"
                accept="application/pdf"
                className="si"
              />
              <button
                type="button"
                className={`btn-pd-toggle ${pdHasValues ? "btn-pd-active" : ""}`}
                onClick={() => setShowProjDetails(!showProjDetails)}
              >
                {showProjDetails ? "\u25B4 Hide" : "\u25BE Fill"} Project
                Details
                {pdHasValues && <span className="btn-pd-dot" />}
              </button>
              <button className="btn-upload" disabled={uploading}>
                {uploading ? "Analyzing\u2026" : "Analyze PDF"}
              </button>
              {(uploading || progress) && (
                <div className="prog">
                  <div className="prog-track">
                    <div
                      className="prog-bar"
                      style={{ width: `${progressPct}%` }}
                    />
                  </div>
                  <div className="prog-label">
                    {progress} <strong>{progressPct}%</strong>
                  </div>
                </div>
              )}
            </form>

            <div className="run-list">
              <div className="run-list-title">Analysis Runs</div>
              {runs.map((r) => (
                <div
                  key={r.id}
                  className={`run-item ${r.id === runId ? "active" : ""}`}
                  onClick={() => {
                    void refresh(r.id);
                    setCat("All");
                    setStatusFilter("all");
                  }}
                >
                  <div className="run-item-name">{r.project_name}</div>
                  <div className="run-item-meta">{r.original_filename}</div>
                  <div className="run-item-pills">
                    <span className="pill pill-p">
                      {r.issues
                        ? r.issues.filter((i) => i.status === "Pass").length
                        : (r.status_counts.Pass ?? 0)}
                    </span>
                    <span className="pill pill-f">
                      {r.issues
                        ? r.issues.filter((i) => i.status === "Fail").length
                        : (r.status_counts.Fail ?? 0)}
                    </span>
                    <span className="pill pill-r">
                      {r.issues
                        ? r.issues.filter((i) => i.status === "Needs Review")
                            .length
                        : (r.status_counts["Needs Review"] ?? 0)}
                    </span>
                  </div>
                  <div className="run-item-actions">
                    <button
                      className="run-act"
                      title="Re-analyze"
                      onClick={(e) => {
                        e.stopPropagation();
                        void reanalyze(r.id);
                      }}
                      disabled={uploading}
                    >
                      &#8635;
                    </button>
                    <button
                      className="run-act run-act-del"
                      title="Delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        void deleteRun(r.id);
                      }}
                    >
                      &times;
                    </button>
                  </div>
                </div>
              ))}
              {!runs.length && <div className="dim">No runs yet</div>}
            </div>
          </>
        )}
      </aside>

      {/* ── Main ── */}
      <main className="content">
        {/* ── Project Details Form ── */}
        {showProjDetails && (
          <div className="pd-panel">
            <div className="pd-header">
              <h2 className="pd-title">Project Details Template</h2>
              <p className="pd-sub">
                Fill in known values or upload documents to auto-fill. The AI
                will compare these against the planset and flag mismatches.
                <a className="pd-download" href={`${API}/api/due-diligence-template`} target="_blank" rel="noreferrer">&#8681; Download Excel Template</a>
              </p>
              <button
                className="pd-close"
                onClick={() => setShowProjDetails(false)}
              >
                &times;
              </button>
            </div>

            {/* Document upload for auto-fill */}
            <div className="pd-upload-zone">
              <label
                className={`pd-dropzone ${parsing ? "pd-dropzone-busy" : ""}`}
              >
                <input
                  type="file"
                  multiple
                  accept=".pdf,.png,.jpg,.jpeg,.txt,.eml,.csv,.docx,.doc,.xlsx,.xls"
                  style={{ display: "none" }}
                  onChange={(e) =>
                    e.target.files?.length && parseDocuments(e.target.files)
                  }
                  disabled={parsing}
                />
                {parsing ? (
                  <span className="pd-drop-text">
                    &#9881; AI is reading documents...
                  </span>
                ) : (
                  <>
                    <span className="pd-drop-icon">&#128206;</span>
                    <span className="pd-drop-text">
                      Upload emails, interconnection agreements, CESIR, impact
                      studies, equipment submittals, or any project docs
                    </span>
                    <span className="pd-drop-hint">
                      PDF, images, Word, Excel, text, email &middot; AI will
                      extract project details automatically
                    </span>
                  </>
                )}
              </label>
              {parseMsg && <div className="pd-parse-msg">{parseMsg}</div>}
            </div>

            <div className="pd-grid">
              <fieldset className="pd-section">
                <legend>Project Info</legend>
                <label>
                  Project Name
                  <input
                    value={pd.project_name ?? ""}
                    onChange={(e) => pdSet("project_name", e.target.value)}
                  />
                </label>
                <label>
                  Address
                  <input
                    value={pd.project_address ?? ""}
                    onChange={(e) => pdSet("project_address", e.target.value)}
                  />
                </label>
                <label>
                  Coordinates
                  <input
                    value={pd.site_coordinates ?? ""}
                    onChange={(e) => pdSet("site_coordinates", e.target.value)}
                    placeholder="(lat, long)"
                  />
                </label>
                <label>
                  County
                  <input
                    value={pd.county ?? ""}
                    onChange={(e) => pdSet("county", e.target.value)}
                  />
                </label>
                <label>
                  State
                  <input
                    value={pd.state ?? ""}
                    onChange={(e) => pdSet("state", e.target.value)}
                  />
                </label>
                <label>
                  Parcel ID
                  <input
                    value={pd.parcel_id ?? ""}
                    onChange={(e) => pdSet("parcel_id", e.target.value)}
                  />
                </label>
                <label>
                  Building Codes
                  <input
                    value={pd.building_codes ?? ""}
                    onChange={(e) => pdSet("building_codes", e.target.value)}
                    placeholder="NEC 2020, IBC 2021..."
                  />
                </label>
                <label>
                  DER Number
                  <input
                    value={pd.der_number ?? ""}
                    onChange={(e) => pdSet("der_number", e.target.value)}
                  />
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>Owner / EPC / Engineering</legend>
                <label>
                  Owner Name
                  <input
                    value={pd.owner_name ?? ""}
                    onChange={(e) => pdSet("owner_name", e.target.value)}
                  />
                </label>
                <label>
                  Owner Address
                  <input
                    value={pd.owner_address ?? ""}
                    onChange={(e) => pdSet("owner_address", e.target.value)}
                  />
                </label>
                <label>
                  Owner Phone
                  <input
                    value={pd.owner_phone ?? ""}
                    onChange={(e) => pdSet("owner_phone", e.target.value)}
                  />
                </label>
                <label>
                  EPC Name
                  <input
                    value={pd.epc_name ?? ""}
                    onChange={(e) => pdSet("epc_name", e.target.value)}
                  />
                </label>
                <label>
                  EPC Address
                  <input
                    value={pd.epc_address ?? ""}
                    onChange={(e) => pdSet("epc_address", e.target.value)}
                  />
                </label>
                <label>
                  EPC Phone
                  <input
                    value={pd.epc_phone ?? ""}
                    onChange={(e) => pdSet("epc_phone", e.target.value)}
                  />
                </label>
                <label>
                  EOR Name
                  <input
                    value={pd.eor_name ?? ""}
                    onChange={(e) => pdSet("eor_name", e.target.value)}
                  />
                </label>
                <label>
                  EOR License #
                  <input
                    value={pd.eor_license ?? ""}
                    onChange={(e) => pdSet("eor_license", e.target.value)}
                  />
                </label>
                <label>
                  EOR State
                  <input
                    value={pd.eor_state ?? ""}
                    onChange={(e) => pdSet("eor_state", e.target.value)}
                  />
                </label>
                <label>
                  Checker
                  <input
                    value={pd.checker_name ?? ""}
                    onChange={(e) => pdSet("checker_name", e.target.value)}
                  />
                </label>
                <label>
                  Designer
                  <input
                    value={pd.designer_name ?? ""}
                    onChange={(e) => pdSet("designer_name", e.target.value)}
                  />
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>PV Module</legend>
                <label>
                  Make
                  <input
                    value={pd.module_make ?? ""}
                    onChange={(e) => pdSet("module_make", e.target.value)}
                  />
                </label>
                <label>
                  Model
                  <input
                    value={pd.module_model ?? ""}
                    onChange={(e) => pdSet("module_model", e.target.value)}
                  />
                </label>
                <label>
                  STC Rating (W)
                  <input
                    value={pd.module_stc_watts ?? ""}
                    onChange={(e) => pdSet("module_stc_watts", e.target.value)}
                  />
                </label>
                <label>
                  Voc (V)
                  <input
                    value={pd.module_voc ?? ""}
                    onChange={(e) => pdSet("module_voc", e.target.value)}
                  />
                </label>
                <label>
                  Vmp (V)
                  <input
                    value={pd.module_vmp ?? ""}
                    onChange={(e) => pdSet("module_vmp", e.target.value)}
                  />
                </label>
                <label>
                  Isc (A)
                  <input
                    value={pd.module_isc ?? ""}
                    onChange={(e) => pdSet("module_isc", e.target.value)}
                  />
                </label>
                <label>
                  Imp (A)
                  <input
                    value={pd.module_imp ?? ""}
                    onChange={(e) => pdSet("module_imp", e.target.value)}
                  />
                </label>
                <label>
                  Voc Temp Coeff (%/C)
                  <input
                    value={pd.module_temp_coeff_voc ?? ""}
                    onChange={(e) =>
                      pdSet("module_temp_coeff_voc", e.target.value)
                    }
                    placeholder="-0.27"
                  />
                </label>
                <label>
                  Isc Temp Coeff (%/C)
                  <input
                    value={pd.module_temp_coeff_isc ?? ""}
                    onChange={(e) =>
                      pdSet("module_temp_coeff_isc", e.target.value)
                    }
                    placeholder="0.05"
                  />
                </label>
                <label>
                  Bifacial?
                  <select
                    value={pd.is_bifacial ?? ""}
                    onChange={(e) => pdSet("is_bifacial", e.target.value)}
                  >
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                  </select>
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>System Configuration</legend>
                <label>
                  String Size (mod/str)
                  <input
                    value={pd.string_size ?? ""}
                    onChange={(e) => pdSet("string_size", e.target.value)}
                  />
                </label>
                <label>
                  Total Strings
                  <input
                    value={pd.string_quantity ?? ""}
                    onChange={(e) => pdSet("string_quantity", e.target.value)}
                  />
                </label>
                <label>
                  Total DC (kW)
                  <input
                    value={pd.total_dc_kw ?? ""}
                    onChange={(e) => pdSet("total_dc_kw", e.target.value)}
                  />
                </label>
                <label>
                  Total AC (kVA)
                  <input
                    value={pd.total_ac_kva ?? ""}
                    onChange={(e) => pdSet("total_ac_kva", e.target.value)}
                  />
                </label>
                <label>
                  DC/AC Ratio
                  <input
                    value={pd.dc_ac_ratio ?? ""}
                    onChange={(e) => pdSet("dc_ac_ratio", e.target.value)}
                  />
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>Inverter</legend>
                <label>
                  Make
                  <input
                    value={pd.inverter_make ?? ""}
                    onChange={(e) => pdSet("inverter_make", e.target.value)}
                  />
                </label>
                <label>
                  Model
                  <input
                    value={pd.inverter_model ?? ""}
                    onChange={(e) => pdSet("inverter_model", e.target.value)}
                  />
                </label>
                <label>
                  kVA Rating
                  <input
                    value={pd.inverter_kva ?? ""}
                    onChange={(e) => pdSet("inverter_kva", e.target.value)}
                  />
                </label>
                <label>
                  kW Rating
                  <input
                    value={pd.inverter_kw ?? ""}
                    onChange={(e) => pdSet("inverter_kw", e.target.value)}
                  />
                </label>
                <label>
                  Max Vdc
                  <input
                    value={pd.inverter_max_vdc ?? ""}
                    onChange={(e) => pdSet("inverter_max_vdc", e.target.value)}
                  />
                </label>
                <label>
                  MPPT Range
                  <input
                    value={pd.inverter_mppt_range ?? ""}
                    onChange={(e) =>
                      pdSet("inverter_mppt_range", e.target.value)
                    }
                    placeholder="600-1500V"
                  />
                </label>
                <label>
                  Quantity
                  <input
                    value={pd.inverter_quantity ?? ""}
                    onChange={(e) => pdSet("inverter_quantity", e.target.value)}
                  />
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>Racking</legend>
                <label>
                  Make
                  <input
                    value={pd.racking_make ?? ""}
                    onChange={(e) => pdSet("racking_make", e.target.value)}
                  />
                </label>
                <label>
                  Model
                  <input
                    value={pd.racking_model ?? ""}
                    onChange={(e) => pdSet("racking_model", e.target.value)}
                  />
                </label>
                <label>
                  Type
                  <select
                    value={pd.racking_type ?? ""}
                    onChange={(e) => pdSet("racking_type", e.target.value)}
                  >
                    <option value="">--</option>
                    <option value="fixed">Fixed Tilt</option>
                    <option value="tracker">Tracker</option>
                  </select>
                </label>
                <label>
                  Pitch
                  <input
                    value={pd.pitch ?? ""}
                    onChange={(e) => pdSet("pitch", e.target.value)}
                  />
                </label>
                <label>
                  Interrow Spacing
                  <input
                    value={pd.interrow_spacing ?? ""}
                    onChange={(e) => pdSet("interrow_spacing", e.target.value)}
                  />
                </label>
                <label>
                  GCR
                  <input
                    value={pd.gcr ?? ""}
                    onChange={(e) => pdSet("gcr", e.target.value)}
                  />
                </label>
                <label>
                  Tilt Angle
                  <input
                    value={pd.tilt_angle ?? ""}
                    onChange={(e) => pdSet("tilt_angle", e.target.value)}
                  />
                </label>
                <label>
                  Azimuth
                  <input
                    value={pd.azimuth ?? ""}
                    onChange={(e) => pdSet("azimuth", e.target.value)}
                  />
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>Transformer</legend>
                <label>
                  kVA Rating
                  <input
                    value={pd.transformer_kva ?? ""}
                    onChange={(e) => pdSet("transformer_kva", e.target.value)}
                  />
                </label>
                <label>
                  Primary Voltage
                  <input
                    value={pd.transformer_primary_voltage ?? ""}
                    onChange={(e) =>
                      pdSet("transformer_primary_voltage", e.target.value)
                    }
                  />
                </label>
                <label>
                  Secondary Voltage
                  <input
                    value={pd.transformer_secondary_voltage ?? ""}
                    onChange={(e) =>
                      pdSet("transformer_secondary_voltage", e.target.value)
                    }
                  />
                </label>
                <label>
                  Winding Config
                  <input
                    value={pd.transformer_winding_config ?? ""}
                    onChange={(e) =>
                      pdSet("transformer_winding_config", e.target.value)
                    }
                    placeholder="Delta-Wye"
                  />
                </label>
                <label>
                  Impedance Z%
                  <input
                    value={pd.transformer_impedance ?? ""}
                    onChange={(e) =>
                      pdSet("transformer_impedance", e.target.value)
                    }
                  />
                </label>
                <label>
                  BIL (kV)
                  <input
                    value={pd.transformer_bil ?? ""}
                    onChange={(e) => pdSet("transformer_bil", e.target.value)}
                  />
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>Utility / POI</legend>
                <label>
                  Utility Name
                  <input
                    value={pd.utility_name ?? ""}
                    onChange={(e) => pdSet("utility_name", e.target.value)}
                  />
                </label>
                <label>
                  Feeder
                  <input
                    value={pd.utility_feeder ?? ""}
                    onChange={(e) => pdSet("utility_feeder", e.target.value)}
                  />
                </label>
                <label>
                  NGrid?
                  <select
                    value={pd.is_ngrid ?? ""}
                    onChange={(e) => pdSet("is_ngrid", e.target.value)}
                  >
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                  </select>
                </label>
                <label>
                  POI Voltage
                  <input
                    value={pd.poi_voltage ?? ""}
                    onChange={(e) => pdSet("poi_voltage", e.target.value)}
                  />
                </label>
                <label>
                  Feeder Grounding
                  <input
                    value={pd.feeder_grounding ?? ""}
                    onChange={(e) => pdSet("feeder_grounding", e.target.value)}
                  />
                </label>
                <label>
                  Fault Current (kA)
                  <input
                    value={pd.fault_current ?? ""}
                    onChange={(e) => pdSet("fault_current", e.target.value)}
                  />
                </label>
                <label>
                  IEEE 1547 Category
                  <select
                    value={pd.ieee_category ?? ""}
                    onChange={(e) => pdSet("ieee_category", e.target.value)}
                  >
                    <option value="">--</option>
                    <option value="I">Category I</option>
                    <option value="II">Category II</option>
                    <option value="III">Category III</option>
                  </select>
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>MV / Protection</legend>
                <label>
                  Recloser Make
                  <input
                    value={pd.recloser_make ?? ""}
                    onChange={(e) => pdSet("recloser_make", e.target.value)}
                  />
                </label>
                <label>
                  Recloser Continuous (A)
                  <input
                    value={pd.recloser_continuous_a ?? ""}
                    onChange={(e) =>
                      pdSet("recloser_continuous_a", e.target.value)
                    }
                  />
                </label>
                <label>
                  Recloser Interrupting (kA)
                  <input
                    value={pd.recloser_interrupting_ka ?? ""}
                    onChange={(e) =>
                      pdSet("recloser_interrupting_ka", e.target.value)
                    }
                  />
                </label>
                <label>
                  CT Ratio
                  <input
                    value={pd.ct_ratio ?? ""}
                    onChange={(e) => pdSet("ct_ratio", e.target.value)}
                    placeholder="300:1"
                  />
                </label>
                <label>
                  VT Ratio
                  <input
                    value={pd.vt_ratio ?? ""}
                    onChange={(e) => pdSet("vt_ratio", e.target.value)}
                    placeholder="234.5"
                  />
                </label>
                <label>
                  Meter Accuracy
                  <input
                    value={pd.meter_accuracy_class ?? ""}
                    onChange={(e) =>
                      pdSet("meter_accuracy_class", e.target.value)
                    }
                    placeholder="0.3"
                  />
                </label>
                <label>
                  Surge Arrestor MCOV
                  <input
                    value={pd.surge_arrestor_mcov ?? ""}
                    onChange={(e) =>
                      pdSet("surge_arrestor_mcov", e.target.value)
                    }
                  />
                </label>
              </fieldset>

              <fieldset className="pd-section">
                <legend>Design Temperatures</legend>
                <label>
                  Low Temp (C)
                  <input
                    value={pd.design_temp_low_c ?? ""}
                    onChange={(e) => pdSet("design_temp_low_c", e.target.value)}
                    placeholder="-20"
                  />
                </label>
                <label>
                  High Temp (C)
                  <input
                    value={pd.design_temp_high_c ?? ""}
                    onChange={(e) =>
                      pdSet("design_temp_high_c", e.target.value)
                    }
                    placeholder="45"
                  />
                </label>
                <label>
                  Ambient Temp (C)
                  <input
                    value={pd.ambient_temp_c ?? ""}
                    onChange={(e) => pdSet("ambient_temp_c", e.target.value)}
                    placeholder="35"
                  />
                </label>
              </fieldset>

              <fieldset className="pd-section pd-section-wide">
                <legend>Special Notes</legend>
                <label>
                  Notes
                  <textarea
                    value={pd.special_notes ?? ""}
                    onChange={(e) => pdSet("special_notes", e.target.value)}
                    rows={3}
                    placeholder="Any special requirements, deviations, or notes for the QC reviewer..."
                  />
                </label>
              </fieldset>
            </div>
            <div className="pd-footer">
              <button
                type="button"
                className="btn-cancel"
                onClick={() => {
                  setPd({});
                }}
              >
                Clear All
              </button>
              <button
                type="button"
                className="hdr-btn hdr-btn-accent"
                onClick={() => setShowProjDetails(false)}
              >
                Done
              </button>
            </div>
          </div>
        )}

        {!run ? (
          !showProjDetails ? (
            <div className="welcome">
              <div className="welcome-icon">&#9889;</div>
              <h2>Upload a planset PDF to start</h2>
              <p>
                {CATEGORIES.length * 7}+ checks across {CATEGORIES.length}{" "}
                categories. AI-powered engineering validation with NEC code
                references.
              </p>
              <a className="hdr-btn" href={`${API}/api/due-diligence-template`} target="_blank" rel="noreferrer">&#8681; Download Due Diligence Template</a>
            </div>
          ) : null
        ) : (
          <>
            {/* ── Header ── */}
            <header className="hdr">
              <div className="hdr-left">
                <h1 className="hdr-title">{run.project_name}</h1>
                <div className="hdr-meta">
                  {run.original_filename} &middot; {run.page_count} pages
                  &middot; {new Date(run.created_at).toLocaleDateString()}
                </div>
                <GeminiBar u={run.summary.gemini_usage} />
              </div>
              <div className="hdr-right">
                <button
                  className="hdr-btn"
                  onClick={() => void reanalyze(run.id)}
                  disabled={uploading}
                >
                  {uploading ? "Analyzing\u2026" : "\u21bb Re-analyze"}
                </button>
                <a
                  className="hdr-btn"
                  href={artifactUrl(run.pdf_path)}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open PDF
                </a>
                <a
                  className="hdr-btn hdr-btn-accent"
                  href={`${API}/api/export/${run.id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Export Excel
                </a>
                <button
                  className="hdr-btn hdr-btn-danger"
                  onClick={() => void deleteRun(run.id)}
                >
                  Delete
                </button>
              </div>
            </header>

            {/* ── Score cards ── */}
            <div className="scores">
              <div className="sc sc-total">
                <div className="sc-val">{(run.issues ?? []).length}</div>
                <div className="sc-lab">Total Checks</div>
              </div>
              <div
                className="sc sc-pass"
                onClick={() => {
                  setCat("All");
                  setStatusFilter("pass");
                }}
              >
                <div className="sc-val">{liveStatusCounts.Pass}</div>
                <div className="sc-lab">Pass</div>
              </div>
              <div
                className="sc sc-fail"
                onClick={() => {
                  setCat("All");
                  setStatusFilter("fail");
                }}
              >
                <div className="sc-val">{liveStatusCounts.Fail}</div>
                <div className="sc-lab">Fail</div>
              </div>
              <div
                className="sc sc-review"
                onClick={() => {
                  setCat("All");
                  setStatusFilter("review");
                }}
              >
                <div className="sc-val">{liveStatusCounts["Needs Review"]}</div>
                <div className="sc-lab">Review</div>
              </div>
              <div className="sc">
                <div className="sc-val">{run.summary.pdf_page_count}</div>
                <div className="sc-lab">Pages</div>
              </div>
              <div className="sc">
                <div className="sc-val">
                  {Math.round(
                    ((liveStatusCounts.Pass +
                      liveStatusCounts[
                        "Overridden / Accepted by QC Engineer"
                      ]) /
                      Math.max((run.issues ?? []).length, 1)) *
                      100,
                  )}
                  %
                </div>
                <div className="sc-lab">Complete</div>
              </div>
              <div className={`sc ${issuesOnly ? "sc-fail" : ""}`}
                onClick={() => setIssuesOnly(!issuesOnly)}
                style={{cursor:"pointer"}}
                title="Toggle: show only Fail + Needs Review items"
              >
                <div className="sc-val">{liveStatusCounts.Fail + liveStatusCounts["Needs Review"]}</div>
                <div className="sc-lab">{issuesOnly ? "Showing Issues" : "Issues Only"}</div>
              </div>
            </div>

            {/* ── Two-column: categories + issues ── */}
            <div className="workspace">
              {/* ── Category sidebar ── */}
              <nav className="cat-nav">
                <button
                  className={`cat-item ${cat === "All" ? "cat-active" : ""}`}
                  onClick={() => {
                    setCat("All");
                    setStatusFilter("all");
                  }}
                >
                  <span className="cat-icon cat-icon-all">*</span>
                  <span className="cat-label">All Categories</span>
                  <span className="cat-count">{(run.issues ?? []).length}</span>
                </button>

                {liveCategories.map((c) => {
                  const h = catHealth(c);
                  const pct = catPct(c);
                  return (
                    <button
                      key={c.name}
                      className={`cat-item ${cat === c.name ? "cat-active" : ""}`}
                      onClick={() => {
                        setCat(c.name);
                        setStatusFilter("all");
                      }}
                    >
                      <span className={`cat-icon cat-icon-${h}`}>
                        {h === "pass"
                          ? "\u2713"
                          : h === "fail"
                            ? "\u2717"
                            : "!"}
                      </span>
                      <div className="cat-info">
                        <span className="cat-label">{c.name}</span>
                        <div className="cat-bar">
                          <div
                            className="cat-bar-fill"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                      </div>
                      <span className="cat-nums">
                        <span className="cn-p">{c.Pass ?? 0}</span>
                        <span className="cn-f">{c.Fail ?? 0}</span>
                        <span className="cn-r">{c["Needs Review"] ?? 0}</span>
                      </span>
                    </button>
                  );
                })}
              </nav>

              {/* ── Issue list ── */}
              <section className="issue-panel">
                {/* Toolbar */}
                <div className="toolbar">
                  <div className="toolbar-filters">
                    {(
                      [
                        ["all", "All", counts.t],
                        ["pass", "Pass", counts.p],
                        ["fail", "Fail", counts.f],
                        ["review", "Review", counts.r],
                        ["override", "Accepted", counts.o],
                      ] as [string, string, number][]
                    ).map(([k, l, n]) =>
                      n > 0 || k === "all" ? (
                        <button
                          key={k}
                          className={`fb fb-${k} ${statusFilter === k ? "fb-on" : ""}`}
                          onClick={() => setStatusFilter(k)}
                        >
                          {l} <strong>{n}</strong>
                        </button>
                      ) : null,
                    )}
                  </div>
                  <button
                    className="btn-add"
                    onClick={() => setShowManual(!showManual)}
                  >
                    + Add Issue
                  </button>
                </div>

                {/* Manual issue form */}
                {showManual && (
                  <form className="add-form" onSubmit={addManual}>
                    <select
                      value={manual.category}
                      onChange={(e) =>
                        setManual((v) => ({ ...v, category: e.target.value }))
                      }
                    >
                      {CATEGORIES.map((c) => (
                        <option key={c}>{c}</option>
                      ))}
                    </select>
                    <input
                      placeholder="Title"
                      value={manual.title}
                      onChange={(e) =>
                        setManual((v) => ({ ...v, title: e.target.value }))
                      }
                      required
                    />
                    <input
                      placeholder="Evidence / notes"
                      value={manual.evidence}
                      onChange={(e) =>
                        setManual((v) => ({ ...v, evidence: e.target.value }))
                      }
                      style={{ flex: 2 }}
                    />
                    <input
                      type="number"
                      placeholder="Pg"
                      style={{ width: 56 }}
                      value={manual.page_number}
                      onChange={(e) =>
                        setManual((v) => ({
                          ...v,
                          page_number: e.target.value,
                        }))
                      }
                    />
                    <select
                      value={manual.severity}
                      onChange={(e) =>
                        setManual((v) => ({ ...v, severity: e.target.value }))
                      }
                      style={{ width: 90 }}
                    >
                      <option value="high">High</option>
                      <option value="medium">Medium</option>
                      <option value="low">Low</option>
                    </select>
                    <button type="submit" className="btn-add">
                      Add
                    </button>
                  </form>
                )}

                {/* Issue cards */}
                <div className="issue-list">
                  {issues.length === 0 && (
                    <div
                      className="dim"
                      style={{ padding: "3rem", textAlign: "center" }}
                    >
                      No items match this filter.
                    </div>
                  )}
                  {issues.map((issue) => {
                    const isAI = issue.item_key.startsWith("ai_");
                    const hasPreview = !!(
                      issue.snippet_path || issue.page_preview_path
                    );
                    const evExpanded = expandedEv === issue.id;
                    const isEditing = editId === issue.id;
                    return (
                      <div
                        key={issue.id}
                        className={`card card-${catHealth({ name: "", total: 1, [issue.status === "Overridden / Accepted by QC Engineer" ? "Pass" : issue.status]: 1 } as CategorySummary)}`}
                      >
                        {/* Row 1: header with status, title, actions */}
                        <div className="card-row">
                          <span
                            className={`badge badge-${issue.status === "Overridden / Accepted by QC Engineer" ? "ok" : issue.status === "Pass" ? "pass" : issue.status === "Fail" ? "fail" : "review"}`}
                          >
                            {SL[issue.status] ?? issue.status}
                          </span>
                          <span className={`sev sev-${issue.severity}`}>
                            {SV[issue.severity]}
                          </span>

                          <div
                            className="card-body"
                            onClick={() =>
                              setExpandedEv(evExpanded ? null : issue.id)
                            }
                            style={{ cursor: "pointer" }}
                          >
                            <div className="card-title">
                              {issue.title}
                              {isAI && <span className="ai">AI</span>}
                              <span className="card-conf">
                                {Math.round(issue.confidence * 100)}%
                              </span>
                            </div>
                            <div className="card-sub">{issue.category}</div>
                          </div>

                          {issue.page_number ? (
                            <a
                              className="pg"
                              href={pdfPageUrl(run, issue.page_number)!}
                              target="_blank"
                              rel="noreferrer"
                              title={`Page ${issue.page_number}`}
                            >
                              p.{issue.page_number}
                            </a>
                          ) : (
                            <span className="pg dim">&mdash;</span>
                          )}

                          {/* Quick status */}
                          <div className="card-actions">
                            <StatusBtn
                              current={issue.status}
                              target="Pass"
                              label="&#10003;"
                              color="#16a34a"
                              onClick={() => quickStatus(issue, "Pass")}
                            />
                            <StatusBtn
                              current={issue.status}
                              target="Fail"
                              label="&#10007;"
                              color="#dc2626"
                              onClick={() => quickStatus(issue, "Fail")}
                            />
                            <StatusBtn
                              current={issue.status}
                              target="Needs Review"
                              label="?"
                              color="#d97706"
                              onClick={() => quickStatus(issue, "Needs Review")}
                            />
                            <StatusBtn
                              current={issue.status}
                              target="Overridden / Accepted by QC Engineer"
                              label="OK"
                              color="#7c3aed"
                              onClick={() =>
                                quickStatus(
                                  issue,
                                  "Overridden / Accepted by QC Engineer",
                                )
                              }
                            />
                            {hasPreview && (
                              <button
                                className="ib"
                                onClick={() => setSel(issue)}
                                title="Preview"
                              >
                                &#128065;
                              </button>
                            )}
                            <button
                              className="ib"
                              onClick={() =>
                                isEditing
                                  ? setEditId(null)
                                  : (setEditId(issue.id),
                                    setEditStatus(issue.status),
                                    setEditComment(
                                      issue.override_comment ?? "",
                                    ))
                              }
                              title="Comment"
                            >
                              &#9998;
                            </button>
                          </div>
                        </div>

                        {/* Row 2: description (what was checked) */}
                        {issue.description && (
                          <div className="card-desc">{issue.description}</div>
                        )}

                        {/* Row 3: AI findings / evidence — always visible */}
                        {issue.evidence && (
                          <div
                            className={`card-evidence ${evExpanded ? "card-evidence-full" : ""}`}
                            onClick={() =>
                              setExpandedEv(evExpanded ? null : issue.id)
                            }
                          >
                            <span className="card-evidence-label">
                              {isAI ? "AI Finding" : "Evidence"}
                            </span>
                            <span className="card-evidence-text">
                              {evExpanded
                                ? issue.evidence
                                : issue.evidence.length > 200
                                  ? issue.evidence.slice(0, 200) + "..."
                                  : issue.evidence}
                            </span>
                            {issue.evidence.length > 200 && (
                              <span className="card-evidence-toggle">
                                {evExpanded ? "Show less" : "Show more"}
                              </span>
                            )}
                          </div>
                        )}

                        {/* Override comment if present */}
                        {issue.override_comment && !isEditing && (
                          <div className="card-override">
                            <span className="card-evidence-label">QC Note</span>
                            <span className="card-evidence-text">
                              {issue.override_comment}
                            </span>
                          </div>
                        )}

                        {/* Edit row */}
                        {isEditing && (
                          <div className="card-edit">
                            <select
                              value={editStatus}
                              onChange={(e) =>
                                setEditStatus(e.target.value as Status)
                              }
                            >
                              {STATUSES.map((s) => (
                                <option key={s} value={s}>
                                  {s}
                                </option>
                              ))}
                            </select>
                            <input
                              value={editComment}
                              onChange={(e) => setEditComment(e.target.value)}
                              placeholder="QC note (optional)"
                              style={{ flex: 1 }}
                            />
                            <button
                              className="btn-add"
                              onClick={saveIssue}
                              disabled={saving}
                            >
                              {saving ? "Saving..." : "Save"}
                            </button>
                            <button
                              className="btn-cancel"
                              onClick={() => setEditId(null)}
                            >
                              Cancel
                            </button>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>
            </div>
          </>
        )}
      </main>

      {/* ── Detail modal ── */}
      {sel && (
        <div className="overlay" onClick={() => setSel(null)}>
          <div className="detail" onClick={(e) => e.stopPropagation()}>
            <div className="detail-head">
              <div>
                <div className="detail-crumb">
                  {sel.category} &middot; Page {sel.page_number ?? "\u2014"}
                  {sel.item_key.startsWith("ai_") && (
                    <>
                      {" "}
                      &middot; <span className="ai">AI</span>
                    </>
                  )}
                </div>
                <h2 className="detail-title">{sel.title}</h2>
                <div className="detail-tags">
                  <span
                    className={`badge badge-${sel.status === "Pass" ? "pass" : sel.status === "Fail" ? "fail" : sel.status === "Needs Review" ? "review" : "ok"}`}
                  >
                    {SL[sel.status]}
                  </span>
                  <span className={`sev sev-${sel.severity}`}>
                    {SV[sel.severity]}
                  </span>
                  <span className="badge badge-neutral">
                    {Math.round(sel.confidence * 100)}% conf
                  </span>
                </div>
              </div>
              <button className="detail-close" onClick={() => setSel(null)}>
                &times;
              </button>
            </div>
            {sel.page_preview_path || sel.snippet_path ? (
              <img
                className="detail-img"
                src={artifactUrl(sel.page_preview_path || sel.snippet_path)}
                alt={sel.title}
              />
            ) : (
              <div className="dim" style={{padding:"3rem",textAlign:"center"}}>
                No preview available
              </div>
            )}
            {sel.evidence && (
              <div className="detail-evidence">
                <strong>Evidence</strong>
                <p>{sel.evidence}</p>
              </div>
            )}
            {sel.description && (
              <div className="detail-desc">{sel.description}</div>
            )}
            <div className="detail-foot">
              {sel.page_number && (
                <a
                  className="hdr-btn"
                  href={pdfPageUrl(run!, sel.page_number)!}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open PDF Page {sel.page_number}
                </a>
              )}
              <a
                className="hdr-btn"
                href={artifactUrl(run!.pdf_path)}
                target="_blank"
                rel="noreferrer"
              >
                Full PDF
              </a>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
