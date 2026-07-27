# Changelog

All notable changes to this project are documented here.

## [Unreleased]

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
