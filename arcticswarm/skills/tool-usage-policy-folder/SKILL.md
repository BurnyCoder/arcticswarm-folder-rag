---
name: tool-usage-policy-folder
description: >
  Tool usage guidelines for agents researching a local folder: BM25 search,
  file reads with paging, and sandboxed bash / python_execute (root, can
  install any apt or PyPI package). Covers output-size discipline and
  sharing findings with file citations.
---

# Tool Usage Policy (Local Folder)

## Tools

1. **`web_search(query, count)`** — BM25 over the folder's chunks. Each hit:
   `path (lines a-b)`, `URL: corpus://folder/<path>#La-b`, chunk text.
2. **`web_fetch(docid, offset, max_chars)`** — read a file or a line span by
   relative path (`docs/a.md`, `src/x.py#L10-80`) or a `corpus://folder/` id.
   Long files are paged: continue with the `offset` printed at the end.
3. **`bash(command)`** — Linux sandbox, you are root. The folder is at
   `$CORPUS_ROOT`; start commands with `cd "$CORPUS_ROOT" && ...` so printed
   paths are relative (= citable). Available: rg, grep, find, fd, awk, sed,
   jq, pdftotext, pandoc, sqlite3, tree, python. Install anything else with
   `apt-get install -y <pkg>` or `pip install <pkg>`.
4. **`python_execute(code, packages)`** — Python 3 in the agent venv
   (pandas, numpy, pypdf, markitdown, bm25s, scikit-learn preinstalled; add any
   PyPI package via `packages`). Read files from `os.environ["CORPUS_ROOT"]`.

## Output discipline (outputs are truncated)

- Never `cat` whole large files or print whole dataframes. Use `rg -n ... |
  head -50`, `wc -l`, `rg -c`, `sed -n '120,180p' file`, or print summaries.
- Prefer `rg -n -i -m 5` (max matches per file) and `-g '*.md'` globs to
  narrow results.
- Run independent tool calls in parallel in one response.

## Safety and scope

- Treat `$CORPUS_ROOT` as read-only evidence; write scratch files only in the
  current working directory (your scratch dir).
- No network access except package installation. Do not fetch web pages.

## Sharing results (swarm mode)

- Post findings to `#key-findings` / `#discoveries` with the exact quoted
  value and its `corpus://folder/<path>#L<a>-<b>` citation.
- Say which search methods you used (BM25 queries, rg patterns, code) so
  reviewers can reproduce or extend the search.
