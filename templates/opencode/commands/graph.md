---
description: Execute a cached graph workflow with local checks, selective retries, resume, and an interactive report
agent: build
subtask: false
---
Execute `$ARGUMENTS` using Graph. For trivial single-file tasks, use one implementation node plus quality checks and skip planner/reviewer agents. Initialize `.graph/graph.py init`, or use `.graph/graph.py resume` for `--resume`. Record all nodes and states. Check the local cache before model calls. Run `.graph/graph.py quality` before reviewer agents. Retry only failures returned by `.graph/graph.py retry-plan --include-dependents`, maximum twice. Cache successful reusable nodes. Finish to generate the interactive HTML report. Commit only when explicitly requested with `--commit`.
