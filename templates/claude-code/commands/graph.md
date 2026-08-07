---
description: Execute a task through a cached, validated dependency graph with localized retries and a live report
argument-hint: <task> [--resume] [--commit]
---

Execute `$ARGUMENTS` with Graph.

Sizing: before creating any node, estimate three signals from the request and a quick repo scan — F = files you expect to change, D = open design decisions (more than one reasonable approach), R = cross-cutting risk (touches auth, shared utilities, public APIs, or data migrations). Then pick the tier and do not exceed its agent budget:

| Tier | Signals | Graph shape | Agents |
|---|---|---|---|
| S | F ≤ 1, D = 0, R = no | 1 implementation node + `quality` | none — do the work inline |
| M | F 2–4 or D = 1, R = no | 2–3 implementation nodes + `quality` + review | plan inline; ≤ 1 reviewer agent; workers only for genuinely independent nodes |
| L | F ≥ 5, or D ≥ 2, or R = yes | full protocol: planner → plan-review → workers → quality → reviewer → synthesis | planner agent, 2–6 workers (one per independent file-scope cluster), reviewer |

Never spawn more workers than independent, non-overlapping file clusters — overlap means sequential, not parallel. When signals disagree, size up only for R; otherwise size down. State the chosen tier and F/D/R in one line before building the graph.

The full protocol below is for M and L tiers.

Protocol:
1. If `--resume` is present, run `python3 .graph/graph.py resume` and continue that incomplete run. Otherwise initialize with `python3 .graph/graph.py init "$ARGUMENTS" --host claude`.
2. Inspect the repository and create planner, plan-review, implementation, validation, and synthesis nodes. Record every transition with `.graph/graph.py node`. Each `node` call prints a live ASCII graph of the run and refreshes the HTML report — both are rendered locally and cost no model tokens. Do not re-describe the graph in prose; the printed tree is the progress display (`.graph/graph.py tree <run>` reprints it on demand). After recording the planned nodes, run `python3 .graph/graph.py validate <run>` — it exits non-zero on unknown, self, or cyclic dependencies; fix the plan before executing anything.
3. Before executing a node, query the local cache with `.graph/graph.py cache-get <run> <node> --files <relevant-files>`. Reuse a hit. Do not call an agent for a valid cached result.
4. Use `graph-planner`, then critique the plan. Delegate only independent, non-overlapping work.
5. Run local checks before reviewer agents with `.graph/graph.py quality <run>`. If deterministic checks prove the result, skip redundant review calls.
6. On failure, run `.graph/graph.py retry-plan <run> --include-dependents`. Retry only failed nodes and their dependents, maximum two retries per node.
7. Cache successful reusable nodes using `.graph/graph.py cache-put`.
8. Record exact token usage only when exposed. Never invent it.
9. Finish with `.graph/graph.py finish <run> --status complete`. It prints the final run summary (task, node graph, checks, files, retries, cache hits, duration, report path); include that summary verbatim in your reply instead of writing your own recap.
10. Commit only when the user explicitly includes `--commit`; then use `.graph/graph.py commit <run> --yes`.

Do not destroy user changes, push, deploy, or open a PR without explicit permission.
