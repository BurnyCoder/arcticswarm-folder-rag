# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the local-folder corpus backend (web.corpus_backend: folder).

Covers chunking (line spans, overlap, long-line splitting), identifier
normalisation, BM25 search over a tiny folder, docid/path parsing (relative,
corpus:// URL, line spans, sandbox alias root, unknown files), the CorpusFetchTool
paging protocol, index persistence/reload, and config gating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("bm25s")

from arcticswarm.tools.corpus_search import CorpusFetchTool, CorpusSearchTool  # noqa: E402
from arcticswarm.tools.folder_corpus import (  # noqa: E402
    FolderCorpusRetriever,
    chunk_text,
    normalize_for_bm25,
)


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    (root / "news" / "verge").mkdir(parents=True)
    (root / "src").mkdir()
    (root / ".git").mkdir()
    (root / "news" / "verge" / "2023-10-01_ftx.md").write_text(
        "# FTX trial\nSam Bankman-Fried faces seven counts of fraud.\nThe trial began Tuesday.\n",
        encoding="utf-8")
    (root / "src" / "loader.py").write_text(
        "import csv\n\n\ndef set_limit():\n    csv.field_size_limit(2**31 - 1)\n" + "\n".join(f"x{i} = {i}" for i in range(300)),
        encoding="utf-8")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x89PNG\x00\x00\x00binary")
    return root


def make(folder: Path, tmp_path: Path) -> FolderCorpusRetriever:
    return FolderCorpusRetriever(str(folder), index_dir=str(tmp_path / "idx"), chunk_chars=200)


def test_normalize_splits_identifiers():
    assert normalize_for_bm25("csv.field_size_limit fieldSizeLimit a/b-c") == "csv field size limit field Size Limit a b c"


def test_chunk_text_line_spans_and_overlap():
    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(1, 21))
    chunks = chunk_text("f.txt", text, chunk_chars=200, overlap_lines=1)
    assert chunks[0].start == 1
    assert chunks[-1].end == 20
    # overlap: each chunk starts on the previous chunk's last line
    for a, b in zip(chunks, chunks[1:]):
        assert b.start == a.end
    assert all(c.docid.startswith("f.txt#L") for c in chunks)


def test_chunk_text_splits_long_lines():
    chunks = chunk_text("min.js", "y" * 1000, chunk_chars=300, overlap_lines=0)
    assert len(chunks) == 4 and all(c.start == c.end == 1 for c in chunks)


def test_search_skips_vcs_and_binary_and_ranks(folder: Path, tmp_path: Path):
    r = make(folder, tmp_path)
    hits = r.search("Bankman-Fried fraud trial", 5)
    assert hits and hits[0]["path"] == "news/verge/2023-10-01_ftx.md"
    assert hits[0]["url"].startswith("corpus://folder/news/verge/2023-10-01_ftx.md#L")
    assert ".git/HEAD" not in r.files and "image.bin" not in r.files
    # identifier query matches snake_case code
    code = r.search("field size limit", 3)
    assert code[0]["path"] == "src/loader.py"


def test_parse_docid_variants(folder: Path, tmp_path: Path):
    r = make(folder, tmp_path)
    r.alias_roots.append("/corpus/test")
    assert r.parse_docid("src/loader.py") == ("src/loader.py", None, None)
    assert r.parse_docid("./src/loader.py#L4-5") == ("src/loader.py", 4, 5)
    assert r.parse_docid("corpus://folder/src/loader.py#L4") == ("src/loader.py", 4, 4)
    assert r.parse_docid("/corpus/test/src/loader.py") == ("src/loader.py", None, None)
    assert r.parse_docid(str(folder / "src" / "loader.py")) == ("src/loader.py", None, None)
    assert r.parse_docid("does/not/exist.py") is None


def test_read_line_range_numbers_lines(folder: Path, tmp_path: Path):
    r = make(folder, tmp_path)
    out = r.read("src/loader.py", 4, 5)
    assert out.splitlines() == ["4: def set_limit():", "5:     csv.field_size_limit(2**31 - 1)"]


def test_fetch_tool_pages_long_files(folder: Path, tmp_path: Path):
    tool = CorpusFetchTool(retriever=make(folder, tmp_path), description=CorpusFetchTool.FOLDER_DESCRIPTION,
                           page_chars=500)
    first = tool.execute(docid="src/loader.py")
    assert not first.is_error and "chars 0-500 of" in first.output and "offset=500" in first.output
    second = tool.execute(docid="src/loader.py", offset=500)
    assert "chars 500-1000 of" in second.output
    missing = tool.execute(docid="nope.md")
    assert "No file matches" in missing.output
    assert "docid" in tool.parameters_schema()["properties"]


def test_search_tool_prints_citable_ids(folder: Path, tmp_path: Path):
    tool = CorpusSearchTool(retriever=make(folder, tmp_path), description=CorpusSearchTool.FOLDER_DESCRIPTION)
    out = tool.execute(query="seven counts of fraud").output
    assert "URL: corpus://folder/news/verge/2023-10-01_ftx.md#L" in out
    assert tool.description.startswith("BM25 keyword search over the LOCAL FOLDER")


def test_index_persists_and_reloads(folder: Path, tmp_path: Path):
    r1 = make(folder, tmp_path)
    r1.ensure_built()
    r2 = make(folder, tmp_path)
    r2.ensure_built()
    assert len(r2.chunks) == len(r1.chunks) > 0
    assert len(list((tmp_path / "idx").iterdir())) == 1  # same fingerprint -> one index dir


def test_config_gates_folder_backend(folder: Path):
    from arcticswarm.config import ArcticswarmConfig

    cfg = ArcticswarmConfig()
    cfg.web_search_enabled = True
    cfg.web_search_provider = "corpus"
    cfg.corpus_backend = "folder"
    cfg.corpus_local_path = str(folder)
    assert cfg.has_web_search_capability()
    cfg.corpus_local_path = str(folder / "missing")
    assert not cfg.has_web_search_capability()
