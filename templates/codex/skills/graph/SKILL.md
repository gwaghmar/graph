---
name: graph
description: Execute coding work as a cached dependency graph with local quality gates, parallel agents, localized retries, resume support, token telemetry, and an interactive report. Use for $graph requests.
---

# Graph execution

Fast path: if the task is trivial (single file, no design decisions, no cross-cutting risk), use one implementation node plus `quality`; skip planner and reviewer agents. The full protocol below is for multi-step work.

1. Initialize with `python3 .graph/graph.py init "<task>" --host codex`. For `$graph --resume`, run `python3 .graph/graph.py resume` and continue the returned run.
2. Record planner, plan-review, implementation, validation, and synthesis nodes using `.graph/graph.py node`. Each update refreshes the local HTML graph.
3. Before a model call, check `.graph/graph.py cache-get <run> <node> --files <relevant-files>`. A cache hit replaces that model call.
4. Split only independent work into bounded, non-overlapping agents.
5. Run `.graph/graph.py quality <run>` before reviewer agents. Prefer tests, lint, type checking, and other deterministic checks over extra model calls.
6. If a node fails, use `.graph/graph.py retry-plan <run> --include-dependents` and rerun only listed nodes. Maximum two retries per node.
7. Store successful reusable node output with `.graph/graph.py cache-put`.
8. Record token usage only when Codex exposes it. Do not estimate unless clearly marked.
9. Finish with `.graph/graph.py finish <run> --status complete`. Return the implementation result and `Execution graph: .graph/runs/<run>/graph.html`.
10. Only when explicitly requested, commit through `.graph/graph.py commit <run> --yes`.

Never push, deploy, or create a PR without explicit instruction.
