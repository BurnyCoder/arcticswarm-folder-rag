# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Local-folder corpus backend (``web.corpus_backend: folder``).

Global context
--------------
ArcticSwarm's BrowseComp-Plus mode routes ``web_search`` / ``web_fetch`` to a
pluggable :class:`~arcticswarm.tools.corpus_retriever.CorpusRetriever`. This module
adds a backend that turns *any directory* into that corpus, so the unchanged swarm
(orchestrator, isolation-mode subagents, reviewers) can do retrieval-augmented
generation over a folder of files instead of the web.

Local design
------------
* **Walk** the folder recursively, skipping VCS/cache/env directories and files
  larger than ``max_file_bytes``; binary files are detected by a NUL byte in the
  first 8 KiB.
* **Extract** text: UTF-8 (``errors="replace"``) for text/code; ``.pdf`` via pypdf;
  ``.docx/.pptx/.xlsx/.html`` via markitdown. Both are existing dependencies.
* **Chunk** line-aligned windows of ~``chunk_chars`` characters with
  ``overlap_lines`` of overlap. Over-long single lines (minified files, long
  paragraphs) are hard-split. Every chunk keeps its 1-based line span, so its
  docid ``<relpath>#L<start>-<end>`` is a stable, human-checkable citation.
* **Rank** with BM25 (``bm25s``, Lù 2024, arXiv:2407.03618 — Okapi BM25 with
  eager sparse scoring; https://github.com/xhluca/bm25s, MIT). Docs and queries
  get the same normalisation: identifiers are split on ``_ - . /`` and camelCase
  (so "field size limit" matches ``field_size_limit``), then bm25s tokenisation
  with English stopwords and the Snowball stemmer (PyStemmer) when installed.
* **Cache**: indexes are persisted under ``index_dir`` keyed by a fingerprint of
  (root, every file's relpath/size/mtime, chunking params), and memoised once per
  process behind a lock. The ToolFactory rebuilds retrievers per agent and per
  task, so without the memo every subagent would re-index the folder.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcticswarm.tools.corpus_retriever import CorpusRetriever

logger = logging.getLogger(__name__)

# Directories that never contain corpus content (VCS metadata, caches, envs).
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
})
# Extensions converted by markitdown (rich documents) / pypdf (PDF).
_MARKITDOWN_EXTS = frozenset({".docx", ".pptx", ".xlsx", ".html", ".htm", ".epub"})
_PDF_EXTS = frozenset({".pdf"})
# Docid prefix shown to agents and parsed back by fetch(); matches the
# corpus://<backend>/<id> convention of the existing "local" backend.
URL_PREFIX = "corpus://folder/"
_DOCID_RE = re.compile(r"^(?P<path>.+?)(?:#L(?P<start>\d+)(?:-(?P<end>\d+))?)?$")


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit: a line span of one file."""

    path: str   # POSIX-style path relative to the corpus root
    start: int  # first line (1-based, inclusive)
    end: int    # last line (inclusive)
    text: str

    @property
    def docid(self) -> str:
        return f"{self.path}#L{self.start}-{self.end}"


# ---------------------------------------------------------------------------
# Text normalisation shared by documents and queries
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEP_RE = re.compile(r"[_\-./\\:]+")


def normalize_for_bm25(text: str) -> str:
    """Split code identifiers so natural-language queries can match them.

    ``fieldSizeLimit`` / ``field_size_limit`` / ``eval/data_loader.py`` all become
    space-separated words. Applied identically to documents and queries, so BM25's
    bag-of-words matching stays symmetric (exact strings remain greppable via the
    agents' bash tool).
    """
    return _SEP_RE.sub(" ", _CAMEL_RE.sub(" ", text))


# ---------------------------------------------------------------------------
# File walking + extraction + chunking
# ---------------------------------------------------------------------------


def iter_files(root: Path, skip_dirs: frozenset[str], max_file_bytes: int) -> list[Path]:
    """Return every candidate file under ``root`` (sorted, deterministic)."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped directories in place so os.walk never descends into them.
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                if p.is_file() and p.stat().st_size <= max_file_bytes:
                    out.append(p)
            except OSError:
                continue
    return out


def extract_text(path: Path) -> str | None:
    """Return the text of ``path`` or None when it is binary / unreadable."""
    suffix = path.suffix.lower()
    try:
        if suffix in _PDF_EXTS:
            from pypdf import PdfReader  # lazy: only needed for PDFs
            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        if suffix in _MARKITDOWN_EXTS:
            from markitdown import MarkItDown  # lazy: only for rich documents
            return MarkItDown().convert(str(path)).text_content
        raw = path.read_bytes()
    except Exception as exc:  # corrupt PDF, permission error, converter failure
        logger.debug("folder corpus: cannot extract %s: %s", path, exc)
        return None
    if b"\x00" in raw[:8192]:  # NUL byte => binary file (images, archives, ...)
        return None
    return raw.decode("utf-8", errors="replace")


def chunk_text(rel: str, text: str, chunk_chars: int, overlap_lines: int) -> list[Chunk]:
    """Split ``text`` into line-aligned chunks of roughly ``chunk_chars`` characters."""
    lines = text.splitlines()
    chunks: list[Chunk] = []
    buf: list[tuple[int, str]] = []  # (line_no, line_text)
    size = 0

    def flush() -> None:
        if buf:
            chunks.append(Chunk(rel, buf[0][0], buf[-1][0], "\n".join(t for _, t in buf)))

    for no, line in enumerate(lines, start=1):
        # Hard-split pathological long lines so one line never exceeds a chunk.
        pieces = [line[i:i + chunk_chars] for i in range(0, len(line), chunk_chars)] or [""]
        for piece in pieces:
            if size + len(piece) > chunk_chars and buf:
                flush()
                buf = buf[-overlap_lines:] if overlap_lines > 0 else []
                size = sum(len(t) + 1 for _, t in buf)
            buf.append((no, piece))
            size += len(piece) + 1
    flush()
    # Drop chunks that are pure whitespace (blank files / trailing blank lines).
    return [c for c in chunks if c.text.strip()]


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class FolderCorpusRetriever(CorpusRetriever):
    """BM25 retriever over a directory; see module docstring."""

    def __init__(
        self,
        root: str,
        *,
        index_dir: str = "",
        chunk_chars: int = 1500,
        overlap_lines: int = 2,
        max_file_bytes: int = 5 * 1024 * 1024,
        skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
    ) -> None:
        self.root = Path(root).resolve()
        self.index_dir = Path(index_dir) if index_dir else Path(tempfile.gettempdir()) / "arcticswarm_folder_index"
        self.chunk_chars = chunk_chars
        self.overlap_lines = overlap_lines
        self.max_file_bytes = max_file_bytes
        self.skip_dirs = skip_dirs
        # Extra absolute roots that alias the corpus root (set by the factory to the
        # sandbox mount, so docids copied from bash output still resolve).
        self.alias_roots: list[str] = []
        self._lock = threading.Lock()
        self._built = False
        self.chunks: list[Chunk] = []
        self.files: dict[str, int] = {}  # relpath -> number of chunks
        self._bm25: Any = None
        self._stemmer: Any = None

    # -- index lifecycle ----------------------------------------------------

    def _fingerprint(self, files: list[Path]) -> str:
        """Hash of root + per-file (relpath, size, mtime) + chunking params."""
        h = hashlib.sha1()
        h.update(f"{self.root}|{self.chunk_chars}|{self.overlap_lines}|v1".encode())
        for p in files:
            st = p.stat()
            h.update(f"{p.relative_to(self.root).as_posix()}|{st.st_size}|{int(st.st_mtime)}".encode())
        return h.hexdigest()[:16]

    def _tokenize(self, texts: list[str]) -> Any:
        import bm25s  # lazy: optional 'folder' extra
        return bm25s.tokenize(
            [normalize_for_bm25(t) for t in texts],
            stopwords="en",
            stemmer=self._stemmer,
            show_progress=False,
        )

    def ensure_built(self) -> None:
        """Load the persisted index for the current folder state, or build it."""
        if self._built:
            return
        with self._lock:
            if self._built:
                return
            import bm25s
            try:
                import Stemmer  # PyStemmer (optional): Snowball English stemmer
                self._stemmer = Stemmer.Stemmer("english")
            except ImportError:
                self._stemmer = None
            files = iter_files(self.root, self.skip_dirs, self.max_file_bytes)
            fp = self._fingerprint(files)
            cache = self.index_dir / fp
            meta_path = cache / "chunks.jsonl"
            if meta_path.exists():
                with meta_path.open(encoding="utf-8") as fh:
                    self.chunks = [Chunk(**json.loads(line)) for line in fh]
                self._bm25 = bm25s.BM25.load(str(cache / "bm25"))
                logger.info("folder corpus: loaded index %s (%d chunks)", cache, len(self.chunks))
            else:
                chunks: list[Chunk] = []
                for p in files:
                    text = extract_text(p)
                    if text is None:
                        continue
                    rel = p.relative_to(self.root).as_posix()
                    chunks.extend(chunk_text(rel, text, self.chunk_chars, self.overlap_lines))
                self.chunks = chunks
                # Index the path with the content so queries naming files/modules hit.
                corpus_tokens = self._tokenize([f"{c.path}\n{c.text}" for c in chunks])
                self._bm25 = bm25s.BM25()
                self._bm25.index(corpus_tokens, show_progress=False)
                cache.mkdir(parents=True, exist_ok=True)
                self._bm25.save(str(cache / "bm25"))
                tmp = meta_path.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    for c in chunks:
                        fh.write(json.dumps(c.__dict__, ensure_ascii=False) + "\n")
                tmp.replace(meta_path)  # atomic: a half-written index is never loaded
                logger.info("folder corpus: built index %s (%d files, %d chunks)", cache, len(files), len(chunks))
            self.files = {}
            for c in self.chunks:
                self.files[c.path] = self.files.get(c.path, 0) + 1
            self._built = True

    # -- retrieval ------------------------------------------------------------

    def rank(self, query: str, count: int) -> list[tuple[float, Chunk]]:
        """Top-``count`` (score, chunk) pairs by BM25; zero-score hits dropped."""
        self.ensure_built()
        if not self.chunks or not query.strip():
            return []
        k = max(1, min(count, len(self.chunks)))
        q_tokens = self._tokenize([query])
        try:
            docs, scores = self._bm25.retrieve(q_tokens, k=k, show_progress=False)
        except Exception as exc:  # e.g. query made only of stopwords / unseen tokens
            logger.debug("folder corpus: retrieve failed for %r: %s", query, exc)
            return []
        out = [(float(s), self.chunks[int(i)]) for i, s in zip(docs[0], scores[0]) if float(s) > 0]
        return out

    def search(self, query: str, count: int) -> list[dict[str, Any]] | None:
        ranked = self.rank(query, count)
        if not ranked:
            return None
        top = ranked[0][0] or 1.0
        return [{
            "title": f"{c.path} (lines {c.start}-{c.end})",
            "url": URL_PREFIX + c.docid,
            "docid": c.docid,
            "path": c.path,
            "description": c.text,
            # Field name kept for formatter compatibility; value is BM25 / top-score.
            "cosine_similarity": round(score / top, 4),
            "bm25": round(score, 4),
            "reranker_score": 0.0,
            "text_match": round(score, 4),
        } for score, c in ranked]

    def parse_docid(self, ref: str) -> tuple[str, int | None, int | None] | None:
        """Map a docid / path / corpus:// URL to (relpath, start, end) if it names a file."""
        self.ensure_built()
        ref = ref.strip().strip("`'\"")
        if ref.startswith(URL_PREFIX):
            ref = ref[len(URL_PREFIX):]
        m = _DOCID_RE.match(ref)
        if not m:
            return None
        path = m.group("path").replace("\\", "/")
        # Accept absolute paths on the host or inside the exec sandbox
        # (e.g. $CORPUS_ROOT=/corpus/<name>), and "./"-prefixed relative paths.
        for prefix in [str(self.root).replace("\\", "/"), *self.alias_roots]:
            prefix = prefix.rstrip("/")
            if prefix and path.lower().startswith(prefix.lower() + "/"):
                path = path[len(prefix) + 1:]
                break
        while path.startswith("./"):
            path = path[2:]
        if path not in self.files:
            return None
        start = int(m.group("start")) if m.group("start") else None
        end = int(m.group("end")) if m.group("end") else start
        return path, start, end

    def read(self, path: str, start: int | None = None, end: int | None = None) -> str:
        """Full text of a corpus file (optionally a 1-based inclusive line range)."""
        text = extract_text(self.root / path) or ""
        if start is None:
            return text
        lines = text.splitlines()
        lo = max(1, start)
        hi = min(len(lines), end if end is not None else lo)
        return "\n".join(f"{n}: {lines[n - 1]}" for n in range(lo, hi + 1))

    def fetch(self, query: str, count: int) -> list[dict[str, Any]] | None:
        """Full document(s): exact lookup when ``query`` names a file, else BM25 files."""
        parsed = self.parse_docid(query)
        if parsed is not None:
            path, start, end = parsed
            span = f"#L{start}-{end}" if start is not None else ""
            return [{
                "title": path + (f" (lines {start}-{end})" if start is not None else ""),
                "url": URL_PREFIX + path + span,
                "docid": path + span,
                "text": self.read(path, start, end),
                "cosine_similarity": 1.0,
                "reranker_score": 0.0,
            }]
        # Query fallback: rank chunks, return the full text of the best distinct files.
        seen: list[str] = []
        for _, c in self.rank(query, max(count * 10, 20)):
            if c.path not in seen:
                seen.append(c.path)
            if len(seen) >= count:
                break
        if not seen:
            return None
        return [{
            "title": p, "url": URL_PREFIX + p, "docid": p,
            "text": self.read(p), "cosine_similarity": 0.0, "reranker_score": 0.0,
        } for p in seen]


# ---------------------------------------------------------------------------
# Process-wide memo (one index per folder per process)
# ---------------------------------------------------------------------------

_MEMO: dict[tuple[str, str, int, int], FolderCorpusRetriever] = {}
_MEMO_LOCK = threading.Lock()


def get_folder_retriever(root: str, *, index_dir: str = "", chunk_chars: int = 1500,
                         overlap_lines: int = 2, max_file_bytes: int = 5 * 1024 * 1024) -> FolderCorpusRetriever:
    """Return the shared retriever for ``root`` (built lazily on first query)."""
    key = (str(Path(root).resolve()), index_dir, chunk_chars, overlap_lines)
    with _MEMO_LOCK:
        r = _MEMO.get(key)
        if r is None:
            r = FolderCorpusRetriever(root, index_dir=index_dir, chunk_chars=chunk_chars,
                                      overlap_lines=overlap_lines, max_file_bytes=max_file_bytes)
            _MEMO[key] = r
        return r
