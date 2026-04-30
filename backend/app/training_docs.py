"""Training-doc corpus: chunk, tag, and retrieve for V4 RAG injection.

The Castillo Training_Docs folder contains 41 Joe Jancauskas memos extracted
into MASTER_TRAINING_CONTEXT.txt with the following structure::

    ==================================================
    SOURCE DOCUMENT: Training - <topic> - <date>.pdf
    ==================================================

    --- PAGE 1 ---
    ...body text...

    --- PAGE 2 ---
    ...body text...


    END_OF_DOCUMENT<<>>

This module parses that file into one chunk per PDF, loads the category
tags produced by ``scripts/tag_training_docs.py`` from
``backend/data/training_docs/index.yaml``, and exposes a retrieval helper
``get_chunks_for_category(cat)`` used by the V4 prompt builder when
``ENABLE_TRAINING_RAG=1`` is set.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # deferred — only needed if the index is loaded

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
PROJECT = BACKEND.parent
TRAINING_DIR = PROJECT / "2026-04-16-AI QC" / "Training_Docs"
MASTER_TXT = TRAINING_DIR / "MASTER_TRAINING_CONTEXT.txt"
INDEX_YAML = BACKEND / "data" / "training_docs" / "index.yaml"

# ---------------------------------------------------------------------------
# Chunk model
# ---------------------------------------------------------------------------


@dataclass
class TrainingChunk:
    """One training memo (one PDF) with its body text and metadata."""
    source_pdf: str            # original filename, e.g. "Training - Cables - ..."
    body: str                  # plain-text body (no SOURCE/END markers)
    page_count: int
    char_len: int
    # Populated by tag_training_docs.py / index.yaml
    primary_category: Optional[str] = None
    secondary_categories: list[str] = field(default_factory=list)
    summary: str = ""
    checkpoints: list[str] = field(default_factory=list)   # testable assertions
    tags: list[str] = field(default_factory=list)          # free-form keywords

    @property
    def short_id(self) -> str:
        """Filename-safe short id (first 40 chars of source_pdf stem)."""
        stem = re.sub(r"\.pdf$", "", self.source_pdf, flags=re.IGNORECASE)
        stem = re.sub(r"[^a-zA-Z0-9._\- ]+", "", stem)
        return stem[:60].strip()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_SOURCE_LINE = re.compile(r"^SOURCE DOCUMENT:\s*(.+?)\s*$")
_PAGE_MARKER = re.compile(r"^--- PAGE \d+ ---\s*$")
_END_MARKER = "END_OF_DOCUMENT<<>>"
_SEP_LINE = re.compile(r"^=+\s*$")


def parse_master_context(path: Path | str = MASTER_TXT) -> list[TrainingChunk]:
    """Split MASTER_TRAINING_CONTEXT.txt into one chunk per source PDF."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Training master file not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")

    chunks: list[TrainingChunk] = []
    # Greedy split by SOURCE DOCUMENT: header
    blocks = re.split(r"(?m)^SOURCE DOCUMENT:\s*", text)
    for block in blocks[1:]:  # blocks[0] is preamble (usually empty)
        # The PDF name is the first line of the block
        nl = block.find("\n")
        if nl == -1:
            continue
        pdf_name = block[:nl].strip()
        rest = block[nl + 1:]

        # Drop closing separator + everything after END_OF_DOCUMENT
        end_idx = rest.find(_END_MARKER)
        if end_idx != -1:
            rest = rest[:end_idx]

        # Strip separator lines and page-marker lines; count pages
        lines = rest.splitlines()
        kept: list[str] = []
        page_count = 0
        for ln in lines:
            if _SEP_LINE.match(ln):
                continue
            if _PAGE_MARKER.match(ln):
                page_count += 1
                continue
            kept.append(ln)
        body = "\n".join(kept).strip()
        if not body:
            continue
        chunks.append(TrainingChunk(
            source_pdf=pdf_name,
            body=body,
            page_count=max(page_count, 1),
            char_len=len(body),
        ))
    return chunks


# ---------------------------------------------------------------------------
# Index load / save
# ---------------------------------------------------------------------------


def load_index(path: Path | str = INDEX_YAML) -> list[TrainingChunk]:
    """Load the tagged training-doc index from index.yaml.

    The yaml schema is::

        chunks:
          - source_pdf: "Training - ..."
            primary_category: "E-101/E-102"
            secondary_categories: ["E-100"]
            summary: "one-line summary"
            checkpoints:
              - "Check A..."
              - "Check B..."
            tags: [cable, MV, shield]

    Bodies are NOT stored in yaml (too large); they are re-parsed from the
    master txt on demand via ``parse_master_context`` and joined on
    ``source_pdf``.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required to load the training-doc index.")
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    entries = data.get("chunks", [])

    by_pdf: dict[str, dict] = {e["source_pdf"]: e for e in entries if "source_pdf" in e}

    chunks = parse_master_context()
    for c in chunks:
        meta = by_pdf.get(c.source_pdf)
        if not meta:
            continue
        c.primary_category = meta.get("primary_category")
        c.secondary_categories = list(meta.get("secondary_categories") or [])
        c.summary = meta.get("summary", "") or ""
        c.checkpoints = list(meta.get("checkpoints") or [])
        c.tags = list(meta.get("tags") or [])
    return chunks


def save_index(chunks: list[TrainingChunk], path: Path | str = INDEX_YAML) -> Path:
    """Write chunks to index.yaml (metadata only, no bodies)."""
    if yaml is None:
        raise RuntimeError("PyYAML is required to save the training-doc index.")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "chunks": [
            {
                "source_pdf": c.source_pdf,
                "page_count": c.page_count,
                "char_len": c.char_len,
                "primary_category": c.primary_category,
                "secondary_categories": c.secondary_categories,
                "summary": c.summary,
                "checkpoints": c.checkpoints,
                "tags": c.tags,
            }
            for c in chunks
        ]
    }
    p.write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True),
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def get_chunks_for_category(
    category: str,
    *,
    include_secondary: bool = True,
    max_chunks: int = 2,
) -> list[TrainingChunk]:
    """Return training chunks tagged to a V4 category.

    Called by the V4 prompt builder (behind ``ENABLE_TRAINING_RAG=1``) to
    inject doctrine context. Returns [] if index is missing or category has
    no tagged chunks.
    """
    try:
        all_chunks = load_index()
    except Exception:
        return []
    hits: list[TrainingChunk] = []
    for c in all_chunks:
        if c.primary_category == category:
            hits.append(c)
        elif include_secondary and category in c.secondary_categories:
            hits.append(c)
        if len(hits) >= max_chunks:
            break
    return hits


def is_training_rag_enabled() -> bool:
    """True if ENABLE_TRAINING_RAG=1 in env (gated rollout)."""
    return os.getenv("ENABLE_TRAINING_RAG", "").strip() in ("1", "true", "yes", "on")


__all__ = [
    "TrainingChunk",
    "parse_master_context",
    "load_index",
    "save_index",
    "get_chunks_for_category",
    "is_training_rag_enabled",
    "MASTER_TXT",
    "INDEX_YAML",
    "TRAINING_DIR",
]
