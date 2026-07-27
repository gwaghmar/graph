---
description: Execute a task through a cached, validated dependency graph with localized retries and a live report
argument-hint: <task> [--resume] [--commit]
---

Execute `$ARGUMENTS` with Graph.

Fast path: if the task is trivial (single file, no design decisions, no cross-cutting risk), use one implementation node plus `quality`; skip planner, plan-review, and reviewer agents. The full protocol below is for multi-step work.

Protocol:
1. If `--resume` is present, run `python3 .graph/graph.py resume` and continue that incomplete run. Otherwise initialize with `python3 .graph/graph.py init "$ARGUMENTS" --host claude`.
2. Inspect the repository and create planner, plan-review, implementation, validation, and synthesis nodes. Record every transition with `.graph/graph.py node`. The HTML report updates after every transition.
3. Before executing a node, query the local cache with `.graph/graph.py cache-get <run> <node> --files <relevant-files>`. Reuse a hit. Do not call an agent for a valid cached result.
4. Use `graph-planner`, then critique the plan. Delegate only independent, non-overlapping work.
5. Run local checks before reviewer agents with `.graph/graph.py quality <run>`. If deterministic checks prove the result, skip redundant review calls.
6. On failure, run `.graph/graph.py retry-plan <run> --include-dependents`. Retry only failed nodes and their dependents, maximum two retries per node.
7. Cache successful reusable nodes using `.graph/graph.py cache-put`.
8. Record exact token usage only when exposed. Never invent it.
9. Finish with `.graph/graph.py finish <run> --status complete`. Report `.graph/runs/<run>/graph.html`.
10. Commit only when the user explicitly includes `--commit`; then use `.graph/graph.py commit <run> --yes`.

Do not destroy user changes, push, deploy, or open a PR without explicit permission.
