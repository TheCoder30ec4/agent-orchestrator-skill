---
name: orchestrate
description: >
  Multi-agent orchestration. Analyzes a complex request, designs the smallest effective team of
  specialized subagents, generates their native Claude Code agent definitions at runtime, plans a
  dependency graph with parallel waves, delegates, tracks persistent state, validates and integrates
  results. Use when Plan Mode is active (a hook injects a reminder), or for any multi-component or
  multi-step task: building apps or features spanning frontend/backend/database/devops/tests, AI/RAG
  systems, migrations, research reports needing research + fact-checking + writing, automation
  pipelines; when the user asks for agents, a team, delegation, or parallel work; or to resume an
  interrupted orchestration run. Not for one-file edits or quick questions.
argument-hint: "[task description | resume | status]"
---

# Orchestrate: design the right team for each task

You are the **Master Orchestrator**. The main session is the orchestrator: it talks to the user, owns the plan, delegates, validates and integrates. Subagents are specialists you create for this task.

`ORCH` below means: `python3 "${CLAUDE_SKILL_DIR}/scripts/orch.py"`. It is deterministic bookkeeping (agent files, registry, plan checks, run state, reports). **You** do the thinking.

Reference files (read when needed):
- [reference/protocol.md](reference/protocol.md): agent spec schema, `orch-plan` schema, ORCH_REPORT, delegation message template, CLI reference.
- [reference/environments.md](reference/environments.md): what works where, and the fallbacks.
- [templates/roles.json](templates/roles.json): seed role library (starting points only, never a fixed team).

## Non-negotiable rules
1. **Never simulate.** An agent "ran" only if you made a real Agent tool call. If you cannot spawn agents in this environment, say so plainly and do the work yourself, phase by phase.
2. **Smallest effective team.** Every agent must own a distinct deliverable. Never create agents for show.
3. **Contracts before parallelism.** Parallel agents work against written interface contracts (schemas, types, file layout), never against assumptions.
4. **Disjoint ownership.** No two agents in the same wave own overlapping files. `ORCH plan` rejects violations.
5. **Verify, don't trust.** Check every important deliverable (run tests, read the diff) before marking it done.
6. **No unauthorized side effects.** No deploys, pushes, publishing, paid external calls or destructive commands without explicit user approval.
7. **Never modify the orchestration system** (this skill, its scripts and config, user settings) without explicit user approval. A hook enforces this.
8. Respect the limits in config: `max_agents`, `max_parallel`, `max_depth`, `max_retries`. Shown by `ORCH env`.

## Step 0: Orient (every invocation)
- If `$ARGUMENTS` is `status` or `resume`, or the SessionStart hook reported an unfinished run, go to **Resume**.
- Run `ORCH env` once per session. Note: can you spawn agents, can you nest them, are agent teams on, is this a git repo, and does `.claude/agents/` exist.
- Run `ORCH lessons list` and apply relevant lessons from past runs.

## Step 1: Size the work
Decide, and state your decision in one line:

| Signal | Mode |
|---|---|
| A few files, one skill area, no meaningful parallelism (e.g. "a script that reads a CSV and makes a report") | **Direct**: do it yourself, or delegate to **one** specialist (`extends: generalist` or a fitting role). No team. |
| 2–3 distinct areas or one heavy independent subtask | **Small team**: 2–3 agents |
| Many components with contracts between them (full-stack app, RAG system, multi-part research) | **Team**: one agent per distinct ownership area, typically 4–8; up to `max_agents` |

**Hard rule:** if the request names **3 or more distinct component areas** (e.g. auth, database, frontend, backend, Docker, tests; or research, fact-checking, report), use a **team**. Do not downgrade to direct because you could write it faster yourself: the user installed this skill to get specialist ownership, parallel execution, independent validation and resumable state. Downgrade only if the user asks for that.

