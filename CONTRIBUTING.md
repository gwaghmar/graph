# Contributing

## Setup

No build step. Clone the repo and run the test suite:

```bash
npm test
```

Requires Node.js 18+ and `python3` on PATH — the tests exercise both the installer (`bin/graph-skill.js`) and the runtime (`core/graph.py`).

## Project layout

- `bin/graph-skill.js` — the installer CLI (`npx graph-skill install/uninstall`). Detects the active host and copies the right adapter.
- `core/graph.py` — the shared runtime: run state, caching, quality gates, retries, resume, and report rendering. Framework-free, dependency-free stdlib Python.
- `templates/<host>/` — one adapter per host (Claude Code, Codex, OpenCode, Cursor). Each is a thin prompt/rule file that points at the same `.graph/` runtime.
- `tests/install.test.js` — covers install/uninstall across hosts and the runtime's cache/retry/resume/quality/report behavior.

## Making a change

- If you touch `core/graph.py`, run `npm test` — it spins up a real run and exercises the CLI end to end.
- If you touch an adapter's protocol (`templates/*/commands|skills/graph*`), keep the sizing tiers (S/M/L) and the retry/cache/quality steps consistent across hosts — they intentionally mirror each other.
- Don't add a host-specific feature that the others can't express without discussing it first; the adapters are meant to stay thin and equivalent.
- Keep `core/graph.py` dependency-free — it has to run with a stock `python3`, no pip install step.

## Pull requests

- Tests must pass (`npm test`); CI runs the same suite on Node 18/20/22.
- Describe what changed and why in the PR body — the "why" is what reviewers actually need.
- Small, focused PRs over bundled ones.

## Reporting bugs / requesting features

Use the [issue templates](https://github.com/gwaghmar/graph/issues/new/choose).
