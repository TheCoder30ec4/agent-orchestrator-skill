#!/usr/bin/env python3
"""Install agent-orchestrator.

  python3 install.py plugin [--scope user|project|local]   # recommended (CLI, VS Code, Desktop)
  python3 install.py skill  [--scope user|project] [--project DIR] [--no-hooks]
  python3 install.py zip    [--out FILE]                    # skill ZIP for claude.ai upload
  python3 install.py uninstall [--scope user|project] [--project DIR]
  python3 install.py authorize   [--scope user|project] [--project DIR]  # your opt-in line in CLAUDE.md
  python3 install.py unauthorize [--scope user|project] [--project DIR]

Claude Code only spawns subagents when you ask. `authorize` appends ONE marked
line to your CLAUDE.md that asks it to spawn the teams this skill plans for
multi-part work; `unauthorize` removes it. Without it, the skill proposes the
team and asks first (Plan Mode approval also counts as asking).

`plugin` registers this repo as a local marketplace and installs the plugin
(skill + hooks) with the official `claude plugin` commands.
`skill` is the fallback: copies the skill folder and merges the hooks into
settings.json (with a timestamped backup). Existing settings are preserved.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL_SRC = ROOT / "plugin" / "skills" / "orchestrate"
HOOKS_SRC = ROOT / "plugin" / "hooks" / "hooks.json"
AUTH_MARK = "<!-- agent-orchestrator:authorize -->"
AUTH_LINE = (f"{AUTH_MARK} When the agent-orchestrator `orchestrate` skill sizes a task as a team, "
             "you may spawn the subagents it plans without asking me first. "
             "(Remove this line, or run `install.py unauthorize`, to revoke.)")


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def base_dir(scope, project):
    return config_dir() if scope == "user" else Path(project).resolve() / ".claude"


def run(cmd):
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd).returncode


def install_plugin(a):
    if not shutil.which("claude"):
        sys.exit("`claude` CLI not found on PATH. Install Claude Code, or use: install.py skill")
    if run(["claude", "plugin", "marketplace", "add", str(ROOT), "--scope", a.scope]) != 0:
        print("(marketplace may already be added; continuing)")
    rc = run(["claude", "plugin", "install", "agent-orchestrator@agent-orchestrator", "--scope", a.scope])
    if rc == 0:
        print("\nInstalled. Restart Claude Code (or run /reload-plugins). Skill: /agent-orchestrator:orchestrate")
    sys.exit(rc)


def hook_entries(script):
    """Hooks from plugin/hooks/hooks.json with an absolute script path."""
    raw = HOOKS_SRC.read_text().replace('${CLAUDE_PLUGIN_ROOT}/skills/orchestrate/scripts/orch.py', str(script))
    return json.loads(raw)["hooks"]


def is_ours(group):
    return any("orch.py" in h.get("command", "") and "hook" in h.get("command", "")
               for h in group.get("hooks", []))


def edit_settings(path, add_hooks):
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except json.JSONDecodeError:
            sys.exit(f"{path} is not valid JSON; fix it first (nothing changed)")
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy(path, backup)
        print(f"backup: {backup}")
    hooks = settings.setdefault("hooks", {})
    for ev in list(hooks):  # drop previous copies of our hooks only
        hooks[ev] = [g for g in hooks[ev] if not is_ours(g)]
        if not hooks[ev]:
            del hooks[ev]
    for ev, groups in (add_hooks or {}).items():
        hooks.setdefault(ev, []).extend(groups)
    if not hooks:
        settings.pop("hooks")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, path)
    print(f"updated hooks in {path}")


def install_skill(a):
    base = base_dir(a.scope, a.project)
    dest = base / "skills" / "orchestrate"
    if dest.exists():
        if not (dest / ".agent-orchestrator").exists():
            sys.exit(f"{dest} exists and was not installed by this script; not overwriting")
        shutil.rmtree(dest)
    shutil.copytree(SKILL_SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dest / ".agent-orchestrator").write_text("installed by install.py\n")
    print(f"skill installed: {dest}")
    if not a.no_hooks:
        edit_settings(base / "settings.json", hook_entries(dest / "scripts" / "orch.py"))
    print("Restart Claude Code (a new top-level skills dir needs a restart or /reload-skills). Skill: /orchestrate")


def uninstall(a):
    base = base_dir(a.scope, a.project)
    dest = base / "skills" / "orchestrate"
    if (dest / ".agent-orchestrator").exists():
        shutil.rmtree(dest)
        print(f"removed {dest}")
    if (base / "settings.json").exists():
        edit_settings(base / "settings.json", None)
    if shutil.which("claude"):
        run(["claude", "plugin", "uninstall", "agent-orchestrator@agent-orchestrator", "--scope",
             "user" if a.scope == "user" else "project"])
    print("Run state in .claude/orchestrator/ and reusable agents were left in place.")


def claude_md(a):
    return (config_dir() if a.scope == "user" else Path(a.project).resolve()) / "CLAUDE.md"


def authorize(a):
    path = claude_md(a)
    text = path.read_text() if path.exists() else ""
    if AUTH_MARK in text:
        return print(f"already authorized in {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("" if not text or text.endswith("\n") else "\n") + AUTH_LINE + "\n")
    print(f"added to {path}:\n  {AUTH_LINE}")


def unauthorize(a):
    path = claude_md(a)
    if not path.exists() or AUTH_MARK not in path.read_text():
        return print(f"no authorization line in {path}")
    lines = [l for l in path.read_text().splitlines(keepends=True) if AUTH_MARK not in l]
    path.write_text("".join(lines))
    print(f"removed authorization line from {path}")


def make_zip(a):
    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(SKILL_SRC.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, Path("orchestrate") / p.relative_to(SKILL_SRC))
    print(f"wrote {out}  (claude.ai: Customize > Skills > upload; subagents/hooks are not available there)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plugin")
    p.add_argument("--scope", default="user", choices=["user", "project", "local"])
    for name in ("skill", "uninstall", "authorize", "unauthorize"):
        p = sub.add_parser(name)
        p.add_argument("--scope", default="user", choices=["user", "project"])
        p.add_argument("--project", default=os.getcwd())
        if name == "skill":
            p.add_argument("--no-hooks", action="store_true")
    p = sub.add_parser("zip")
    p.add_argument("--out", default=str(ROOT / "dist" / "orchestrate-skill.zip"))
    a = ap.parse_args()
    {"plugin": install_plugin, "skill": install_skill, "uninstall": uninstall, "zip": make_zip,
     "authorize": authorize, "unauthorize": unauthorize}[a.cmd](a)


if __name__ == "__main__":
    main()
