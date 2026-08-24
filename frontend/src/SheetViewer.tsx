import { useCallback, useEffect, useRef, useState } from "react";

/* ---------------------------------------------------------------------------
   Sheet viewer — pan and zoom over a full drawing preview.

   These are 36 x 24 inch sheets shown in a modal a few hundred pixels wide, so
   the fit view is around a third of native size and the reviewer is always
   going to want to get closer. The previous version toggled between "fit" and
   "actual" on click, which gave one fixed magnification, made panning a
   scrollbar exercise, and meant any attempt to drag the image dismissed the
   zoom instead.

   Wheel zooms about the cursor, drag pans, double-click toggles fit and 1:1.
   --------------------------------------------------------------------------- */
export default function SheetViewer({ src, alt }: { src: string; alt: string }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [nat, setNat] = useState({ w: 0, h: 0 });
  const [box, setBox] = useState({ w: 0, h: 0 });
  const [scale, setScale] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  // Scale at which the sheet fits the frame. Also the zoom-out floor: there is
  // no reason to shrink a drawing below the size of the window showing it.
  const fit = nat.w && box.w
    ? Math.min(box.w / nat.w, box.h / nat.h)
    : 1;
  const maxScale = Math.max(fit * 10, 4);

  const clampPan = useCallback((x: number, y: number, sc: number) => {
    const sw = nat.w * sc;
    const sh = nat.h * sc;
    // Centred while it is smaller than the frame, bounded once it is bigger,
    // so the drawing can never be flung off into empty space.
    const cx = sw <= box.w ? (box.w - sw) / 2 : Math.min(0, Math.max(box.w - sw, x));
    const cy = sh <= box.h ? (box.h - sh) / 2 : Math.min(0, Math.max(box.h - sh, y));
    return { x: cx, y: cy };
  }, [nat.w, nat.h, box.w, box.h]);

  const zoomAbout = useCallback((factor: number, cx: number, cy: number) => {
    setScale(prev => {
      const next = Math.min(maxScale, Math.max(fit, prev * factor));
      if (next === prev) return prev;
      // Keep whatever is under the pointer pinned there.
      setPan(p => clampPan(
        cx - (cx - p.x) * (next / prev),
        cy - (cy - p.y) * (next / prev),
        next,
      ));
      return next;
    });
  }, [fit, maxScale, clampPan]);

  const measure = useCallback(() => {
    const el = boxRef.current;
    if (el) setBox({ w: el.clientWidth, h: el.clientHeight });
  }, []);

  useEffect(() => {
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  // Reset to fit whenever a different sheet is shown.
  useEffect(() => {
    setScale(fit);
    setPan(clampPan(0, 0, fit));
  }, [src, fit, clampPan]);

  // Non-passive so the page behind the modal does not scroll while zooming.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      zoomAbout(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAbout]);

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    // Start the drag FIRST. Pointer capture is a nicety -- it keeps the drag
    // alive if the cursor leaves the frame -- but it throws for a pointer the
    // browser does not consider active, and doing it first meant one throw
    // took the whole drag with it.
    drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
    setDragging(true);
    try {
      (e.currentTarget as Element).setPointerCapture(e.pointerId);
    } catch {
      /* capture unavailable — dragging still works inside the frame */
    }
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    setPan(clampPan(d.px + (e.clientX - d.x), d.py + (e.clientY - d.y), scale));
  };
  const endDrag = () => { drag.current = null; setDragging(false); };

  const setZoom = (next: number) => {
    const n = Math.min(maxScale, Math.max(fit, next));
    setScale(n);
    setPan(p => clampPan(p.x, p.y, n));
  };
  const zoomCentre = (f: number) => zoomAbout(f, box.w / 2, box.h / 2);

  const pct = Math.round(scale * 100);
  const atFit = Math.abs(scale - fit) < 0.005;

  return (
    <div className="sheet-viewer">
      <div
        ref={boxRef}
        className={"sheet-stage" + (dragging ? " is-dragging" : "")}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={() => setZoom(atFit ? 1 : fit)}
      >
        <img
          className="sheet-img"
          src={src}
          alt={alt}
          draggable={false}
          onLoad={e => {
            const i = e.currentTarget;
            setNat({ w: i.naturalWidth, h: i.naturalHeight });
            measure();
          }}
          style={{
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})`,
            transformOrigin: "0 0",
          }}
        />
      </div>

      <div className="sheet-controls" role="group" aria-label="Zoom">
        <button onClick={() => zoomCentre(1 / 1.4)} disabled={atFit}
                title="Zoom out" aria-label="Zoom out">&minus;</button>
        <span className="sheet-pct" title="Current zoom">{pct}%</span>
        <button onClick={() => zoomCentre(1.4)} disabled={scale >= maxScale - 0.001}
                title="Zoom in" aria-label="Zoom in">+</button>
        <button className="sheet-preset" onClick={() => setZoom(fit)} disabled={atFit}>Fit</button>
        <button className="sheet-preset" onClick={() => setZoom(1)}
                disabled={Math.abs(scale - 1) < 0.005}>1:1</button>
      </div>
      <span className="sheet-hint">scroll to zoom &middot; drag to pan &middot; double-click to toggle</span>
    </div>
  );
}
