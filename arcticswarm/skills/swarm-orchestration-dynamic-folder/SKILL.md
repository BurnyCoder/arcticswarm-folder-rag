---
name: swarm-orchestration-dynamic-folder
description: >
  Orchestrator workflow for coordinating on-demand subagents that research a
  LOCAL FOLDER of files (no web), via task board delegation with dynamic
  scaling. Adapted from swarm-orchestration-dynamic-web: web-specific heuristics
  (region/language pivots, anti-fame bias, "always exactly one answer") removed.
---

# Swarm Orchestration Skill (Dynamic Scaling, Local-Folder Research)

## Core Principles

1. **Comprehensive Information Gathering**: Collect evidence from every
   relevant file in the local folder (there is no web access).
2. **Document Everything**: Preserve detailed facts, evidence, reasoning
   steps, and intermediate findings.
3. **Flag Uncertainties**: Clearly note any conflicts, ambiguities, or
   alternative interpretations.
4. **Maximum Transparency**: Enable users to make informed decisions with
   complete information.

## Your Tools

### Planning Tools
- **read_bbs**: Read the shared Bulletin Board System.
- **post_to_bbs**: Post your own observations or instructions to the BBS.

### Orchestration Tools (delegate work)
- **create_task**: Create a task and assign it to a worker. The system
  spawns workers on demand. Use `assign_to` to reuse a specific worker's context.
  Use `isolated=true` for independent exploration tasks (see BBS Isolation below).
- **list_tasks**: Check the current status of all tasks.
- **wait_for_tasks**: Block until specific tasks finish.

### Reporting (deliver the final answer)
- **prepare_report** then **send_user_markdown_report**: The ONLY way to
  deliver your final answer.

**IMPORTANT**: You do NOT have task-execution tools. All execution MUST
be delegated via `create_task`.

## Dynamic Scaling

Workers are spawned **on demand** when you call `create_task`:
- The system auto-assigns an available worker or spawns a new one.
- An **auditor agent** is automatically spawned to verify findings.
- Workers stay alive after their task for idle review.
- Use `assign_to` from a previous task result to reuse worker context.

## Workflow

1. **Understand the question**. Break complex questions into sub-questions.
   Decompose by **constraint or angle**, not by subtopic.
2. **Post tasks to the board**. Use `create_task`.
3. **Wait for results**. Use `wait_for_tasks`. BBS updates arrive automatically.
4. **Review and verify** (MANDATORY). Check `read_bbs` especially **#discussion**.
   If the best candidate fails 1+ constraints, create a new task that
   searches **files, directories or search methods NOT yet used** (e.g. other
   news sources / date ranges / modules; exact-string `rg` instead of BM25;
   code that filters files by metadata).
5. **Verify before reporting** (MANDATORY).
6. **Prepare and deliver your report**. `prepare_report` then `send_user_markdown_report`.

## Rules

- You MUST delegate all execution to subagents.
- Subagents communicate via the shared BBS.
- You MUST call `prepare_report` before `send_user_markdown_report`.
- Use `[N]` inline citations. Do NOT write a `## References` section.
- Default to **parallel** tasks.

## Search Diversity (IMPORTANT for multi-task research)

When creating multiple browsing tasks for the same question, ensure
**search diversity**. Do NOT create tasks that all say "research X" — they
will all find the same results and converge on the same (possibly wrong)
answer.

Instead, decompose by **constraint or angle**:
- Task 1: Search for entities matching constraint A
- Task 2: Search for entities matching constraint B independently
- Task 3: Cross-reference — find the intersection of the above results
- Task 4: Use a different search METHOD (exact-string/regex search with bash,
  or Python that filters/aggregates files) to catch what BM25 missed

The goal is to **cast a wide net** so the team finds ALL plausible
candidates, not just the most obvious one. Obvious partial matches are
the #1 source of wrong answers.

### BBS Isolation (MANDATORY for initial exploration)

Use `isolated=true` when creating tasks to make subagents search
WITHOUT seeing other agents' findings. Isolated agents cannot read the
BBS — they search independently and only post their own discoveries.
This prevents premature convergence on a single (possibly wrong) candidate.

**When to use `isolated=true`:**
- First 2-3 exploration tasks (initial broad search from different angles)
- Tasks that need fresh, unbiased investigation of a specific constraint
- "Contrarian search" tasks that must find alternatives independently

**When to use `isolated=false` (default):**
- Verification tasks that need to check findings from earlier tasks
- Cross-referencing tasks that compare candidates
- Follow-up tasks that build on prior discoveries
- Any task with `depends_on` (it naturally needs prior context)

**Recommended pattern for identification questions:**
1. Create 2-3 `isolated=true` browsing tasks, each approaching from
   a different constraint angle
2. `wait_for_tasks` until those initial tasks complete
3. Review all independent findings via `read_bbs`
4. Create `isolated=false` verification/comparison tasks that can
   see all prior findings
5. If candidates disagree, create a targeted resolution task

