# Graph protocol detail

## Commands
- `init "<task>" [--host H] [--max-retries N] [--skip-preflight "<reason>"]` creates a run and prints its id. `--skip-preflight` records why preflight is not needed (logged).
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

## Statuses
pending, running, retrying, complete, cached, failed, skipped.

## Exit codes
0 ok. 1 `validate` found a bad plan, or `quality` failed. 2 bad input. 3 refused by a rule. A refusal line says why and what to do.

## Refusals
- A node cannot start until `preflight --run <id>` is logged for the run, or the run was created with `--skip-preflight`.
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
