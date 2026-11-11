# Orchestration protocol reference

## Agent spec (input to the Agent Factory)

```json
{
  "name": "api",                       // short kebab-case id; becomes orch-<run>-api for run-scoped agents
  "extends": "backend",                // optional seed role from templates/roles.json (or your custom roles.json)
  "role": "Backend Engineer",          // display name
  "description": "One or two sentences: what it does (also used by Claude to route work).",
  "objective": "The single outcome this agent is responsible for.",
  "responsibilities": ["concrete duty", "..."],          // required, non-empty
  "expertise": ["..."],
  "tools": ["Read", "Grep", "Glob", "Write", "Edit", "Bash"],   // least privilege; omit = inherit all
  "disallowedTools": ["WebFetch"],
  "model": "sonnet",                   // sonnet | opus | haiku | fable | inherit | claude-* id
  "permissionMode": "acceptEdits",     // optional; bypassPermissions blocked unless config allows
  "maxTurns": 60, "effort": "high", "isolation": "worktree", "color": "blue",
  "inputs": ["contracts/openapi.yaml"],
  "outputs": ["backend/** source", "backend tests"],     // required
  "depends_on": ["architect"],         // other agents in the team whose output it builds on
  "owns": ["backend/**"],              // globs it may write (enforced by the PreToolUse hook)
  "must_not_modify": ["contracts/**"], // globs it must never write (enforced)
  "constraints": ["no new dependencies without listing them in the report"],
  "validation": ["pytest backend passes", "all contract endpoints implemented"],  // required
  "parent": "backend-lead",            // optional: REAL nested delegation (that agent spawns this one)
  "group": "Backend Team"              // optional: visual grouping only
}
```

The spec becomes `.claude/agents/<name>.md`: native frontmatter (`name`, `description`, `tools`, `model`, …) plus a generated system prompt from `templates/agent.md.tmpl`.

Scopes:
- `run` (default): temporary, prefixed, removed by `run close`.
- `project`: `.claude/agents/`, reusable.
- `user`: `~/.claude/agents/`, reusable everywhere.

The factory never overwrites a file it did not generate. To regenerate one of its own files, pass `--force`.

## Task (in the plan)

```json
{"id": "T3", "title": "Implement task CRUD API", "agent": "api",
 "depends_on": ["T1"], "owns": ["backend/app/tasks/**"],
 "description": "what to do, precisely", "acceptance": ["pytest -q backend passes"], "weight": 2}
```

- `agent` is a team member's name, or `orchestrator` for work you do yourself (integration, final checks).
- `owns` narrows the agent's ownership for this task. Leave it out to use the agent's.
- `weight` is used for the critical path.

## Plan block

Embed this in the plan markdown:

````markdown
```json orch-plan
{"title": "Task manager", "team": {"agents": [ ...specs ]}, "tasks": [ ...tasks ]}
```
````

`ORCH plan --check FILE` rejects any of the following:
- unknown agents or dependencies
- duplicate task ids or titles
- dependency cycles
- teams larger than `max_agents`
- invalid specs
- overlapping ownership inside one wave

It also computes the waves (topological levels, split by `max_parallel`) and the critical path.

## Delegation message template (the Agent tool `prompt`)

```
Task <ID>: <title>   (orchestration run <run-id>)

Context:
- <what already exists, decisions made, relevant files>
- Contracts: <paths>. Treat them as the source of truth; report mismatches instead of improvising.

Do:
<precise description>

Acceptance criteria (verify each, and report evidence):
- <criterion>

Boundaries: you may modify only <globs>. Anything else goes in `issues`.
<Retry only: previous attempt failed because: <feedback>. Previous report: <summary>.>

End your final message with the ORCH_REPORT block (task "<ID>").
```

## ORCH_REPORT (every agent's final message)

````
```ORCH_REPORT
{"status": "done|partial|failed|blocked", "task": "T3",
 "completed": ["..."], "files_changed": ["backend/app/tasks/routes.py"],
 "dependencies": ["contracts/openapi.yaml v1 — matched"],
 "issues": ["UI expects `dueDate`, contract says `due_date`: owner ui"],
 "remaining": [], "validation": [{"check": "pytest -q backend", "result": "pass", "evidence": "14 passed"}]}
```
````

Pass the report to `ORCH task done <id> --report -`. It checks:
- that the report has all 8 fields
- that the reported status is done
- that every file in `files_changed` is inside the agent's ownership

Warnings mean you must look before you accept the work.

## Run state on disk

```
.claude/orchestrator/
├── active                    # id of the active run
├── registry.json             # generated agents: scope, run, spec, status, history
├── config.json               # optional project overrides of config/defaults.json
├── roles.json                # optional project-specific seed roles
└── runs/<run-id>/
    ├── run.json              # team, tasks, statuses, attempts, reports (atomic writes)
    ├── events.jsonl          # SubagentStart/Stop (from hooks), task events, ownership denials
    ├── plan.md               # copy of the approved plan
    └── report.md             # written by status --full / run close
~/.claude/orchestrator/       # user-level: registry.json, config.json, roles.json, lessons.jsonl
```

## CLI reference

| Command | Purpose |
|---|---|
| `env [--json]` | environment and capability report with fallbacks |
| `init` | create `.claude/orchestrator/` and `.claude/agents/` |
| `plan --check FILE [--mermaid] [--json]` | validate a plan; tree, waves, critical path |
| `run init --from-plan FILE [--id ID]` | create agents and state; set active |
| `run list` / `run activate --run ID` / `run close [--status S] [--keep-agents]` | run lifecycle |
| `team create --spec FILE [--run ID]` | add agents to a run mid-flight |
| `agent create --spec FILE [--scope run\|project\|user] [--force]` | single or batch agent creation |
| `agent validate [NAME\|PATH...] [--all]` | validate any agent file (generated or hand-written) |
| `agent list [--json]` / `agent remove NAME` / `agent prompt NAME` / `agent promote NAME --to user [--as N]` | registry ops |
| `task start\|done\|fail\|retry\|skip ID` / `task add ID --title T --agent A [--depends-on ..]` | task state |
| `status [--full] [--json]` | execution report |
| `stats` | cross-run failures per role, agent lint |
| `lessons add --tag T "text"` / `lessons list [--tag T]` | reusable lessons |
| `hook prompt\|session\|pretool\|subagent` | hook entrypoints (stdin JSON from Claude Code) |
