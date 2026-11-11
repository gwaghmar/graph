# Graph

A plan gate and run recorder for coding agents. You write a plan as a dependency graph. A small Python script checks the plan and refuses rule breaks. It also records every run.

[![CI](https://github.com/gwaghmar/graph/actions/workflows/ci.yml/badge.svg)](https://github.com/gwaghmar/graph/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What the code enforces

These rules live in `graph.py`, not in a prompt. The model cannot skip them. A refused action exits with code 3 and prints one line saying why and what to do. Other codes: 2 is bad input, and 1 is a failed `validate` (bad plan) or failed `quality` run.

- A plan with unknown, self or cyclic dependencies is rejected.
- A node cannot start while a dependency is unfinished.
- A node cannot start after it used all its attempts. The default cap is 2 retries.
- A node with `--check` cannot complete unless the check passes. The script runs the check itself. A failed check marks the node failed.
- A run cannot finish as complete while nodes are open or the last quality run failed.

What the code does not enforce: that the host runs ready nodes in parallel, or that the model does good work. The script only records and refuses. Parallel runs need a host with subagents.

## Preflight scan

`graph.py preflight --needs github,deploy` scans your machine before planning. It checks which CLIs are installed (git, gh, vercel, supabase, node, docker and others), whether gh, vercel and supabase are logged in, and what you can use to verify work (headless browser, simulators). It picks the best tool for each job you name. The result is cached in `.graph/preflight.json`.

It records names and yes or no answers only. It parses MCP config files but keeps only server names, and never stores or prints env values or tokens. Account names are shown only with `--with-accounts`.

## Install

On Claude Code, the plugin is the first choice. Two commands:

```bash
claude plugin marketplace add gwaghmar/graph
claude plugin install graph@graph-skill
```

For any other host, or as a fallback, use the installer. It needs Node 18 or newer. The runtime needs Python 3.9 or newer.

```bash
npx graph-skill install            # this project
npx graph-skill install --global   # your home folder
```

It copies one skill folder to `.claude/skills/graph` and `.agents/skills/graph`. It also cleans up files from 0.x installs. Other commands: `update`, `uninstall`, `doctor`. Each takes `--global`.

## Quick example

```bash
G() { python3 .agents/skills/graph/scripts/graph.py "$@"; }
G init "Add CSV export" --host claude --id r1
G context --run r1                        # read the user: dated files, stale notes
G profile --run r1 --text "short replies, likes tables"
G preflight --run r1 --needs github,deploy
G tools --run r1 --task "add CSV export"
G choose --run r1 --clis gh --why "needs gh for the PR"
G node r1 scaffold --role worker --status pending --goal "project scaffold"
G node r1 api --role worker --status pending --depends-on scaffold \
   --goal "export endpoint" --check "npm test"
G node r1 ui --role worker --status pending --depends-on scaffold --goal "export button"
G validate r1                             # rejects bad plans
G approve --run r1 --note "go"            # only after the user says go; nodes cannot start before this
G next r1                                 # lists ready nodes
G node r1 scaffold --role worker --status running
G node r1 scaffold --role worker --status complete
G node r1 api --role worker --status running
G node r1 api --role worker --status complete   # runs the check; refuses on failure
G brief r1 api                            # retry brief for a new subagent
G quality r1
G finish r1 --status complete
G summary r1
```

In normal use the agent runs these for you. Type `/graph <task>` on Claude Code, `$graph <task>` on Codex.

## Commands

| Command | What it does |
|---|---|
| `preflight --needs a,b [--refresh] [--with-accounts] [--json] [--run id]` | Scan the environment, pick tools (`--run` logs it; nodes cannot start until it is logged) |
| `log <run> [--refusals] [--node X] [--json]` | Show what the run did and what the rules refused |
| `init "<task>" [--host H] [--max-retries N] [--skip-preflight R] [--skip-context R] [--skip-budget R] [--auto-approve WORDS]` | Start a run |
| `context --run id` | Read the user: dated list of global, project and memory files, stale notes flagged |
| `profile --run id --text "..."` | Record how this user works (10 lines max) |
| `tools --run id --task "..."` | Rank skills and plugins for the task, show CLI and MCP picks |
| `choose --run id [--skills --plugins --clis --mcp] --why "..."` | Record the chosen toolkit |
| `budget --run id [--left-pct N --resets HH:MM] [--host auto]` | Usage left per window with reset time, plan estimate, and a recommendation. Required before approve. `--left-pct` records the user's answer when the host gives no usage |
| `defer <run> --from-level N --not-before ISO` | Refuse starting nodes on level N and up until that time. A printed suggestion: nothing is scheduled |
| `find --run id --part P --query "..."` | Search Reddit, Hacker News and GitHub (no login, last 90 days) for skills and repos people already made. Trust bar, top 3, one recommended |
| `candidate --run id --part P --url U --mentions N --note "..."` | Add a find from X or the web. GitHub links are checked the same way |
| `scan --run id --part P --pick owner/repo` | Shallow clone into `.graph/vendor` and a static safety scan: clean, warn or block |
| `pick --run id --part P --use owner/repo [--as skill\|reference\|tool] [--ok "..."]` | Use a scanned find. Refuses unscanned or blocked. A skill installs in this project only, after approve |
| `keep --run id --pick owner/repo` | Keep a pick at finish. The rest is removed |
| `vendor-run --run id --pick owner/repo [--in vendor\|project] -- cmd...` | Run a picked tool, no shell, PATH and LANG and a temporary HOME only. Not a full sandbox |
| `parts --run id --set a,b`, `part-none --run id --part P --note "..."` | Name the parts of a large job and log "none found". `init --size large` blocks approve until each part has a pick or a note |
| `approve --run id --note "..."` | Record the user's go-ahead, in their words |
| `node <run> <id> --role R --status S [...]` | Add or update a node, with rule checks |
| `validate <run>` | Reject bad dependencies |
| `next <run>` | List ready nodes |
| `usage --run id [--host auto] [--session id] [--project-dir dir]` | Read real token use from the host's own files for this run, by node and by model, with cost at API prices. `finish` does this for you |
| `prices [--refresh] [--run id]` | Show the price list in use. `--refresh` downloads a new one to `~/.cache/graph-skill/prices.json` |
| `models [--refresh] [--host auto\|all\|H] [--run id]` | Show what deep, build and quick mean on each host, with source and data age |
| `resolve <run> <node> [--host auto]` | Host-specific value for a node (Claude Code options, Codex flags, OpenCode `--model`), logged |
| `brief <run> <node>` | Fresh-context retry brief |
| `verify-plan <run> <node>` | Verification steps this machine supports |
| `quality <run> [--command C]` | Run lint, type and test checks |
| `retry-plan <run> --include-dependents` | List nodes to rerun |
| `export-workflow <run> [--out F]` | Write a Claude Code Workflow script |
| `finish <run> --status complete\|failed` | Close the run |
| `resume` | Continue the latest open run |
| `status`, `tree`, `summary`, `render`, `event` | Reports and run log |

## Start: read the user, pick tools, agree the plan

Before any node runs, the agent follows this order:

1. Size the task. Trivial tasks skip the graph. Otherwise `init` creates the run.
2. Read the user. `context` lists the global, project and memory files with their age and flags notes older than 30 days as STALE. `profile` records how this user works.
3. Pick tools. `preflight` scans the machine. `tools` ranks the installed skills and plugins for the task by word overlap (plain code, no AI) and notes strong matches that are turned off. `choose` records the toolkit. Unknown names are allowed and flagged.
4. Work with the user. The agent shares options and asks for the user's own ideas before planning.
5. Plan, then 6. show the plan (diagram, nodes, toolkit, checks) and ask "Go, or what would you change?". `approve` records the user's words.
7. Run. 8. Finish. The summary shows the run date, stale context flagged, the toolkit and the approval.

The first node start is refused (exit 3) until preflight, profile, choose and approve are recorded. The refusal lists exactly what is missing. Escape hatches, all logged: `init --skip-preflight "<reason>"`, `--skip-context "<reason>"` (hosts with no user files, waives profile and choose) and `--auto-approve "<the user's words>"` (the user already said to go ahead; task text or text without a go-ahead phrase is refused).

Honest limit: the runtime cannot prove that an approval came from a human. It only records the text the agent passes. The log is a plain local file. The gates stop an agent from skipping steps by accident, not from lying.

Usage budget: `budget` reads usage left from the host: Codex from the newest `~/.codex/sessions` rollout file (`rate_limits`), Claude Code from `~/.cache/graph-skill/claude-rate-limits.json`. A refilled Codex window counts as 0% used. Other hosts are unknown, so you ask the user and record the answer. Estimates come from your own `~/.config/graph-skill/history.jsonl` by (tier, role) when there are 5 or more samples, else from the shipped defaults, labeled "default, not your history". Tokens turn into percent only after 3 finished runs on that host and window (learned in `~/.config/graph-skill/budget-calib.jsonl`). Everything is approximate. The summary shows usage before and after, and estimate against actual per node.

Claude Code statusline helper (opt-in): `npx graph-skill statusline --enable` prints the `settings.json` change. `--yes` writes it after a backup and wraps any statusLine you already have. It stores only usage percentages, reset times, the model id and the context size. Without it, Claude Code usage left is unknown.

Token ledger: `usage` reads usage numbers, model ids, timestamps and ids from Claude Code transcripts (`~/.claude/projects/<project>/`, including subagent files) and Codex sessions (`~/.codex/sessions/`). It never reads message content, secrets or tokens. Cost is the API-price equivalent. On a subscription you pay your plan, not this. Other hosts show tokens as unknown. `finish` adds the table to the summary and appends one line per finished node (date, host, role, tokens, duration) to `~/.config/graph-skill/history.jsonl`.

Privacy: `context` and `tools` print file names, dates and skill names with short descriptions only. They never print file bodies or env values, and home paths show as `~`.

## Use what people already built

For each part of a job, `find` looks at Reddit, Hacker News and GitHub, in that order, with no login. The model adds X and web finds with `candidate`. A find is shown only with 200 or more stars or 3 or more mentions in 90 days, and an update in the last 90 days. `scan` reads every file of the top pick without running it: network calls, shell use, secret places, install scripts, hidden code, binaries, hook settings and prompt-injection text. A block cannot be picked. Picks install in the project only and `finish` removes them. `vendor-run` is not a sandbox: it hides your HOME and env values, but the tool can still reach the network and any file you can reach by full path. Everything fetched is treated as untrusted text, and every command and refusal goes to the run log.

## Routing by capability tier

Plans never name a model. Each node asks for a tier with `--needs deep|build|quick` (and `--effort low|medium|high`), or the runtime derives it from the role. `resolve` turns the tier into a value for the current host. Order: your own `~/.config/graph-skill/tiers.json`, then the host's model catalog (Codex, OpenCode) or the aliases in `scripts/capabilities.json` (Claude Code), with models.dev prices used to rank catalog models. If nothing is known it says "host default, routing not applied". High-risk nodes never route below their tier. The last allowed attempt goes one tier up. The summary shows tier asked, value resolved and the model that really ran, and flags mismatches.

## Logging

Every command on a run is appended to `.graph/runs/<id>/log.jsonl`, one JSON line each: time, command, node, main arguments, outcome, exit code, and the refusal text. Refused moves are logged too, so you can see what the rules stopped. Check commands record exit code and duration. Run `graph.py log <run> --refusals` for a timeline, or open the HTML report (Activity section). The log is a plain local file, not a tamper-proof audit trail.

## Supported hosts

Graph is one skill folder in the [Agent Skills](https://agentskills.io) format. Hosts that read that standard can load it: Claude Code, Codex, OpenCode, Cursor and OpenClaw. Claude Code reads `.claude/skills`. The others read `.agents/skills`. Only Claude Code was used to build and test it. The other hosts are expected to work and are untested here.

## Removed in 1.0

| Removed | Why |
|---|---|
| Content cache | Skipping nodes on a cache hit was never proven to be safe or to save anything. |
| Token ledger | Most hosts do not expose real usage, so the numbers were zero or guesses. |
| `--commit` flag | Committing for the user is unsafe. Commit yourself. |
| Timeline and critical path report | Unproven value, and extra code to keep. |
| Per-host adapter files | Replaced by one skill folder. |

## Benchmark status

A benchmark scaffold is in `bench/`. It compares a plain agent, the native Claude Code Workflow and Graph on five tasks. It has not been run. There are no results. Do not assume Graph is faster or cheaper until it is.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `npm test`.

## License

[MIT](LICENSE)

## What the runtime records itself
- Files: a git snapshot at node start and a diff at node end fill each node's files (parallel nodes may share attributions; no git, nothing recorded).
- Start gates: the first node start is refused until preflight, profile, choose and approve are recorded (or the run used `--skip-preflight`, `--skip-context`, `--auto-approve`).
- Goals: every node needs `--goal`. `validate` and `export-workflow` refuse the plan otherwise.
- Summary: "What failed" lists every failed check and refusal from the log with how it was resolved. A "Parallel:" line says whether nodes truly overlapped in time.
- Exported workflow scripts use a project-relative script path when the skill is installed in the project.