### Candidate Breadth Rule (MANDATORY for identification questions)

When the question asks you to identify an entity (person, place, work, event):

1. **Create at least 2 INDEPENDENT search tasks with `isolated=true`** that
   approach the question from different constraint angles. Each task should
   be capable of finding the answer without the other.
2. **Do NOT create verification tasks until at least 2 search tasks have
   completed.** Premature verification causes fixation on the first candidate.
3. **If the reasoning tool suggests a candidate before searches complete,
   treat it as ONE hypothesis — not the answer.** Always wait for search
   results to confirm or contradict it.
4. **If all search tasks converge on the same candidate**, create one final
   "contrarian search" task with `isolated=true` that explicitly searches
   for ALTERNATIVES to that candidate. Only proceed to reporting after
   this returns.

### Constraint Verification Protocol (MANDATORY before reporting)

Before calling `prepare_report`, you MUST perform a structured constraint
verification on your best candidate(s):

1. **List every constraint** from the original question (dates, locations,
   attributes, relationships, quantities — every factual requirement).
2. **For each constraint**, cite the specific evidence that your candidate
   satisfies it. Use the format:
   - VERIFIED: Constraint "born before 1900" → Evidence: [source] confirms birth year 1872
   - UNVERIFIED: Constraint "won award X" → NOT VERIFIED — no source found
3. **If ANY constraint is unverified or contradicted**, do NOT report yet.
   Instead, create a targeted follow-up task to specifically verify or
   disprove that constraint.
4. **If you have 2+ candidates**, compare them constraint-by-constraint.
   Choose the one satisfying the MOST constraints with the strongest evidence.
5. **Never report an answer with 2+ unverified constraints.** If stuck,
   create a "contrarian search" task targeting the unverified constraints
   before reporting your best guess.

### Anti-Fixation Rule (CRITICAL)

After your first round of search tasks completes:
1. **Count distinct candidates** found across all task results and BBS posts.
2. **If only 1 candidate emerged**: You MUST create at least one additional
   "alternative search" task before reporting. This task must explicitly
   search for entities OTHER than the current candidate that could match
   the constraints. Include in the task prompt: "Do NOT search for
   [current candidate]. Find alternative entities matching: [key constraints]."
3. **If 2+ candidates emerged**: Compare them directly against all
   constraints before choosing. Do NOT default to the first one found.
4. **The folder may not contain the answer.** Conclude "Insufficient
   information" only after diverse searches (several BM25 phrasings, exact
   grep for key names/terms, and a check of the relevant files) all fail.
   Never fill gaps from general knowledge.

### Hard Constraint Violations = Mandatory Pivot (CRITICAL)

When reviewing candidates after search tasks complete:

1. **Distinguish HARD vs SOFT constraints.** Hard constraints are explicit
   factual requirements with specific values (dates, numbers, age differences,
   relationships like "7 years younger", geographic locations, specific titles).
   Soft constraints are interpretive (descriptions, characterizations).

2. **If a candidate VIOLATES any hard constraint** (e.g., the question says
   "7 years younger" but the candidate's sibling is 6 years older), you MUST:
   - Immediately flag it as DISQUALIFIED
   - Do NOT rationalize the violation ("data may be imprecise", "sources vary")
   - Create new search tasks targeting the violated constraint specifically
   - Search for entities that EXACTLY match the hard constraint values

3. **Never submit a candidate with a known hard constraint violation.** A
   candidate matching 5/6 constraints but violating 1 hard constraint is
   WRONG. A less-famous candidate matching 6/6 is correct.

### Best-So-Far Tracking (prevents abandoning correct answers)

Maintain a running tally of ALL candidates found during the investigation:

1. **Never discard a candidate entirely.** When a candidate partially matches
   constraints but you cannot verify all of them, keep it as "best so far"
   while searching for better alternatives.
2. **When pivoting to a new candidate**, explicitly compare it against the
   previous best: does the new candidate match MORE constraints? If not,
   keep the previous one.
3. **If verification tasks fail to find a better candidate**, revert to the
   best-so-far candidate — do NOT submit a less-verified alternative just
   because it was found more recently.
4. **In your final report**, if you considered multiple candidates, briefly
   note why you chose one over the others.

### Folder-Specific Task Design

- Tell subagents which tools to combine: `web_search` (BM25 concepts),
  `bash` (`cd "$CORPUS_ROOT" && rg -n ...` for exact strings, `find`/`tree`
  for structure), `python_execute` (parse/filter/count; may install packages),
  and `web_fetch` (read files/line spans by path).
- Multi-source questions ("according to A … and B …"): create one isolated
  task per named source/file so each required file is found independently.
- Code questions: split by component (definition site, call sites, tests,
  docs) rather than by phrasing.

### Final Answer Format

Every claim in the report cites `corpus://folder/<path>#L<a>-<b>`. If the user's
question asks for a specific final line (e.g. `FINAL ANSWER: ...`), the report
MUST end with exactly that line.
