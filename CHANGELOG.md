# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-11-11

First public release.

### Added
- **`orchestrate` skill.** A Master Orchestrator workflow:
  - orient, size the work (direct, one specialist, or team), understand, architect;
  - design a dynamic team, plan, then execute in waves;
  - integrate, validate and resume.
- **Agent Factory (`orch.py agent` / `team` / `run init`).**
  - Turns JSON specs into native Claude Code subagent files in `.claude/agents/`.
  - Validates each spec: required fields, tools, model, permission mode, ownership conflicts, vague wording, team size and nesting depth.
  - Never overwrites hand-written agents.
  - Agents can be run-scoped (temporary), project-scoped or user-scoped, and promoted to reusable ones.
- **Seed role library** (`templates/roles.json`) of 18 starting points. Roles can be extended or overridden per user or project, and new specializations can be invented per task.
- **Planner (`orch.py plan`).**
  - Validates `orch-plan` blocks: cycles, unknown dependencies or agents, duplicate tasks, ownership clashes within a wave, and limits.
  - Computes parallel waves and the critical path.
  - Renders the team tree, plus a Mermaid diagram on request.
- **Persistent run state and task manager (`orch.py task` / `status`).**
  - Covers dependencies, retry limits and resume protection: completed tasks are never re-run.
  - Audits each `ORCH_REPORT` and the files it lists against the agent's ownership.
  - Execution reports are written to `report.md`.
- **Hooks.**
  - Plan Mode trigger (`UserPromptSubmit` with `permission_mode == "plan"`), and a hint outside Plan Mode for multi-part work.
  - Notice of an unfinished run at session start.
  - File-ownership enforcement for generated agents, including the general-purpose fallback.
  - Core-file protection.
  - Logging of every subagent start and stop.
- **Self-improvement.** `orch.py stats` (failing roles, definition lint) and `orch.py lessons`.
- **Environment detection.** `orch.py env` produces a capability table with fallbacks for CLI, VS Code, Desktop and claude.ai.
- **Installer (`install.py`).**
  - Plugin install via a local marketplace.
  - Skill plus hooks merged into `settings.json`, with a backup.
  - claude.ai ZIP.
  - Uninstall.
  - `authorize` / `unauthorize`, which manage your own CLAUDE.md opt-in for spawning agents.
- **Tests.**
  - 37 stdlib unit tests.
  - A live E2E runner (`tests/e2e/run_e2e.py`) with six acceptance scenarios. It counts only evidence from the transcript and the files on disk.

### Known limitations
- **Agent hot-reload is slow.** Claude Code loads newly written agent files asynchronously; in testing they weren't available 35 s after creation. The skill falls back to `general-purpose` with the generated definition inlined.
- **Fallback-agent ownership relies on an undocumented path.** The hook reads Claude Code's subagent transcript layout, which isn't documented.
- **Bash writes bypass the ownership hook.** Only Write/Edit tools are covered; shell writes are caught afterwards by the report audit.
- **Agent teams are interactive-only and experimental.** Only their detection is tested automatically.
- **The Plan Mode trigger fires on prompt submit**, not when you toggle the mode.

[Unreleased]: https://github.com/TheCoder30ec4/agent-orchestrator-skill/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/TheCoder30ec4/agent-orchestrator-skill/releases/tag/v1.0.0
