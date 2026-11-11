# Graph protocol detail

## Commands
- `init "<task>" [--host H] [--max-retries N] [--skip-preflight "<reason>"] [--skip-context "<reason>"] [--auto-approve "<user words>"]` creates a run, stores today's date (UTC) as `run_date`, and prints its id. `--skip-preflight` and `--skip-context` record why a step is not needed. `--auto-approve` records the user's words that allow going ahead without review. All are logged.
- `context --run <id>` prints "Today is YYYY-MM-DD.", then the user's context files in priority order (global: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.cursor/rules`; project: `CLAUDE.md`, `AGENTS.md`, all `.cursor/rules/*.mdc` (alwaysApply ones marked); then the Claude memory index and each note it links; the memory folder is chosen from `--project-dir`, else `CLAUDE_PROJECT_DIR`, else the git top-level, else the current folder; if no index is found it prints "memory index not found for this folder" instead of a stale count) with last-modified date and age in days. A note older than 30 days is marked STALE. It prints "Stale: N notes". It reads only the frontmatter of memory notes (`metadata.modified`, else file date). It never prints file bodies, and home paths show as `~`. The stale names are stored in the run state.
- `profile --run <id> --text "<note>"` stores the "how this user works" note. Refused (exit 2) above 10 lines or 1200 characters.
- `tools --run <id> --task "<text>"` ranks enabled skills and plugins by word overlap between the task and name plus description (no AI), prints the top 8, the CLI and MCP pick per need from `jobs.json`, and lists strong matches that are turned off.
- `choose --run <id> [--skills a,b] [--plugins p] [--clis gh,vercel] [--mcp x] --why "<one line>"` records the toolkit. Names not found on this machine are allowed and flagged. Names that are turned off are flagged.
- `approve --run <id> --note "<what the user said>"` records approval with the user's words.
- `node <run> <id> --role R --status S [--depends-on ...] [--files ...] [--goal G] [--check CMD] [--target T] [--risk R] [--needs deep|build|quick] [--effort low|medium|high] [--model M] [--check-timeout S]` records a node. `--model` is a manual override: it is logged and noted in the report.
- `models [--refresh] [--host auto|all|<host>] [--run <id>]` prints tier -> value per host with the source and data age. `--refresh` refetches models.dev and the host catalog.
- `resolve <run> <node> [--host auto]` prints the host value for the node on its next attempt (line 1), then tier, source and notes. It stores the route on the node and logs it.
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
id, role, depends_on, files, goal (required), check, target, risk, needs, effort. Keep nodes small. Split by independent file scope.

## Files
When a node goes to running, the runtime stores a git snapshot (HEAD, changed files and their mtimes). On complete or failed it records the files that changed since (new commits, edits, untracked files) into the node, plus any `--files`. Parallel nodes may share attributions. Without git, nothing is recorded and nothing fails.

## Summary and report
`summary.md` and the report build "What failed" from the whole run log: every failed check, refused action and failed quality run, with how it was resolved. "Nothing" is claimed only when the log shows nothing. A node counts as checked only if its check ran and passed. A "Parallel:" line says how many plan levels had 2+ nodes and whether nodes overlapped in time, from recorded start and end times.

## Export
`export-workflow` writes a script for one run (a comment at the top says so). It calls graph.py by a project-relative path when the skill is installed in `.claude/skills/graph` or `.agents/skills/graph`, else by absolute path. The launcher is `python3`, or `py -3` / `python` on Windows. If preflight is not yet logged, the script starts with a preflight step.

## Token ledger
`usage.py` collects usage between the run's `created_at` and `finished_at` (or now) for the current project. Claude Code: `~/.claude/projects/<slug>/*.jsonl` and `<session>/subagents/agent-*.jsonl`, deduped on `message.id` (else `requestId`), the last line of a message wins. Codex: `~/.codex/sessions/**/rollout-*.jsonl`, deltas of `total_token_usage` for sessions whose cwd is the project, model from `turn_context`. Other hosts: unknown. Only numbers, model ids, timestamps and ids are read.
Main-session usage is `orchestrator`. A subagent goes to the one node whose time span overlaps it, `shared` if it overlaps 2 or more, `unattributed` if none. Reasoning tokens are part of output.
Prices: `scripts/prices.json` (snapshot of models.dev, providers anthropic, openai, google). `prices --refresh` writes `~/.cache/graph-skill/prices.json`. The cache is used if under 30 days old, else the snapshot. Ids match exactly, then without a date suffix. An unknown model shows tokens and cost n/a. The result is stored in `state.json` under `usage` and shown in the summary. `finish` fails soft to unknown.

