# Contributing

## Setup

No build step. Clone the repo and run the test suite:

```bash
node --test tests/
python3 -m unittest discover -s tests -p "test_*.py"
```

`npm test` runs both. Requires Node.js 18+ and Python 3.9+ on PATH. The Node tests cover the installer (`bin/graph-skill.js`). The Python tests cover the runtime (`skills/graph/scripts/graph.py`) and `preflight.py`.

## Project layout

- `bin/graph-skill.js`: the installer CLI. Copies the skill folder to `.claude/skills/graph` and `.agents/skills/graph`. No dependencies.
- `skills/graph/SKILL.md` and `references/protocol.md`: the skill the agent reads.
- `skills/graph/scripts/graph.py`: the runtime. Run state, rule enforcement, checks, reports. Stdlib Python 3.9+ only.
- `skills/graph/scripts/preflight.py` and `jobs.json`: the environment scan and job-to-tool picker.
- `.claude-plugin/`: the Claude Code plugin and marketplace files. Keep `plugin.json` version equal to `package.json`.
- `bench/`: benchmark scaffold. Not run.
- `tests/`: `install.test.js`, `test_graph.py`, `test_preflight.py`.

## Making a change

- Rules go in `graph.py` code, not only in SKILL.md. Add a test for each refusal.
- Keep Python portable: no `shell=True` (except the user check command), use `shutil.which`, `pathlib`, and `encoding='utf-8'`.
- Every command in the argparse definitions must be listed in SKILL.md or `protocol.md`.
- Keep the runtime free of third-party packages.

## Pull requests

- Tests must pass (`npm test`). CI runs both suites.
- Describe what changed and why in the PR body: the "why" is what reviewers actually need.
- Small, focused PRs over bundled ones.

## Reporting bugs / requesting features

Use the [issue templates](https://github.com/gwaghmar/graph/issues/new/choose).
