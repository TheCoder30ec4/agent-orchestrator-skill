# agent-orchestrator

A Claude Code plugin that turns Claude into a multi-agent orchestrator. You describe a task. Claude sizes the work, designs the **smallest effective team of specialized subagents for that task**, and writes their native agent definitions at runtime. It then plans a dependency graph with parallel waves, delegates through real Agent tool calls, tracks resumable state, validates every deliverable, and integrates the results.

There is no fixed team. A CSV script gets zero or one helper. A full-stack app gets an architect plus API, UI, schema, DevOps and QA agents. A research report gets researchers, a fact-checker and a writer. When no seed role fits, Claude invents a new specialization.

---

## 1. What it does

| You get | How |
|---|---|
| Task sizing: direct, one specialist, or a team | Skill workflow (`orchestrate`) |
| Agents generated per task, as native `.claude/agents/*.md` files | `orch.py` Agent Factory, from JSON specs written by Claude |
| Execution plan: tasks, owners, dependencies, parallel waves, critical path | `orch-plan` block, checked by `orch.py plan --check` |
| Real parallel delegation | Several Agent tool calls in one message per wave |
| File ownership boundaries | Globs per agent; `PreToolUse` hook denies writes outside them |
| Standard reporting | Every agent ends with an `ORCH_REPORT` JSON block |
| Persistent, resumable state and reports | `.claude/orchestrator/runs/<id>/` |
| Plan Mode activation | `UserPromptSubmit` hook (checks `permission_mode == "plan"`) |
| Proof that spawns were real | `SubagentStart`/`SubagentStop` hooks log every spawn |
| Self-improvement | `stats` and `lessons`; agent promotion only with your approval |

## 2. Architecture

```
User request
   │
   ▼
Main session = Master Orchestrator  (skills/orchestrate/SKILL.md)
   │  0 orient   ── orch.py env / lessons / resume check
   │  1 size     ── direct │ 1 specialist │ team
   │  2 understand + architect  (contracts first)
   │  3 design team  ── JSON agent specs (extends seed roles or invents new ones)
   │  4 plan  ── orch-plan block → orch.py plan --check (DAG, waves, ownership, limits)
   │        ── Plan Mode: written to the plan file, wait for approval
   │  5 execute ── orch.py run init → .claude/agents/orch-<run>-*.md (native subagents)
   │        ── per wave: task start → parallel Agent calls → ORCH_REPORT → verify → task done/fail/retry
   │  6 integrate + validate ── tests, e2e, fix tasks routed to the owners
   ▼
Final report  (orch.py status --full → report.md, run close)
```

How the pieces map onto Claude Code primitives:

| Piece | Implemented as |
|---|---|
| **Skill** (`orchestrate`) | Workflow and judgement: sizing, decomposition, team design, validation |
| **Subagents** | Runtime-generated `.claude/agents/*.md`, invoked by `subagent_type`. Isolated contexts, own tools and models |
| **Agent teams** (experimental) | Used when `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is set and the session is interactive; otherwise subagents |
| **Hooks** | Plan Mode trigger, resume notice, ownership and core protection, spawn logging |
| **Scripts** (`orch.py`) | Deterministic work only: validation, file generation, registry, DAG maths, state, reports |
| **MCP** | Optional. Give an agent `mcp__server__tool` names in `tools`; the validator accepts them |

The orchestrator is the **main session**, not a separate agent file. Only the main thread can talk to you, approve plans and lead a team.

The hierarchy works as follows:
- Visual grouping (`group`) is just a label.
- Real nesting (`parent`) only happens when an agent itself spawns sub-agents. It is capped by `max_depth` and the platform's spawn depth.

## 3. Dynamic agent generation

Claude writes one spec per agent. The full schema is in `plugin/skills/orchestrate/reference/protocol.md`.

```json
{"name": "api", "extends": "backend", "owns": ["backend/**"], "must_not_modify": ["contracts/**"],
 "depends_on": ["architect"], "model": "sonnet",
 "validation": ["pytest -q backend passes", "every contract endpoint implemented"]}
