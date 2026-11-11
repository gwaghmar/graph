# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added (field brief and research mode)
- `brief --run <id> --field ...`: a short field brief the model writes from today's sources (no per-field code or lists). Required before approve on medium and large runs. One locked card on the checkpoint.
- `source`: date checked against the brief's window per source type with the runtime's own clock. Undated and too old are refused. Landmarks need `--landmark --why` and a brief with landmark-ok yes, and are labeled.
- `gap` (weak under 2 sources), `claim`, and `research --close` (5 valid sources and cited gaps and claims, or `--partial "<reason>"`).
- Summary and report: sources table, evidence levels, the brief, who should check, and safety lines at the top.
- SKILL.md: a short "Any field" section.

### Added (growth mode)
- `grow`: campaigns with one ordinary graph run per cycle. Experiments with a state machine enforced in code (one change, dated baseline before the change, one live experiment per metric and surface, no decision before the window end, below `min_sample` only inconclusive, stale or unknown readings cannot support a decision, a revert needs a `revert_ref` and a logged undo).
- Metrics ladder: public numbers, the user's CLI or MCP, a CSV in `inbox/`, or typed. Unknown is a value, never 0. Every reading has a source, a method and a date.
- Permission queue for anything that posts: content-hash approval, 72 hour expiry, no auto-approve, one card per item on the checkpoint screen, caps and gaps per channel, duplicate refusal, own accounts only, disclosure, replies only to listed threads.
- Posting ladder recorded per post (API or CLI, MCP, logged-in browser, computer use). The runtime records and gates; the model posts with host tools.
- Video by code: `video-check` (render output exists, duration above 0 by `ffprobe`, thumbnail by `ffmpeg`), `video-done`, and video cards with a thumbnail.
- `grow tick` (one cycle, stops at the checkpoint, never posts, prints a host schedule hint), `grow brief` and `grow report` ("moved with", same-weekday comparison, unknowns by name).