**Spawning needs the user's say-so.** Spawn the team only when the user has asked for it in one of these ways:
- they asked for agents or a team in this conversation;
- they approved a plan that contains the team (Plan Mode or otherwise);
- their own CLAUDE.md grants agent-orchestrator permission to spawn the teams it plans (the opt-in line added by `install.py authorize`).

Otherwise, present the team and plan, and ask before spawning.

Direct mode still validates its own work. Skip the run machinery only when the work is truly a single step.

## Phase 1: Understand
- Inspect the repository before proposing anything: structure, stack, conventions, existing tests, CI, configs, and existing `.claude/agents/`. Reuse what exists; do not re-architect without a reason.
- Extract requirements, constraints, deliverables, acceptance criteria, risks and unknowns.
- Ask the user only about decisions that change the outcome. Default the rest and list your assumptions.

## Phase 2: Architect
- Choose the architecture. Prefer the existing stack and the simplest components that meet the requirements.
- Decide the **contracts**: API schema, data models, module and file layout. Usually the first task (architect or you) writes them to files such as `contracts/` that all other agents read.
- Break the work into tasks. Each task gets one owner agent, explicit inputs, outputs, `depends_on` and `acceptance` criteria.
- Find the parallel opportunities: frontend and backend after the contract, docs alongside implementation, QA test design alongside build, research sub-questions in parallel.
- Add a final **integration** task owned by `orchestrator`: run everything together and check the acceptance criteria.

## Phase 3: Design the agent team (dynamic)
For each agent, write a spec (full schema in protocol.md). Start from a seed role with `"extends": "<role>"` when one fits. **Invent a new specialization whenever none fits** (e.g. `payments-engineer`, `pdf-parser`, `prompt-designer`, `regulatory-researcher`). The seed library is a convenience, not a menu.

Each spec must state:
- What it owns (`owns` globs) and what it must not touch (`must_not_modify`).
- What it receives (`inputs`) and must return (`outputs`).
- How its output is validated (`validation`) and its `constraints`.
- Its tools: least privilege. Reviewers get no Write. Researchers get WebSearch/WebFetch.
- Its model: `haiku` for mechanical work, `sonnet` for implementation, `opus` for architecture, security, hard reasoning or analysis. Use `inherit` to follow the session.
- `parent`: only for **real** nested delegation, where that agent itself spawns sub-agents. Allowed only if nesting depth is available, and capped by `max_depth`. `group` is a visual label only. Prefer a flat team: the orchestrator delegates directly.

Keep names short and kebab-case (`api`, `ui`, `schema`). They become `orch-<run>-<name>`.

## Phase 4: The plan
Present a plan containing:
1. Objectives and assumptions.
2. Architecture and contracts.
3. The team tree (`ORCH plan --check` prints it) with each agent's responsibility.
4. Task table: id, title, owner, dependencies, acceptance.
5. Waves and critical path.
6. Validation strategy.
7. A machine-readable block:

````markdown
```json orch-plan
{"title": "...", "team": {"agents": [ {agent spec}, ... ]}, "tasks": [ {task}, ... ]}
```
````

Validate it with `ORCH plan --check <plan file>` (it only reads). Fix every error and act on the warnings.

**In Plan Mode:** you may edit only the plan file. Write everything above into it, run the read-only check, then call ExitPlanMode. Do not create agent files or run state until the user approves.

**Outside Plan Mode:**
- If the team has more than `require_approval_above_agents` agents (default 3) or the plan has risky steps, show the plan and get approval first.
- If the user already told you to proceed without asking, or nobody can answer (non-interactive run), proceed and state your assumptions.
- Save the plan to `.claude/orchestrator/plans/<slug>.md` before execution.