```

The Agent Factory (`orch.py agent create`, `team create` and `run init`) then does the following:
- Merges the spec with the seed role it `extends`, if any, from `templates/roles.json`. Your own roles can override seeds.
- Validates the spec:
  - Required fields and list types.
  - Kebab-case name.
  - Known tools (MCP tools are allowed).
  - Model, permissionMode, effort, color and isolation values.
  - `bypassPermissions` is blocked unless config allows it.
  - Ownership conflicts.
  - Vague wording ("various", "etc", "help with").
  - Team size and nesting depth.
- Renders native frontmatter (`name`, `description`, `tools`, `model`, …) plus a system prompt containing:
  - objective, responsibilities, expertise
  - inputs and outputs
  - ownership boundaries
  - dependencies and constraints
  - validation criteria and the mandatory report format
- Writes `.claude/agents/orch-<run>-<name>.md` (run scope), `.claude/agents/<name>.md` (project) or `~/.claude/agents/<name>.md` (user). It **refuses to overwrite** any file it did not generate, and its own files only with `--force`.
- Registers the agent in `.claude/orchestrator/registry.json` (or the user registry) with status and history.

The docs say agent files added mid-session are hot-reloaded within seconds. **In live tests the reload was asynchronous and slow:**
- spawns made 0–35 s after writing the file returned "Agent type … not found";
- the same agent loaded later in the session.

So the skill does this:
- On "not found", it delegates the task straight away to `general-purpose`, with `orch.py agent prompt <name>` (an `<agent-definition name="…">` block) at the top of the prompt.
- It uses the native `subagent_type` again in later waves.

Ownership is still enforced for these fallback agents. The PreToolUse hook reads the subagent's first prompt from `<session transcript>/subagents/agent-<agent_id>.jsonl` and finds the generated agent name in it. That path layout was observed, not documented, so this is best-effort: if it changes, fallback agents become unrestricted. The `task done` ownership audit remains as a second check.

## 4. How agents communicate

Agents don't talk to each other directly. The orchestrator is the hub.

1. **Contracts.** Shared interfaces are written to files (e.g. `contracts/`) by an early task. Parallel agents build against them.
2. **Delegation message.** For each task, the Agent tool prompt contains the task id, context, contract paths, acceptance criteria, ownership reminder and retry feedback.
3. **ORCH_REPORT.** Each agent's final message ends with JSON containing `status`, `task`, `completed`, `files_changed`, `dependencies`, `issues`, `remaining` and `validation`.
4. **Verification.** The orchestrator runs the checks itself. Then `orch.py task done --report -` stores the report, and warns about missing fields, non-done status, and files outside the agent's ownership.
5. **Change requests.** An agent that needs a change outside its boundary lists it in `issues`. The orchestrator routes it to the owner with `orch.py task add`.
6. **Agent teams** (when enabled). Teammates can also message each other through `SendMessage`; state is still mirrored in `orch.py task`.

## 5. Plan Mode integration

There is no "Plan Mode entered" hook event in Claude Code. What exists is the `permission_mode` field on every hook input. The plugin's `UserPromptSubmit` hook checks it, and when it is `"plan"` it injects context telling Claude to use the `agent-orchestrator:orchestrate` workflow. This happens on every prompt submitted in Plan Mode. Verified live: `claude -p --permission-mode plan` received the injected context.

Limitations and controls:
- The hook fires when you **submit a prompt** while in Plan Mode, not at the moment you toggle into it.
- To skip the hook for one prompt, add `[no-orchestrate]` to it.
- To disable it, set `"plan_mode_trigger": false` in a config file (see §7).
- In Plan Mode Claude writes only the plan file: the team specs and tasks as an `orch-plan` block. Agents are created only after you approve (ExitPlanMode).
- Outside Plan Mode, the same hook adds a short hint when a prompt looks like multi-part work (at least `suggest_min_words` words plus a keyword such as build, research, app, pipeline). The hint tells Claude to load the skill, and the skill's sizing step keeps small tasks direct. You can also call it explicitly with `/agent-orchestrator:orchestrate <task>`. Set `"suggest_outside_plan_mode": false` to turn the hint off.

### Authorizing agent spawning (your opt-in)

Claude Code's built-in rule is to spawn subagents only when the user asks. Live testing showed that this applies even with the skill loaded: Claude planned a team, then declined to spawn it. The skill therefore spawns a team only when you have asked, in one of these ways:

1. you ask for agents or a team in the conversation;
2. you approve a plan that contains the team (Plan Mode approval counts);
3. **standing opt-in:** your own CLAUDE.md says so. Add it with:

```bash
python3 install.py authorize --scope user          # appends one marked line to ~/.claude/CLAUDE.md
python3 install.py authorize --scope project --project .   # or only for one repo
python3 install.py unauthorize --scope user        # revoke
```

The line it adds (visible, editable, removable):
`<!-- agent-orchestrator:authorize --> When the agent-orchestrator `orchestrate` skill sizes a task as a team, you may spawn the subagents it plans without asking me first.`

The plugin never grants itself this permission. Without the line, outside Plan Mode the skill shows the team and asks before spawning.

## 6. Installation

Requirements:
- Claude Code (tested on 2.1.316 and 2.1.322)
- Python 3.8+ on PATH as `python3`
- No pip dependencies

**Option A: plugin (recommended; CLI, VS Code and Desktop share it)**

```bash
git clone <this repo> ~/agent-orchestrator && cd ~/agent-orchestrator
python3 install.py plugin              # = claude plugin marketplace add . && claude plugin install agent-orchestrator@agent-orchestrator
# or by hand inside Claude Code:
#   /plugin marketplace add ~/agent-orchestrator
#   /plugin install agent-orchestrator@agent-orchestrator
```

Restart Claude Code, or run `/reload-plugins`.

To try it without installing:

```bash
claude --plugin-dir ~/agent-orchestrator/plugin
```

**Option B: plain skill plus hooks (no plugin system)**

```bash
python3 install.py skill --scope user                 # ~/.claude/skills/orchestrate + hooks merged into ~/.claude/settings.json
python3 install.py skill --scope project --project .  # .claude/skills/orchestrate + .claude/settings.json
python3 install.py uninstall --scope user
```

Existing settings are backed up (`settings.json.bak-<timestamp>`) and preserved. Only our hook entries are added or removed.

**Option C: claude.ai (skill only).** Run `python3 install.py zip`, then upload `dist/orchestrate-skill.zip` under Customize › Skills. See §11 for the limitations.

## 7. Global configuration

Configuration files are merged in this order, later winning:
1. `plugin/skills/orchestrate/config/defaults.json`
2. `~/.claude/orchestrator/config.json` (global)
3. `<project>/.claude/orchestrator/config.json` (project)

```json
{
  "max_agents": 12, "max_parallel": 5, "max_depth": 2, "max_retries": 2,
  "require_approval_above_agents": 3,
  "default_model": "inherit",
  "temp_agent_prefix": "orch",
  "allow_bypass_permissions": false,
  "plan_mode_trigger": true, "enforce_ownership": true, "protect_core": true,
  "suggest_outside_plan_mode": true, "suggest_min_words": 10
}
```

Global reusable agents live in `~/.claude/agents/` (`--scope user`, or `agent promote --to user`). Global custom role templates go in `~/.claude/orchestrator/roles.json`. Lessons are stored in `~/.claude/orchestrator/lessons.jsonl`.

## 8. Per-project configuration

- `<project>/.claude/orchestrator/config.json`: limits for this repository, e.g. `{"max_agents": 4}`.
- `<project>/.claude/orchestrator/roles.json`: project-specific seed roles.
- `python3 install.py plugin --scope project` records the plugin in `.claude/settings.json`, so the whole team gets it.
- Run `orch.py init` once to create `.claude/agents/` before your first session. Generated agents are then hot-loaded rather than going through the fallback.
- Add `.claude/orchestrator/runs/` to `.gitignore` if you don't want run history in git.

## 9. Using it from the CLI

```bash
claude                                   # then Shift+Tab to Plan Mode and describe the project
claude --permission-mode plan            # start in Plan Mode
> /agent-orchestrator:orchestrate Build a task manager with auth, DB, React UI, Docker and tests
> /agent-orchestrator:orchestrate status # or: resume
```

Headless or CI use works with subagents. Agent teams are not available in `-p`.

```bash
claude -p "Build … This is a non-interactive run: proceed without questions." \
  --permission-mode acceptEdits --allowedTools Bash Read Write Edit Glob Grep Agent Skill