## Capability tiers
Plans name a capability, never a model: `deep` (design, review, root cause, security, migration), `build` (implement, fix, refactor, test), `quick` (lookup, rename, config, list). `--needs` sets it. Without it the tier comes from the role, through `ROLE_TIER` in `routing.py` (one table; unknown roles use build). `--effort` is low, medium or high.
Values per host, highest priority first:
1. `~/.config/graph-skill/tiers.json`, `{"<host>": {"deep": "<value>", "build": "...", "quick": "..."}}`, host is `claude-code`, `codex`, `opencode` or `cursor`. It wins per tier. Values must be plain ids (letters, digits, `. : / @ + [ ] -`).
2. The host catalog. Codex: `codex debug models` (JSON; entries with a `visibility` other than `list` are skipped). OpenCode: `opencode models --verbose`. Both cached 24 hours in `~/.cache/graph-skill/catalog-<host>.json` (ids and capability numbers only). Claude Code has no catalog: its aliases are data in `scripts/capabilities.json`.
3. models.dev data (the prices cache, or `models --refresh`) ranks the catalog models by output price. deep is the highest price with reasoning true. quick is the lowest price with tool calling. build is the median-priced reasoning model (lower median); with two models it is the cheaper one, with one it is deep. A model with no price is not ranked. If nothing can be ranked the value is "host default, routing not applied".
`resolve` prints, per host: Claude Code `model=<alias> [effort=<x>]` (the Agent tool options), Codex `-m <slug> -c model_reasoning_effort=<x>` (the requested effort if the model supports it, else the model default), OpenCode `--model <provider/model>`, Cursor and unknown `host default`.
Rules in code: a missing tier falls back upward first; high risk never falls back downward. `node` refuses (exit 3) a high-risk node whose `--needs` is below its role's tier. The last allowed attempt after retries (needs `--max-retries` 1 or more) goes one tier up, and `brief` says so; a deep node gets effort high. `--needs` and `--effort` lock when planning ends. `plan_hash` includes `--needs` when set. `export-workflow` writes `model` and `effort` into each `agent()` options from the Claude Code values.
Report: `summary.md` has a "Model routing" table per node: tier asked, value resolved, model that ran (from the token ledger), and flags for MISMATCH, escalation, manual override and "not applied".

## Second end-to-end fixes

- `init --auto-approve` is refused (exit 3, run still created without it) when the text is the task, part of it, or over 60% the same words, or has no go-ahead phrase (list: `GO_AHEAD` in `graph.py`).
- `preflight --needs` accepts unknown needs ("no mapping for X, use your judgment", logged as `unmapped`) and `--needs none`.
- The summary flags checks that use an absolute path outside the project, or install a tool outside it.
- Files changed while 2+ nodes overlapped are stored as `shared_files` with `shared_with`. Files passed with `--files` stay the node's own.
- `profile` lines must end with a source in brackets (exit 2 otherwise). With no user files found the text must be `No user rules found [none]`.
- `validate` (and so `approve`) refuses build nodes (role not research, docs, review or design) with no `--check` and no `--no-check "<reason>"` (exit 3). Summary lists the reasons.
- `init` and `preflight` share `preflight.detect_host`. `CODEX_*` variables win over `CLAUDECODE`. `init` stores `host_detected` next to `--host`.

## Usage budget
`budget.py` returns `{status, windows:[{name, used_pct, window_minutes, resets_at, source, age_s}]}` or unknown with a reason.
- Codex: the newest `~/.codex/sessions/**/rollout-*.jsonl` that has a `token_count` event with `payload.rate_limits.primary` or `.secondary` (`used_percent`, `window_minutes`, `resets_at`). The last event counts. If `resets_at` is past, the window refilled: used 0, source `refilled`.
- Claude Code: `~/.cache/graph-skill/claude-rate-limits.json`, written by the opt-in `scripts/statusline.py` (`written_at`, `model`, `context_window`, `rate_limits.five_hour` and `seven_day` with `used_percentage` and `resets_at`). It is stale if older than 10 minutes or if `resets_at` has passed. The helper stores numbers and ids only.
- Other hosts: unknown. `budget --left-pct N --resets HH:MM` records the user's answer (source `user`). It is used only when the host source is unknown, and dropped once its reset time passes.
Estimate: fresh tokens = input + output + cache write (cache reads are left out). Per node by (tier, role): median and p80 over `history.jsonl`, needing 5 samples, else `scripts/estimates.json` labeled "default, not your history". A lighter tier is offered only for nodes that are not high risk, not already quick, and whose role is not a deep role.
Compare: `budget-calib.jsonl` holds one line per host, window and run: percent before, percent after, tokens. Recorded at `finish` when usage rose by 1 point or more and the window did not refill. Tokens per percent is the median, and needs 3 lines; below that the report says "cannot convert to % yet". Windows are compared with p80 so the advice errs on the safe side.
`start_gaps` has a `budget` step (schema 5 and later). `approve` refuses without it and recomputes the budget for the final plan. `defer` stores `deferred {from_level, not_before}`. `guard` refuses starting a node on a deferred level before that time (exit 3, shows the time). Every `budget`, `defer` and refusal is in `log.jsonl`. The summary section "Usage budget" shows before and after per window and estimate against actual.

