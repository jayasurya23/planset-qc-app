import { useEffect, useMemo, useRef, useState } from "react";
import type {
  ProjectIdSuggestion,
  PortfolioResponse,
  ProjectInfo,
} from "./types";

/* ---------------------------------------------------------------------------
   Castillo Project ID — named after the "Project ID" column on monday.com's
   Portfolio board, and the key a QC project shares with PMO 360 and monday.
   (castillo_project_id in code: project_id is the QC app's own project key.)

   The value is opaque (two formats are in circulation and the scheme has
   changed before), so it is only ever compared after removing presentation
   noise, never parsed. Links come from the server, which resolves monday once
   when the Project ID is saved; nothing here calls monday or PMO 360.
   --------------------------------------------------------------------------- */

/** Comparison key: dashes unified, spaces around them dropped, case folded. */
export function projectIdKey(value: string | null | undefined): string {
  return (value || "")
    .replace(/[‐-―−﹣－]/g, "-")
    .replace(/\s*-\s*/g, "-")
    .trim()
    .replace(/\s+/g, " ")
    .toLowerCase();
}

/** The drawings print a different Project ID than the project carries. */
export function projectIdMismatch(p: ProjectInfo | undefined): string | null {
  if (!p?.castillo_project_id || !p.title_block_project_id) return null;
  return projectIdKey(p.castillo_project_id) === projectIdKey(p.title_block_project_id)
    ? null
    : p.title_block_project_id;
}

function mondayNote(p: ProjectInfo): { text: string; title: string } | null {
  const m = p.monday;
  switch (m.status) {
    case "not_found":
      return { text: "not on monday", title: m.detail || "No Portfolio item has this Project ID." };
    case "ambiguous":
      return { text: "several on monday", title: m.detail || "More than one Portfolio item has this Project ID." };
    case "error":
      return { text: "monday unavailable", title: m.detail || "monday.com could not be reached." };
    default:
      return null;
  }
}

/** Project ID chip plus PMO 360 / monday links, for a project card or the run header. */
export function ProjectIdLinks({
  project,
  onEdit,
  onUse,
  compact = false,
}: {
  project: ProjectInfo | undefined;
  onEdit: () => void;
  /** One-click adopt of the Project ID printed on the drawings. */
  onUse?: (castilloProjectId: string) => void;
  compact?: boolean;
}) {
  if (!project) return null;
  const num = project.castillo_project_id;
  const printed = project.title_block_project_id;
  const cls = compact ? "pid-links pid-links-compact" : "pid-links";

  if (!num) {
    return (
      <div className={cls}>
        {printed && onUse ? (
          <>
            <span className="pid-detected" title="Read from the title block of the newest drawings">
              Drawings print <b>{printed}</b>
            </span>
            <button className="pid-btn pid-btn-primary" onClick={() => onUse(printed)}>
              Use as Project ID
            </button>
            <button className="pid-btn" onClick={onEdit}>
              Other&hellip;
            </button>
          </>
        ) : (
          <button className="pid-btn" onClick={onEdit} title="Link this project to PMO 360 and monday.com">
            + Project ID
          </button>
        )}
      </div>
    );
  }

  const mismatch = projectIdMismatch(project);
  const note = mondayNote(project);
  return (
    <div className={cls}>
      <button
        className="pid-chip"
        onClick={onEdit}
        title={`Castillo Project ID${project.castillo_project_id_set_by ? ` · set by ${project.castillo_project_id_set_by}` : ""} — click to change`}
      >
        <span className="pid-chip-label">Project ID</span>
        {num}
      </button>
      {mismatch && (
        <span
          className="pid-warn"
          title={`The newest drawings print ${mismatch} beside CASTILLO PROJECT ID, not ${num}.`}
        >
          &#9888; drawings: {mismatch}
        </span>
      )}
      <span className="pid-ext">
        {project.links.pmo360 && (
          <a className="pid-ext-link" href={project.links.pmo360} target="_blank" rel="noreferrer">
            PMO 360 &#8599;
          </a>
        )}
        {project.links.monday_board ? (
          <a
            className="pid-ext-link"
            href={project.links.monday_board}
            target="_blank"
            rel="noreferrer"
            title={project.monday.board_name ? `monday board: ${project.monday.board_name}` : undefined}
          >
            monday &#8599;
          </a>
        ) : project.links.monday_item ? (
          <a
            className="pid-ext-link"
            href={project.links.monday_item}
            target="_blank"
            rel="noreferrer"
            title="Portfolio item (no project board is linked to it)"
          >
            monday &#8599;
          </a>
        ) : (
          note && (
            <span className="pid-muted" title={note.title}>
              {note.text}
            </span>
          )
        )}
      </span>
    </div>
  );
}

