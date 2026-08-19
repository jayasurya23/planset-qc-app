"""Cross every check family's page keywords against every sheet title we have.

WHY. Three separate families have now been caught grading the wrong sheet, and
every one was found by a human reading a report rather than by anything
automatic:

  ai_pad   graded an inverter zone map and failed it for missing rebar
  ai_eaf   shared a sheet with ai_pad, so at most one could be on-topic
  ai_equip graded a 13.2 kV riser-pole BOM and failed it for having no
           inverter, no transformer and no recloser (six HIGH Fails on one
           run) while the sheet titled ENGINEERED EQUIPMENT went unopened

The mechanism is always one of four, and all four are detectable without a
model in the loop:

  COLLISION  two single-page families match the same sheet; at most one can be
             on-topic, and the loser grades a drawing it should never see
  DEAD       a family whose keywords match nothing in the corpus, so its
             checklist has never actually run
  THIN       a family matching so few sheets it is probably mis-keyed
  ORPHAN     a sheet no family claims - not always wrong, but it is where the
             next ai_equip is hiding

Families are parsed out of the dispatch code itself, so this audit cannot
drift out of date the moment somebody adds one.

Usage:
    python backend/scripts/audit_page_routing.py --pdf-dir /home/data/runs
    python backend/scripts/audit_page_routing.py --titles titles.json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

def _find_src() -> Path:
    """Locate gemini_analyzer.py from the repo OR from a deployed container.

    The audit is useful in both places: in the repo before a change, and in
    production against the real corpus, where the app lives under /app.
    """
    here = Path(__file__).resolve()
    for base in (here.parents[2] / "backend", Path("/app/backend"),
                 here.parent.parent):
        cand = base / "app" / "gemini_analyzer.py"
        if cand.exists():
            sys.path.insert(0, str(base))
            return cand
    raise SystemExit("cannot locate backend/app/gemini_analyzer.py")


SRC = _find_src()


def _const_strs(nodes) -> tuple:
    out = []
    for n in nodes:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, (ast.Tuple, ast.List)):
            out.extend(_const_strs(n.elts))
    return tuple(out)


def extract_families(src_path: Path) -> list:
    """Every dispatched family: prefix, keywords, exclude, and how many pages."""
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_gemini_checks")

    searches: dict = {}
    claims: dict = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call, target = node.value, node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        fname = getattr(call.func, "id", None)
        if fname == "find_pages":
            searches[target.id] = {
                "keywords": _const_strs(call.args),
                "exclude": next((_const_strs([k.value]) for k in call.keywords
                                 if k.arg == "exclude"), ()),
            }
        elif fname == "claim_page" and call.args:
            src = call.args[0]
            if isinstance(src, ast.Name):
                claims[target.id] = src.id

    def page_var(node):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            v = node.value.id
            takes = "[:N]" if isinstance(node.slice, ast.Slice) else "[0]"
            return claims.get(v, v), takes
        if isinstance(node, ast.Name):
            v = claims.get(node.id, node.id)
            return (v if v in searches else None), "[0]"
        return None, "?"

    families: list = []
    seen: set = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_safe_call"):
            continue
        if len(node.args) < 8:
            continue
        var, takes = page_var(node.args[2])
        if var not in searches:
            continue
        strs = [a.value for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        prefix = next((s for s in strs if s.startswith("ai_")), None)
        if not prefix:
            continue
        key = (prefix, var, takes)
        if key in seen:
            continue
        seen.add(key)
        entry = {"prefix": prefix, "label": strs[-1] if strs else "",
                 "var": var, "takes": takes}
        entry.update(searches[var])
        families.append(entry)
    return families


try:
    from app.gemini_analyzer import _sheet_title_matches as _matches
except Exception:  # pragma: no cover
    def _matches(title, keywords, exclude=()):
        t = (title or "").upper()
        if any(x.upper() in t for x in exclude):
            return False
        return any(k.upper() in t for k in keywords)


def collect_titles(pdf_dir: Path) -> dict:
    """Sheet numbers and titles for every planset PDF under a directory."""
    import fitz
    from app.analyzer import extract_pages
    out: dict = {}
    for pdf in sorted(pdf_dir.rglob("*.pdf")):
        try:
            doc = fitz.open(pdf)
            pages = extract_pages(doc)
        except Exception as exc:  # noqa: BLE001
            print("  skip " + pdf.name + ": " + type(exc).__name__, file=sys.stderr)
            continue
        key = pdf.parent.name[:12] + "/" + pdf.stem[:28]
        out[key] = [{"page": p.number, "sheet_number": p.sheet_number,
                     "sheet_title": p.sheet_title} for p in pages]
        doc.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf-dir")
    ap.add_argument("--titles")
    ap.add_argument("--dump-titles")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    families = extract_families(SRC)
    print(str(len(families)) + " dispatched families parsed from " + SRC.name)

    if args.titles:
        corpus = json.loads(Path(args.titles).read_text(encoding="utf-8"))
    elif args.pdf_dir:
        corpus = collect_titles(Path(args.pdf_dir))
        if args.dump_titles:
            Path(args.dump_titles).write_text(
                json.dumps(corpus, indent=1), encoding="utf-8")
    else:
        ap.error("pass --pdf-dir or --titles")

    n_sheets = sum(len(v) for v in corpus.values())
    print("corpus: " + str(len(corpus)) + " plansets, " + str(n_sheets) + " sheets")
    print("")

    claimed = defaultdict(list)
    hits = defaultdict(int)
    for planset, sheets in corpus.items():
        for s in sheets:
            title = s["sheet_title"] or ""
            for f in families:
                if _matches(title, f["keywords"], f["exclude"]):
                    claimed[(planset, s["page"], title)].append(f["prefix"])
                    hits[f["prefix"]] += 1

    single = set(f["prefix"] for f in families if f["takes"] == "[0]")

    print("=" * 74)
    print("COLLISIONS - one sheet claimed by more than one single-page family")
    print("=" * 74)
    pairs = defaultdict(set)
    n_coll = 0
    for key, owners in sorted(claimed.items()):
        s = sorted(set(o for o in owners if o in single))
        if len(s) > 1:
            n_coll += 1
            pairs[tuple(s)].add(key[2])
            if n_coll <= 20:
                print("  %-26s p%-4s %-32s %s"
                      % (key[0][:26], key[1], key[2][:32], ", ".join(s)))
    print("")
    print("  " + str(n_coll) + " colliding sheet(s)")
    for pair, titles in sorted(pairs.items(), key=lambda x: -len(x[1])):
        print("    %-38s %3d sheet(s)  e.g. %s"
              % (" + ".join(pair), len(titles), sorted(titles)[0][:32]))

    print("")
    print("=" * 74)
    print("DEAD / THIN - families matching few or no sheets")
    print("=" * 74)
    for f in sorted(families, key=lambda x: hits[x["prefix"]]):
        h = hits[f["prefix"]]
        if h <= 3:
            tag = "DEAD" if h == 0 else "THIN"
            print("  [%s] %-16s %3d sheet(s)  kw=%s"
                  % (tag, f["prefix"], h, list(f["keywords"])[:4]))

    print("")
    print("=" * 74)
    print("ORPHANS - sheet titles no family claims")
    print("=" * 74)
    by_title = defaultdict(int)
    for p, sheets in corpus.items():
        for s in sheets:
            if (p, s["page"], s["sheet_title"] or "") not in claimed:
                by_title[(s["sheet_title"] or "(no title)")[:44]] += 1
    for t, c in sorted(by_title.items(), key=lambda x: -x[1])[:25]:
        print("  x%-4d %s" % (c, t))
    print("")
    print("  " + str(sum(by_title.values())) + " unclaimed sheet(s), "
          + str(len(by_title)) + " distinct titles")

    print("")
    print("=" * 74)
    print("PER-FAMILY HIT COUNT")
    print("=" * 74)
    for f in sorted(families, key=lambda x: -hits[x["prefix"]]):
        ex = "  excl=" + str(list(f["exclude"])) if f["exclude"] else ""
        print("  %-18s %-6s %4d  kw=%s%s"
              % (f["prefix"], f["takes"], hits[f["prefix"]],
                 list(f["keywords"])[:4], ex))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "families": families,
            "collisions": dict((" + ".join(k), sorted(v))
                               for k, v in pairs.items()),
            "dead": [f["prefix"] for f in families if hits[f["prefix"]] == 0],
            "thin": [f["prefix"] for f in families
                     if 0 < hits[f["prefix"]] <= 3],
            "unclaimed_titles": dict(sorted(by_title.items(),
                                            key=lambda x: -x[1])),
            "hits": dict(hits),
        }, indent=1), encoding="utf-8")
        print("")
        print("wrote " + args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