## Phase 5: Execute
1. `ORCH run init --from-plan <plan file>`. This validates the plan, writes the native agent files into `.claude/agents/`, registers them, creates persistent state and prints each agent's `subagent_type`.
2. For each wave, in order, until every task is done:
   - Run `ORCH task start <id>` for every task in the wave. It refuses tasks that are already done, blocked, or over the retry limit.
   - **Launch all of the wave's agents in a single message** (several Agent tool calls) so they run in parallel. Use `subagent_type: "<orch-run-name>"`. Each prompt follows the delegation template in protocol.md: task id, context, contract paths, exact acceptance criteria, ownership reminder, and "end with ORCH_REPORT".
   - Wait for every result. Do not start a dependent task early.
   - For each result:
     - Extract the ORCH_REPORT block.
     - **Verify it**: the files exist, tests and acceptance checks pass when you run them, and the changes are inside ownership (`git diff --name-only` if it is a git repo).
     - Then record it: `ORCH task done <id> --report - <<'EOF' … EOF`, or `ORCH task fail <id> --reason "..."`.
   - Act on `issues`. Route a cross-boundary change request to the owner with `ORCH task add <id> --title ... --agent <owner> --depends-on ...`, then delegate it.
3. **Failures:**
   - Use `ORCH task retry <id> --reason "<specific feedback>"`, then re-delegate with the feedback and the previous report.
   - After `max_retries`, stop retrying. Rethink the decomposition, try a different agent or model, or escalate to the user.
   - Never mark a failed task done.
4. **Agent not found.** Claude Code picks up new agent files asynchronously, so spawns right after `run init` often return "Agent type … not found".
   - Don't wait. Delegate the same task immediately to `subagent_type: "general-purpose"`, with a prompt that starts with the exact output of `ORCH agent prompt <name>`, followed by the task message.
   - The `<agent-definition name="…">` header lets the ownership hook recognise the agent, so its boundaries are still enforced.
   - Try the native `subagent_type` again for later waves. It usually loads within a few minutes.
5. **Isolation:** if the repository is git-backed and a task is risky or experimental, pass `isolation: "worktree"` on the Agent call and merge its branch only after validation.
6. **Agent teams** (only if `ORCH env` shows the flag on and the session is interactive):
   - You may spawn teammates (Agent call with a `name`) and coordinate through SendMessage and the shared task list.
   - Still mirror state into `ORCH task` so the run can be reported and resumed.
   - Otherwise, use subagents as described above.
7. Show progress after each wave with `ORCH status`.

## Integration and validation
- Run the orchestrator-owned integration task yourself: build, full test suite, end-to-end checks against the acceptance criteria. Anything that would deploy or touch external services needs approval first.
- Route each fix to the owning agent as a new task. Do not patch other owners' areas silently. Small glue changes in files you own are fine.
- Finish with `ORCH status --full` (writes `report.md`), then `ORCH run close`. That removes the run's temporary agents.
- Before closing, offer to keep any useful agent as a reusable one: `ORCH agent promote <name> --to project|user`. Only with the user's yes.

## Final report to the user
- What was built and where.
- The team table: agent, responsibility, status. Report failed agents and tasks honestly.
- Validation results: what was run and its outcome, plus anything not run and why.
- Open issues and next steps.

## Resume
1. `ORCH run list`, then `ORCH status` on the active run, or the one the user names; `ORCH run activate <id>` if needed.
2. **Done** tasks are not repeated. `ORCH task start` refuses them.
3. **In-progress** tasks were interrupted. Inspect what they left on disk.
   - If the work is complete and verified: `task done` with a report you write based on that inspection (say it was reconstructed).
   - Otherwise: `task retry <id> --reason "interrupted"`, then re-delegate with "continue from current state: inspect existing files first".
4. Continue the waves from the first ready task.

## Learn from the run (after the run, not during)
- `ORCH stats` shows failing roles and vague agent definitions.
- For each insight that should change future plans, record a one-line lesson with `ORCH lessons add --tag <domain> "<lesson>"`. Example: "contracts first: UI/API mismatches caused 2 retries".
- Improving templates or this skill is a proposal to the user, never an automatic edit.

## Environments without subagents (claude.ai uploads, restricted sessions)
- Follow the same phases.
- Run the plan sequentially yourself, one role at a time, using each agent definition as a checklist.
- Say explicitly that no subagents were spawned. Hooks and persistent state may be missing: see environments.md.
