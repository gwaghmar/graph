# Graph protocol detail

## Commands
- `init "<task>" [--host H] [--max-retries N]` creates a run and prints its id.
- `node <run> <id> --role R --status S [--depends-on ...] [--files ...] [--goal G] [--check CMD] [--target T] [--risk R] [--check-timeout S]` records a node.
- `brief <run> <node>` prints a fresh-context retry brief: goal, check, failing output tail, git diff --stat, attempt n of max.
- `verify-plan <run> <node>` prints the verification steps this machine supports for the node target, cheapest first.
- `next <run>` lists ready nodes, or `none`.
- `validate <run>` rejects unknown, self and cyclic dependencies.
- `quality <run> [--command C]` runs checks and records the result.
- `retry-plan <run> --include-dependents` lists nodes to rerun.
- `export-workflow <run> [--out F]` writes a Claude Code Workflow script.
- `event <run> <type> <message>` appends a note to the run log.
- `render <run>` rewrites the run report files.
- `finish <run> --status complete|failed`, `summary`, `tree`, `status`, `resume`.
- `preflight --needs a,b [--refresh] [--json]` scans the environment.

## Statuses
pending, running, retrying, complete, cached, failed, skipped.

## Exit codes
0 ok. 1 `validate` found a bad plan, or `quality` failed. 2 bad input. 3 refused by a rule. A refusal line says why and what to do.

## Refusals
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
id, role, depends_on, files, goal, check, target, risk. Keep nodes small. Split by independent file scope.