### Fixed (third end-to-end findings)
- Checkpoint: `--serve --background` starts the page detached, writes pid and port to `checkpoint.json`, and prints the link with its token at once. `--wait` and `--stop` added. The browser opens only with `--open`. GO records the request User-Agent class and the summary says "pressed GO from a browser" or "from a script (not proof of a human)". The honest limit is documented: the runtime cannot prove a human pressed GO.
- SKILL.md: on a large job run find for every part and scan the top picks before asking anything, then show the checkpoint (link or multi-select) and end the turn.
- Huge repos: new pick kind `package` (`--package npm:<name>` or `pip:<name>`), trusted from npm download counts or the PyPI entry plus GitHub stars, shown as "package: installed by npm, not scanned". A repo over the clone limit gets a sparse partial clone of its skill folders (`scan --path` names one), else "too big: reference only".
- Scanner: URLs in LICENSE, COPYING and NOTICE and markdown links in plain docs are not network use. Secret-path plus URL, injection phrases, hidden comments and hidden characters BLOCK only in agent files (SKILL.md, AGENTS.md, CLAUDE.md, `*.mdc`, `prompts/`, `commands/`, `agents/`) and code. In other docs they WARN.
- `graph-skill install --help`, `-h` and `help` print help and never install.
- Usage ledger reads every host session of the project in the run window (also sessions started in a sub folder or worktree, checked by each line's cwd), with their subagent files, de-duplicated by message id. The report says how many sessions it counted.

### Added (human checkpoint, sizes, short loud output)
- `checkpoint --run <id> [--serve] [--terminal]`: one short keep/drop screen (plan steps, top pick per part with "+2 more", safety badge, researched features, cost, security) and one GO. `--serve` uses a local page with a one-time token (403 on a wrong one, 30 minute timeout); `--terminal` reads `go` or `drop 2,4` and honours `NO_COLOR`. GO records approval as "pressed GO" with the drops, removes dropped pending steps and picks, and refuses if a kept step needs a dropped one.
- `init --size small|medium|large` (required). Small needs only preflight and approve. Medium keeps the current gates and gates only `parts --main` parts. Large gates every part and the checkpoint. `size --to` changes it (logged, gates re-checked). `feature` adds a researched feature to the checkpoint.
- The summary starts with a big status line and at most 3 "Do this" items, then a divider and the details. It says whether approval was "pressed GO" or "recorded by the model". Refusals print a heading, a "Do this" list and show home as `~`.

### Added (outside skills and repos)
- `find`, `candidate`, `scan`, `pick`, `keep`, `vendor-run`, `parts` and `part-none`: look for skills and repos people already made (Reddit, Hacker News, GitHub, plus X and web finds from the model), apply a trust bar (200 stars or 3 mentions, and updated in 90 days), show the top 3 with one recommended, scan the pick without running it, install it in the project only and remove it at `finish` unless kept.
- `init --size large`: `approve` is refused until each part has a pick or a "none found" note.
- `vendor-run` runs a picked tool with no shell and a scrubbed env.

### Fixed (review findings for finder, checkpoint, usage and routing)
- Scan reads whole files and every line, blocks on limits, shipped dependency folders and unreadable files, checks prose and SKILL.md, and finds secret paths without a HOME token.
- `vendor-run` runs in a sandbox (macOS, Linux `bwrap`), refuses without one unless `--no-sandbox`, and undoes writes to agent config.
- Checkpoint token is no longer printed, POST checks Origin, the server is threaded with a socket timeout, and the summary keeps the "cannot prove" line.
- `size --to` cannot lower the size after a node started or approval; size is in the plan hash and the summary.
- Mentions count distinct people (owner excluded, capped); no-star repos do not pass on mentions alone.
- `git clone` uses a scrubbed env; cleanup and install are per run.
- Budget answer now gives a recommendation; statusline writer keeps symlinks and file mode; usage prices 1 hour cache writes; non-object JSON no longer crashes.

### Added (usage budget)
- `budget --run <id>`: usage left per window with local reset time (Codex rollout files, or the Claude Code statusline cache), a plan estimate by (tier, role) from your history with shipped defaults under 5 samples, and a recommendation (all now, lighter tier, levels 1-N now, or the user decides). The step is required before approve and node start. `init --skip-budget "<reason>"` waives it.
- `budget --left-pct N --resets HH:MM` records the user's answer when the host gives no usage.
- Tokens per percent are learned per host and window from finished runs (3 needed). Until then the report says "cannot convert to % yet".
- `defer <run> --from-level N --not-before <ISO>`: nodes in those levels are refused (exit 3) until the time, `next` shows the wait, `resume` re-reads usage, `export-workflow` exports only levels that can run now. Nothing is scheduled.
- The summary reports usage before and after per window, and estimate against actual per node with error percent.
- `scripts/statusline.py` and `graph-skill statusline --enable [--yes]`: opt-in helper that writes usage numbers to a cache atomically and can wrap an existing statusLine. Settings are only written with `--yes`, after a backup.

### Added (capability tiers)
- Route by capability tier, not model name: `node --needs deep|build|quick --effort low|medium|high`, derived from the role when omitted. `--model` stays as a logged manual override.
- `models [--refresh] [--host]` and `resolve <run> <node>`: user `tiers.json` first, then the Codex or OpenCode catalog (cached 24 hours) or Claude Code aliases from `scripts/capabilities.json`, ranked with models.dev. Unknown means "host default, routing not applied".
- High risk never routes below its tier, the last allowed attempt goes one tier up (`brief` says so), and `export-workflow` writes `model` and `effort` into each agent.
- The summary has a "Model routing" table that compares the tier asked, the value resolved and the model that ran.
- The models.dev price cache also keeps `reasoning` and `tool_call` flags.

### Added (token ledger)
- `usage --run <id>` reads real token use from Claude Code transcripts (with subagents) and Codex sessions, by node and by model, with cost at API prices. `finish` adds it to the summary and fails soft to unknown.
- `prices [--refresh]` with a shipped `scripts/prices.json` snapshot and a 30-day cache in `~/.cache/graph-skill/`.
- Finished nodes are appended to `~/.config/graph-skill/history.jsonl` (date, host, role, tokens, duration).

### Fixed (second end-to-end test)
- `--auto-approve` refuses task text and text with no go-ahead phrase.
- `preflight --needs` accepts unknown needs and `none`.
- Summary flags checks that depend on paths outside the project.
- Overlapping nodes record shared files instead of crediting one node.
- Profile lines need a source in brackets; no user files means "No user rules found [none]".
- `validate` refuses build nodes without a check unless `--no-check "<reason>"`.
- One shared host detection for `init` and `preflight`; Codex variables win over a leaked `CLAUDECODE`.

### Added
- New start order: size, read the user, pick tools, work with the user, plan, show and approve, run, finish.
- `context` (dated user files, stale memory notes flagged after 30 days), `profile`, `tools` (rank skills and plugins by word overlap, CLI and MCP picks), `choose`, `approve`, and `init --skip-context` / `--auto-approve`.
- Preflight discovers skills with descriptions, including plugin skills, and marks skills and plugins that are turned off.
- The first node start is refused until preflight, profile, choose and approval are recorded. `export-workflow` is refused without profile, choose and approval. `approve` needs a valid plan and is tied to it: changing the plan afterwards needs a new approval.
- Freshness: runs store the run date. The summary and report show the run date, stale context flagged, toolkit and approval. SKILL.md adds rules for stale notes, newer-rule-wins and checking current docs.

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