```

The bookkeeping CLI is useful on its own as well:

```bash
ORCH="python3 plugin/skills/orchestrate/scripts/orch.py"
$ORCH env                          # capability report for this environment
$ORCH plan --check plan.md --mermaid
$ORCH status --full                # execution report (also written to report.md)
$ORCH agent validate --all         # lint every agent file, including hand-written ones
$ORCH stats                        # roles that fail repeatedly, vague definitions
```

Example `status` output:

```
PROJECT: Task app   (run taskapp-1f3a, ACTIVE)
Progress: 3/6 tasks (50%)   Verified subagent spawns (hook log): 4 started, 3 finished
| Agent | Responsibility | Status |
| orch-taskapp-1f3a-architect | System Architect | COMPLETED |
| orch-taskapp-1f3a-api | Backend Engineer | RUNNING |
| orch-taskapp-1f3a-ui | Frontend Engineer | RUNNING |
| orch-taskapp-1f3a-qa | QA Engineer | PENDING |
```

## 10. Using it from VS Code

The VS Code extension shares `~/.claude/settings.json`, plugins, skills and hooks with the CLI, so installing once covers both.

- Use the mode selector (or `/plan`) for Plan Mode. The plan opens as a Markdown document, including the team tree and the `orch-plan` block.
- Subagents appear as progress rows. Hooks can be inspected under Customize › Hooks.
- Agent teams run in-process only. Split panes aren't supported in the VS Code terminal.

## 11. Claude Desktop and claude.ai

- **Desktop (Code tab):** shares CLAUDE.md, skills, hooks, plugins and settings with the CLI. Everything works except agent teams, which aren't available there; it uses subagents instead.
- **claude.ai (uploaded skill ZIP):**
  - No subagents, hooks or plugins.
  - The skill still runs its phases, but it executes the plan itself role by role and says that no agents were spawned.
  - Run state lives only in the sandbox.
- Run `orch.py env` to see the live capability table and fallbacks. `reference/environments.md` has the full matrix.

## 12. Custom agent templates

Add roles to `~/.claude/orchestrator/roles.json` (global) or `.claude/orchestrator/roles.json` (project). They use the same fields as a spec, minus `name`:

```json
{
  "payments-engineer": {
    "role": "Payments Engineer",
    "description": "Implements subscription billing flows against a payment provider using test-mode keys only.",
    "objective": "Correct, idempotent billing flows with webhook handling.",
    "responsibilities": ["Implement checkout and subscription lifecycle", "Verify webhook signatures", "Write tests with mocked provider"],
    "tools": ["Read", "Grep", "Glob", "Write", "Edit", "Bash"],
    "model": "sonnet",
    "constraints": ["test-mode keys only; never call live endpoints"],
    "outputs": ["billing module", "tests"],
    "validation": ["tests pass", "webhook replay is idempotent"]
  }
}
```

Specs can then use `"extends": "payments-engineer"`.

To keep a good generated agent, run `orch.py agent promote orch-<run>-api --to user --as api-dev`. The skill only does this when you say yes.

To edit the generated prompt structure for every agent, change `templates/agent.md.tmpl`.

## 13. Execution limits and safety

| Limit | Enforced by |
|---|---|
| `max_agents` | `team create`, `run init`, `plan --check` |
| `max_parallel` | waves are split |
| `max_depth` | nesting via `parent`; also capped by `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` |
| `max_retries` | `task start` and `task retry` refuse beyond 1 + max_retries attempts; then escalate to you |
| Duplicate tasks | duplicate ids or normalized titles are rejected |
| Dependencies | cycles and unknown deps are rejected; `task start` refuses unmet deps |
| File ownership | hook denies Write/Edit outside `owns` or inside `must_not_modify`; `task done` audits `files_changed` |
| No overwriting | the factory never touches files it didn't generate |
| Core protection | hook asks before any edit to the orchestrator's own files |
| Permissions | `bypassPermissions` agents are blocked by default; destructive or external actions need your approval |
| Isolation | `isolation: "worktree"` for risky tasks in git repos |

Token and context usage: every subagent has its own context window. Set `maxTurns` and `effort` per agent spec to bound cost. In headless runs, use `--max-budget-usd`.

## 14. Troubleshooting

| Symptom | Fix |
|---|---|
| Plan Mode doesn't trigger the workflow | Check `orch.py env` → "Plan Mode auto-trigger hook". Make sure the plugin is enabled (`/plugin`), or reinstall the hooks with `install.py skill`. The hook fires on prompt submit, not on mode toggle. |
| `Agent type 'orch-…' not found` | Expected right after `run init`: Claude Code loads new agent files asynchronously. The skill falls back to general-purpose (ownership is still enforced) and uses the native agents once they have loaded. A brand-new `.claude/agents/` directory may need a restart; run `orch.py init` once per project. |
| Claude plans a team but does it alone | It has no spawning authorization. Approve the plan, ask for a team, or add the opt-in line with `install.py authorize` (§5). |
| Need to see what the hooks receive | Start Claude with `ORCH_HOOK_LOG=/tmp/orch-hooks.jsonl` set. Every hook invocation and its payload is appended to that file. |
| `task start` refuses | Read the message: dependency not done, already done (resume protection), or retry limit reached. |
| Write denied by "agent-orchestrator" | Ownership is working as designed. Re-split `owns`, or route the change to the owner. |
| `run X is still active` | Resume it (`/agent-orchestrator:orchestrate resume`), or `orch.py run close --status aborted`. |
| Invalid agent file | `orch.py agent validate <name or path>` |
| Hook errors | Hooks never block on internal errors; they print to stderr. Run `echo '{}' \| python3 …/orch.py hook session` to debug. |
| Python not found (Windows) | Make sure `python3` is on PATH, or edit the commands in `hooks/hooks.json` to use `py -3`. |

## 15. Extending

- **New roles:** `roles.json` (§12). **New agent prompt structure:** `templates/agent.md.tmpl`.
- **New CLI commands:** `orch.py` is a single stdlib module. Add a `cmd_*` function and a subparser, then add a test in `tests/test_orch.py`.
- **New hooks:** add a `hook_<name>` function and register it in `plugin/hooks/hooks.json`. Keep hooks fail-open (exit 0).
- **MCP:** give agents MCP tools by listing `mcp__<server>__<tool>` in their `tools`.
- **Native task tools:** if your model has `TaskCreate`/`TaskList`, you can mirror tasks there; `orch.py` stays the persistent source of truth.

---

## Testing

```bash
python3 -m unittest discover tests                 # 37 unit tests: factory, validation, plans, state, hooks, packaging
claude plugin validate plugin
python3 tests/e2e/run_e2e.py T1 --workdir /tmp/e2e # live acceptance tests T1..T6 (real tokens; budget-capped)
```

The E2E tests only count evidence from the stream-json transcript and the files on disk:
- Agent tool_use events
- hook-logged SubagentStart events
- run state
- scenario commands

## Layout

```
.claude-plugin/marketplace.json     local marketplace
plugin/.claude-plugin/plugin.json
plugin/hooks/hooks.json             UserPromptSubmit, SessionStart, PreToolUse, SubagentStart/Stop
plugin/skills/orchestrate/
  SKILL.md                          the orchestrator workflow
  reference/{protocol,environments}.md
  templates/{agent.md.tmpl,roles.json}
  config/defaults.json
  scripts/orch.py                   agent factory, registry, planner, task manager, env check, hooks
install.py                          plugin / skill+hooks / zip / uninstall
tests/test_orch.py, tests/e2e/
```

The 9 separate scripts from the original proposal (create_agent, create_team, register, validate, list, remove, generate_plan, task_manager, validate_environment) are subcommands of one `orch.py`. They share one parser, one registry and one state format, which avoids nine copies of the same plumbing.
