#!/usr/bin/env python3
"""Evidence checks for e2e scenarios. Run from a scenario workdir:  python3 check.py <check> [args]
Evidence = stream-json transcripts (../<scenario>.session*.jsonl) + files on disk."""
import glob
import json
import re
import subprocess
import sys
from pathlib import Path

ORCH = Path(__file__).resolve().parents[2] / "plugin/skills/orchestrate/scripts/orch.py"
cwd = Path.cwd()
name = cwd.name


def events():
    for log in sorted(cwd.parent.glob(f"{name}.session*.jsonl")):
        for line in log.read_text().splitlines():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


def tool_uses(tool=None):
    for e in events():
        msg = e.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and (tool is None or c.get("name") in tool):
                yield c.get("input") or {}


def runs():
    return [json.loads(p.read_text()) for p in sorted(cwd.glob(".claude/orchestrator/runs/*/run.json"))]


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def agents_max(n):
    calls = list(tool_uses({"Agent", "Task"}))
    print(f"{len(calls)} Agent calls: {[c.get('subagent_type') for c in calls]}")
    if len(calls) > int(n):
        fail(f"more than {n} agents for a simple task")


def agents_min(n):
    """Distinct specialist identities: the subagent_type, or for general-purpose fallback
    delegations the generated agent named in the prompt (orch-<run>-<name>)."""
    types = set()
    for c in tool_uses({"Agent", "Task"}):
        t = c.get("subagent_type")
        if t == "general-purpose":
            m = re.search(r"\borch-[a-z0-9]+-[a-z0-9]+-[a-z0-9-]+", c.get("prompt") or "")
            t = m.group(0).rstrip("-.") if m else t
        if t:
            types.add(t)
    print(f"distinct specialist agents: {sorted(types)}")
    if len(types) < int(n):
        fail(f"expected >= {n} distinct specialized agents")


def generated_agents_used():
    """At least one delegation used a runtime-generated agent (or its general-purpose fallback)."""
    rs = runs()
    if not rs:
        fail("no orchestration run state")
    names = {n for r in rs for n in r["names"].values()}
    used = [c.get("subagent_type") for c in tool_uses({"Agent", "Task"})]
    native = [u for u in used if u in names]
    fallback = [c for c in tool_uses({"Agent", "Task"}) if c.get("subagent_type") == "general-purpose"
                and "agent-definition" in (c.get("prompt") or "")]
    print(f"generated: {sorted(names)}\nnative delegations: {len(native)}  fallback delegations: {len(fallback)}")
    if not native and not fallback:
        fail("no delegation to a generated agent")


def hook_spawns_min(n):
    starts = 0
    for p in cwd.glob(".claude/orchestrator/runs/*/events.jsonl"):
        starts += sum(1 for l in p.read_text().splitlines() if '"SubagentStart"' in l)
    print(f"SubagentStart events logged by hook: {starts}")
    if starts < int(n):
        fail("hook log does not confirm real subagent spawns")


def run_complete():
    rs = runs()
    if not rs:
        fail("no run")
    r = rs[-1] if len(rs) == 1 else max(rs, key=lambda r: r["created"])
    bad = {k: t["status"] for k, t in r["tasks"].items() if t["status"] not in ("done", "skipped")}
    print(f"run {r['id']} status={r['status']} tasks={len(r['tasks'])} not-done={bad}")
    if bad:
        fail("unfinished tasks")


def no_redo():
    """T6: tasks done before the interruption were not started again afterwards."""
    logs = sorted(cwd.parent.glob(f"{name}.session*.jsonl"))
    if len(logs) < 2:
        fail("no resume session")
    rep = json.loads((cwd.parent / f"{name}.report.json").read_text())
    before = rep["sessions"][1]["orch_before"][-1]["tasks"]
    after = rep["sessions"][1]["orch_after"][-1]["tasks"]
    redone = [k for k, t in before.items() if t["status"] == "done" and
              (after[k]["attempts"] != t["attempts"] or after[k]["started"] != t["started"])]
    done_before = [k for k, t in before.items() if t["status"] == "done"]
    print(f"done before interruption: {done_before}; redone: {redone}")
    if not done_before:
        fail("interruption happened before any task completed")
    if redone:
        fail(f"completed tasks were repeated: {redone}")


def hook_fired():
    """stream-json omits UserPromptSubmit hook output, so use the hook's own invocation log
    (ORCH_HOOK_LOG). Given permission_mode=plan the injection is deterministic (unit-tested)."""
    log = cwd.parent / f"{name}.hooks.log"
    rows = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    hits = [r for r in rows if r["event"] == "prompt" and r["payload"].get("permission_mode") == "plan"]
    print(f"hook invocations: {[(r['event'], r['payload'].get('permission_mode')) for r in rows]}")
    if not hits:
        fail("UserPromptSubmit hook did not run in plan mode")


def skill_invoked():
    skills = [c.get("skill") for c in tool_uses({"Skill"})]
    print(f"skills invoked: {skills}")
    if not any(s and "orchestrate" in s for s in skills):
        fail("orchestrate skill not invoked")


def plan_valid():
    paths = [c.get("file_path") for c in tool_uses({"Write", "Edit"}) if "plans/" in (c.get("file_path") or "")]
    if not paths:
        fail("no plan file written")
    p = paths[-1]
    text = Path(p).read_text()
    if "orch-plan" not in text:
        fail(f"{p} has no orch-plan block")
    r = subprocess.run([sys.executable, str(ORCH), "plan", "--check", p], capture_output=True, text=True)
    print(r.stdout[-2500:], r.stderr[-800:])
    if r.returncode:
        fail("orch-plan block does not validate")


def no_project_writes():
    r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    changed = [l for l in r.stdout.splitlines() if l.strip()]
    print("changed:", changed)
    if changed:
        fail("plan mode wrote project files")


def exists(pattern):
    hits = [h for h in glob.glob(pattern, recursive=True) if "node_modules" not in h]
    print(f"{pattern}: {hits[:10]}")
    if not hits:
        fail(f"nothing matches {pattern}")


def csv_report():
    scripts = [p for p in cwd.glob("**/*.py") if ".claude" not in p.parts and "test" not in p.name]
    if not scripts:
        fail("no python script")
    for s in scripts:
        for args in (["data.csv"], []):
            r = subprocess.run([sys.executable, str(s), *args], capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                print(f"{s.name} {' '.join(args)} -> ok\n{r.stdout[-800:]}")
                return
    fail("no script ran successfully on data.csv")


def urls_min(path, n):
    text = Path(path).read_text()
    urls = set(re.findall(r"https?://[^\s)\]>\"']+", text))
    print(f"{path}: {len(text.split())} words, {len(urls)} distinct URLs")
    if len(urls) < int(n):
        fail("too few sources")


if __name__ == "__main__":
    globals()[sys.argv[1]](*sys.argv[2:])
