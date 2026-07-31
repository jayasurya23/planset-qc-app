import { useEffect, useMemo, useState, useCallback, useRef } from "react";
import type {
  Issue,
  Job,
  JobsResponse,
  Me,
  RunData,
  RunFeedback,
  RunRating,
  Status,
  CategorySummary,
  GeminiUsage,
  ProjectDetails,
  SupportingDoc,
} from "./types";

const REPORT_TAGS: Array<{ value: string; label: string }> = [
  { value: "wrong_status", label: "Wrong status" },
  { value: "wrong_page", label: "Wrong page" },
  { value: "wrong_location", label: "Wrong location / bbox" },
  { value: "wrong_source_citation", label: "Wrong source citation" },
  { value: "wrong_reason", label: "Wrong reason / description" },
  { value: "wrong_category", label: "Wrong category" },
  { value: "rule_shouldnt_exist", label: "Rule shouldn't exist" },
  { value: "other", label: "Other (see comment)" },
];

// In production the built SPA is served by the FastAPI backend itself, so the
// API is same-origin (""). During local Vite dev (port 5173) the UI is served
// separately, so talk to the backend on :8000.
const API =
  window.location.port === "5173"
    ? `http://${window.location.hostname}:8000`
    : "";
const STATUSES: Status[] = [
  "Pass",
  "Fail",
  "Needs Review",
  "Deferred",
  "Overridden / Accepted by QC Engineer",
];
const DESIGN_STAGES: Array<{ value: string; label: string }> = [
  { value: "", label: "Auto-detect from title block" },
  { value: "30", label: "30%" },
  { value: "60", label: "60% / IFP" },
  { value: "90", label: "90%" },
  { value: "IFC", label: "IFC" },
  { value: "AsBuilt", label: "As-Built" },
];
// Canonical stage labels + ordering, shared by the badges and the project
// sidebar grouping. Mirrors backend rule_registry.STAGE_ORDER.
const STAGE_LABELS: Record<string, string> = {
  "30": "30%",
  "60": "60%",
  "90": "90%",
  IFC: "IFC",
  AsBuilt: "As-Built",
};
const STAGE_ORDER: Record<string, number> = {
  "30": 0,
  "60": 1,
  "90": 2,
  IFC: 3,
  AsBuilt: 4,
};
const stageRank = (s?: string | null) =>
  s && s in STAGE_ORDER ? STAGE_ORDER[s] : 99;

// Project sidebar grouping: Project → Stage → Lineage (a rerun chain keyed by
// root_run_id) → versions.
type Lineage = { root: string; latest: RunData; versions: RunData[] };
type StageGroup = { stage: string; lineages: Lineage[] };
type ProjectGroup = {
  key: string;
  name: string;
  createdBy: string | null;
  lastActivity: string;
  runCount: number;
  stages: StageGroup[];
};

function StageBadge({
  stage,
  variant = "light",
}: {
  stage?: string | null;
  variant?: "light" | "dark";
}) {
  if (!stage) return null;
  const key = String(stage);
  return (
    <span
      className={`stage-badge stage-badge-${variant} stage-${key}`}
      title={`Design stage: ${STAGE_LABELS[key] ?? key}`}
    >
      {STAGE_LABELS[key] ?? key}
    </span>
  );
}
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

