# Environment capabilities and fallbacks

Verified against the Claude Code docs (code.claude.com/docs) for v2.1.3xx. Run `ORCH env` to see the live answer for this session.

| Capability | CLI | VS Code ext. | Desktop (Code tab) | claude.ai skill upload |
|---|---|---|---|---|
| Skill auto-discovery and `/orchestrate` | yes | yes | yes | yes (skill only) |
| Subagents (Agent tool) | yes | yes | yes | no: run phases yourself, sequentially |
| Runtime-generated `.claude/agents/*.md` | hot-reload (restart if the dir is new) | same | same | n/a |
| Nested subagents | up to `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` (default 3) | same | same | n/a |
| Hooks (Plan Mode trigger, ownership, logging) | yes | yes | yes | no |
| Plan Mode | yes | yes (plan opens as a document) | yes | no: present the plan in chat |
| Agent teams (experimental) | interactive only, flag `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` | in-process only (no split panes) | not available | no |
| `-p` / SDK headless | subagents yes, teams no | n/a | n/a | n/a |
| Worktree isolation | in a git repo | same | same | no |
| Persistent run state (orch.py) | yes | yes | yes | sandbox only; not persistent |

## Fallbacks
- **Generated agent "not found":** this is normal right after `run init`. Claude Code loads new agent files asynchronously; in live tests it took longer than 35 s.
  - Delegate to `general-purpose` straight away, with `ORCH agent prompt <name>` prepended to the task.
  - The ownership hook recognises such agents from the `<agent-definition name="…">` header in their first prompt (best-effort, based on the observed transcript layout).
  - Use the native agent in later waves.
  - A brand-new `.claude/agents/` directory may need a restart.
- **Spawning authorization:** Claude Code spawns subagents only when the user asks. That means a request in the conversation, plan approval, or the user's own CLAUDE.md opt-in (`install.py authorize`). The plugin never grants this to itself.
- **No nesting:** keep the team flat and have the orchestrator delegate everything.
- **No hooks** (skill copied without hooks, or claude.ai): Plan Mode will not auto-trigger, so the user invokes `/orchestrate`. Ownership and core protection are instruction-only.
- **No agent teams:** use subagents and `ORCH task` state. Teams are never required.
- **Writes via Bash:** the ownership hook covers Write/Edit/MultiEdit/NotebookEdit, but not shell redirection. The `task done` ownership audit catches what an agent reports, and `git diff` catches the rest.
