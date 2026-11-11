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

## Start: read the user, pick tools, agree the plan

Do these in order. The runtime refuses to start any node (exit 3) until preflight, profile, choose and approve are recorded (or waived at init), and it lists what is missing. `context` and `validate` are not gated, so run them. If the plan changes after approval, show it again and approve again.

1. **Size the task.** One-line or trivial tasks skip the graph. Just do them. Otherwise run `G init "<task>" --host <host>` now. It prints a run id. Use `--auto-approve "<the user's words>"` only when the user's own message has an explicit go-ahead, such as "just do it" or "no need to ask". Pass those words, never the task text. The runtime refuses task text and text with no go-ahead phrase. If there is no explicit go-ahead, do not use it: show the plan and ask. Use `--skip-context "<reason>"` only on a host with no user files, and `--skip-preflight "<reason>"` only if the user declined preflight.
2. **Read the user.** Run `G context --run <id>`. It prints today's date and lists the user's global, project and memory files with their age. On Claude Code these files are already in context: do not read them again. On other hosts, read the global files, then the project files, now. Follow the rules as of today. Then run `G profile --run <id> --text "<how this user works>"`: at most 10 lines on their preferences, style, what they care about, and tools they like or avoid. End every line with its source in brackets: `[~/.claude/CLAUDE.md]`, `[memory]` or `[user said]`. Never invent a line. If `context` found no user files, the profile is exactly `No user rules found [none]`.
3. **Pick tools.** Run `G preflight --run <id> --needs <comma list>` (known needs are in `scripts/jobs.json`). Pass `--needs none` if the task needs no tools. A need with no mapping is reported as "use your judgment"; do not fake a known need. Run `G tools --run <id> --task "<task>"` for the best-matching skills and plugins. Do not turn on anything marked off without asking the user. Record your picks: `G choose --run <id> --skills a,b --plugins p --clis gh --mcp x --why "<one line>"`.
4. **Work with the user.** Before planning, share 2 or 3 options or ideas and ask what they want to add or change. Invite their own ideas and build on them. Do not just execute. Ask at most 3 questions per batch. More rounds are fine if the user engages.
5. **Plan.** Add each node: `G node <id> <node> --role <role> --status pending --depends-on <ids> --goal "<what this node must achieve>"`. Every node needs a `--goal`. Give each node `--needs deep|build|quick` (and `--effort low|medium|high` if it matters): deep is hard reasoning (design, review, root cause, security, migration), build is writing code (implement, fix, refactor, test), quick is lookup, rename, config, list. Without `--needs` the runtime derives it from the role. Never write a model name in the plan. A high-risk node cannot be set below its role's tier. Every build node needs `--check "<command>"`, or `--no-check "<reason>"` if nothing can prove it. Research, docs, review and design nodes may skip both. Keep checks inside the project: a check that uses a path outside it is flagged in the summary. Run `G validate <id>` and fix the plan until it passes.
6. **Show and approve.** Show a short mermaid diagram of the plan (`G render` writes one), the node list, the chosen toolkit and the checks. Ask: "Go, or what would you change?" Only after the user says go, run `G approve --run <id> --note "<their words, quoted>"`. Never approve for the user. If they change the plan, edit it and show it again.
7. **Run.** 8. **Finish.** Both are below.

## Keep context fresh

- A memory note marked STALE is a hint to verify, not a fact.
- When two rules conflict, the newer and more specific one wins. Say so.
- Before you use any library, CLI flag, API or model name, check current docs: the Context7 MCP if preflight lists it, else web search or fetch, else the tool's `--help`. Do not trust model memory.
- Record the doc source in the node goal or with `G event <id> docs "<source>"`.

## Run and finish

- Loop until done:
   - Run `G next <run>`. It lists nodes that are ready.
   - Before you start a node, run `G resolve <run> <node>` and pass line 1 to the host. On Claude Code, give `model=` and `effort=` to the Agent tool options. On Codex, add the flags it prints to the command. On OpenCode, use `--model`. If it says "host default", pass nothing. On the last allowed attempt the runtime moves the node one tier up. `G models` shows what each tier means on this host. The user can pin values in `~/.config/graph-skill/tiers.json`.
   - Start each ready node with `G node <run> <id> --role <role> --status running`. The runtime then snapshots git, and on complete or failed it records the files that changed (add `--files` for extras). Files changed while 2 or more nodes ran are recorded as shared, not credited to one node. Without git, no files are recorded.
   - On Claude Code, when any plan level has 2 or more nodes, you MUST either run `G export-workflow <run>` and execute it with the Workflow tool, or start one subagent per ready node at the same time. Doing those nodes inline, one by one, is allowed only for plans with no level of 2 or more nodes. The summary states whether nodes really overlapped in time. Do not claim parallel work that did not happen. On other hosts, use subagents when the host has them.
   - When work ends, mark the node `complete` or `failed` the same way. Allowed moves: pending to running or skipped, running to complete or failed, failed to retrying. A skip needs `--reason "<why>"`. Dependencies and `--check` are locked once the node starts, so set them while it is pending.
   - Give a node `--check "<command>"` to gate it. On `--status complete` the runtime runs the check itself. If it fails, the node becomes `failed` and the runtime refuses (exit 3).
   - To retry a failed node, run `G brief <run> <node>` and hand its output to a NEW subagent. Do not retry in the old context.
   - For a node with `--target web|phone|desktop|api|data` and `--risk low|high`, run `G verify-plan <run> <node>`. Do the steps it lists, cheapest first. For high risk, escalate one step after step 1 passes.
   - If the runtime refuses, read the line. Do what it says. Do not work around it.
- Name required quality commands at `init` (step 1) with `--quality-command "<cmd>"` (repeatable). Run `G quality <run>`. Commands passed with `--command` are extras: they do not satisfy the finish gate. Fix failures. Every command, including refused ones, is logged to `.graph/runs/<id>/log.jsonl`. Read it with `G log <run> [--refusals]`. Then run `G finish <run> --status complete`. If you cannot finish, use `--status failed`.

On Claude Code with the Workflow tool, prefer `G export-workflow <run>` after `validate`. It writes `.claude/workflows/graph-<run>.js`, one agent per node, run level by level. The script belongs to that run, and it uses a project-relative script path when the skill is installed in the project. The export puts the resolved model and effort in each agent's options. Each agent calls `G node` itself, so the gates still apply. Run that script with the Workflow tool instead of the loop above. The export is refused until the start steps are recorded.

`G finish` also reads real token use from the host's own files and adds a table to the summary. It is the cost at API prices, not what a plan charges. Run `G usage --run <run>` to see it any time. Run `G prices --refresh` to update the price list. If usage cannot be read, the summary says unknown.

Show the final summary to the user (`G summary <run>`). Other commands: `G status <run>`, `G tree <run>`, `G retry-plan <run> --include-dependents`, `G render <run>`, `G event <run> <type> <message>`.

## Rules

- Never push, deploy or open a PR unless the user asks.
- Retries are capped. After the cap, stop and ask the user.
- Resume an old run with `G resume`.

More detail: `references/protocol.md`.