function relativeDate(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const startOfDay = (x: Date) =>
    new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
  if (Number.isNaN(diff)) return "";
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff < 7) return `${diff}d ago`;
  const opts: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatDuration(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec - m * 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m - h * 60}m`;
}

// ── Tiny helpers ──
const SL: Record<string, string> = {
  Pass: "Pass",
  Fail: "Fail",
  "Needs Review": "Review",
  Deferred: "Deferred",
  "Overridden / Accepted by QC Engineer": "Accepted",
};
const SV: Record<string, string> = { high: "HIGH", medium: "MED", low: "LOW" };
function catHealth(c: CategorySummary): "pass" | "fail" | "review" | "deferred" {
  if ((c.Fail ?? 0) > 0) return "fail";
  if ((c["Needs Review"] ?? 0) > 0) return "review";
  if ((c.Pass ?? 0) === 0 && (c.Deferred ?? 0) > 0) return "deferred";
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

const WAITING_LINES: string[] = [
  "Counting modules one by one…",
  "Arguing with NEC 310.16 about ampacity…",
  "Looking for sheet E-300 in the wrong folder…",
  "Squinting at title blocks…",
  "Verifying that 4160V is not 480V (again)…",
  "Converting kcmil to feelings…",
  "Asking the inverter what its true kVA is…",
  "Re-reading the CESIR so you don't have to…",
  "Chasing a grounding conductor down a rabbit hole…",
  "Checking if bifacial means 2× the drama…",
  "Counting strings so many times it got emotional…",
  "Politely disagreeing with the designer…",
  "Confirming north arrow points up-ish…",
  "Calculating DC/AC ratio with vibes…",
  "Double-checking the PVSyst loss stack…",
  "Looking for a missing fuse rating…",
  "Zooming into the title block at 800%…",
  "Asking: is this a 3-line or a 2.5-line diagram?…",
  "Counting dashes on the dashed line (again)…",
  "Wondering if Z% should be a love language…",
  "Re-checking the BIL because BIL always lies…",
  "Making sure the inverter isn't actually a toaster…",
  "Contemplating the difference between AL and CU philosophically…",
  "Asking: where did all these disconnects come from?…",
  "Measuring working clearance in imaginary feet…",
];

const MASCOT_FRAMES = ["\u{1F50D}", "\u{1F4D0}", "\u{1F4CF}", "\u{1F4CB}", "\u{1F9EE}", "\u26A1"];
// magnifying glass, triangular ruler, straight ruler, clipboard, abacus, lightning

function WaitingAnimation({ pct, label }: { pct: number; label?: string }) {
  const [lineIdx, setLineIdx] = useState(() =>
    Math.floor(Math.random() * WAITING_LINES.length),
  );
  const [frameIdx, setFrameIdx] = useState(0);
  useEffect(() => {
    const t1 = setInterval(
      () => setLineIdx((i) => (i + 1) % WAITING_LINES.length),
      3500,
    );
    const t2 = setInterval(
      () => setFrameIdx((i) => (i + 1) % MASCOT_FRAMES.length),
      550,
    );
    return () => {
      clearInterval(t1);
      clearInterval(t2);
    };
  }, []);
  const clamped = Math.min(100, Math.max(0, pct));
  return (
    <div className="topwait" aria-live="polite" role="status">
      <div className="topwait-bar">
        <div
          className="topwait-fill"
          style={{ width: `${clamped}%` }}
        />
        <div className="topwait-shimmer" />
        <span
          className="topwait-mascot"
          style={{ left: `${Math.min(98, Math.max(1, clamped))}%` }}
        >
          {MASCOT_FRAMES[frameIdx]}
        </span>
      </div>
      <div className="topwait-meta">
        <span className="topwait-pct">{Math.round(clamped)}%</span>
        {label && <span className="topwait-step">{label}</span>}
        <span className="topwait-quip" key={lineIdx}>
          {WAITING_LINES[lineIdx]}
        </span>
      </div>
    </div>
  );
}

function GeminiBar({
  u,
  durationSeconds,
  deepMode,
  designStage,
}: {
  u?: GeminiUsage;
  durationSeconds?: number | null;
  deepMode?: boolean | null;
  designStage?: string | null;
}) {
  if (!u || !u.api_calls) return null;
  const stageLabel: Record<string, string> = {
    "30": "30%",
    "60": "60%",
    "90": "90%",
    IFC: "IFC",
    AsBuilt: "As-Built",
  };
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
      {durationSeconds != null && (
        <>
          <span className="gem-sep">&middot;</span>
          <span className="gem-text" title="Total analysis time">
            {formatDuration(durationSeconds)}
          </span>
        </>
      )}
      {deepMode != null && (
        <>
          <span className="gem-sep">&middot;</span>
          <span className={`mode-pill ${deepMode ? "mode-deep" : "mode-mini"}`}>
            {deepMode ? "Deep" : "Mini"}
          </span>
        </>
      )}
      {designStage && (
        <>
          <span className="gem-sep">&middot;</span>
          <span
            className="mode-pill stage-pill"
            title="Design stage used for rule gating. Rules requiring later stages were deferred."
          >
            Stage: {stageLabel[designStage] ?? designStage}
          </span>
        </>
      )}
    </div>
  );
}

function initials(s: string): string {
  const t = (s || "").trim();
  if (!t) return "?";
  const local = t.replace(/@.*/, "");
  const parts = local.split(/[\s._-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return local.slice(0, 2).toUpperCase();
}

// A run's display label: its custom name, else the PDF filename.
function runLabel(r: RunData): string {
  return (
    (r.run_name || "").trim() ||
    r.original_filename ||
    r.project_name ||
    "Untitled run"
  );
}

// Top-right account widget. In production the Azure Container Apps EasyAuth
// sidecar handles the actual Microsoft Entra sign-in (/.auth/login/aad) and
// sign-out (/.auth/logout); this surfaces the signed-in identity and those
// controls. When unauthenticated (or locally), it shows a Sign in button.
function ProfileMenu({ me }: { me: Me | null }) {
  const [open, setOpen] = useState(false);
  if (!me || !me.email) {
    const back = encodeURIComponent(
      typeof window !== "undefined"
        ? window.location.pathname + window.location.search
        : "/",
    );
    return (
      <div className="profile-widget">
        <a
          className="profile-signin"
          href={`/.auth/login/aad?post_login_redirect_uri=${back}`}
        >
          Sign in
        </a>
      </div>
    );
  }
  const label = me.name || me.email;
  return (
    <div className="profile-widget">
      <button
        className="profile-btn"
        onClick={() => setOpen((o) => !o)}
        title={me.email ?? undefined}
      >
        <span className="profile-avatar">{initials(label)}</span>
        <span className="profile-name">{label}</span>
        <span className="profile-caret">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <>
          <div className="profile-backdrop" onClick={() => setOpen(false)} />
          <div className="profile-menu" role="menu">
            <div className="profile-menu-head">
              <span className="profile-avatar profile-avatar-lg">
                {initials(label)}
              </span>
              <div className="profile-menu-id">
                <div className="profile-menu-name">{label}</div>
                {me.name && me.email && me.name !== me.email && (
                  <div className="profile-menu-email">{me.email}</div>
                )}
              </div>
            </div>
            <a
              className="profile-menu-item"
              href="/.auth/logout?post_logout_redirect_uri=/"
            >
              Sign out
            </a>
          </div>
        </>
      )}
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

// ── Shared "Activity" panel: every analysis in process, across all engineers.
//    Polled from GET /api/jobs; persists wherever you scroll; click a finished
//    run to open it.
interface ToastItem {
  key: string;
  kind: "queued" | "done" | "error";
  title: string;
  detail?: string;
  runId?: string | null;
}

function ActivityPanel({
  jobs,
  concurrency,
  queuedCount,
  runningCount,
  onOpenRun,
  desktopAlerts,
  onEnableAlerts,
  canDesktop,
}: {
  jobs: Job[];
  concurrency: number;
  queuedCount: number;
  runningCount: number;
  onOpenRun: (runId: string) => void;
  desktopAlerts: boolean;
  onEnableAlerts: () => void;
  canDesktop: boolean;
}) {
  const [open, setOpen] = useState(true);
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running");
  const finished = jobs
    .filter((j) => j.status === "done" || j.status === "error")
    .slice(-6)
    .reverse();
  const ordered: Job[] = [
    ...active.filter((j) => j.status === "running"),
    ...active
      .filter((j) => j.status === "queued")
      .sort((a, b) => (a.queue_position || 0) - (b.queue_position || 0)),
    ...finished,
  ];
  if (ordered.length === 0) return null;
  const activeCount = active.length;
  const nameOf = (j: Job) => j.run_name || j.project_name || "Run";
  const sub = (j: Job) => {
    const who = j.started_by ? `${j.started_by} · ` : "";
    const kind = j.kind === "reanalyze" ? "re-run" : "new run";
    if (j.status === "queued") return `${who}${kind} · Queued #${j.queue_position}`;
    if (j.status === "running") return `${who}Analyzing… ${j.detail || j.step || ""}`;
    if (j.status === "done") return `${who}Complete — click to open`;
    return `${who}Failed: ${j.detail || j.error || "error"}`;
  };

  return (
    <div className="activity">
      <button className="activity-head" onClick={() => setOpen((o) => !o)}>
        <span className="activity-title">
          {activeCount > 0 && <span className="activity-spin" />}
          Activity
        </span>
        {activeCount > 0 && <span className="activity-badge">{activeCount}</span>}
        <span className="activity-caret">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="activity-body">
          {ordered.map((j) => (
            <button
              key={j.id}
              className={`activity-row activity-${j.status}`}
              onClick={() => j.status === "done" && j.run_id && onOpenRun(j.run_id)}
              disabled={j.status !== "done"}
              title={j.status === "done" ? "Open this run" : undefined}
            >
              <span className={`activity-dot activity-dot-${j.status}`} />
              <span className="activity-info">
                <span className="activity-name">{nameOf(j)}</span>
                <span className="activity-sub">{sub(j)}</span>
                {j.status === "running" && (
                  <span className="activity-bar">
                    <span className="activity-bar-fill" style={{ width: `${j.pct}%` }} />
                  </span>
                )}
              </span>
              {j.status === "running" && (
                <span className="activity-pct">{Math.round(j.pct)}%</span>
              )}
            </button>
          ))}
          <div className="activity-foot">
            <span>
              Running {runningCount}/{concurrency}
              {queuedCount ? ` · ${queuedCount} queued` : ""}
            </span>
            {canDesktop && !desktopAlerts && (
              <button className="activity-alerts" onClick={onEnableAlerts}>
                🔔 Desktop alerts
              </button>
            )}
            {desktopAlerts && <span className="activity-alerts-on">🔔 Alerts on</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function Toasts({
  toasts,
  onOpenRun,
  onDismiss,
}: {
  toasts: ToastItem[];
  onOpenRun: (runId: string) => void;
  onDismiss: (key: string) => void;
}) {
  if (!toasts.length) return null;
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.key} className={`toast toast-${t.kind}`}>
          <div className="toast-body">
            <div className="toast-title">{t.title}</div>
            {t.detail && <div className="toast-detail">{t.detail}</div>}
          </div>
          {t.kind === "done" && t.runId && (
            <button className="toast-action" onClick={() => onOpenRun(t.runId!)}>
              View
            </button>
          )}
          <button className="toast-close" onClick={() => onDismiss(t.key)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

// ── QC Copilot chat (Phase 1: read-only, grounded in the open run) ──────────
// The chat explains, locates, and prioritizes findings; it cannot change a
// status and nothing it says reaches the Excel export — the checklist stays
// the system of record.

interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  content: string;
  model?: string | null;
}

const CHAT_STARTERS = [
  "What actually matters on this planset? Prioritize for the drafter.",
  "Summarize the critical fails and group related findings.",
  "Which sheets are missing or couldn't be read?",
];

function renderChatContent(
  content: string,
  citations: Record<string, string>,
  onCite: (issueId: string) => void,
) {
  // Light formatting: ## headings, bullets, **bold**, [#abcdef12] chips.
  return content.split("\n").map((line, li) => {
    const isHead = /^#{1,3}\s/.test(line);
    const isBullet = /^\s*[-•*]\s/.test(line);
    const text = line.replace(/^#{1,3}\s/, "").replace(/^\s*[-•*]\s/, "");
    const parts: React.ReactNode[] = [];
    const rx = /\[#([0-9a-f]{8})\]|\*\*([^*]+)\*\*/g;
    let last = 0;
    let k = 0;
    let m: RegExpExecArray | null;
    while ((m = rx.exec(text))) {
      if (m.index > last) parts.push(text.slice(last, m.index));
      if (m[1]) {
        const full = citations[m[1]];
        parts.push(
          full ? (
            <button
              key={`c${li}-${k++}`}
              className="chat-cite"
              title="Jump to this finding"
              onClick={() => onCite(full)}
            >
              #{m[1].slice(0, 4)}
            </button>
          ) : (
            <span key={`c${li}-${k++}`} className="chat-cite chat-cite-dead">
              #{m[1].slice(0, 4)}
            </span>
          ),
        );
      } else if (m[2]) {
        parts.push(<b key={`b${li}-${k++}`}>{m[2]}</b>);
      }
      last = rx.lastIndex;
    }
    if (last < text.length) parts.push(text.slice(last));
    if (isHead) return <div key={li} className="chat-h">{parts}</div>;
    if (isBullet) return <div key={li} className="chat-li">{parts}</div>;
    if (!text.trim()) return <div key={li} className="chat-gap" />;
    return <div key={li}>{parts}</div>;
  });
}

function ChatPanel({
  runId,
  runLabel,
  onCite,
}: {
  runId: string;
  runLabel: string;
  onCite: (issueId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [citations, setCitations] = useState<Record<string, string>>({});
  const [input, setInput] = useState("");
  const [streamText, setStreamText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [model, setModel] = useState<string>("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // New run → reset and load its thread.
    setMsgs([]);
    setCitations({});
    setErr(null);
    setStreamText(null);
    fetch(`${API}/api/runs/${runId}/chat`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => {
        setMsgs(
          (d.messages || []).map((m: any) => ({
            id: m.id, role: m.role, content: m.content, model: m.model,
          })),
        );
        setCitations(d.citations || {});
        setModel(d.config?.model || "");
      })
      .catch(() => {});
  }, [runId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [msgs, streamText, open]);

  async function send(text: string) {
    const q = text.trim();
    if (!q || busy) return;
    setErr(null);
    setBusy(true);
    setInput("");
    setMsgs((m) => [...m, { id: `local-${Date.now()}`, role: "user", content: q }]);
    setStreamText("");
    let acc = "";
    try {
      const res = await fetch(`${API}/api/runs/${runId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: q }),
      });
      if (!res.ok || !res.body) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (!raw.startsWith("data: ")) continue;
          const ev = JSON.parse(raw.slice(6));
          if (ev.type === "delta") {
            acc += ev.text;
            setStreamText(acc);
          } else if (ev.type === "error") {
            setErr(ev.message);
          } else if (ev.type === "done") {
            if (ev.citations) setCitations(ev.citations);
            if (ev.model) setModel(ev.model);
            if (acc) {
              setMsgs((m) => [
                ...m,
                {
                  id: ev.message_id || `a-${Date.now()}`,
                  role: "assistant",
                  content: acc,
                  model: ev.model,
                },
              ]);
            }
          }
        }
      }
    } catch (e: any) {
      setErr(e?.message || "Chat failed — please retry.");
    } finally {
      setStreamText(null);
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        className="chat-fab"
        onClick={() => setOpen(true)}
        title="Ask the QC copilot about this run"
      >
        💬 Ask this run
      </button>
    );
  }
  return (
    <div className="chat-panel">
      <div className="chat-head">
        <div>
          <div className="chat-head-title">QC Copilot</div>
          <div className="chat-head-sub">
            grounded in {runLabel}
            {model ? ` · ${model}` : ""} · read-only
          </div>
        </div>
        <button className="chat-close" onClick={() => setOpen(false)}>
          ×
        </button>
      </div>
      <div className="chat-scroll" ref={scrollRef}>
        {msgs.length === 0 && streamText === null && (
          <div className="chat-empty">
            <div className="chat-empty-title">
              Ask anything about this run's findings.
            </div>
            {CHAT_STARTERS.map((s) => (
              <button key={s} className="chat-starter" onClick={() => send(s)}>
                {s}
              </button>
            ))}
            <div className="chat-note">
              Answers cite findings like{" "}
              <span className="chat-cite">#a1b2</span> — click one to jump to
              the card. Statuses are only changed on the cards; the exported
              checklist stays the record.
            </div>
          </div>
        )}
        {msgs.map((m) => (
          <div key={m.id} className={`chat-msg chat-${m.role}`}>
            {m.role === "assistant"
              ? renderChatContent(m.content, citations, onCite)
              : m.content}
          </div>
        ))}
        {streamText !== null && (
          <div className="chat-msg chat-assistant">
            {streamText ? (
              renderChatContent(streamText, citations, onCite)
            ) : (
              <span className="chat-thinking">thinking…</span>
            )}
          </div>
        )}
        {err && <div className="chat-err">{err}</div>}
      </div>
      <div className="chat-inputrow">
        <textarea
          className="chat-input"
          value={input}
          placeholder="Why is E-105 failing? What matters most?"
          rows={2}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(input);
            }
          }}
          disabled={busy}
        />
        <button
          className="chat-send"
          onClick={() => send(input)}
          disabled={busy || !input.trim()}
        >
          ➤
        </button>
      </div>
    </div>
  );
}

export default function App() {
  const [runs, setRuns] = useState<RunData[]>([]);
  // Read ``?run=<id>`` from the URL on first render so deep links like
  // ``http://host:5173/?run=abc123`` open straight to that run.
  const [runId, setRunId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("run");
  });
  // Dashboard (projects grid) vs a single-run view. Default to the dashboard
  // unless a specific run is deep-linked via ?run=.
  const [showDashboard, setShowDashboard] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    return !new URLSearchParams(window.location.search).get("run");
  });
  const [cat, setCat] = useState("All");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [sortBy, setSortBy] = useState<string>("default");
  const [sel, setSel] = useState<Issue | null>(null);
  // Modal preview zoom state. "fit" scales to modal width (default), "actual"
  // shows native pixels and overflows the modal so the user can scroll/pan.
  // Reset to "fit" whenever a new finding is opened.
  const [selZoom, setSelZoom] = useState<"fit" | "actual">("fit");
  useEffect(() => {
    setSelZoom("fit");
  }, [sel?.id]);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState("");
  const [projName, setProjName] = useState("");
  const [runName, setRunName] = useState("");
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
  // Per-card preview expansion. When a finding has no bbox the inline
  // thumbnail is just a full-page render with no highlighted region —
  // basically noise — so it's collapsed by default. The reviewer can
  // click "Show preview" to expand it inline if they actually want it.
  const [expandedPreview, setExpandedPreview] = useState<Set<string>>(
    () => new Set(),
  );
  const togglePreview = useCallback((id: string) => {
    setExpandedPreview((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  // Group-similar-findings state. When the same rule key fires across
  // multiple pages (e.g. "TBD in equipment list" hitting Row 11, 12, 14),
  // collapse the instances behind a single group header by default.
  // Manual issues never group (different item_keys), and the user can
  // disable grouping entirely.
  const [groupingEnabled, setGroupingEnabled] = useState<boolean>(true);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(
    () => new Set(),
  );
  const toggleGroup = useCallback((key: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);
  // Keyboard-navigation focus on a single finding card. Set/cleared by
  // j/k handlers below; visualized via .card-focused CSS.
  const [focusedIssueId, setFocusedIssueId] = useState<string | null>(null);
  const [showShortcuts, setShowShortcuts] = useState<boolean>(false);
  // Compare-mode state. When ``compareRunId`` is set we fetch that run
  // and render a diff against the currently-selected run. URL-synced
  // so a "before/after" view can be shared via deep link.
  const [compareRunId, setCompareRunId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("compare");
  });
  const [compareRun, setCompareRun] = useState<RunData | null>(null);
  useEffect(() => {
    if (!compareRunId) {
      setCompareRun(null);
      return;
    }
    let cancelled = false;
    fetch(`${API}/api/runs/${compareRunId}`)
      .then((r) => r.json())
      .then((d) => {
        if (!cancelled) setCompareRun(d);
      })
      .catch(() => {
        if (!cancelled) setCompareRun(null);
      });
    return () => {
      cancelled = true;
    };
  }, [compareRunId]);
  // Mirror compareRunId into the URL.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (compareRunId) url.searchParams.set("compare", compareRunId);
    else url.searchParams.delete("compare");
    window.history.replaceState({}, "", url.toString());
  }, [compareRunId]);
  const [sideOpen, setSideOpen] = useState(true);
  const [mobileNav, setMobileNav] = useState(false);
  const [runSearch, setRunSearch] = useState("");
  // Signed-in engineer (EasyAuth). Used to attribute runs and auto-fill the
  // name picker. null until /api/me resolves.
  const [me, setMe] = useState<Me | null>(null);
  // ── Shared run queue / activity feed ──
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobStats, setJobStats] = useState({ concurrency: 0, queued: 0, running: 0 });
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [desktopAlerts, setDesktopAlerts] = useState(
    typeof Notification !== "undefined" && Notification.permission === "granted",
  );
  const myJobIds = useRef<Set<string>>(new Set()); // jobs started in this tab
  const notifiedJobs = useRef<Set<string>>(new Set()); // completions already alerted
  const jobsSeeded = useRef(false); // first poll seeds (no spurious alerts on reload)
  const baseTitleRef = useRef<string>(
    typeof document !== "undefined" ? document.title : "Planset QC",
  );
  // Version history under a lineage is collapsed by default, opened per
  // root_run_id, shown in the dashboard project cards.
  const [expandedVersions, setExpandedVersions] = useState<Set<string>>(
    () => new Set(),
  );
  const toggleVersions = useCallback((root: string) => {
    setExpandedVersions((prev) => {
      const next = new Set(prev);
      if (next.has(root)) next.delete(root);
      else next.add(root);
      return next;
    });
  }, []);
  // Dashboard project cards collapse by default (uniform height, no wasted
  // space); expanding a card reveals its full run rows.
  const [expandedCards, setExpandedCards] = useState<Set<string>>(
    () => new Set(),
  );
  const toggleCard = useCallback((key: string) => {
    setExpandedCards((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);
  const [issuesOnly, setIssuesOnly] = useState(false);
  const [showProjDetails, setShowProjDetails] = useState(false);
  const [pd, setPd] = useState<Partial<ProjectDetails>>({});
  const pdSet = (k: keyof ProjectDetails, v: string) =>
    setPd((p) => ({ ...p, [k]: v }));
  const pdHasValues = Object.values(pd).some((v) => v && String(v).trim());
  const [parsing, setParsing] = useState(false);
  const [parseMsg, setParseMsg] = useState("");
  const [deepMode, setDeepMode] = useState(true);
  const [designStage, setDesignStage] = useState<string>("");
  const [supportingDocs, setSupportingDocs] = useState<SupportingDoc[]>([]);
  const [supportingLoading, setSupportingLoading] = useState(false);
  const [supportingMsg, setSupportingMsg] = useState("");
  const [plansetFile, setPlansetFile] = useState<File | null>(null);
  const [plansetDragOver, setPlansetDragOver] = useState(false);
  const [supportingDragOver, setSupportingDragOver] = useState(false);
  const [pdDragOver, setPdDragOver] = useState(false);
  // Engineer name (who's running this QC). Persisted in localStorage so
  // the browser remembers the picker list across sessions. Default seed
  // is the office crew (Manjil, Jay, Sam) — a fresh browser sees these
  // immediately. Adding via "+ Add new engineer..." appends to the list
  // and persists for next time.
  const DEFAULT_ENGINEERS = ["Jay", "Manjil", "Sam"];
  // Attribution defaults to the signed-in identity (set from /api/me below).
  // The field stays editable so a run can be logged under a different name, but
  // that override is session-only — it is NOT persisted, so every load defaults
  // back to the authenticated castillope.com identity.
  const [engineerName, setEngineerName] = useState<string>("");
  // Flips true once the user manually overrides the name, so the identity
  // default from /api/me doesn't clobber their choice.
  const nameTouchedRef = useRef(false);
  const [knownEngineers, setKnownEngineers] = useState<string[]>(() => {
    let saved: string[] = [];
    if (typeof localStorage !== "undefined") {
      try {
        const raw = localStorage.getItem("known_engineers");
        const parsed = raw ? JSON.parse(raw) : [];
        if (Array.isArray(parsed)) {
          saved = parsed.filter((s) => typeof s === "string");
        }
      } catch {
        saved = [];
      }
    }
    // Merge defaults so the seed crew is present on first load and any
    // additions stay. Case-insensitive dedupe — "jay" from past data
    // shouldn't double up with "Jay" from the seed.
    const merged: string[] = [];
    const seen = new Set<string>();
    for (const n of [...DEFAULT_ENGINEERS, ...saved]) {
      const k = n.trim().toLowerCase();
      if (!k || seen.has(k)) continue;
      seen.add(k);
      merged.push(n.trim());
    }
    merged.sort((a, b) => a.localeCompare(b));
    if (typeof localStorage !== "undefined") {
      localStorage.setItem("known_engineers", JSON.stringify(merged));
    }
    return merged;
  });
  const rememberEngineer = useCallback((name: string) => {
    const trimmed = name.trim();
    if (!trimmed) return trimmed;
    let resolved = trimmed;
    setKnownEngineers((prev) => {
      // Case-insensitive match against existing list — if the name is
      // already there, keep the canonical casing and don't duplicate.
      const existing = prev.find(
        (n) => n.toLowerCase() === trimmed.toLowerCase(),
      );
      if (existing) {
        resolved = existing;
        return prev;
      }
      const next = [...prev, trimmed].sort((a, b) => a.localeCompare(b));
      if (typeof localStorage !== "undefined") {
        localStorage.setItem("known_engineers", JSON.stringify(next));
      }
      return next;
    });
    return resolved;
  }, []);
  const [addingEngineer, setAddingEngineer] = useState(false);
  const [newEngineerDraft, setNewEngineerDraft] = useState("");
  const confirmNewEngineer = useCallback(() => {
    const trimmed = newEngineerDraft.trim();
    if (!trimmed) {
      setAddingEngineer(false);
      setNewEngineerDraft("");
      return;
    }
    const resolved = rememberEngineer(trimmed);
    nameTouchedRef.current = true;
    setEngineerName(resolved || trimmed);
    setAddingEngineer(false);
    setNewEngineerDraft("");
  }, [newEngineerDraft, rememberEngineer]);
  // Per-finding feedback panel state. ``reportingId`` holds the issue
  // currently being reported (only one open at a time); ``reportedIds``
  // tracks which findings have feedback submitted in this session so the
  // card can show a small confirmation.
  const [reportingId, setReportingId] = useState<string | null>(null);
  const [reportTags, setReportTags] = useState<Set<string>>(new Set());
  const [reportComment, setReportComment] = useState<string>("");
  const [reportedIds, setReportedIds] = useState<Set<string>>(new Set());
  const [reportSubmitting, setReportSubmitting] = useState(false);
  const openReport = useCallback((issueId: string) => {
    setReportingId(issueId);
    setReportTags(new Set());
    setReportComment("");
  }, []);
  const closeReport = useCallback(() => {
    setReportingId(null);
    setReportTags(new Set());
    setReportComment("");
  }, []);
  const toggleReportTag = useCallback((tag: string) => {
    setReportTags((prev) => {
      const next = new Set(prev);
      if (next.has(tag)) next.delete(tag);
      else next.add(tag);
      return next;
    });
  }, []);
  const submitIssueFeedback = useCallback(async () => {
    if (!reportingId) return;
    if (reportTags.size === 0 && !reportComment.trim()) return;
    setReportSubmitting(true);
    try {
      const r = await fetch(`${API}/api/issues/${reportingId}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tags: Array.from(reportTags),
          comment: reportComment.trim() || null,
          engineer_name: engineerName.trim() || null,
        }),
      });
      if (!r.ok) {
        alert(`Failed: ${await r.text()}`);
        return;
      }
      setReportedIds((prev) => {
        const next = new Set(prev);
        next.add(reportingId);
        return next;
      });
      closeReport();
    } finally {
      setReportSubmitting(false);
    }
  }, [reportingId, reportTags, reportComment, engineerName, closeReport]);

  // Run-level feedback (saved time / even / cost time).
  const [runFeedback, setRunFeedback] = useState<RunFeedback | null>(null);
  const [runFeedbackLoaded, setRunFeedbackLoaded] = useState(false);
  const [runFeedbackDismissed, setRunFeedbackDismissed] = useState(false);
  const [runRatingDraft, setRunRatingDraft] = useState<RunRating | null>(null);
  const [runRatingComment, setRunRatingComment] = useState<string>("");
  const [runFeedbackSubmitting, setRunFeedbackSubmitting] = useState(false);
  const submitRunFeedback = useCallback(async (
    runId: string, rating: RunRating, comment: string,
  ) => {
    setRunFeedbackSubmitting(true);
    try {
      const r = await fetch(`${API}/api/runs/${runId}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rating,
          comment: comment.trim() || null,
          engineer_name: engineerName.trim() || null,
        }),
      });
      if (!r.ok) {
        alert(`Failed: ${await r.text()}`);
        return;
      }
      const fb: RunFeedback = await r.json();
      setRunFeedback(fb);
      setRunRatingDraft(null);
      setRunRatingComment("");
    } finally {
      setRunFeedbackSubmitting(false);
    }
  }, [engineerName]);

  const parseSupportingDocs = async (fileList: FileList) => {
    setSupportingLoading(true);
    setSupportingMsg("Reading engineering documents...");
    const fd = new FormData();
    for (let i = 0; i < fileList.length; i++) fd.append("files", fileList[i]);
    try {
      const r = await fetch(`${API}/api/parse-supporting-docs`, {
        method: "POST",
        body: fd,
      });
      if (!r.ok) {
        setSupportingMsg(`Error: ${await r.text()}`);
        return;
      }
      const d = (await r.json()) as { supporting_docs: SupportingDoc[] };
      const incoming = d.supporting_docs || [];
      setSupportingDocs((prev) => {
        const byName = new Map<string, SupportingDoc>();
        for (const p of prev) byName.set(p.filename, p);
        for (const x of incoming) byName.set(x.filename, x);
        return Array.from(byName.values());
      });
      setSupportingMsg(
        incoming.length
          ? `Loaded ${incoming.length} evidence doc${incoming.length > 1 ? "s" : ""}.`
          : "No evidence extracted.",
      );
      setTimeout(() => setSupportingMsg(""), 4000);
    } catch (err) {
      setSupportingMsg(
        err instanceof Error ? err.message : "Failed to parse supporting docs",
      );
    } finally {
      setSupportingLoading(false);
    }
  };

  const removeSupportingDoc = (filename: string) => {
    setSupportingDocs((prev) => prev.filter((d) => d.filename !== filename));
  };

  // Convert a DataTransferItemList/FileList into a plain File[] and filter
  // by accept patterns. ``accept`` is the same comma-separated list the
  // <input accept=…> attribute takes (e.g. ".pdf,.xlsx,application/pdf").
  const filesFromDrop = (
    dt: DataTransfer | null,
    accept: string,
  ): File[] => {
    if (!dt?.files?.length) return [];
    const patterns = accept
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
    const matches = (f: File) => {
      if (!patterns.length) return true;
      const name = f.name.toLowerCase();
      const type = (f.type || "").toLowerCase();
      return patterns.some((p) => {
        if (p.startsWith(".")) return name.endsWith(p);
        if (p.endsWith("/*")) return type.startsWith(p.slice(0, -1));
        return type === p;
      });
    };
    return Array.from(dt.files).filter(matches);
  };

  const filesToList = (files: File[]): FileList => {
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    return dt.files;
  };

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
    // If the URL named a run that exists, keep it. Otherwise default to
    // the most recent run (the API returns runs newest-first).
    if (d.length) {
      const requested = runId && d.some((x: RunData) => x.id === runId);
      if (!requested) {
        setRunId(d[0].id);
      }
    }
  }, [runId]);

  // Keep the URL's ``?run`` query param in sync with the selected run so
  // browser refresh, copy-link, and shared links all land on the same run.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    // Only deep-link a run when actually viewing one — on the dashboard the
    // URL stays clean so a refresh returns to the dashboard, not a run.
    if (runId && !showDashboard) {
      url.searchParams.set("run", runId);
    } else {
      url.searchParams.delete("run");
    }
    // ``replaceState`` (not pushState) — selecting a run shouldn't pollute
    // the back/forward stack with every click.
    window.history.replaceState({}, "", url.toString());
  }, [runId, showDashboard]);

  const refresh = useCallback(async (id: string) => {
    const r = await fetch(`${API}/api/runs/${id}`);
    const d = await r.json();
    setRuns((p) => [d, ...p.filter((x: RunData) => x.id !== id)]);
    setRunId(id);
  }, []);

  // Hydrate full run detail for runs that only exist as list entries (no
  // ``issues``). Covers deep links (?run=...) — which set runId on mount
  // without going through openRun() — and list refreshes that replace a
  // hydrated entry with a bare one.
  const hydratingRef = useRef<string | null>(null);
  useEffect(() => {
    if (!runId) return;
    const entry = runs.find((r) => r.id === runId);
    if (!entry || entry.issues !== undefined) return;
    if (hydratingRef.current === runId) return;
    hydratingRef.current = runId;
    void refresh(runId).finally(() => {
      if (hydratingRef.current === runId) hydratingRef.current = null;
    });
  }, [runId, runs, refresh]);

  useEffect(() => {
    void load();
  }, []);

  // Resolve the signed-in engineer once. In production the EasyAuth sidecar
  // injects identity; locally /api/me returns nulls (DEV_USER_EMAIL fallback).
  // The signed-in name (from the user's Entra ID) is the DEFAULT attribution —
  // it wins over any prior value unless the user has overridden it this session.
  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/api/me`)
      .then((r) => (r.ok ? r.json() : null))
      .then((m: Me | null) => {
        if (cancelled || !m) return;
        setMe(m);
        const display = (m.name || m.email || "").trim();
        if (display && !nameTouchedRef.current) {
          const resolved = rememberEngineer(display);
          setEngineerName(resolved || display);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // Mount-only: auto-fill should reflect the name as it was at load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Run-queue helpers: poll the shared feed, alert on my completions ──
  const pushToast = useCallback((t: ToastItem) => {
    setToasts((p) => [t, ...p.filter((x) => x.key !== t.key)].slice(0, 4));
    setTimeout(() => setToasts((p) => p.filter((x) => x.key !== t.key)), 9000);
  }, []);
  const dismissToast = useCallback(
    (key: string) => setToasts((p) => p.filter((x) => x.key !== key)),
    [],
  );
  const flashTitle = useCallback((msg: string) => {
    if (typeof document !== "undefined" && document.hidden) document.title = msg;
  }, []);
  // Restore the tab title when the user returns to the tab.
  useEffect(() => {
    const restore = () => {
      if (typeof document !== "undefined" && !document.hidden) {
        document.title = baseTitleRef.current;
      }
    };
    document.addEventListener("visibilitychange", restore);
    window.addEventListener("focus", restore);
    return () => {
      document.removeEventListener("visibilitychange", restore);
      window.removeEventListener("focus", restore);
    };
  }, []);
  const enableDesktopAlerts = useCallback(async () => {
    if (typeof Notification === "undefined") return;
    try {
      const perm = await Notification.requestPermission();
      setDesktopAlerts(perm === "granted");
    } catch {
      /* ignore */
    }
  }, []);
  const notifyJob = useCallback(
    (j: Job) => {
      const name = j.run_name || j.project_name || "Run";
      const okNotif =
        desktopAlerts &&
        typeof Notification !== "undefined" &&
        Notification.permission === "granted";
      if (j.status === "done") {
        pushToast({ key: `${j.id}-done`, kind: "done", title: `✓ ${name} — analysis complete`, runId: j.run_id });
        flashTitle(`✓ ${name} done`);
        if (okNotif) {
          try { new Notification("Planset QC — analysis complete", { body: `${name} is ready to review.` }); } catch { /* */ }
        }
      } else {
        pushToast({ key: `${j.id}-error`, kind: "error", title: `✗ ${name} — analysis failed`, detail: j.detail || j.error || undefined });
        flashTitle(`✗ ${name} failed`);
        if (okNotif) {
          try { new Notification("Planset QC — analysis failed", { body: `${name}: ${j.detail || "error"}` }); } catch { /* */ }
        }
      }
    },
    [pushToast, flashTitle, desktopAlerts],
  );
  // Poll the shared job feed; alert once on my completions, then refresh runs.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const r = await fetch(`${API}/api/jobs`);
        if (!r.ok || cancelled) return;
        const data: JobsResponse = await r.json();
        if (cancelled) return;
        const list = data.jobs || [];
        setJobs(list);
        setJobStats({ concurrency: data.concurrency, queued: data.queued, running: data.running });
        const mineFinished = list.filter(
          (j) =>
            (j.status === "done" || j.status === "error") &&
            (myJobIds.current.has(j.id) || (me?.email != null && j.created_by === me.email)),
        );
        if (!jobsSeeded.current) {
          // First poll (incl. after reload): don't alert for pre-existing completions.
          mineFinished.forEach((j) => notifiedJobs.current.add(j.id));
          jobsSeeded.current = true;
          return;
        }
        const fresh = mineFinished.filter((j) => !notifiedJobs.current.has(j.id));
        if (fresh.length) {
          fresh.forEach((j) => {
            notifiedJobs.current.add(j.id);
            notifyJob(j);
          });
          try {
            const rr = await fetch(`${API}/api/runs`);
            if (rr.ok && !cancelled) setRuns(await rr.json());
          } catch {
            /* ignore */
          }
        }
      } catch {
        /* transient — keep polling */
      }
    };
    void tick();
    const iv = setInterval(tick, 2500);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [me, notifyJob]);

  const run = useMemo(
    () => runs.find((r) => r.id === runId) ?? null,
    [runs, runId],
  );

  // Fetch existing run-feedback whenever the run or engineer changes.
  // If this engineer has already rated this run, the banner shows the
  // recorded rating with an Edit link instead of the empty rate form.
  useEffect(() => {
    if (!runId) {
      setRunFeedback(null);
      setRunFeedbackLoaded(false);
      setRunFeedbackDismissed(false);
      return;
    }
    setRunFeedbackLoaded(false);
    setRunFeedback(null);
    setRunFeedbackDismissed(false);
    setRunRatingDraft(null);
    setRunRatingComment("");
    let cancelled = false;
    const params = new URLSearchParams();
    if (engineerName.trim()) params.set("engineer_name", engineerName.trim());
    fetch(`${API}/api/runs/${runId}/feedback?${params.toString()}`)
      .then((r) => r.json())
      .then((d) => {
        if (cancelled) return;
        setRunFeedback(d.feedback || null);
        setRunFeedbackLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setRunFeedbackLoaded(true);
      });
    return () => { cancelled = true; };
  }, [runId, engineerName]);

  const issues = useMemo(() => {
    if (!run) return [];
    let list = run.issues ?? [];
    if (cat !== "All") list = list.filter((i) => i.category === cat);
    if (statusFilter === "fail") list = list.filter((i) => i.status === "Fail");
    else if (statusFilter === "review")
      list = list.filter((i) => i.status === "Needs Review");
    else if (statusFilter === "pass")
      list = list.filter((i) => i.status === "Pass");
    else if (statusFilter === "deferred")
      list = list.filter((i) => i.status === "Deferred");
    else if (statusFilter === "override")
      list = list.filter(
        (i) => i.status === "Overridden / Accepted by QC Engineer",
      );
    if (issuesOnly)
      list = list.filter(
        (i) =>
          i.status !== "Pass" &&
          i.status !== "Deferred" &&
          i.status !== "Overridden / Accepted by QC Engineer",
      );

    // Sort. Default = registration order (don't mutate the input). All other
    // sorts use stable secondary keys so equal-primary findings stay grouped
    // by category + page so the list still reads coherently.
    if (sortBy !== "default") {
      const SEV_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };
      const STATUS_RANK: Record<string, number> = {
        Fail: 0,
        "Needs Review": 1,
        Pass: 2,
        Deferred: 3,
        "Overridden / Accepted by QC Engineer": 4,
      };
      list = [...list].sort((a, b) => {
        let primary = 0;
        if (sortBy === "severity") {
          primary = (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9);
        } else if (sortBy === "status") {
          primary = (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9);
        } else if (sortBy === "category") {
          primary = (a.category || "").localeCompare(b.category || "");
        } else if (sortBy === "page") {
          primary = (a.page_number ?? 9999) - (b.page_number ?? 9999);
        } else if (sortBy === "confidence") {
          // Low-confidence first — those need more reviewer attention.
          primary = (a.confidence ?? 0) - (b.confidence ?? 0);
        }
        if (primary !== 0) return primary;
        // Stable secondary: category, then page, then title.
        const c = (a.category || "").localeCompare(b.category || "");
        if (c !== 0) return c;
        const p = (a.page_number ?? 9999) - (b.page_number ?? 9999);
        if (p !== 0) return p;
        return (a.title || "").localeCompare(b.title || "");
      });
    }
    return list;
  }, [run, cat, statusFilter, issuesOnly, sortBy]);

  // Compare-mode diff. Match findings between the two runs by item_key
  // (the rule identifier) — same rule on the same planset should
  // produce the same key. Bucket by transition type so reviewers can
  // answer "did the designer fix the comments?" at a glance.
  type DiffEntry = {
    key: string;
    title: string;
    category: string;
    severity: string;
    before: Issue | null;
    after: Issue | null;
  };
  const isProblemStatus = (s: Status | string | undefined) =>
    s === "Fail" || s === "Needs Review";
  const diff = useMemo<{
    fixed: DiffEntry[];
    newly: DiffEntry[];
    drift: DiffEntry[];
    same: DiffEntry[];
    removed: DiffEntry[];
  } | null>(() => {
    if (!run || !compareRun) return null;
    const beforeMap = new Map<string, Issue>();
    for (const i of compareRun.issues ?? []) {
      // Strip stage_deferred / xref_deferred prefixes so the same rule
      // matches whether it deferred at engine level or actually ran.
      const k = i.item_key
        .replace(/^stage_deferred_/, "")
        .replace(/^xref_deferred_/, "");
      beforeMap.set(k, i);
    }
    const afterKeys = new Set<string>();
    const fixed: DiffEntry[] = [];
    const newly: DiffEntry[] = [];
    const drift: DiffEntry[] = [];
    const same: DiffEntry[] = [];
    for (const after of run.issues ?? []) {
      const k = after.item_key
        .replace(/^stage_deferred_/, "")
        .replace(/^xref_deferred_/, "");
      afterKeys.add(k);
      const before = beforeMap.get(k) ?? null;
      const entry: DiffEntry = {
        key: k,
        title: after.title,
        category: after.category,
        severity: after.severity,
        before,
        after,
      };
      if (before == null) {
        if (isProblemStatus(after.status)) newly.push(entry);
        // else: new but Pass/Deferred — not interesting in diff
      } else {
        const wasProblem = isProblemStatus(before.status);
        const isProblem = isProblemStatus(after.status);
        if (wasProblem && !isProblem) fixed.push(entry);
        else if (!wasProblem && isProblem) newly.push(entry);
        else if (before.status !== after.status) drift.push(entry);
        else same.push(entry);
      }
    }
    // Rules in before but not after (removed / disabled / didn't fire).
    const removed: DiffEntry[] = [];
    for (const [k, before] of beforeMap) {
      if (!afterKeys.has(k) && isProblemStatus(before.status)) {
        removed.push({
          key: k,
          title: before.title,
          category: before.category,
          severity: before.severity,
          before,
          after: null,
        });
      }
    }
    return { fixed, newly, drift, same, removed };
  }, [run, compareRun]);

  // Group findings by rule for the issue list. Same rule firing on
  // multiple pages (e.g. "TBD in equipment list" on Row 11, 12, 14)
  // collapses to one card by default. Strip ``stage_deferred_`` and
  // ``xref_deferred_`` prefixes so deferral pathways for the same
  // underlying rule still group together.
  const issueGroups = useMemo(() => {
    type Group = {
      key: string;
      title: string;
      category: string;
      severity: string;
      statuses: Set<Status>;
      pages: number[];
      instances: Issue[];
    };
    const groups: Group[] = [];
    const idx = new Map<string, number>();
    for (const i of issues) {
      const baseKey = i.item_key
        .replace(/^stage_deferred_/, "")
        .replace(/^xref_deferred_/, "");
      const k = `${baseKey}::${i.title}`;
      if (!idx.has(k)) {
        idx.set(k, groups.length);
        groups.push({
          key: k,
          title: i.title,
          category: i.category,
          severity: i.severity,
          statuses: new Set([i.status]),
          pages: i.page_number ? [i.page_number] : [],
          instances: [i],
        });
      } else {
        const g = groups[idx.get(k)!];
        g.instances.push(i);
        g.statuses.add(i.status);
        if (i.page_number && !g.pages.includes(i.page_number)) {
          g.pages.push(i.page_number);
        }
        // Severity goes to highest. Order: high > medium > low.
        if (i.severity === "high") g.severity = "high";
        else if (i.severity === "medium" && g.severity !== "high") {
          g.severity = "medium";
        }
      }
    }
    return groups;
  }, [issues]);

  // Live status counts computed from actual issues (updates when statuses change)
  const liveStatusCounts = useMemo(() => {
    if (!run)
      return {
        Pass: 0,
        Fail: 0,
        "Needs Review": 0,
        Deferred: 0,
        "Overridden / Accepted by QC Engineer": 0,
      };
    const all = run.issues ?? [];
    return {
      Pass: all.filter((i) => i.status === "Pass").length,
      Fail: all.filter((i) => i.status === "Fail").length,
      "Needs Review": all.filter((i) => i.status === "Needs Review").length,
      Deferred: all.filter((i) => i.status === "Deferred").length,
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
          Deferred: 0,
          "Overridden / Accepted by QC Engineer": 0,
        };
      }
      const c = byCat[issue.category];
      c.total += 1;
      if (issue.status === "Pass") c.Pass = (c.Pass ?? 0) + 1;
      else if (issue.status === "Fail") c.Fail = (c.Fail ?? 0) + 1;
      else if (issue.status === "Needs Review")
        c["Needs Review"] = (c["Needs Review"] ?? 0) + 1;
      else if (issue.status === "Deferred")
        c.Deferred = (c.Deferred ?? 0) + 1;
      else if (issue.status === "Overridden / Accepted by QC Engineer")
        c["Overridden / Accepted by QC Engineer"] =
          (c["Overridden / Accepted by QC Engineer"] ?? 0) + 1;
    }
    // Preserve original category order
    return (run.categories ?? []).map((orig) => byCat[orig.name] ?? orig);
  }, [run]);

  const counts = useMemo(() => {
    if (!run) return { p: 0, f: 0, r: 0, d: 0, o: 0, t: 0 };
    const all =
      cat === "All"
        ? (run.issues ?? [])
        : (run.issues ?? []).filter((i) => i.category === cat);
    return {
      p: all.filter((i) => i.status === "Pass").length,
      f: all.filter((i) => i.status === "Fail").length,
      r: all.filter((i) => i.status === "Needs Review").length,
      d: all.filter((i) => i.status === "Deferred").length,
      o: all.filter((i) => i.status === "Overridden / Accepted by QC Engineer")
        .length,
      t: all.length,
    };
  }, [run, cat]);

  // ── Polling helper ──
  // ── Actions ──
  const upload = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = e.currentTarget;
    if (!plansetFile) return;
    const fd = new FormData();
    fd.append("file", plansetFile);
    if (projName.trim()) fd.append("project_name", projName.trim());
    if (runName.trim()) fd.append("run_name", runName.trim());
    if (pdHasValues) fd.append("project_details", JSON.stringify(pd));
    fd.append("use_deep", deepMode ? "true" : "false");
    if (designStage) fd.append("design_stage", designStage);
    if (supportingDocs.length > 0) {
      fd.append("supporting_docs", JSON.stringify(supportingDocs));
    }
    if (engineerName.trim()) {
      fd.append("engineer_name", engineerName.trim());
      rememberEngineer(engineerName);
    }
    const jobLabel = runName.trim() || projName.trim() || plansetFile.name;
    setUploading(true);
    try {
      const r = await fetch(`${API}/api/analyze`, { method: "POST", body: fd });
      if (!r.ok) {
        const msg = await r.text();
        pushToast({ key: `up-${Date.now()}-error`, kind: "error", title: "Couldn't start analysis", detail: msg.slice(0, 160) });
        return;
      }
      const { upload_id } = await r.json();
      // Non-blocking: the analysis runs on the shared queue. Track it here so we
      // can alert this tab on completion, reset the form, and let the user keep
      // working or queue another run — the Activity panel watches the rest.
      myJobIds.current.add(upload_id);
      pushToast({ key: `${upload_id}-queued`, kind: "queued", title: `Queued: ${jobLabel}`, detail: "Tracking in Activity — you'll be alerted when it's done." });
      form.reset();
      setProjName("");
      setRunName("");
      setPlansetFile(null);
    } catch (err) {
      pushToast({ key: `up-${Date.now()}-error`, kind: "error", title: "Couldn't start analysis", detail: err instanceof Error ? err.message : "Check logs." });
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
      setShowDashboard(true);
      setCat("All");
      setStatusFilter("all");
    }
  };

  const reanalyze = async (id: string) => {
    if (
      !confirm(
        "Re-run analysis on this PDF? This saves a new version; the current one is kept in history.",
      )
    )
      return;
    const reLabel =
      runs.find((x) => x.id === id)?.run_name ||
      runs.find((x) => x.id === id)?.project_name ||
      "Re-run";
    setUploading(true);
    try {
      const rfd = new FormData();
      rfd.append("use_deep", deepMode ? "true" : "false");
      if (engineerName.trim()) {
        rfd.append("engineer_name", engineerName.trim());
        rememberEngineer(engineerName);
      }
      const r = await fetch(`${API}/api/runs/${id}/reanalyze`, {
        method: "POST",
        body: rfd,
      });
      if (!r.ok) {
        pushToast({ key: `re-${Date.now()}-error`, kind: "error", title: "Couldn't start re-analysis", detail: (await r.text()).slice(0, 160) });
        return;
      }
      const { upload_id } = await r.json();
      // Non-blocking: tracked by the Activity panel; the completion poller
      // refreshes the run list (so the new version + superseded prior both
      // reflect) and alerts this tab when it's done.
      myJobIds.current.add(upload_id);
      pushToast({ key: `${upload_id}-queued`, kind: "queued", title: `Queued re-run: ${reLabel}`, detail: "Tracking in Activity — you'll be alerted when it's done." });
    } catch (err) {
      pushToast({ key: `re-${Date.now()}-error`, kind: "error", title: "Couldn't start re-analysis", detail: err instanceof Error ? err.message : "Check logs." });
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

  // ── Keyboard navigation for triage ──
  // The list of issues actually visible on screen — respects group
  // collapse so j/k skips hidden findings. Recomputed whenever the
  // filter / sort / grouping changes.
  const visibleIssueIds = useMemo<string[]>(() => {
    const out: string[] = [];
    for (const g of issueGroups) {
      const showAsGroup = groupingEnabled && g.instances.length > 1;
      if (showAsGroup && !expandedGroups.has(g.key)) continue;
      for (const i of g.instances) out.push(i.id);
    }
    return out;
  }, [issueGroups, groupingEnabled, expandedGroups]);

  // Keep focusedIssueId valid across filter/sort/expand changes. If the
  // current focus disappears (filtered out), drop it; the user picks
  // again with j/k.
  useEffect(() => {
    if (focusedIssueId && !visibleIssueIds.includes(focusedIssueId)) {
      setFocusedIssueId(null);
    }
  }, [focusedIssueId, visibleIssueIds]);

  // Auto-scroll the focused card into view when navigation moves it.
  useEffect(() => {
    if (!focusedIssueId) return;
    const el = document.getElementById(`issue-card-${focusedIssueId}`);
    if (el) {
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [focusedIssueId]);

  // Global keydown handler. Handles j/k navigation, p/f/r/o status,
  // Enter to open modal, Escape to close modal / clear focus, ? to
  // show help. Suppressed when the user is typing in an input/textarea
  // or contenteditable, and when the manual-add form is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Don't hijack typing.
      const t = e.target as HTMLElement | null;
      if (t) {
        const tag = t.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        if (t.isContentEditable) return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      // Modal-aware keys.
      if (sel) {
        if (e.key === "Escape") {
          setSel(null);
          e.preventDefault();
        }
        return;
      }

      // Help overlay toggle.
      if (e.key === "?" || (e.shiftKey && e.key === "/")) {
        setShowShortcuts((s) => !s);
        e.preventDefault();
        return;
      }
      if (e.key === "Escape") {
        if (showShortcuts) {
          setShowShortcuts(false);
        } else {
          setFocusedIssueId(null);
        }
        e.preventDefault();
        return;
      }

      const ids = visibleIssueIds;
      if (ids.length === 0) return;

      // Navigation.
      if (e.key === "j" || e.key === "ArrowDown") {
        const i = focusedIssueId ? ids.indexOf(focusedIssueId) : -1;
        const next = i < ids.length - 1 ? ids[i + 1] : ids[0];
        setFocusedIssueId(next);
        e.preventDefault();
        return;
      }
      if (e.key === "k" || e.key === "ArrowUp") {
        const i = focusedIssueId ? ids.indexOf(focusedIssueId) : -1;
        const next = i > 0 ? ids[i - 1] : ids[ids.length - 1];
        setFocusedIssueId(next);
        e.preventDefault();
        return;
      }

      // Status / action keys require a focused finding.
      if (!focusedIssueId) return;
      const focused = (run?.issues ?? []).find((i) => i.id === focusedIssueId);
      if (!focused) return;

      if (e.key === "p") {
        void quickStatus(focused, "Pass");
        e.preventDefault();
      } else if (e.key === "f") {
        void quickStatus(focused, "Fail");
        e.preventDefault();
      } else if (e.key === "r") {
        void quickStatus(focused, "Needs Review");
        e.preventDefault();
      } else if (e.key === "o") {
        void quickStatus(focused, "Overridden / Accepted by QC Engineer");
        e.preventDefault();
      } else if (e.key === "Enter") {
        setSel(focused);
        e.preventDefault();
      }
    };

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    sel,
    showShortcuts,
    visibleIssueIds,
    focusedIssueId,
    run,
  ]);

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

  const runQuery = runSearch.trim().toLowerCase();
  const filteredRuns = runQuery
    ? runs.filter(
        (r) =>
          (r.project_name || "").toLowerCase().includes(runQuery) ||
          (r.original_filename || "").toLowerCase().includes(runQuery) ||
          (r.engineer_name || "").toLowerCase().includes(runQuery) ||
          (r.created_by || "").toLowerCase().includes(runQuery),
      )
    : runs;

  // Group the flat run list into Project → Stage → Lineage (rerun chain) for
  // the sidebar. Everything is derived from /api/runs, which now carries
  // project_id / design_stage / version / is_latest / root_run_id.
  const projectGroups: ProjectGroup[] = useMemo(() => {
    const byProj = new Map<string, RunData[]>();
    for (const r of filteredRuns) {
      const key =
        r.project_id || `name:${(r.project_name || "").trim().toLowerCase()}`;
      const arr = byProj.get(key);
      if (arr) arr.push(r);
      else byProj.set(key, [r]);
    }
    const projects: ProjectGroup[] = [];
    for (const [key, prs] of byProj) {
      // Lineages by root_run_id (a rerun chain shares a root).
      const byRoot = new Map<string, RunData[]>();
      for (const r of prs) {
        const root = r.root_run_id || r.id;
        const arr = byRoot.get(root);
        if (arr) arr.push(r);
        else byRoot.set(root, [r]);
      }
      const lineages: Lineage[] = [];
      for (const [root, vers] of byRoot) {
        const versions = [...vers].sort(
          (a, b) =>
            (b.version ?? 1) - (a.version ?? 1) ||
            (b.created_at > a.created_at ? 1 : -1),
        );
        const latest = versions.find((v) => v.is_latest) ?? versions[0];
        lineages.push({ root, latest, versions });
      }
      // Group lineages by their latest run's stage.
      const byStage = new Map<string, Lineage[]>();
      for (const ln of lineages) {
        const s = ln.latest.design_stage || "";
        const arr = byStage.get(s);
        if (arr) arr.push(ln);
        else byStage.set(s, [ln]);
      }
      const stages: StageGroup[] = [...byStage.entries()]
        .map(([stage, lins]) => ({
          stage,
          lineages: lins.sort((a, b) =>
            b.latest.created_at > a.latest.created_at ? 1 : -1,
          ),
        }))
        .sort((a, b) => stageRank(a.stage) - stageRank(b.stage));
      const first = prs[0];
      const lastActivity = prs.reduce(
        (m, r) => (r.created_at > m ? r.created_at : m),
        "",
      );
      // Prefer the human name (engineer_name, now the signed-in display name)
      // over created_by (the email identity key) for display.
      const createdBy =
        prs.map((r) => r.engineer_name).find(Boolean) ||
        prs.map((r) => r.created_by).find(Boolean) ||
        null;
      projects.push({
        key,
        name: first.project_name || "(untitled project)",
        createdBy,
        lastActivity,
        runCount: prs.length,
        stages,
      });
    }
    projects.sort((a, b) => (b.lastActivity > a.lastActivity ? 1 : -1));
    return projects;
  }, [filteredRuns]);

  // Distinct existing project names for the upload form's datalist, so an
  // engineer reuses an exact name (and the run joins that project).
  const projectNames = useMemo(
    () =>
      Array.from(
        new Set(runs.map((r) => (r.project_name || "").trim()).filter(Boolean)),
      ).sort((a, b) => a.localeCompare(b)),
    [runs],
  );

  // Recent current-version runs for the sidebar quick-switcher.
  const recentRuns = useMemo(
    () => filteredRuns.filter((r) => r.is_latest !== 0).slice(0, 25),
    [filteredRuns],
  );

  // Open a run from the dashboard or the sidebar quick-switcher.
  const openRun = (id: string) => {
    setShowDashboard(false);
    void refresh(id);
    setCat("All");
    setStatusFilter("all");
    setMobileNav(false);
  };

  // Rename a run (blank clears back to the PDF filename).
  const renameRun = async (r: RunData) => {
    const next = window.prompt(
      "Run name (leave blank to use the PDF filename):",
      r.run_name || "",
    );
    if (next === null) return;
    const name = next.trim();
    try {
      const res = await fetch(`${API}/api/runs/${r.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_name: name || null }),
      });
      if (!res.ok) return;
      const updated: RunData = await res.json();
      setRuns((p) =>
        p.map((x) =>
          x.id === r.id ? { ...x, run_name: updated.run_name ?? null } : x,
        ),
      );
    } catch {
      /* ignore network errors — the name just won't change */
    }
  };

  // Compact one-line run row for the sidebar quick-switcher (latest versions).
  const recentItem = (r: RunData) => (
    <button
      key={r.id}
      className={`recent-item ${r.id === runId && !showDashboard ? "active" : ""}`}
      onClick={() => openRun(r.id)}
      title={`${r.project_name} — ${r.original_filename}\n${formatDateTime(r.created_at)}`}
    >
      <div className="recent-top">
        {r.design_stage && <StageBadge stage={r.design_stage} variant="dark" />}
        <span className="recent-name">{r.project_name}</span>
      </div>
      <div className="recent-sub">
        {relativeDate(r.created_at)} &middot; {runLabel(r)}
      </div>
    </button>
  );

  // One project card in the dashboard grid: stages, each with its run(s) and
  // expandable version history.
  const renderProjectCard = (pg: ProjectGroup) => {
    const expanded = expandedCards.has(pg.key);
    return (
    <div className={`pcard ${expanded ? "pcard-open" : ""}`} key={pg.key}>
      <button className="pcard-head" onClick={() => toggleCard(pg.key)}>
        <span className="pcard-caret">{expanded ? "▾" : "▸"}</span>
        <div className="pcard-head-main">
          <h3 className="pcard-name" title={pg.name}>
            {pg.name}
          </h3>
          <div className="pcard-meta">
            {pg.createdBy && (
              <span className="pcard-owner">{pg.createdBy}</span>
            )}
            <span>
              {pg.runCount} run{pg.runCount === 1 ? "" : "s"}
            </span>
            {pg.lastActivity && (
              <span>&middot; {relativeDate(pg.lastActivity)}</span>
            )}
          </div>
        </div>
      </button>
      {!expanded ? (
        <div className="pcard-summary">
          {pg.stages.map((sg) => {
            const latest = sg.lineages[0]?.latest;
            return (
              <button
                className="pcard-sum-row"
                key={sg.stage || "none"}
                onClick={() => latest && openRun(latest.id)}
                title={latest ? `Open latest: ${runLabel(latest)}` : undefined}
              >
                {sg.stage ? (
                  <StageBadge stage={sg.stage} />
                ) : (
                  <span className="stage-none-l">Unstaged</span>
                )}
                {latest && (
                  <span className="pcard-run-pills">
                    <span className="pill pill-p">
                      {latest.status_counts.Pass ?? 0}
                    </span>
                    <span className="pill pill-f">
                      {latest.status_counts.Fail ?? 0}
                    </span>
                    <span className="pill pill-r">
                      {latest.status_counts["Needs Review"] ?? 0}
                    </span>
                  </span>
                )}
                {sg.lineages.length > 1 && (
                  <span className="pcard-sum-more">
                    +{sg.lineages.length - 1}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      ) : (
      <div className="pcard-stages">
        {pg.stages.map((sg) => (
          <div className="pcard-stage" key={sg.stage || "none"}>
            <div className="pcard-stage-head">
              {sg.stage ? (
                <StageBadge stage={sg.stage} />
              ) : (
                <span className="stage-none-l">Unstaged</span>
              )}
            </div>
            <div className="pcard-runs">
              {sg.lineages.map((ln) => {
                const r = ln.latest;
                const open = expandedVersions.has(ln.root);
                return (
                  <div className="pcard-lineage" key={ln.root}>
                    <button
                      className={`pcard-run ${r.id === runId && !showDashboard ? "active" : ""}`}
                      onClick={() => openRun(r.id)}
                    >
                      <span className="pcard-run-main">
                        <span
                          className="pcard-run-file"
                          title={`${runLabel(r)}\n${r.original_filename}`}
                        >
                          {runLabel(r)}
                        </span>
                        <span className="pcard-run-sub">
                          {(r.version ?? 1) > 1 && <>v{r.version} &middot; </>}
                          {relativeDate(r.created_at)}
                          {(r.created_by || r.engineer_name) && (
                            <> &middot; {r.engineer_name || r.created_by}</>
                          )}
                        </span>
                      </span>
                      <span className="pcard-run-pills">
                        <span className="pill pill-p">
                          {r.status_counts.Pass ?? 0}
                        </span>
                        <span className="pill pill-f">
                          {r.status_counts.Fail ?? 0}
                        </span>
                        <span className="pill pill-r">
                          {r.status_counts["Needs Review"] ?? 0}
                        </span>
                      </span>
                    </button>
                    <div className="pcard-run-actions">
                      <button
                        className="pcard-act"
                        title="Rename this run"
                        onClick={() => void renameRun(r)}
                      >
                        &#9998; Rename
                      </button>
                      <button
                        className="pcard-act"
                        title="Re-analyze (saves a new version)"
                        onClick={() => void reanalyze(r.id)}
                        disabled={uploading}
                      >
                        &#8635; Re-analyze
                      </button>
                      {ln.versions.length > 1 && (
                        <button
                          className="pcard-act"
                          onClick={() => toggleVersions(ln.root)}
                        >
                          {open ? "▾" : "▸"} {ln.versions.length} versions
                        </button>
                      )}
                      <button
                        className="pcard-act pcard-act-del"
                        title="Delete this version"
                        onClick={() => void deleteRun(r.id)}
                      >
                        Delete
                      </button>
                    </div>
                    {open && ln.versions.length > 1 && (
                      <div className="pcard-vers">
                        {ln.versions.map((v) => (
                          <button
                            key={v.id}
                            className={`pcard-ver ${v.id === runId && !showDashboard ? "active" : ""}`}
                            onClick={() => openRun(v.id)}
                          >
                            <span className="pcard-ver-num">
                              v{v.version ?? 1}
                            </span>
                            <span className="pcard-ver-date">
                              {relativeDate(v.created_at)}
                            </span>
                            {v.is_latest ? (
                              <span className="ver-tag">current</span>
                            ) : null}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      )}
    </div>
    );
  };

  const renderDashboard = () => (
    <div className="dashboard">
      <div className="dash-head">
        <div>
          <h1 className="dash-title">Projects</h1>
          <p className="dash-sub">
            {projectGroups.length} project
            {projectGroups.length === 1 ? "" : "s"} &middot; {runs.length} run
            {runs.length === 1 ? "" : "s"}
          </p>
        </div>
        <div className="dash-actions">
          {projectGroups.length > 0 && (
            <button
              className="dash-toggle"
              onClick={() =>
                setExpandedCards((prev) =>
                  prev.size >= projectGroups.length
                    ? new Set()
                    : new Set(projectGroups.map((p) => p.key)),
                )
              }
            >
              {expandedCards.size >= projectGroups.length
                ? "Collapse all"
                : "Expand all"}
            </button>
          )}
          {runs.length > 5 && (
            <input
              className="dash-search"
              type="text"
              placeholder="Search projects, runs, engineers…"
              value={runSearch}
              onChange={(e) => setRunSearch(e.target.value)}
            />
          )}
        </div>
      </div>
      {projectGroups.length ? (
        <div className="pcard-grid">{projectGroups.map(renderProjectCard)}</div>
      ) : runs.length ? (
        <div className="dim">No projects match &ldquo;{runSearch}&rdquo;.</div>
      ) : (
        <div className="welcome">
          <div className="welcome-icon">&#9889;</div>
          <h2>Upload a planset PDF to start</h2>
          <p>
            {CATEGORIES.length * 7}+ checks across {CATEGORIES.length} categories.
            AI-powered engineering validation with NEC code references.
          </p>
          <a
            className="hdr-btn"
            href={`${API}/api/due-diligence-template`}
            target="_blank"
            rel="noreferrer"
          >
            &#8681; Download Due Diligence Template
          </a>
        </div>
      )}
    </div>
  );

  // ── Render ──
  return (
    <div className="app">
      {/* ── Top-right account / sign-in widget ── */}
      <ProfileMenu me={me} />
      {/* ── Shared run queue / activity feed + completion toasts ── */}
      <ActivityPanel
        jobs={jobs}
        concurrency={jobStats.concurrency}
        queuedCount={jobStats.queued}
        runningCount={jobStats.running}
        onOpenRun={(rid) => {
          setRunId(rid);
          setShowDashboard(false);
          setCat("All");
          setStatusFilter("all");
        }}
        desktopAlerts={desktopAlerts}
        onEnableAlerts={enableDesktopAlerts}
        canDesktop={typeof Notification !== "undefined"}
      />
      <Toasts
        toasts={toasts}
        onOpenRun={(rid) => {
          setRunId(rid);
          setShowDashboard(false);
          setCat("All");
          setStatusFilter("all");
        }}
        onDismiss={dismissToast}
      />
      {/* ── QC copilot: per-run grounded chat (read-only) ── */}
      {runId && !showDashboard && (
        <ChatPanel
          key={runId}
          runId={runId}
          runLabel={
            runs.find((r) => r.id === runId)?.original_filename || "this run"
          }
          onCite={(issueId) => {
            // Clear filters so the cited card is actually rendered, focus it,
            // then scroll once the list has re-rendered.
            setCat("All");
            setStatusFilter("all");
            setFocusedIssueId(issueId);
            setTimeout(() => {
              document
                .getElementById(`issue-card-${issueId}`)
                ?.scrollIntoView({ behavior: "smooth", block: "center" });
            }, 150);
          }}
        />
      )}
      {/* ── Global top-bar waiting animation ── */}
      {uploading && (
        <WaitingAnimation pct={progressPct} label={progress} />
      )}
      {/* ── Mobile nav toggle + backdrop ── */}
      <button
        className="nav-hamburger"
        onClick={() => setMobileNav(true)}
        aria-label="Open menu"
      >
        &#9776;
      </button>
      {mobileNav && (
        <div className="side-backdrop" onClick={() => setMobileNav(false)} />
      )}
      {/* ── Sidebar ── */}
      <aside
        className={`side ${sideOpen ? "" : "collapsed"} ${mobileNav ? "mobile-open" : ""}`}
      >
        <div className="side-head">
          <div className="brand">
            <div className="brand-mark">CE</div>
            <div className="brand-text">
              <span className="brand-sub">Castillo Engineering</span>
              <span className="brand-name">Planset QC <span style={{fontSize:"0.6em",opacity:0.5,fontWeight:400}}>v0.3.0</span></span>
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
              <div className="eng-row">
                {addingEngineer ? (
                  <div className="eng-add">
                    <input
                      value={newEngineerDraft}
                      onChange={(e) => setNewEngineerDraft(e.target.value)}
                      placeholder="New engineer name"
                      className="si"
                      autoFocus
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          confirmNewEngineer();
                        } else if (e.key === "Escape") {
                          e.preventDefault();
                          setAddingEngineer(false);
                          setNewEngineerDraft("");
                        }
                      }}
                    />
                    <button
                      type="button"
                      className="eng-add-ok"
                      onClick={confirmNewEngineer}
                      disabled={!newEngineerDraft.trim()}
                      title="Add engineer"
                    >
                      &#10003;
                    </button>
                    <button
                      type="button"
                      className="eng-add-cancel"
                      onClick={() => {
                        setAddingEngineer(false);
                        setNewEngineerDraft("");
                      }}
                      title="Cancel"
                    >
                      &times;
                    </button>
                  </div>
                ) : (
                  <select
                    value={engineerName}
                    onChange={(e) => {
                      const v = e.target.value;
                      if (v === "__add__") {
                        setAddingEngineer(true);
                        setNewEngineerDraft("");
                      } else {
                        nameTouchedRef.current = true;
                        setEngineerName(v);
                      }
                    }}
                    className="si"
                  >
                    <option value="">Your name (QC engineer)…</option>
                    {knownEngineers.map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                    <option value="__add__">+ Add new engineer…</option>
                  </select>
                )}
                {me?.email && (
                  <div
                    className="eng-signed-in"
                    title={`Signed in via Microsoft Entra — runs are attributed to this account${me.email ? ` (${me.email})` : ""}`}
                  >
                    ✓ Signed in as {me.name || me.email}
                  </div>
                )}
              </div>
              <input
                value={projName}
                onChange={(e) => setProjName(e.target.value)}
                placeholder="Project (reuse a name to group runs)"
                className="si"
                list="known-projects"
              />
              <datalist id="known-projects">
                {projectNames.map((n) => (
                  <option key={n} value={n} />
                ))}
              </datalist>
              <input
                value={runName}
                onChange={(e) => setRunName(e.target.value)}
                placeholder="Run name (optional — defaults to file name)"
                className="si"
              />
              <label
                className={`planset-drop ${plansetDragOver ? "planset-drop-over" : ""} ${plansetFile ? "planset-drop-filled" : ""}`}
                onDragOver={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
                  setPlansetDragOver(true);
                }}
                onDragLeave={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setPlansetDragOver(false);
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setPlansetDragOver(false);
                  const accepted = filesFromDrop(
                    e.dataTransfer,
                    "application/pdf,.pdf",
                  );
                  if (accepted[0]) setPlansetFile(accepted[0]);
                }}
              >
                <input
                  name="pdf"
                  type="file"
                  accept="application/pdf"
                  style={{ display: "none" }}
                  onChange={(e) => {
                    const f = e.target.files?.[0] || null;
                    setPlansetFile(f);
                  }}
                />
                {plansetFile ? (
                  <span className="planset-drop-row">
                    <span className="planset-drop-icon">&#128196;</span>
                    <span
                      className="planset-drop-name"
                      title={plansetFile.name}
                    >
                      {plansetFile.name}
                    </span>
                    <button
                      type="button"
                      className="planset-drop-clear"
                      title="Remove"
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        setPlansetFile(null);
                      }}
                    >
                      &times;
                    </button>
                  </span>
                ) : (
                  <span className="planset-drop-row">
                    <span className="planset-drop-icon">&#11015;</span>
                    <span className="planset-drop-text">
                      Drop planset PDF here or click to browse
                    </span>
                  </span>
                )}
              </label>
              <button
                type="button"
                className={`btn-pd-toggle ${pdHasValues ? "btn-pd-active" : ""}`}
                onClick={() => setShowProjDetails(!showProjDetails)}
              >
                {showProjDetails ? "\u25B4 Hide" : "\u25BE Fill"} Project
                Details
                {pdHasValues && <span className="btn-pd-dot" />}
              </button>
              <label
                className="deep-toggle"
                title={
                  deepMode
                    ? "Hybrid: heavy reasoning checks (SLD, DC, TLD, cross-sheet, sysinfo) use the full model; the rest use mini."
                    : "Fast/cheap: every check uses the mini model."
                }
              >
                <input
                  type="checkbox"
                  checked={deepMode}
                  onChange={(e) => setDeepMode(e.target.checked)}
                />
                <span>Deep mode {deepMode ? "(hybrid)" : "(mini only)"}</span>
              </label>
              <label
                className="stage-select"
                title="Design stage — rules requiring later-stage sheets will be deferred (shown as N/A for this stage). Lenient: if a later-stage sheet is actually in the PDF, its rules still fire."
              >
                <span className="stage-label">Design stage</span>
                <select
                  value={designStage}
                  onChange={(e) => setDesignStage(e.target.value)}
                  className="si"
                >
                  {DESIGN_STAGES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </label>
              <div
                className={`sd-box ${supportingDragOver ? "sd-box-over" : ""}`}
                onDragOver={(e) => {
                  if (supportingLoading) return;
                  e.preventDefault();
                  e.stopPropagation();
                  if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
                  setSupportingDragOver(true);
                }}
                onDragLeave={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setSupportingDragOver(false);
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setSupportingDragOver(false);
                  if (supportingLoading) return;
                  const accepted = filesFromDrop(
                    e.dataTransfer,
                    ".pdf,.xlsx,.xls,.csv,.txt,.eml,.msg,.png,.jpg,.jpeg",
                  );
                  if (accepted.length) {
                    parseSupportingDocs(filesToList(accepted));
                  }
                }}
              >
                <div className="sd-label">
                  Supporting documents{" "}
                  <span className="sd-hint">
                    (CESIR, PVSyst, ampacity, …)
                  </span>
                </div>
                <div className="sd-drop-hint">
                  Drop files here or use the picker below
                </div>
                <input
                  type="file"
                  multiple
                  accept=".pdf,.xlsx,.xls,.csv,.txt,.eml,.msg,.png,.jpg,.jpeg"
                  className="si"
                  disabled={supportingLoading}
                  onChange={(e) => {
                    if (e.target.files?.length) {
                      parseSupportingDocs(e.target.files);
                      e.target.value = "";
                    }
                  }}
                />
                {supportingMsg && (
                  <div className="sd-msg">{supportingMsg}</div>
                )}
                {supportingDocs.length > 0 && (
                  <ul className="sd-list">
                    {supportingDocs.map((d) => (
                      <li key={d.filename} className="sd-item">
                        <span className={`sd-type sd-type-${d.doc_type}`}>
                          {d.doc_type}
                        </span>
                        <span className="sd-name" title={d.summary || ""}>
                          {d.filename}
                        </span>
                        <button
                          type="button"
                          className="sd-del"
                          title="Remove"
                          onClick={() => removeSupportingDoc(d.filename)}
                        >
                          &times;
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
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
              <button
                className={`side-projects-btn ${showDashboard ? "active" : ""}`}
                onClick={() => {
                  setShowDashboard(true);
                  setMobileNav(false);
                }}
              >
                <span className="side-projects-icon">&#128193;</span>
                All Projects
                {projectGroups.length > 0 && (
                  <span className="side-projects-count">
                    {projectGroups.length}
                  </span>
                )}
              </button>
              <div className="run-list-head">
                <span className="run-list-title">Recent</span>
              </div>
              {runs.length > 5 && (
                <input
                  className="run-search"
                  type="text"
                  placeholder={"Search…"}
                  value={runSearch}
                  onChange={(e) => setRunSearch(e.target.value)}
                />
              )}
              {recentRuns.map((r) => recentItem(r))}
              {!runs.length && <div className="dim">No runs yet</div>}
              {runs.length > 0 && !recentRuns.length && (
                <div className="dim">No runs match &ldquo;{runSearch}&rdquo;</div>
              )}
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
                className={`pd-dropzone ${parsing ? "pd-dropzone-busy" : ""} ${pdDragOver ? "pd-dropzone-over" : ""}`}
                onDragOver={(e) => {
                  if (parsing) return;
                  e.preventDefault();
                  e.stopPropagation();
                  if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
                  setPdDragOver(true);
                }}
                onDragLeave={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setPdDragOver(false);
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setPdDragOver(false);
                  if (parsing) return;
                  const accepted = filesFromDrop(
                    e.dataTransfer,
                    ".pdf,.png,.jpg,.jpeg,.txt,.eml,.csv,.docx,.doc,.xlsx,.xls",
                  );
                  if (accepted.length) {
                    parseDocuments(filesToList(accepted));
                  }
                }}
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

        {showDashboard ? (
          renderDashboard()
        ) : !run ? (
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
                <button
                  className="hdr-back"
                  onClick={() => setShowDashboard(true)}
                >
                  &#8249; All Projects
                </button>
                <div className="hdr-title-row">
                  <h1 className="hdr-title">{run.project_name}</h1>
                  {run.design_stage && <StageBadge stage={run.design_stage} />}
                  {(run.version ?? 1) > 1 && (
                    <span className="hdr-version" title="Re-analysis version">
                      v{run.version}
                    </span>
                  )}
                  {run.is_latest === 0 && (
                    <span
                      className="hdr-superseded"
                      title="A newer version of this run exists"
                    >
                      superseded
                    </span>
                  )}
                </div>
                <div className="hdr-meta">
                  <span title={run.original_filename}>{runLabel(run)}</span>{" "}
                  &middot; {run.page_count} pages
                  &middot; {formatDateTime(run.created_at)}
                  {(run.created_by || run.engineer_name) && (
                    <> &middot; by {run.engineer_name || run.created_by}</>
                  )}
                  &middot;{" "}
                  <code
                    className="run-id"
                    title={`Run ID: ${run.id}\nClick to copy a shareable link to this run.`}
                    onClick={() => {
                      const link = `${window.location.origin}${window.location.pathname}?run=${run.id}`;
                      navigator.clipboard?.writeText(link);
                    }}
                  >
                    run {run.id.slice(0, 8)}
                  </code>
                </div>
                <GeminiBar
                  u={run.summary.gemini_usage}
                  durationSeconds={run.summary.duration_seconds}
                  deepMode={run.summary.deep_mode}
                  designStage={run.summary.design_stage}
                />
                {run.summary.call_timings && run.summary.call_timings.length > 0 && (
                  <details className="timing-details">
                    <summary>
                      Slowest AI calls: {run.summary.call_timings
                        .slice(0, 3)
                        .map((t) => `${t.label.replace(/\s*\(\d+ pages\)$/, "")} ${t.duration_s.toFixed(0)}s`)
                        .join(" · ")}
                    </summary>
                    <table className="timing-table">
                      <thead>
                        <tr>
                          <th>Category / dispatch</th>
                          <th>Model</th>
                          <th style={{ textAlign: "right" }}>Duration</th>
                        </tr>
                      </thead>
                      <tbody>
                        {run.summary.call_timings.slice(0, 10).map((t, i) => (
                          <tr key={i}>
                            <td>{t.label}</td>
                            <td>
                              <span className={`mode-pill ${t.deep ? "mode-deep" : "mode-mini"}`}>
                                {t.deep ? "Deep" : "Mini"}
                              </span>
                            </td>
                            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                              {t.duration_s.toFixed(1)}s
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </details>
                )}
                {run.summary.supporting_docs &&
                  run.summary.supporting_docs.length > 0 && (
                    <div className="evidence-bar">
                      <span className="evidence-label">Evidence:</span>
                      {run.summary.supporting_docs.map((d) => (
                        <span
                          key={d.filename}
                          className={`sd-type sd-type-${d.doc_type}`}
                          title={`${d.filename}${d.summary ? " — " + d.summary : ""}`}
                        >
                          {d.doc_type}
                        </span>
                      ))}
                    </div>
                  )}
              </div>
              <div className="hdr-right">
                <button
                  className="hdr-btn"
                  onClick={() => void renameRun(run)}
                  title="Rename this run"
                >
                  &#9998; Rename
                </button>
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

            {/* ── Run-level feedback banner ── */}
            {runFeedbackLoaded && !runFeedbackDismissed && (
              <div className={`run-fb ${runFeedback ? "run-fb-rated" : ""}`}>
                {runFeedback && runRatingDraft === null ? (
                  <>
                    <div className="run-fb-rated-text">
                      Rated{" "}
                      <strong>
                        {runFeedback.rating === "saved_time" && "🙂 saved time"}
                        {runFeedback.rating === "even" && "😐 even"}
                        {runFeedback.rating === "cost_time" && "😞 cost time"}
                      </strong>
                      {runFeedback.engineer_name && (
                        <> by {runFeedback.engineer_name}</>
                      )}
                      {" — "}
                      {new Date(runFeedback.created_at).toLocaleDateString()}
                      {runFeedback.comment && (
                        <span className="run-fb-quote">
                          {" "}"{runFeedback.comment}"
                        </span>
                      )}
                    </div>
                    <button
                      className="run-fb-edit"
                      onClick={() => {
                        setRunRatingDraft(runFeedback.rating);
                        setRunRatingComment(runFeedback.comment || "");
                      }}
                    >
                      Edit
                    </button>
                  </>
                ) : (
                  <>
                    <div className="run-fb-prompt">
                      <span className="run-fb-q">How was this run?</span>
                      <div className="run-fb-buttons">
                        {([
                          ["saved_time", "🙂 Saved time"],
                          ["even", "😐 About even"],
                          ["cost_time", "😞 Cost time"],
                        ] as Array<[RunRating, string]>).map(
                          ([val, label]) => (
                            <button
                              key={val}
                              className={`run-fb-btn ${runRatingDraft === val ? "run-fb-btn-on" : ""}`}
                              onClick={() => setRunRatingDraft(val)}
                            >
                              {label}
                            </button>
                          ),
                        )}
                      </div>
                      <input
                        className="run-fb-input"
                        placeholder="Optional: one specific thing (saved time on cover sheet, lost time on E-110, …)"
                        value={runRatingComment}
                        onChange={(e) => setRunRatingComment(e.target.value)}
                      />
                      <button
                        className="run-fb-submit"
                        disabled={
                          runFeedbackSubmitting || runRatingDraft === null
                        }
                        onClick={() =>
                          runRatingDraft &&
                          void submitRunFeedback(
                            run.id, runRatingDraft, runRatingComment,
                          )
                        }
                      >
                        {runFeedbackSubmitting ? "Sending..." : "Submit"}
                      </button>
                      <button
                        className="run-fb-dismiss"
                        title="Hide for now (won't ask again until next run)"
                        onClick={() => setRunFeedbackDismissed(true)}
                      >
                        ×
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}

            {/* ── Score cards ── */}
            <div className="scores">
              <div
                className="sc sc-total"
                title="Total findings emitted (Pass + Fail + Needs Review + Deferred + Accepted)"
              >
                <div className="sc-val">{(run.issues ?? []).length}</div>
                <div className="sc-lab">Total Checks</div>
              </div>
              <div
                className="sc sc-actual"
                title="Checks actually evaluated (Deferred excluded — those couldn't be run with the available evidence)"
              >
                <div className="sc-val">
                  {(run.issues ?? []).length - liveStatusCounts.Deferred}
                </div>
                <div className="sc-lab">Actual Checks</div>
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
              {liveStatusCounts.Deferred > 0 && (
                <div
                  className="sc sc-deferred"
                  onClick={() => {
                    setCat("All");
                    setStatusFilter("deferred");
                  }}
                  title="N/A at this stage OR requires evidence not in this run"
                >
                  <div className="sc-val">{liveStatusCounts.Deferred}</div>
                  <div className="sc-lab">Deferred</div>
                </div>
              )}
              <div className="sc">
                <div className="sc-val">{run.summary.pdf_page_count}</div>
                <div className="sc-lab">Pages</div>
              </div>
              <div
                className="sc"
                title="Pass rate among evaluable checks (Deferred excluded — those weren't runnable with the evidence provided)"
              >
                <div className="sc-val">
                  {Math.round(
                    ((liveStatusCounts.Pass +
                      liveStatusCounts[
                        "Overridden / Accepted by QC Engineer"
                      ]) /
                      Math.max(
                        (run.issues ?? []).length - liveStatusCounts.Deferred,
                        1,
                      )) *
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
                  // Per-status click handler \u2014 picking a count drills into
                  // category + status in one click instead of two.
                  const drill = (
                    e: React.MouseEvent,
                    statusKey: string,
                  ) => {
                    e.stopPropagation();
                    setCat(c.name);
                    setStatusFilter(statusKey);
                  };
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
                        <span
                          className={`cn-f ${(c.Fail ?? 0) === 0 ? "cn-zero" : ""}`}
                          onClick={(e) => drill(e, "fail")}
                          title={`${c.Fail ?? 0} Fail`}
                        >
                          {c.Fail ?? 0}
                        </span>
                        <span
                          className={`cn-r ${(c["Needs Review"] ?? 0) === 0 ? "cn-zero" : ""}`}
                          onClick={(e) => drill(e, "review")}
                          title={`${c["Needs Review"] ?? 0} Needs Review`}
                        >
                          {c["Needs Review"] ?? 0}
                        </span>
                        <span
                          className={`cn-p ${(c.Pass ?? 0) === 0 ? "cn-zero" : ""}`}
                          onClick={(e) => drill(e, "pass")}
                          title={`${c.Pass ?? 0} Pass`}
                        >
                          {c.Pass ?? 0}
                        </span>
                        <span
                          className={`cn-d ${(c.Deferred ?? 0) === 0 ? "cn-zero" : ""}`}
                          onClick={(e) => drill(e, "deferred")}
                          title={`${c.Deferred ?? 0} Deferred`}
                        >
                          {c.Deferred ?? 0}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </nav>

              {/* ── Issue list ── */}
              <section className="issue-panel">
                {/* Compare-mode banner. Visible whenever ``compareRun`` is
                    loaded — replaces the regular list with a diff view. */}
                {compareRun && diff && (
                  <div className="compare-banner">
                    <div className="compare-banner-text">
                      <strong>Diff mode</strong> · comparing{" "}
                      <code>{compareRun.id.slice(0, 8)}</code> →{" "}
                      <code>{run.id.slice(0, 8)}</code>
                      {run.original_filename === compareRun.original_filename ? (
                        <> · same file ({run.original_filename})</>
                      ) : (
                        <> · <em>different files — diff may be misleading</em></>
                      )}
                    </div>
                    <div className="compare-banner-actions">
                      <button
                        className="ib"
                        onClick={() => {
                          // Swap: make the comparison run the current,
                          // and the current run the comparison.
                          const oldCmp = compareRunId;
                          if (oldCmp) {
                            setCompareRunId(runId);
                            setRunId(oldCmp);
                          }
                        }}
                        title="Swap before/after"
                      >
                        ⇄
                      </button>
                      <button
                        className="ib"
                        onClick={() => setCompareRunId(null)}
                        title="Exit compare mode"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                )}

                {/* Compare-mode controls (above the regular toolbar) — show
                    a "Compare with…" picker when not in compare mode, or
                    nothing when we are (banner above handles closing). */}
                {!compareRun && (run?.issues?.length ?? 0) > 0 && runs.length > 1 && (
                  <div className="compare-picker">
                    <label htmlFor="compare-picker-select">Compare with:</label>
                    <select
                      id="compare-picker-select"
                      value=""
                      onChange={(e) => {
                        if (e.target.value) setCompareRunId(e.target.value);
                      }}
                    >
                      <option value="">(pick a run)</option>
                      {runs
                        .filter((r) => r.id !== runId)
                        .slice(0, 30)
                        .map((r) => (
                          <option key={r.id} value={r.id}>
                            {r.project_name || "(no project name)"}
                            {" — "}
                            {r.original_filename}
                            {" · "}
                            {new Date(r.created_at).toLocaleDateString()}
                            {" · "}
                            {r.id.slice(0, 8)}
                          </option>
                        ))}
                    </select>
                  </div>
                )}

                {/* Toolbar */}
                <div className="toolbar">
                  <div className="toolbar-filters">
                    {(
                      [
                        ["all", "All", counts.t],
                        ["pass", "Pass", counts.p],
                        ["fail", "Fail", counts.f],
                        ["review", "Review", counts.r],
                        ["deferred", "Deferred", counts.d],
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
                  <label
                    className="toolbar-toggle"
                    title="Collapse multi-instance findings of the same rule into a single card"
                  >
                    <input
                      type="checkbox"
                      checked={groupingEnabled}
                      onChange={(e) => setGroupingEnabled(e.target.checked)}
                    />
                    <span>Group similar</span>
                  </label>
                  <label className="toolbar-sort">
                    <span className="toolbar-sort-label">Sort</span>
                    <select
                      value={sortBy}
                      onChange={(e) => setSortBy(e.target.value)}
                      title="Order findings by severity, status, category, page, or confidence"
                    >
                      <option value="default">Default</option>
                      <option value="severity">Severity (high → low)</option>
                      <option value="status">Status (Fail → Pass)</option>
                      <option value="category">Category (A → Z)</option>
                      <option value="page">Page (low → high)</option>
                      <option value="confidence">Confidence (low → high)</option>
                    </select>
                  </label>
                  <button
                    className="btn-shortcut-help"
                    onClick={() => setShowShortcuts(true)}
                    title="Keyboard shortcuts (?)"
                    type="button"
                  >
                    ?
                  </button>
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
                  {/* Diff sections — when compare mode is on, show
                      Fixed / New / Drift / Removed instead of the regular
                      cards. Each entry shows before-status → after-status
                      with the rule title, category, page, and a click-
                      through to open the full finding modal. */}
                  {compareRun && diff && (() => {
                    const renderDiffSection = (
                      title: string,
                      cssClass: string,
                      entries: DiffEntry[],
                    ) => {
                      if (entries.length === 0) return null;
                      return (
                        <div
                          key={title}
                          className={`diff-section diff-section-${cssClass}`}
                        >
                          <div className="diff-section-head">
                            <span className="diff-section-title">{title}</span>
                            <span className="diff-section-count">
                              {entries.length}
                            </span>
                          </div>
                          {entries.map((d) => {
                            const beforeS = d.before?.status ?? "—";
                            const afterS = d.after?.status ?? "—";
                            const target = d.after ?? d.before;
                            return (
                              <div
                                key={d.key}
                                className="diff-row"
                                onClick={() => target && setSel(target)}
                              >
                                <span className={`sev sev-${d.severity}`}>
                                  {SV[d.severity]}
                                </span>
                                <div className="diff-row-info">
                                  <div className="diff-row-title">
                                    {d.title}
                                  </div>
                                  <div className="diff-row-sub">
                                    {d.category}
                                    {d.after?.page_number && (
                                      <> · p.{d.after.page_number}</>
                                    )}
                                  </div>
                                </div>
                                <div className="diff-row-transition">
                                  <span
                                    className={`badge badge-${
                                      beforeS === "Pass"
                                        ? "pass"
                                        : beforeS === "Fail"
                                          ? "fail"
                                          : beforeS === "Needs Review"
                                            ? "review"
                                            : beforeS === "Deferred"
                                              ? "deferred"
                                              : beforeS === "Overridden / Accepted by QC Engineer"
                                                ? "ok"
                                                : "neutral"
                                    }`}
                                  >
                                    {SL[beforeS as Status] ?? beforeS}
                                  </span>
                                  <span className="diff-arrow">→</span>
                                  <span
                                    className={`badge badge-${
                                      afterS === "Pass"
                                        ? "pass"
                                        : afterS === "Fail"
                                          ? "fail"
                                          : afterS === "Needs Review"
                                            ? "review"
                                            : afterS === "Deferred"
                                              ? "deferred"
                                              : afterS === "Overridden / Accepted by QC Engineer"
                                                ? "ok"
                                                : "neutral"
                                    }`}
                                  >
                                    {SL[afterS as Status] ?? afterS}
                                  </span>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      );
                    };
                    return (
                      <>
                        {renderDiffSection("Fixed (problem → resolved)", "fixed", diff.fixed)}
                        {renderDiffSection("New problems", "newly", diff.newly)}
                        {renderDiffSection("Drift (status changed)", "drift", diff.drift)}
                        {renderDiffSection("Removed (no longer fired)", "removed", diff.removed)}
                        {diff.fixed.length === 0 &&
                          diff.newly.length === 0 &&
                          diff.drift.length === 0 &&
                          diff.removed.length === 0 && (
                            <div className="dim" style={{ padding: "2rem", textAlign: "center" }}>
                              No differences. {diff.same.length} findings unchanged.
                            </div>
                          )}
                      </>
                    );
                  })()}

                  {!compareRun && issues.length === 0 && (
                    <div
                      className="dim"
                      style={{ padding: "3rem", textAlign: "center" }}
                    >
                      No items match this filter.
                    </div>
                  )}
                  {!compareRun && issueGroups.flatMap((g) => {
                    const showAsGroup =
                      groupingEnabled && g.instances.length > 1;
                    const groupExpanded =
                      !showAsGroup || expandedGroups.has(g.key);

                    const header = showAsGroup ? (
                      <div
                        key={`gh-${g.key}`}
                        className={`group-header sev-${g.severity}`}
                        onClick={() => toggleGroup(g.key)}
                      >
                        <span className="group-toggle">
                          {expandedGroups.has(g.key) ? "▾" : "▸"}
                        </span>
                        <span className={`sev sev-${g.severity}`}>
                          {SV[g.severity]}
                        </span>
                        <div className="group-info">
                          <div className="group-title">{g.title}</div>
                          <div className="group-sub">
                            {g.category} · {g.instances.length} instances
                            {g.pages.length > 0 && (
                              <>
                                {" · pages "}
                                {[...g.pages].sort((a, b) => a - b).join(", ")}
                              </>
                            )}
                          </div>
                        </div>
                        <div className="group-statuses">
                          {(["Fail", "Needs Review", "Pass", "Deferred"] as Status[]).map(
                            (s) => {
                              const n = g.instances.filter(
                                (i) => i.status === s,
                              ).length;
                              if (n === 0) return null;
                              const cls =
                                s === "Fail"
                                  ? "fail"
                                  : s === "Needs Review"
                                    ? "review"
                                    : s === "Pass"
                                      ? "pass"
                                      : "deferred";
                              return (
                                <span
                                  key={s}
                                  className={`group-status group-status-${cls}`}
                                >
                                  {n}
                                  {s === "Fail"
                                    ? "F"
                                    : s === "Needs Review"
                                      ? "R"
                                      : s === "Pass"
                                        ? "P"
                                        : "D"}
                                </span>
                              );
                            },
                          )}
                        </div>
                      </div>
                    ) : null;

                    const cards = (groupExpanded ? g.instances : []).map((issue) => {
                    const isAI = issue.item_key.startsWith("ai_");
                    const hasPreview = !!(
                      issue.snippet_path || issue.page_preview_path
                    );
                    const evExpanded = expandedEv === issue.id;
                    const isEditing = editId === issue.id;
                    return (
                      <div
                        key={issue.id}
                        id={`issue-card-${issue.id}`}
                        className={`card card-${catHealth({ name: "", total: 1, [issue.status === "Overridden / Accepted by QC Engineer" ? "Pass" : issue.status]: 1 } as CategorySummary)} ${focusedIssueId === issue.id ? "card-focused" : ""}`}
                        onClick={(e) => {
                          // Click anywhere on the card focuses it (helps when
                          // you've been keyboarding and want to switch back).
                          // Don't trigger if the click hit a button / link.
                          const t = e.target as HTMLElement;
                          if (t.closest("button, a, input, select, textarea")) return;
                          setFocusedIssueId(issue.id);
                        }}
                      >
                        {/* Row 1: header with status, title, actions */}
                        <div className="card-row">
                          <span
                            className={`badge badge-${issue.status === "Overridden / Accepted by QC Engineer" ? "ok" : issue.status === "Pass" ? "pass" : issue.status === "Fail" ? "fail" : issue.status === "Deferred" ? "deferred" : "review"}`}
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

                          {issue.locations && issue.locations.length > 1 ? (
                            <div
                              className="pg-multi"
                              title={`Spans ${issue.locations.length} sheets`}
                            >
                              {issue.locations.map((loc, i) => (
                                <a
                                  key={i}
                                  className="pg pg-mini"
                                  href={pdfPageUrl(run, loc.page_number)!}
                                  target="_blank"
                                  rel="noreferrer"
                                  title={
                                    loc.source_label ||
                                    `${loc.sheet_number ?? ""} p${loc.page_number}`.trim()
                                  }
                                >
                                  {loc.sheet_number
                                    ? `${loc.sheet_number}·p${loc.page_number}`
                                    : `p.${loc.page_number}`}
                                </a>
                              ))}
                            </div>
                          ) : issue.page_number ? (
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
                            <button
                              className={`ib ib-report ${reportedIds.has(issue.id) ? "ib-report-done" : ""}`}
                              onClick={() =>
                                reportingId === issue.id
                                  ? closeReport()
                                  : openReport(issue.id)
                              }
                              title={
                                reportedIds.has(issue.id)
                                  ? "Feedback submitted — click to add more"
                                  : "Report a problem with this finding (location, citation, reason, etc.)"
                              }
                            >
                              &#9873;
                            </button>
                          </div>
                        </div>

                        {/* Row 2: description (what was checked) */}
                        {issue.description && (
                          <div className="card-desc">{issue.description}</div>
                        )}

                        {/* Inline preview thumbnail.
                            When there IS a bbox, the snippet is a focused
                            crop with the highlight — actually useful
                            inline, so it's shown by default.
                            When there is NO bbox, the "snippet" is just a
                            full-page image with no highlighted region —
                            noisy and not informative — so it's collapsed
                            behind a "Show preview" button. */}
                        {issue.locations && issue.locations.length > 1 ? (
                          <button
                            className="card-thumb-strip"
                            onClick={() => setSel(issue)}
                            title="Conflicting values across sheets — click to compare"
                            type="button"
                          >
                            {issue.locations.map((loc, i) =>
                              loc.snippet_path || loc.page_preview_path ? (
                                <img
                                  key={i}
                                  src={artifactUrl(
                                    loc.snippet_path || loc.page_preview_path,
                                  )}
                                  alt={`${loc.source_label ?? loc.sheet_number ?? "location"} preview`}
                                  loading="lazy"
                                />
                              ) : (
                                <span key={i} className="card-thumb-strip-noimg">
                                  {loc.sheet_number ?? `p${loc.page_number}`}
                                </span>
                              ),
                            )}
                          </button>
                        ) : hasPreview && (issue.bbox || expandedPreview.has(issue.id)) ? (
                          <button
                            className="card-thumb"
                            onClick={() => setSel(issue)}
                            title="Click to open full-resolution preview"
                            type="button"
                          >
                            <img
                              src={artifactUrl(
                                issue.snippet_path || issue.page_preview_path,
                              )}
                              alt={`${issue.title} preview`}
                              loading="lazy"
                            />
                          </button>
                        ) : hasPreview ? (
                          <button
                            className="card-thumb-toggle"
                            onClick={() => togglePreview(issue.id)}
                            type="button"
                            title="No specific location for this finding — click to show the full page anyway"
                          >
                            ▸ Show full-page preview
                          </button>
                        ) : null}

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

                        {/* Source-document citation — when the AI grounded
                            this finding in an uploaded supporting doc, show
                            which file + page + the verbatim excerpt. */}
                        {issue.source_doc_filename && (
                          <div className="card-srcdoc">
                            <span className="card-evidence-label">Source</span>
                            <span className="card-evidence-text">
                              <strong>{issue.source_doc_filename}</strong>
                              {issue.source_doc_page != null && (
                                <> · p.{issue.source_doc_page}</>
                              )}
                              {issue.source_doc_excerpt && (
                                <>
                                  {" — "}
                                  <em>&ldquo;{issue.source_doc_excerpt}&rdquo;</em>
                                </>
                              )}
                            </span>
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

                        {/* Report-a-problem panel */}
                        {reportingId === issue.id && (
                          <div className="card-report">
                            <div className="report-head">
                              Report a problem with this finding
                              <span className="report-sub">
                                {" "}— for the rule itself, not the QC status
                              </span>
                            </div>
                            <div className="report-tags">
                              {REPORT_TAGS.map((t) => {
                                const on = reportTags.has(t.value);
                                return (
                                  <button
                                    key={t.value}
                                    type="button"
                                    className={`report-tag ${on ? "report-tag-on" : ""}`}
                                    onClick={() => toggleReportTag(t.value)}
                                  >
                                    {on ? "✓ " : ""}{t.label}
                                  </button>
                                );
                              })}
                            </div>
                            <textarea
                              className="report-comment"
                              rows={2}
                              placeholder="Optional: details (e.g. 'bbox is on row 5 but the value is on row 4')"
                              value={reportComment}
                              onChange={(e) => setReportComment(e.target.value)}
                            />
                            <div className="report-actions">
                              <button
                                className="btn-add"
                                onClick={submitIssueFeedback}
                                disabled={
                                  reportSubmitting ||
                                  (reportTags.size === 0 &&
                                    !reportComment.trim())
                                }
                              >
                                {reportSubmitting ? "Sending..." : "Submit"}
                              </button>
                              <button
                                className="btn-cancel"
                                onClick={closeReport}
                                disabled={reportSubmitting}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                    });
                    return header ? [header, ...cards] : cards;
                  })}
                </div>
              </section>
            </div>
          </>
        )}
      </main>

      {/* ── Keyboard shortcuts overlay (toggled with ?) ── */}
      {showShortcuts && (
        <div className="overlay" onClick={() => setShowShortcuts(false)}>
          <div
            className="shortcut-help"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="shortcut-head">
              <h2>Keyboard shortcuts</h2>
              <button
                className="detail-close"
                onClick={() => setShowShortcuts(false)}
              >
                ×
              </button>
            </div>
            <table className="shortcut-table">
              <tbody>
                <tr><td><kbd>j</kbd> / <kbd>↓</kbd></td><td>Next finding</td></tr>
                <tr><td><kbd>k</kbd> / <kbd>↑</kbd></td><td>Previous finding</td></tr>
                <tr><td><kbd>p</kbd></td><td>Mark as Pass</td></tr>
                <tr><td><kbd>f</kbd></td><td>Mark as Fail</td></tr>
                <tr><td><kbd>r</kbd></td><td>Mark as Needs Review</td></tr>
                <tr><td><kbd>o</kbd></td><td>Mark as Accepted (Override)</td></tr>
                <tr><td><kbd>Enter</kbd></td><td>Open detail modal</td></tr>
                <tr><td><kbd>Esc</kbd></td><td>Close modal / clear focus</td></tr>
                <tr><td><kbd>?</kbd></td><td>Show / hide this help</td></tr>
              </tbody>
            </table>
            <div className="shortcut-foot">
              Click a card or press <kbd>j</kbd> to start navigating. Status
              keys require a focused finding (highlighted ring).
            </div>
          </div>
        </div>
      )}

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
                    className={`badge badge-${sel.status === "Pass" ? "pass" : sel.status === "Fail" ? "fail" : sel.status === "Needs Review" ? "review" : sel.status === "Deferred" ? "deferred" : "ok"}`}
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
            {sel.locations && sel.locations.length > 1 ? (
              <div className="detail-twoup">
                {sel.locations.map((loc, i) => (
                  <div className="detail-twoup-cell" key={i}>
                    {loc.snippet_path || loc.page_preview_path ? (
                      <img
                        className="detail-twoup-img"
                        src={artifactUrl(
                          loc.snippet_path || loc.page_preview_path,
                        )}
                        alt={`${loc.source_label ?? loc.sheet_number ?? "Location"} preview`}
                      />
                    ) : (
                      <div className="detail-twoup-noimg">
                        No preview for this location
                      </div>
                    )}
                    <div className="detail-twoup-cap">
                      <span className="detail-twoup-loc">
                        {loc.source_label ||
                          [
                            loc.sheet_number,
                            loc.page_number ? `p${loc.page_number}` : null,
                          ]
                            .filter(Boolean)
                            .join(" · ") ||
                          `p${loc.page_number}`}
                        {loc.sheet_number && loc.page_number ? (
                          <span className="detail-twoup-pg">
                            {" "}
                            &middot; p{loc.page_number}
                          </span>
                        ) : null}
                      </span>
                      {loc.value && (
                        <span className="detail-twoup-val">{loc.value}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ) : sel.page_preview_path || sel.snippet_path ? (
              <div
                className={`detail-img-wrap detail-img-${selZoom}`}
                onClick={() =>
                  setSelZoom(selZoom === "fit" ? "actual" : "fit")
                }
                title={
                  selZoom === "fit"
                    ? "Click to zoom in to actual size"
                    : "Click to fit to window"
                }
              >
                <img
                  className="detail-img"
                  src={artifactUrl(sel.page_preview_path || sel.snippet_path)}
                  alt={sel.title}
                />
                <span className="detail-zoom-hint">
                  {selZoom === "fit" ? "Click to zoom in" : "Click to fit"}
                </span>
              </div>
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
              {sel.locations && sel.locations.length > 1
                ? sel.locations
                    .filter((loc) => loc.page_number)
                    .map((loc, i) => (
                      <a
                        key={i}
                        className="hdr-btn"
                        href={pdfPageUrl(run!, loc.page_number)!}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open PDF Page {loc.page_number}
                        {loc.sheet_number ? ` (${loc.sheet_number})` : ""}
                      </a>
                    ))
                : sel.page_number && (
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
