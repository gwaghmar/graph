# Graph

A plan gate and run recorder for coding agents. You write a plan as a dependency graph. A small Python script checks the plan and refuses rule breaks. It also records every run.

[![CI](https://github.com/gwaghmar/graph/actions/workflows/ci.yml/badge.svg)](https://github.com/gwaghmar/graph/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What the code enforces

These rules live in `graph.py`, not in a prompt. The model cannot skip them. A refusal exits with code 3 and prints one line saying why and what to do.

- A plan with unknown, self or cyclic dependencies is rejected.
- A node cannot start while a dependency is unfinished.
- A node cannot start after it used all its attempts. The default cap is 2 retries.
- A node with `--check` cannot complete unless the check passes. The script runs the check itself. A failed check marks the node failed.
- A run cannot finish as complete while nodes are open or the last quality run failed.

What the code does not enforce: that the host runs ready nodes in parallel, or that the model does good work. The script only records and refuses. Parallel runs need a host with subagents.

## Preflight scan

`graph.py preflight --needs github,deploy` scans your machine before planning. It checks which CLIs are installed (git, gh, vercel, supabase, node, docker and others), whether gh, vercel and supabase are logged in, and what you can use to verify work (headless browser, simulators). It picks the best tool for each job you name. The result is cached in `.graph/preflight.json`.

It records names and yes or no answers only. It never reads tokens or env values. Account names are shown only with `--with-accounts`.

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
G="python3 .agents/skills/graph/scripts/graph.py"
$G preflight --needs github,deploy
$G init "Add CSV export" --host claude --id r1
$G node r1 scaffold --role worker --status pending
$G node r1 api --role worker --status pending --depends-on scaffold \
   --check "npm test"
$G node r1 ui --role worker --status pending --depends-on scaffold
$G validate r1                             # rejects bad plans
$G next r1                                 # lists ready nodes
$G node r1 scaffold --role worker --status running
$G node r1 scaffold --role worker --status complete
$G node r1 api --role worker --status running
$G node r1 api --role worker --status complete   # runs the check; refuses on failure
$G brief r1 api                            # retry brief for a new subagent
$G quality r1
$G finish r1 --status complete
$G summary r1
```

In normal use the agent runs these for you. Type `/graph <task>` on Claude Code, `$graph <task>` on Codex.

## Commands

| Command | What it does |
|---|---|
| `preflight --needs a,b [--refresh] [--with-accounts] [--json]` | Scan the environment, pick tools |
| `init "<task>" [--host H] [--max-retries N]` | Start a run |
| `node <run> <id> --role R --status S [...]` | Add or update a node, with rule checks |
| `validate <run>` | Reject bad dependencies |
| `next <run>` | List ready nodes |
| `brief <run> <node>` | Fresh-context retry brief |
| `verify-plan <run> <node>` | Verification steps this machine supports |
| `quality <run> [--command C]` | Run lint, type and test checks |
| `retry-plan <run> --include-dependents` | List nodes to rerun |
| `export-workflow <run> [--out F]` | Write a Claude Code Workflow script |
| `finish <run> --status complete\|failed` | Close the run |
| `resume` | Continue the latest open run |
| `status`, `tree`, `summary`, `render`, `event` | Reports and run log |

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
