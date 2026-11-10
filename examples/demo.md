# Demo

```text
/graph Add rate limiting to the API, include tests, and preserve current clients.
```

What the agent does:
1. `preflight --needs git,node` scans the environment.
2. `init` and `node` record the plan. `validate` rejects bad dependencies.
3. `next` lists ready nodes. Independent nodes can run in parallel on hosts with subagents.
4. `node ... --status complete` runs each node check. A failing check refuses completion.
5. `brief` gives a failed node a fresh-context retry. Retries are capped.
6. `quality` runs lint, type and test checks. `finish` closes the run. `summary` prints the recap.
