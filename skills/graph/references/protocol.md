# Graph protocol detail

## Commands
- `init "<task>" [--host H] [--max-retries N] [--skip-preflight "<reason>"] [--skip-context "<reason>"] [--auto-approve "<user words>"]` creates a run, stores today's date (UTC) as `run_date`, and prints its id. `--skip-preflight` and `--skip-context` record why a step is not needed. `--auto-approve` records the user's words that allow going ahead without review. All are logged.
- `context --run <id>` prints "Today is YYYY-MM-DD.", then the user's context files in priority order (global: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.cursor/rules`; project: `CLAUDE.md`, `AGENTS.md`, all `.cursor/rules/*.mdc` (alwaysApply ones marked); then the Claude memory index and each note it links; the memory folder is chosen from `--project-dir`, else `CLAUDE_PROJECT_DIR`, else the git top-level, else the current folder; if no index is found it prints "memory index not found for this folder" instead of a stale count) with last-modified date and age in days. A note older than 30 days is marked STALE. It prints "Stale: N notes". It reads only the frontmatter of memory notes (`metadata.modified`, else file date). It never prints file bodies, and home paths show as `~`. The stale names are stored in the run state.
- `profile --run <id> --text "<note>"` stores the "how this user works" note. Refused (exit 2) above 10 lines or 1200 characters.
- `tools --run <id> --task "<text>"` ranks enabled skills and plugins by word overlap between the task and name plus description (no AI), prints the top 8, the CLI and MCP pick per need from `jobs.json`, and lists strong matches that are turned off.
- `choose --run <id> [--skills a,b] [--plugins p] [--clis gh,vercel] [--mcp x] --why "<one line>"` records the toolkit. Names not found on this machine are allowed and flagged. Names that are turned off are flagged.
- `approve --run <id> --note "<what the user said>"` records approval with the user's words.
- `node <run> <id> --role R --status S [--depends-on ...] [--files ...] [--goal G] [--check CMD] [--target T] [--risk R] [--check-timeout S]` records a node.
- `brief <run> <node>` prints a fresh-context retry brief: goal, check, failing output tail, git diff --stat, attempt n of max.
- `verify-plan <run> <node>` prints the verification steps this machine supports for the node target, cheapest first.
- `next <run>` lists ready nodes, or `none`.
- `validate <run>` rejects unknown, self and cyclic dependencies (exit 1) and nodes without a goal (exit 3).
- `quality <run> [--command C]` runs checks and records the result.
- `retry-plan <run> --include-dependents` lists nodes to rerun.
- `export-workflow <run> [--out F]` writes a Claude Code Workflow script.
- `event <run> <type> <message>` appends a note to the run log.
- `render <run>` rewrites the run report files.
- `finish <run> --status complete|failed`, `summary`, `tree`, `status`, `resume`.
- `preflight --needs a,b [--refresh] [--json] [--run <id>]` scans the environment. With `--run` it also writes the needs, chosen tools and workarounds to the run log (no account names unless `--with-accounts`).
- `log <run> [--refusals] [--node X] [--json]` prints the run log as a timeline.

## Run log
Every command call on an existing run appends one line to `.graph/runs/<id>/log.jsonl`: time, command, node, main args, outcome (`ok`, `refused` exit 3, `invalid` exit 2, `error`), exit code, the refusal text, and for checks the command, exit code, duration and timeout. Refused calls are logged too. Calls with an unknown run id are not logged. Env values are never logged. The log is not tamper-proof: it is a plain file the agent could edit.

## Start order
1 size the task, 2 read the user (`context`, `profile`), 3 preflight and pick tools (`tools`, `choose`), 4 work with the user on ideas, 5 plan, 6 show the plan and get approval (`approve`), 7 run, 8 finish. The runtime cannot prove an approval came from a human. It records the text it is given.

## Freshness
`init` stores `run_date`. `summary.md` and the report show "Run date", "Stale context flagged: N (names)", the toolkit and the approval text. Rules for the agent: STALE notes are hints to verify; the newer and more specific rule wins when two conflict; check current docs (Context7, web, or `--help`) before using a library, flag, API or model name, and record the source.