/** Set, change or clear a project's Project ID. */
export function ProjectIdDialog({
  api,
  project,
  portfolio,
  onClose,
  onSaved,
}: {
  api: string;
  project: ProjectInfo;
  portfolio: PortfolioResponse | null;
  onClose: () => void;
  onSaved: (p: ProjectInfo) => void;
}) {
  const [value, setValue] = useState(project.castillo_project_id || "");
  const [suggestions, setSuggestions] = useState<ProjectIdSuggestion[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<ProjectInfo>(project);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
    let cancelled = false;
    fetch(`${api}/api/projects/${project.id}/project-id-suggestions`)
      .then((r) => (r.ok ? r.json() : { suggestions: [] }))
      .then((d) => {
        if (!cancelled) setSuggestions(d.suggestions || []);
      })
      .catch(() => {
        if (!cancelled) setSuggestions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [api, project.id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onMonday = useMemo(() => {
    const k = projectIdKey(value);
    return k ? portfolio?.items.find((i) => projectIdKey(i.castillo_project_id) === k) || null : null;
  }, [value, portfolio]);

  const save = async (next: string | null, refresh = false) => {
    setSaving(true);
    setError(null);
    try {
      const res = refresh
        ? await fetch(`${api}/api/projects/${project.id}/monday-refresh`, { method: "POST" })
        : await fetch(`${api}/api/projects/${project.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ castillo_project_id: next }),
          });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.detail ? String(body.detail) : `Save failed (HTTP ${res.status}).`);
        return;
      }
      const updated: ProjectInfo = await res.json();
      setCurrent(updated);
      setValue(updated.castillo_project_id || "");     // show the saved, normalised form
      onSaved(updated);
      // Stay open when there is something to read: monday could not link it,
      // or another project already carries the number.
      const clean = updated.monday.status === "linked" || updated.monday.status === "not_configured";
      if (!updated.castillo_project_id || (clean && !updated.duplicates?.length)) {
        onClose();
      }
    } catch {
      setError("Could not reach the server.");
    } finally {
      setSaving(false);
    }
  };

  const status = current.castillo_project_id ? current.monday.status : null;
  const dirty = projectIdKey(value) !== projectIdKey(current.castillo_project_id);

  return (
    <div className="overlay" onClick={onClose}>
      <div className="piddlg" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Project ID">
        <div className="piddlg-head">
          <div>
            <div className="piddlg-crumb">Castillo Project ID</div>
            <h2 className="piddlg-title" title={project.name}>{project.name}</h2>
          </div>
          <button className="detail-close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>

        <form
          className="piddlg-body"
          onSubmit={(e) => {
            e.preventDefault();
            if (!saving) save(value.trim() || null);
          }}
        >
          <label className="piddlg-label" htmlFor="piddlg-input">
            Project ID, as on the monday Portfolio board and in PMO 360
          </label>
          <input
            id="piddlg-input"
            ref={inputRef}
            className="piddlg-input"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="e.g. 264-066"
            list="piddlg-portfolio"
            autoComplete="off"
            spellCheck={false}
            maxLength={50}
          />
          <datalist id="piddlg-portfolio">
            {(portfolio?.items || []).map((i) => (
              <option key={`${i.castillo_project_id}-${i.name}`} value={i.castillo_project_id}>
                {i.name}
                {i.client ? ` — ${i.client}` : ""}
              </option>
            ))}
          </datalist>

          <div className="piddlg-hint">
            {onMonday ? (
              <>
                On monday: <b>{onMonday.name}</b>
                {onMonday.client ? ` · ${onMonday.client}` : ""}
                {onMonday.status ? ` · ${onMonday.status}` : ""}
              </>
            ) : portfolio?.configured && value.trim() ? (
              <>Not on the monday Portfolio board.</>
            ) : !portfolio?.configured ? (
              <>monday lookup is not configured on this server; PMO 360 links still work.</>
            ) : (
              <>&nbsp;</>
            )}
          </div>

          {suggestions && suggestions.length > 0 && (
            <div className="piddlg-sugs">
              {suggestions.map((s) => (
                <button
                  type="button"
                  key={`${s.source}-${s.castillo_project_id}`}
                  className={`piddlg-sug ${projectIdKey(s.castillo_project_id) === projectIdKey(value) ? "on" : ""}`}
                  onClick={() => setValue(s.castillo_project_id)}
                  title={s.source === "title_block"
                    ? "Printed beside CASTILLO PROJECT ID on the newest drawings"
                    : "A monday Portfolio project with a similar name"}
                >
                  <span className="piddlg-sug-src">
                    {s.source === "title_block" ? "On the drawings" : "Similar name"}
                  </span>
                  <span className="piddlg-sug-num">{s.castillo_project_id}</span>
                  {s.name && <span className="piddlg-sug-name">{s.name}</span>}
                  {s.source === "title_block" && s.on_monday === false && (
                    <span className="piddlg-sug-name">not on monday</span>
                  )}
                </button>
              ))}
            </div>
          )}

          {current.castillo_project_id && !dirty && status && status !== "linked" && (
            <div className={`piddlg-status piddlg-status-${status}`}>
              {status === "not_configured"
                ? "Saved. monday lookup is not configured on this server, so there is no monday link."
                : current.monday.detail || "Saved, but monday.com did not link it."}
              {status !== "not_configured" && (
                <button type="button" className="pid-btn" disabled={saving} onClick={() => save(null, true)}>
                  Retry monday
                </button>
              )}
            </div>
          )}
          {current.castillo_project_id && !dirty && status === "linked" && (
            <div className="piddlg-status piddlg-status-linked">
              Linked to monday{current.monday.board_name ? ` board “${current.monday.board_name}”` : " Portfolio item"}.
            </div>
          )}
          {current.duplicates && current.duplicates.length > 0 && !dirty && (
            <div className="piddlg-status piddlg-status-ambiguous">
              Also used by {current.duplicates.map((d) => d.name).join(", ")}. That can be right (a
              permit and an IFC set of one project) — check it is not a typo.
            </div>
          )}
          {error && <div className="piddlg-status piddlg-status-error">{error}</div>}

          <div className="piddlg-foot">
            {current.castillo_project_id && (
              <button
                type="button"
                className="hdr-btn hdr-btn-danger"
                disabled={saving}
                onClick={() => {
                  setValue("");
                  save(null);
                }}
              >
                Remove
              </button>
            )}
            <span style={{ flex: 1 }} />
            <button type="button" className="hdr-btn" onClick={onClose} disabled={saving}>
              Cancel
            </button>
            <button
              type="submit"
              className="hdr-btn hdr-btn-accent"
              disabled={saving || !value.trim() || !dirty}
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
