---
name: folder-research
description: >
  Research methodology for answering questions from a LOCAL FOLDER of files
  (document collections or code repositories) with no web access. Covers the
  iterative search loop (BM25 search, file reads, grep/regex and code over the
  folder), evidence and citation rules, completion criteria, and how to answer
  when the folder does not contain the information.
---

# Folder Research Skill

## Mission

Answer the question using ONLY evidence found in the local folder at
`$CORPUS_ROOT`. Every factual claim must be traceable to a file and line span.
There is no internet: `web_search` and `web_fetch` operate on the folder.

## Search strategies — combine them

1. **Concept search (`web_search`)**: BM25 over line-aligned chunks. Use
   distinctive words: names, identifiers, numbers, titles, rare terms. Run
   several phrasings; BM25 matches words, not meaning.
2. **Exact / regex search (`bash`)**: `cd "$CORPUS_ROOT" && rg -n -i "pattern"`
   (or `grep -rn`). Use for exact identifiers, quoted phrases, dates, numbers,
   and for listing every occurrence. `rg -l` lists files; `rg -c` counts.
3. **Structure (`bash`)**: `find . -name '*.py' | head`, `tree -L 2`,
   `ls -R | head` to learn the layout (sources, dates, modules).
4. **Read in context (`web_fetch` or `sed -n 'a,bp' file`)**: always read the
   lines around a hit before relying on it; follow imports/definitions in code.
5. **Compute (`python_execute`)**: parse files, filter by metadata (author,
   date, source), count, compare, aggregate. Install any library you need via
   the `packages` argument or `pip install` in bash.

## Iterative workflow

Search → Read → Assess → Decide (continue or stop).

- **Round 1 — discovery**: identify which files are relevant (2–4 different
  queries / patterns), then read the best hits in full context.
- **After each round, assess**: Which parts of the question are answered, with
  which file citations? What is still missing or contradictory?
- **Round 2+ — targeted gap filling**: exact-string searches for names/values
  found in round 1, metadata filters (e.g. publication date, news source,
  module path), and cross-checks in other files.

## Evidence and citation rules

- Cite every claim as `corpus://folder/<relative path>#L<start>-<end>` (copy the
  id from `web_search` results, or build it from `rg -n` line numbers).
- Quote exact values (names, numbers, dates, identifiers) from the file text.
- Questions often combine several files (e.g. "according to source A … and
  source B …"): find and cite EACH required file.
- For comparisons/temporal questions, extract the relevant fact (and date)
  from each file before comparing.

## Completion criteria

STOP only when every part of the question is answered with cited evidence
from the files, or when targeted searches show the information is absent.

If the folder does not contain the information after diverse searches
(BM25 with several phrasings + grep for key terms), say so explicitly
(e.g. "Insufficient information") — do NOT fill gaps from general knowledge.

## Answer format

If the question asks for a specific final-answer format (for example a final
line `FINAL ANSWER: ...`), follow it exactly in your final report/summary.
