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

"""Corpus search/fetch tools (BrowseComp-Plus corpus path).

Backend-agnostic ``web_search`` / ``web_fetch`` tools that delegate the actual
retrieval to a pluggable :class:`~arcticswarm.tools.corpus_retriever.CorpusRetriever`
(stub / cortex / local — selected by ``web.corpus_backend``). The tool layer
owns the LLM-facing schema, source scoring, result formatting, and the stats
duck-typing that the agent loop relies on; the retriever owns only the
"go fetch documents" step. This keeps the Snowflake Cortex Search call
isolated behind the retriever interface so the harness is open-sourceable.
"""

from __future__ import annotations

import logging
import time
from typing import Any, TYPE_CHECKING

from arcticswarm.tools.base import BaseTool, ToolResult
from arcticswarm.tools.corpus_retriever import CorpusRetriever

if TYPE_CHECKING:
    from arcticswarm.tools.source_scorer import SourceScorer

logger = logging.getLogger(__name__)


class CorpusSearchTool(BaseTool):
    """Search a curated document corpus (chunked snippets). Tool name: ``web_search``."""

    name = "web_search"
    description = (
        "Search a curated document corpus. "
        "Returns text chunks with relevance scores. "
        "Use this to find information relevant to the research question."
    )

    FOLDER_DESCRIPTION = (
        "BM25 keyword search over the LOCAL FOLDER being researched (not the web). "
        "Files are split into line-aligned chunks; each hit shows the file path, its "
        "line span and a citable id 'corpus://folder/<path>#L<start>-<end>'. Use "
        "distinctive keywords (names, identifiers, numbers, quoted phrases). For exact "
        "strings, regexes or structured analysis use the bash / python_execute tools."
    )

    def __init__(
        self,
        *,
        retriever: CorpusRetriever,
        judge: "SourceScorer | None" = None,
        description: str = "",
    ) -> None:
        self._retriever = retriever
        self._judge = judge
        if description:  # instance override (folder backend)
            self.description = description

        # Stats (duck-type compat with WebSearchTool)
        self._total_searches = 0
        self._chunked_searches = 0
        self._fallback_log: list[dict[str, Any]] = []
        self._search_log: list[dict[str, Any]] = []

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query text."},
                "count": {
                    "type": "integer",
                    "description": "Number of results to return (1-100). Default: 10.",
                },
            },
            "required": ["query"],
        }

    def execute(
        self,
        *,
        query: str,
        count: int = 10,
        columns: list[str] | None = None,
        filter: dict[str, Any] | None = None,
        **_: Any,
    ) -> ToolResult:
        q = (query or "").strip()
        if not q:
            return ToolResult(error="Missing required parameter: query", is_error=True)
        if len(q) > 2000:
            return ToolResult(
                error=f"Query too long ({len(q)} chars). Max 2000 characters.",
                is_error=True,
            )

        capped = max(1, min(int(count or 10), 100))

        try:
            results = self._retriever.search(q, capped)
        except Exception as exc:
            logger.warning("Corpus search backend error: %s", exc)
            self._fallback_log.append({"query": q, "error": str(exc)})
            results = None

        if results:
            scores = self._score_results(q, results)
            self._total_searches += 1
            self._chunked_searches += 1
            self._record_search(q, "corpus_chunked", len(results), scores)
            return self._format_results(results, q, capped, source="corpus_chunked", scores=scores)

        self._total_searches += 1
        self._record_search(q, "corpus_empty", 0)
        return ToolResult(
            output=(
                f"No results for: {q}\n\n"
                "Suggestions: Try rephrasing with different keywords, "
                "use broader terms, or break the query into simpler parts."
            ),
        )

    def _score_results(
        self, query: str, results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self._judge:
            return []
        try:
            sources = [
                {
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "content": r.get("description", ""),
                }
                for r in results
            ]
            scored, _ = self._judge.evaluate(query, sources)
            return scored
        except Exception as exc:
            logger.warning("Corpus search result scoring failed: %s", exc)
            return []

    @staticmethod
    def _format_results(
        results: list[dict[str, Any]],
        query: str,
        count: int,
        source: str = "corpus_chunked",
        scores: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        score_by_idx: dict[int, dict[str, Any]] = {}
        if scores:
            for s in scores:
                score_by_idx[s["index"]] = s

        lines: list[str] = [f"Top {min(len(results), count)} result(s) for: {query}", ""]
        for i, r in enumerate(results[:count], start=1):
            desc = str(r.get("description") or "").strip()
            if not desc:
                continue
            cosine = r.get("cosine_similarity", 0.0)
            preview = desc[:2000] + "..." if len(desc) > 2000 else desc
            if r.get("docid"):
                # Folder backend: show the file + line span and a citable URL line
                # (the "URL:" line also feeds WebSourceTracker -> report references).
                lines.append(f"{i}. {r.get('title', r['docid'])}  [bm25={r.get('bm25', 0.0):.2f}]")
                lines.append(f"   URL: {r.get('url', '')}")
                lines.append(preview)
            else:
                lines.append(f"{i}. (cosine={cosine:.2f}) {preview}")
            sc = score_by_idx.get(i - 1)
            if sc:
                from arcticswarm.tools.source_scorer import SourceScorer
                lines.append(f"   {SourceScorer.format_annotation(sc).strip()}")
            lines.append("")

        return ToolResult(
            output="\n".join(lines).rstrip(),
            metadata={"search_source": source},
        )

    def _record_search(
        self,
        query: str,
        source: str,
        result_count: int,
        scores: list[dict[str, Any]] | None = None,
    ) -> None:
        from arcticswarm.logging_utils import summarize_search_scores

        entry: dict[str, Any] = {
            "query": query,
            "source": source,
            "result_count": result_count,
        }
        score_summary = summarize_search_scores(scores)
        if score_summary:
            entry["scores"] = score_summary
        self._search_log.append(entry)

    def log_and_reset_stats(self) -> None:
        if self._total_searches > 0:
            logger.info(
                "Corpus search stats: %d total (chunked=%d)",
                self._total_searches, self._chunked_searches,
            )
        self._total_searches = 0
        self._chunked_searches = 0

    def drain_fallback_log(self) -> list[dict[str, Any]]:
        log = list(self._fallback_log)
        self._fallback_log.clear()
        return log

    def drain_search_log(self) -> list[dict[str, Any]]:
        log = list(self._search_log)
        self._search_log.clear()
        return log


class CorpusFetchTool(BaseTool):
    """Retrieve full document text from the corpus. Tool name: ``web_fetch``."""

    name = "web_fetch"
    description = (
        "Retrieve the full text of a document from the corpus. "
        "Pass a descriptive search query (NOT a URL) to find and return "
        "the complete document text. Use this when search snippets from "
        "web_search aren't sufficient and you need the full document."
    )

    FOLDER_DESCRIPTION = (
        "Read a file from the LOCAL FOLDER being researched (not the web). Pass "
        "`docid` = a relative path ('src/app.py'), a line span ('src/app.py#L10-80') "
        "or a 'corpus://folder/...' id copied from web_search results. Long files are "
        "paged: use `offset` (character offset) to continue where the previous call "
        "stopped. Without `docid`, `query` returns the best-matching whole file(s)."
    )

    def __init__(self, *, retriever: CorpusRetriever, description: str = "",
                 page_chars: int = 0) -> None:
        self._retriever = retriever
        self._total_fetches = 0
        self._total_failures = 0
        self._fetch_log: list[dict[str, Any]] = []
        # Folder backend: docid lookup + paging (page_chars = default page size).
        self._folder_mode = bool(description)
        self._page_chars = page_chars or 16000
        if description:
            self.description = description

    def parameters_schema(self) -> dict[str, Any]:
        if self._folder_mode:
            return {
                "type": "object",
                "properties": {
                    "docid": {
                        "type": "string",
                        "description": "Relative file path, optionally with #L<start>-<end>, or a corpus://folder/ id.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Fallback when no docid: keywords describing the file to read.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start from (paging through long files). Default 0.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": f"Characters to return (default {self._page_chars}).",
                    },
                },
            }
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A descriptive search query to find the full document. "
                        "This is NOT a URL — describe what document you want to read."
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": "Number of full documents to return (1-10). Default: 1.",
                },
            },
            "required": ["query"],
        }

    def execute(
        self,
        *,
        query: str | None = None,
        url: str | None = None,
        count: int = 1,
        docid: str | None = None,
        offset: int = 0,
        max_chars: int | None = None,
        **_: Any,
    ) -> ToolResult:
        if self._folder_mode and (docid or url):
            return self._fetch_page(str(docid or url), int(offset or 0), max_chars)
        if self._folder_mode and query:
            # A query (or a path passed as query) -> resolve to the best file, then page it.
            try:
                hits = self._retriever.fetch(query.strip(), 1)
            except Exception as exc:
                logger.warning("Corpus fetch backend error: %s", exc)
                hits = None
            if hits:
                return self._fetch_page(hits[0].get("docid", ""), int(offset or 0), max_chars)
        q = (query or url or "").strip()
        if not q:
            return ToolResult(
                error="Missing required parameter: query (describe what document you want to read)",
                is_error=True,
            )

        capped = max(1, min(int(count or 1), 10))

        t0 = time.monotonic()
        try:
            results = self._retriever.fetch(q, capped)
        except Exception as exc:
            logger.warning("Corpus fetch backend error: %s", exc)
            results = None
        latency_ms = int((time.monotonic() - t0) * 1000)

        if not results:
            self._total_fetches += 1
            self._total_failures += 1
            self._fetch_log.append({"query": q, "success": False, "latency_ms": latency_ms})
            return ToolResult(
                output=(
                    f"No documents found for: {q}\n\n"
                    "Suggestions: Try a different query or use broader terms."
                ),
            )

        self._total_fetches += 1
        self._fetch_log.append({
            "query": q, "success": True, "latency_ms": latency_ms,
            "result_count": len(results),
            "total_chars": sum(len(r.get("text", "")) for r in results),
        })
        return self._format_results(results, q)

    def _fetch_page(self, ref: str, offset: int, max_chars: int | None) -> ToolResult:
        """Folder backend: exact file / line-span lookup with character paging."""
        t0 = time.monotonic()
        try:
            results = self._retriever.fetch(ref, 1)
        except Exception as exc:
            logger.warning("Corpus fetch backend error: %s", exc)
            results = None
        exact = bool(results) and results[0].get("cosine_similarity", 0.0) == 1.0
        self._total_fetches += 1
        if not results or not exact:
            self._total_failures += 1
            self._fetch_log.append({"query": ref, "success": False,
                                    "latency_ms": int((time.monotonic() - t0) * 1000)})
            return ToolResult(output=(
                f"No file matches docid {ref!r}. Use a path relative to the folder root "
                "exactly as shown by web_search or `ls`/`find` (e.g. 'docs/a.md' or "
                "'docs/a.md#L10-40')."
            ))
        r = results[0]
        text = r.get("text", "")
        size = max(1, int(max_chars or self._page_chars))
        start = max(0, min(offset, len(text)))
        page = text[start:start + size]
        end = start + len(page)
        header = f"File: {r.get('title', ref)}  |  {r.get('url', '')}  |  chars {start}-{end} of {len(text)}"
        footer = (f"\n[... {len(text) - end} more chars: call web_fetch again with docid={r.get('docid', ref)!r}, offset={end}]"
                  if end < len(text) else "")
        self._fetch_log.append({"query": ref, "success": True, "result_count": 1,
                                "latency_ms": int((time.monotonic() - t0) * 1000),
                                "total_chars": len(page)})
        return ToolResult(output=f"{header}\n\n{page}{footer}",
                          metadata={"fetch_source": "folder_file", "docid": r.get("docid", ref)})

    @staticmethod
    def _format_results(results: list[dict[str, Any]], query: str) -> ToolResult:
        parts: list[str] = []
        for i, r in enumerate(results, start=1):
            text = r.get("text", "")
            cosine = r.get("cosine_similarity", 0.0)
            chars = len(text)
            if len(results) > 1:
                parts.append(f"--- Document {i} ({chars} chars, cosine={cosine:.2f}) ---")
            else:
                parts.append(f"Full document ({chars} chars, cosine={cosine:.2f}):")
            parts.append("")
            parts.append(text)
            parts.append("")
        return ToolResult(
            output="\n".join(parts).rstrip(),
            metadata={"fetch_source": "corpus_nonchunked"},
        )

    def log_and_reset_stats(self) -> None:
        if self._total_fetches > 0:
            success = self._total_fetches - self._total_failures
            logger.info(
                "Corpus fetch stats: %d total, %d success, %d failed",
                self._total_fetches, success, self._total_failures,
            )
        self._total_fetches = 0
        self._total_failures = 0

    def drain_fetch_log(self) -> list[dict[str, Any]]:
        log = list(self._fetch_log)
        self._fetch_log.clear()
        return log