## Skill discovery
Preflight lists skills with name and a description cut to 200 chars (frontmatter only) from `.claude/skills`, `~/.claude/skills`, `.agents/skills`, `~/.agents/skills`, `~/.codex/skills`, and the `skills/<name>/SKILL.md` folders (plus any skill paths `plugin.json` declares) of plugins in `enabledPlugins`. Skills of a plugin are named with the `name` in its `plugin.json`, not the settings key. `enabledPlugins` and `skillOverrides` are merged in Claude Code order: `~/.claude/settings.json`, then the project `.claude/settings.json`, then `.claude/settings.local.json` (later wins) (found through `~/.claude/plugins/installed_plugins.json`, else `~/.claude/plugins/cache/<market>/<plugin>/<version>`; only folders under `~/.claude/plugins` are read). A skill is OFF when `skillOverrides` sets it to `off`; a plugin and its skills are OFF when `enabledPlugins` is false.

## Statuses
pending, running, retrying, complete, cached, failed, skipped.

## Exit codes
0 ok. 1 `validate` found a bad plan, or `quality` failed. 2 bad input. 3 refused by a rule. A refusal line says why and what to do.

## Refusals
- A node cannot start (move to running or retrying) until all of these are recorded, and the refusal (exit 3) lists exactly which are missing, with the commands: preflight logged (or `--skip-preflight`); `profile` and `choose` recorded (or `--skip-context`); `approve` recorded (or `--auto-approve` at init). `approve` is refused when the plan has no nodes or fails `validate`; it stores a fingerprint of node ids, dependencies, goals and checks, and if the plan changes afterwards the start is refused until the plan is shown again and approved again. `context` and `validate` are not gated. Runs made before schema 4 only need preflight. `export-workflow` is refused while profile, choose or approval is missing (preflight is not required for it).
- A node cannot start while its dependencies are unfinished.
- A node cannot start after it used all its attempts.
- A run cannot finish as complete while nodes are open or the last quality run failed.

- Completing a node with a check runs the check. A failing check sets the node to failed and exits 3.

## Checks
`--check` is a shell command string. The runtime runs it with `shell=True` (the only place it does), because it is a user or plan command that may use pipes or `&&`. Timeout is 300s, change it with `--check-timeout`. The runtime stores the exit code and the last 40 lines of output on the node.

## Verify plan
Targets: web, phone, desktop, api, data, none. Risk: low or high.
- web: build/tests/http status, headless screenshot, real browser.
- phone: browser device emulation, iOS Simulator (mac) or Android emulator (windows/linux), iPhone Mirroring (mac, needs user consent).
- desktop: tests and launch exit code, screenshot, computer-use.
- api: contract tests or curl, staging call.
- data: counts and schema diff, reconcile with source.
Listing stops at the first step the machine lacks, with a one-line workaround. High-risk nodes escalate one step after step 1 passes. Data comes from the cached preflight scan.

## Plan fields
id, role, depends_on, files, goal (required), check, target, risk. Keep nodes small. Split by independent file scope.

## Files
When a node goes to running, the runtime stores a git snapshot (HEAD, changed files and their mtimes). On complete or failed it records the files that changed since (new commits, edits, untracked files) into the node, plus any `--files`. Parallel nodes may share attributions. Without git, nothing is recorded and nothing fails.

## Summary and report
`summary.md` and the report build "What failed" from the whole run log: every failed check, refused action and failed quality run, with how it was resolved. "Nothing" is claimed only when the log shows nothing. A node counts as checked only if its check ran and passed. A "Parallel:" line says how many plan levels had 2+ nodes and whether nodes overlapped in time, from recorded start and end times.

## Export
`export-workflow` writes a script for one run (a comment at the top says so). It calls graph.py by a project-relative path when the skill is installed in `.claude/skills/graph` or `.agents/skills/graph`, else by absolute path. The launcher is `python3`, or `py -3` / `python` on Windows. If preflight is not yet logged, the script starts with a preflight step.

## Second end-to-end fixes

- `init --auto-approve` is refused (exit 3, run still created without it) when the text is the task, part of it, or over 60% the same words, or has no go-ahead phrase (list: `GO_AHEAD` in `graph.py`).
- `preflight --needs` accepts unknown needs ("no mapping for X, use your judgment", logged as `unmapped`) and `--needs none`.
- The summary flags checks that use an absolute path outside the project, or install a tool outside it.
- Files changed while 2+ nodes overlapped are stored as `shared_files` with `shared_with`. Files passed with `--files` stay the node's own.
- `profile` lines must end with a source in brackets (exit 2 otherwise). With no user files found the text must be `No user rules found [none]`.
- `validate` (and so `approve`) refuses build nodes (role not research, docs, review or design) with no `--check` and no `--no-check "<reason>"` (exit 3). Summary lists the reasons.
- `init` and `preflight` share `preflight.detect_host`. `CODEX_*` variables win over `CLAUDECODE`. `init` stores `host_detected` next to `--host`.
