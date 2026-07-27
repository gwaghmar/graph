# Graph protocol schema

Plan node fields: `id`, `role`, `description`, `depends_on`, `file_scope`, `acceptance`, `tests`, `risk`.

Allowed statuses: `pending`, `running`, `retrying`, `passed`, `complete`, `cached`, `failed`, `skipped`, `blocked`. The runtime counts `passed`, `complete`, `cached`, and `skipped` as terminal success, `failed` as terminal failure, and treats `blocked` as non-terminal (waiting on a dependency or a human decision).

Plan review passes only when requirements are mapped, dependencies are valid, scopes do not conflict, validation is executable, and risky operations have rollback.

Implementation review severity: critical, high, medium, low. A run fails on unresolved critical/high issues, failing required tests, or unmet acceptance criteria.
