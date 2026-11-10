# graph-skill benchmark (scaffold)

**This benchmark has NOT been run. Running it costs money. Nothing here reports results.**

## Question
Does graph-skill beat a plain agent or the native Claude Code Workflow on cost, speed and correctness?

## Arms
1. **plain**: one Claude Code agent, task prompt only.
2. **workflow**: native Claude Code Workflow, script written by Claude for the task.
3. **graph**: graph-skill (`/graph`), including `export-workflow` where available.

## Fairness rules
- Same model, same effort level, same Claude Code version in every arm.
- Fresh copy of the task folder per run. No shared state or memory.
- Same task text (the `PROMPT` section of each task file) in every arm. Hidden tests are never shown to the agent.
- Run each task 3 times per arm. Report the median.
- Same machine, same network, no other load.

## Tasks
Five fixed tasks in `tasks/01` to `tasks/05`. Each has a prompt, starter files, and a hidden test suite kept outside the agent's folder (`hidden_tests/`, to be written before the first run).

## Metrics
| Metric | How |
|---|---|
| Total tokens | Sum of input + output + cache tokens, from `/cost` or the usage log |
| Wall time | Start of prompt to final agent message |
| Pass rate | Hidden test suite: tests passed / tests total |

## Results
Copy `results-template.md`, fill one row per run.
