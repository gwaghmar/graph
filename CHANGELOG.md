# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Fixed (found in a real end-to-end test)
- summary.md "What failed" is built from the full run log, with resolutions. "Checked" is claimed only for nodes whose check ran and passed.
- Files are recorded by the runtime from git snapshots (node start vs end). `--files` stays as an extra.
- Preflight is enforced: first node start is refused until `preflight --run <id>` is logged, or `init --skip-preflight "<reason>"`.
- `validate` and `export-workflow` refuse nodes without a goal (exit 3).
- Exported workflow uses a project-relative script path and a `python3` / `py -3` launcher, and states which run it belongs to.
- Summary and report state honestly whether nodes overlapped in time. SKILL.md requires Workflow or subagents when a level has 2+ nodes on Claude Code.

## [1.0.0] - 2026-11-10

Graph is now a plan gate and run recorder. The rules are enforced in code.

### Added
- Status moves are checked (no jump to complete or skipped), checks and dependencies lock when a node starts, `init` refuses an existing run id, the finish gate needs every required quality command (`init --quality-command`) to pass, timed-out commands are killed with their children, and `.graph/.gitignore` keeps run data out of git.
- Rule enforcement in `graph.py`: a node cannot start before its dependencies finish, retries are capped, `--check` gates completion, and a run cannot finish while work is open or quality failed. Refusals exit 3 with one line.
- `preflight` scan of installed CLIs, logins, and verification tools, with a job-to-tool picker.
- `brief` (retry brief for a fresh subagent), `verify-plan`, `next`, and `export-workflow`.
- One skill folder in the Agent Skills format, a Claude Code plugin, and `update`, `doctor` and `--global` in the installer.
- Test suite for the runtime and preflight, run on macOS, Linux and Windows.
- Benchmark scaffold in `bench/`. It has not been run.

### Removed (breaking)
- Content cache and `cache-prune`.
- Token ledger and token columns.
- `--commit` and the commit step.
- Timeline view and critical path.
- The per-host adapter files (`.claude/commands/graph.md`, agents, `.cursor/rules/graph.mdc`, `.opencode/commands/graph.md`, `.codex/skills/graph`).
- The `.graph/graph.py` runtime location. The runtime now lives inside the skill folder.

### Migration
Run `npx graph-skill install` (or `update`) in each project. It removes old 0.x files that it made and installs the new skill folder. Files it did not make are left in place. Existing run history in `.graph/runs/` is kept. Replace any use of `.graph/graph.py` with the skill path, and drop `--commit`.

## [0.6.0] - 2026-08-07

### Added
- **Timeline view** in the HTML report — a Gantt-style lane per node built from recorded start/end times, showing actual parallelism and bottlenecks.
- **Critical path** — computed locally (longest cumulative-duration dependency chain, falling back to longest chain when no durations exist yet), highlighted in the report's execution graph, shown in the report header, and printed in the final run summary.
- **Live auto-refresh** — the HTML report refreshes itself every 2 seconds while the run is in progress and stops once the run finishes, making the "live report" genuinely live without a server.
- `graph.py validate` — deterministic plan gate that exits non-zero on unknown, self, or cyclic dependencies. All five host adapters now run it after recording the plan, before executing any node.
- `graph.py cache-prune [--days N]` — removes cache entries older than N days (default 30); the cache no longer grows unbounded.
- Per-node token drill-down in the report — clicking a node shows its recorded token usage alongside its state.
- Wall-clock duration (`wall_ms`) tracked separately from summed per-node compute time; the summary now reports both, so parallel runs no longer look slower than they were.
- Planner/reviewer agent guidance updated per current multi-agent research: decompose along context boundaries rather than lifecycle phases, pin cross-cutting decisions (interfaces, naming, file ownership) in the plan, and review against acceptance criteria only.
- CI now also runs on Windows and Node 24.

### Fixed
- HTML report no longer clips large graphs — the SVG viewBox is sized to the laid-out graph instead of a hardcoded 900×500, which cut off nodes beyond four dependency levels (the documented full protocol produces six).
- Mermaid node labels use `<br/>` instead of a literal `\n`, which Mermaid does not render as a line break.
- `quality` auto-detection no longer crashes when `python3` is not on PATH (uses the running interpreter to probe for pytest); `init` no longer crashes when `git` is missing.

## [0.5.0] - 2026-07-27

### Added
- OpenClaw adapter (`--target openclaw`): installs the graph skill as an OpenClaw project-agent skill under `.agents/skills/graph/` (SKILL.md per the agentskills.io standard, `python3` dependency gating, runtime self-bootstrap). Publishable to ClawHub and installable from Hermes.
- GitHub Actions CI running the test suite on Node 18/20/22.
- CONTRIBUTING guide, issue/PR templates.

### Fixed
- Run status now advances from `planning` to `running` when the first node starts, matching the documented live-graph header.
- Completed the MIT license text (it was missing the standard warranty/liability boilerplate, which was causing GitHub to classify the license as "Other" instead of MIT).
- Removed the `blocked` node status from the protocol docs and renderer — it was documented as "waiting on a human decision" but no adapter or agent ever set it.

## [0.4.0] - 2026-07-27

### Added
- **Live text graph** — every node state transition prints a compact rendering of the run graph (progress bar, status glyphs, tree connectors, durations, retries, cache hits, tokens), rendered locally with no model tokens spent. Reprint on demand with `graph.py tree <run>`.
- **Final run summary** — `graph.py finish` / `graph.py summary` prints a full recap: task, node graph, quality checks, files touched, retries, cache hits, duration, and report path.
- `--flag=value` form accepted alongside `--flag value` for installer args.

### Fixed
- `graph-skill uninstall` no longer deletes the shared `.graph/` runtime out from under other hosts still installed in the same repo — it now keeps the runtime alive for any remaining host and only removes it once the last one is uninstalled.
- Codex detection now also checks `CODEX_HOME` outside of live-session detection (it's a persistent config variable, not session-only, so it's no longer restricted to the trusted-env-var fast path).

## [0.3.1] - 2026-07-27

Initial public release: installable graph engineering for Claude Code, Codex, OpenCode, and Cursor.

### Added
- Auto-detecting installer (`npx graph-skill install`) for Claude Code, Codex, OpenCode, and Cursor, with a shared `.graph/` runtime.
- Interactive live HTML report (`.graph/runs/<run-id>/graph.html`), rewritten on every state change.
- Smart retry — reruns only failed nodes and their dependents.
- Local quality gates — auto-detects npm lint/typecheck/test, pytest, Cargo, or Go tests.
- Content cache — reuses successful nodes when task and file contents are unchanged.
- Resume support for the latest incomplete run.
- Optional commit, gated behind an explicit `--commit` flag.
- Local run statistics: completed nodes, failures, retries, cache hits, changed files, duration.
