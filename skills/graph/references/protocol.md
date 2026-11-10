# Graph protocol detail

## Commands
- `init "<task>" [--host H] [--max-retries N]` creates a run and prints its id.
- `node <run> <id> --role R --status S [--depends-on ...] [--files ...]` records a node.
- `next <run>` lists ready nodes, or `none`.
- `validate <run>` rejects unknown, self and cyclic dependencies.
- `quality <run> [--command C]` runs checks and records the result.
- `retry-plan <run> --include-dependents` lists nodes to rerun.
- `finish <run> --status complete|failed`, `summary`, `tree`, `status`, `resume`.
- `preflight --needs a,b [--refresh] [--json]` scans the environment.

## Statuses
pending, running, retrying, complete, cached, failed, skipped.

## Exit codes
0 ok. 2 bad input. 3 refused by a rule. A refusal line says why and what to do.

## Refusals
- A node cannot start while its dependencies are unfinished.
- A node cannot start after it used all its attempts.
- A run cannot finish as complete while nodes are open or the last quality run failed.

## Plan fields
id, role, depends_on, files. Keep nodes small. Split by independent file scope.
