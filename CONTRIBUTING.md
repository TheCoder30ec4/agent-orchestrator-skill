# Contributing and issue guidelines

Thanks for helping improve agent-orchestrator.

## Before opening an issue
1. **Search existing issues**, including closed ones.
2. **Update** to the latest release and re-check.
3. **Collect diagnostics:**
   - `python3 <plugin>/skills/orchestrate/scripts/orch.py env` (capability table)
   - `claude --version`
   - for a failed run: `orch.py status --full` (writes `.claude/orchestrator/runs/<id>/report.md`)
   - for hook problems: start Claude with `ORCH_HOOK_LOG=/tmp/orch-hooks.jsonl` and attach the relevant lines
4. **Remove anything private** before pasting: API keys, tokens, file paths with your name, customer data. Transcripts and hook logs can contain prompt text and file contents.

## Which issue type
| You have | Use |
|---|---|
| Something broken or behaving differently from the docs | **Bug report** |
| An idea for new behaviour, roles, hooks or commands | **Feature request** |
| A question about usage or setup | **Question**, or GitHub Discussions if enabled |
| A security problem, e.g. a hook bypass or permission escalation | **Do not open a public issue.** Use GitHub's private vulnerability reporting (Security tab → Report a vulnerability). |

A good bug report has:
- one problem per issue;
- exact steps to reproduce;
- expected vs. actual behaviour;
- your surface: CLI, VS Code, Desktop or claude.ai;
- your install method: plugin, skill or ZIP.

Please don't report model judgement as a bug unless it contradicts the skill's rules. For example, Claude choosing direct mode for a task you think needs a team is judgement, not a bug. If it does contradict a rule, quote the rule.

## Pull requests
- Keep changes small and focused, and link the issue.
- Use the stdlib only: no new dependencies for `orch.py` or `install.py`.
- Add or adjust a test in `tests/test_orch.py` for any logic change, then run `python3 -m unittest discover tests`.
- Run `claude plugin validate plugin`.
- Keep `SKILL.md` under 500 lines and hooks fail-open (exit 0 on internal errors).
- Update `CHANGELOG.md` under **[Unreleased]**.
- Live E2E runs (`tests/e2e/run_e2e.py`) cost real tokens. Run them only for changes to the skill workflow or hooks, and include the results.

## Releasing (maintainers)
1. Move the **[Unreleased]** entries into a new version section in `CHANGELOG.md`.
2. Bump `version` in `plugin/.claude-plugin/plugin.json` and `VERSION` in `orch.py`.
3. Tag `vX.Y.Z` and publish a GitHub release using that version's changelog section as the release notes.
