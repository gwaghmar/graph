---
name: graph
description: Plan and run a multi-step coding task as a checked dependency graph. The runtime enforces order, retry limits and quality checks, and records every run. Use when the user says "graph", "/graph" or "$graph", or gives a task that touches several files or needs a plan. Skip it for one-line or trivial changes.
license: MIT
metadata:
  version: "1.0.0"
---

# Graph

Graph is a plan gate and a run recorder. You write a plan. The runtime checks it and refuses rule breaks. Obey every refusal.

## Find the runtime

The scripts are in the `scripts/` folder next to this SKILL.md.
On Claude Code, use `${CLAUDE_SKILL_DIR}/scripts/graph.py`.
On other hosts, use the path of this skill folder plus `scripts/graph.py`.
Run it with `python3`. On Windows use `py -3` or `python` if `python3` is missing.
Below, `G` means that command.

## Protocol

1. Size the task. One-line or trivial tasks skip the graph. Just do them.
2. Run `G preflight --needs <comma list>` for what the task needs (for example `git,node,deploy`). Read the summary it prints.
3. Use the context files your host already loaded. Do not read them again. On Claude Code, CLAUDE.md is already in context. In Cursor, read rules with `alwaysApply: true`. Ask the user at most 3 questions, in one message, and only what preflight and those files cannot answer.
4. Write the plan. Run `G init "<task>" --host <host>`. It prints a run id. Add each node with `G node <run> <id> --role <role> --status pending --depends-on <ids>`. Then run `G validate <run>`. Fix the plan until it passes.
5. Loop until done:
   - Run `G next <run>`. It lists nodes that are ready.
   - Start each ready node with `G node <run> <id> --role <role> --status running`. Start them in parallel when the host has subagents.
   - When work ends, mark the node `complete` or `failed` the same way.
   - Give a node `--check "<command>"` to gate it. On `--status complete` the runtime runs the check itself. If it fails, the node becomes `failed` and the runtime refuses (exit 3).
   - To retry a failed node, run `G brief <run> <node>` and hand its output to a NEW subagent. Do not retry in the old context.
   - For a node with `--target web|phone|desktop|api|data` and `--risk low|high`, run `G verify-plan <run> <node>`. Do the steps it lists, cheapest first. For high risk, escalate one step after step 1 passes.
   - If the runtime refuses, read the line. Do what it says. Do not work around it.
6. Run `G quality <run>`. Fix failures. Then run `G finish <run> --status complete`. If you cannot finish, use `--status failed`.

On Claude Code with the Workflow tool, prefer `G export-workflow <run>` after `validate`. It writes `.claude/workflows/graph-<run>.js`, one agent per node, run level by level. Each agent calls `G node` itself, so the gates still apply. Run that script with the Workflow tool instead of steps 5 and 6.

Show the final summary to the user.

## Rules

- Never push, deploy or open a PR unless the user asks.
- Retries are capped. After the cap, stop and ask the user.
- Resume an old run with `G resume`.

More detail: `references/protocol.md`.