## Outside skills and repos
- `find` order: Reddit (arctic-shift posts and comments, 90 days), Hacker News Algolia (90 days), GitHub repo search (sorted by stars, `pushed:>` 90 days, also "<query> skill" and "<query> claude skill"). GitHub links in Reddit and HN text are counted as mentions (one per distinct author, the repo owner's own posts ignored, total capped at 10; `candidate --mentions` is capped the same way) and every candidate gets GitHub metadata. Unauthenticated GitHub limits apply: one wait on 403 or 429 (at most 5 seconds), then the search stops and says so. Network errors never fail the command. All requests use `urllib`, 10 second timeout and User-Agent `graph-skill`.
- Trust bar: (stars >= 200 or mentions >= 3, and stars >= 1 either way) and pushed within 90 days and not archived. Idea-only finds (X or web, no repo) need 3 mentions. Score = mentions x 3 + 1.5 x log10(stars + 1) + recency (0 to 1). Result in `.graph/runs/<id>/finds/<part>.json`.
- `scan` clones with `git clone --depth 1` (60 second timeout, 50 MB cap, no hooks, no credentials, no prompts) into `.graph/vendor/<owner>__<repo>`, removes `.git`, and writes `.graph/runs/<id>/scan.json` (all findings, with file and line, never the matched text). Every line of every non-binary file is read, with no length or extension cut-off. A file over 8 MB, a read error, more than 4000 files, or a shipped `node_modules`, `venv`, `.venv` or `__pycache__` folder is a block (never a quiet skip). Prose such as SKILL.md is checked too: a secret place (`.ssh`, `.aws`, agent config) and any URL or network command in one file is a block, a secret place alone is a warn, and any URL or shell command in SKILL.md is a warn. The injection list is a small set of known phrases, not a guarantee. Block: reads of ssh, cloud, keychain, browser or agent config paths in code, decode-then-run code, install scripts that fetch code, SKILL.md `hooks` or wildcard `allowed-tools`, prompt-injection phrases, hidden comments with instructions, hidden unicode, executable binaries, symlinks out of the repo. Warn: network calls, shell or exec, `.env` or token use, writes outside the repo, install and build hooks, git hooks, long encoded blobs.
- `pick`: refused when the repo is not a shown find for the part, not scanned, blocked, or its copy is gone. After approval a warn needs `--ok "<the user's words>"`. A skill picked before approval is staged and copied by `approve`, into `.claude/skills/<name>` and `.agents/skills/<name>` with `.graph-outside-owner`. A folder without our marker is never overwritten.
- `finish` removes folders that carry this run's marker, vendor copies marked with this run, and `.graph/tmp/<run>`, except `keep` picks. Another run's copies are left alone, and `install_skill` never overwrites a folder owned by another run. `git clone` runs with a scrubbed env (no ssh agent, tokens or user git config, no LFS smudge). The summary lists removed and kept items.
- `vendor-run`: refused before approval and for anything not picked as a tool. No shell. Env is PATH, LANG, HOME, USERPROFILE, XDG dirs and temp dirs under `.graph/tmp`, plus SYSTEMROOT, COMSPEC and PATHEXT on Windows. Sandbox: macOS `sandbox-exec` (reads and writes only in the tool folder and temp home, no home access) or Linux `bwrap` (home hidden by a tmpfs). Elsewhere it refuses unless `--no-sandbox`. Agent config places (`.claude`, `.agents`, `.codex`, `.cursor`, `.git/hooks`) are snapshotted before and after; a change is undone, logged as a refusal and exits 3. The network is open.
- Size gates: `init --size` is required. Small: `start_gaps` needs only preflight and approve; budget, profile and choose are not required. Medium: all current gates, and `outside_gaps` covers only parts listed with `parts --main`. Large: every part needs a pick or a `part-none` note, and `checkpoint_built` must be set (any `checkpoint` call sets it) before `approve`. `size --to` changes the size, logs it (`size_history`, shown in the summary) and prints the gates now missing. Lowering is refused once any node has started or the plan is approved. The size is part of `plan_hash` unless it is medium.

## Checkpoint
`checkpoint.py` builds items from state: one per node (`node:<id>`), one per part with a pick or a recommended find (`pick:<part>`, with safety badge and "+N more"), one per feature (`feat:<id>`). It adds a cost line (`usage.totals.cost` if present, else "$0 / unknown") and a security line from `scan.json`. The page is one inline file with no network loads.
- `--serve`: `http.server` on 127.0.0.1, random port, one-time token (`?t=`) checked on every request, plus a Host check and, on POST, an Origin check. The server is threaded with a 5 second socket timeout, so an idle connection cannot block GO. The link is printed WITH the token: a headless agent has to hand it to the human. This means anyone who can read the output can press GO. The runtime cannot prove a human did; it records the User-Agent class of the `POST /go` (`browser` or `script`, stored in `approval.evidence.pressed_from`) and the summary says "pressed GO from a browser" or "pressed GO from a script (not proof of a human)". The browser opens only with `--open`. Wrong token is 403 and logged. `POST /go` with `{"dropped": [ids]}` calls `apply_checkpoint` under the run lock. A refusal answers 409 and the server stays up. After a good GO the server stops. Timeout 30 minutes (exit 1, nothing approved).
- `--serve --background`: starts the same server as a detached child (`--serve-child`, new session or detached process, no pipes), waits up to 15 seconds for `checkpoint.json` (`status`, `pid`, `port`, `url`, `expires`, then `pressed_from` and `drops`, mode 0600), prints the link and returns. A second call while it waits reuses it. `--wait [--timeout S]` polls that file (exit 0 on GO, 1 otherwise; `dead` means the port no longer answers). `--stop` sends `POST /stop?t=<token>` to the server, never a signal to a saved pid. All three are logged.
- `--terminal`: ANSI color only on a TTY with `NO_COLOR` unset. Input `go` or `drop 2,4`; end of input approves nothing.
- `apply_checkpoint` refuses (exit 3, nothing saved) for unknown ids, a drop of a node that is not pending, a kept node that depends on a dropped node, or when the plan or gates fail after the drops. Dropped picks are removed and the part is logged as none. It records `approval` with `source: "checkpoint"` and `drops`. `approve --note` records `source: "model"`. A background GO also stores `evidence.pressed_from`. The summary shows which.
- Every call, 403 and 409 is in `log.jsonl` under `checkpoint`.

## Output style
Summary top: big status line, `## 👉 Do this:` with at most 3 items, a divider, then the details. Refusals print `✖ STOPPED` and the message; gap lists start with `👉 Do this:`. Home is shown as `~`.

## Outside picks: packages, big repos, scanner levels

- `pick --as package --package npm:<name>|pip:<name>`: no clone, no scan, nothing installed by the runtime. `outside.check_package` reads `https://api.npmjs.org/downloads/point/last-month/<pkg>` (at least 10,000) and the npm `latest` entry, or `https://pypi.org/pypi/<pkg>/json`, and requires the registry entry to point to the repo. The find must also pass the trust bar (stars, activity). A network failure refuses the pick (not trusted). `scan.json` gets `verdict: package`; the checkpoint shows "package: installed by npm, not scanned".
- `scan`: a full shallow clone first. Over `CLONE_MAX` (size excludes `.git`) it retries as `git clone --depth 1 --filter=blob:none --sparse`, lists `SKILL.md` folders with `git ls-tree`, runs `git sparse-checkout set --cone <dirs>` and scans only those. `--path <folder>` skips the full clone. If nothing fits or no skill folder exists the result is `verdict: big` ("too big: reference only"): only `pick --as reference` is allowed, nothing is copied.
- Scanner levels: license files (LICENSE, COPYING, NOTICE) ignore URLs; markdown links in plain docs are not network use. The strict findings (injection phrase, hidden comment, hidden characters, secret place plus network) are BLOCK in agent files (`SKILL.md`, `AGENTS.md`, `CLAUDE.md`, `*.mdc`, files under `prompts/`, `commands/`, `agents/`) and BLOCK or WARN by their own rule in code; in other docs they are WARN.
- Usage ledger (Claude Code): every `*.jsonl` in the project folder and its `*/subagents/agent-*.jsonl`, plus folders for sub folders or worktrees whose lines have a `cwd` inside the project, in the run window, de-duplicated by message id. `usage.sessions` is the session count.

## Growth mode

`grow` is in `scripts/growth.py`. Every `grow` command and refusal is one line in `.graph/campaigns/<id>/log.jsonl`. Exit 2 is bad input. Exit 3 is a refused rule. Files: `campaign.json`, `queue.jsonl` (latest line per id wins), `metrics.jsonl`, `log.jsonl`, `drafts/`, `inbox/`.

Commands: `grow init`, `channel add`, `source add`, `read`, `exp add|baseline|start|decide`, `queue add|show|approve|drop`, `post`, `video-check`, `video-done`, `tick`, `brief`, `report`. `--campaign <id>` is needed only when there is more than one campaign.

Experiment: `{id, hypothesis, surface, change{kind, ref, revert_ref}, metric, baseline{value, source, method, as_of}, window{start, end, min_sample}, state, decision{decision, reading, reason, at}}`. States: proposed, baselined, live, measuring, decided. Refused (exit 3, logged):
- two changes in one experiment;
- live with no baseline, or a baseline dated after the change, or an unknown or stale baseline;
- a second live experiment on the same metric and surface;
- a decision before the window end, on a stale or unknown reading, or on a reading from before the change;
- keep or revert below `min_sample` (the reading's `--sample`): only inconclusive;
- revert with no `revert_ref` or no `--undo`. For posts, revert means stop the pattern.

A reading needs source, method and a date (`as_of`). Stale is older than the source lag (`--lag-days`, default 3). Ladder for metrics: public (`github:<owner>/<repo>` stars, or `json:<https url>#field`), the user's CLI or MCP found by `preflight` (the model records the number), a CSV in `inbox/` (columns `metric,date,value[,sample]`), the user typing it. A failed read is `null`, shown as unknown.

Queue item: `{id, experiment, channel, handle, kind post|reply|video|listing_edit, draft_path, sha256, preview, disclosure, rung, why_not_higher, state, expires_at, approved{at, words|pressed-go, hash}, result{url|screenshot, at}}`. States: drafted, shown, approved or dropped or expired, posted or failed. `sha256` covers the draft, and for a video also the media file. Approval stores that hash. `post` recomputes it: a change voids the approval and returns the item to drafted. `queue approve` needs the item shown, the hash, and the user's own words (not the draft text). `--auto-approve` is always refused. The checkpoint screen shows one card per open item (max 5 per cycle); GO approves the kept cards, bound to the hash on the card.

`post` checks: approved and not expired, hash, own handle in the channel config, reply thread listed in the draft (`thread: <url>` lines), disclosure text present when the channel needs it, no duplicate text, the per-channel daily cap and minimum gap, a `--result` url or an existing screenshot path. Rung 4 also needs `--before-shot` and an after screenshot as `--result`. The runtime records the rung and the reason. It does not post.

`tick` order: load, budget gate (refuses under 10% left; unknown budget goes on), read metrics, decide ended experiments (only closes with "inconclusive" when the data is too small; otherwise it lists the experiment for the model), expire queue, open the cycle run (`<campaign>-c<N>`, reused while still open), draft, stop at the checkpoint. The order is in the log line.

`video-check <file> [--thumb png]`: the file exists and `ffprobe` shows a duration above 0; without `ffprobe`, size above 0. `ffmpeg` writes the thumbnail when present. Use it as the check of the render node.

`report` shows every number with source and date, the same-weekday comparison and the prior trend, "moved with" for experiments, "not enough data to decide" where it applies, and the unknowns by name.

## Field brief and research mode

State: `field_brief` (field, sources, fresh, windows, landmark_ok, evidence, safety, review) and `research` (sources, gaps, claims, started, closed). Commands: `brief --run`, `source`, `gap`, `claim`, `research --close [--partial]`. Each command and each refusal is one line in `log.jsonl`. Exit 2 is bad input. Exit 3 is a refused rule.

- Approve (and the checkpoint GO) is refused for medium and large runs without a brief.
- Windows are days per source type. `any` means no limit and allows undated. `default` covers types not listed. A type with no window is refused.
- Once a source call has run (even a refused one), the windows and the landmark rule cannot change.
- Date rules: undated needs a window of `any`. Older than the window needs `--landmark --why` and landmark-ok yes. Landmarks are labeled.
- A gap with fewer than 2 source ids is WEAK. Unknown source ids are refused.
- Close: research plans (a node with role research, or any source recorded) need 5 valid sources and cited gaps and claims, or `--partial`. Nothing is fetched by these commands. Links lose any user info, fragment and secret-looking query values.
