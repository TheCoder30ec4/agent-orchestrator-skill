#!/usr/bin/env python3
"""Live end-to-end acceptance tests: real `claude -p` sessions with the plugin loaded.

  python3 tests/e2e/run_e2e.py T1 --workdir /tmp/e2e        # one scenario
  python3 tests/e2e/run_e2e.py all --workdir /tmp/e2e

Costs real tokens (each scenario has a --max-budget-usd cap). Evidence comes only
from the stream-json transcript and files on disk — never from the model's claims:
  * Agent tool_use events (real spawns) and their subagent_type
  * SubagentStart/Stop events logged by the plugin hooks
  * .claude/orchestrator run state, generated agent files
  * scenario check commands run against the produced files
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugin"
ORCH = PLUGIN / "skills" / "orchestrate" / "scripts" / "orch.py"
SCEN = Path(__file__).resolve().parent / "scenarios.json"
ALLOWED = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Agent", "Skill",
           "WebSearch", "WebFetch", "NotebookEdit"]
DENIED = ["Bash(git push *)", "Bash(docker push *)", "Bash(rm -rf /*)", "Bash(sudo *)"]


def claude_cmd(prompt, mode, budget, extra=()):
    return ["claude", "-p", prompt, "--plugin-dir", str(PLUGIN), "--permission-mode", mode,
            "--output-format", "stream-json", "--verbose", "--max-budget-usd", str(budget),
            "--allowedTools", *ALLOWED, "--disallowedTools", *DENIED, *extra]


def run_claude(cmd, cwd, log, kill_when=None, timeout=5400):
    """Run a session; optionally kill it when kill_when(cwd) becomes true."""
    with open(log, "w") as f:
        env = {**os.environ, "ORCH_HOOK_LOG": str(Path(cwd).parent / f"{Path(cwd).name}.hooks.log")}
        p = subprocess.Popen(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=env,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        t0, killed = time.time(), False
        while p.poll() is None:
            if kill_when and kill_when(cwd):
                kill_tree(p.pid)
                killed = True
                break
            if time.time() - t0 > timeout:
                kill_tree(p.pid)
                break
            time.sleep(1)
        p.wait()
    return killed, time.time() - t0


def kill_tree(pid):
    """SIGKILL pid and every descendant. Claude's Bash tool runs commands in their own
    sessions, so killing the process group alone lets them finish."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    kids = {}
    for line in out.splitlines():
        c, par = map(int, line.split())
        kids.setdefault(par, []).append(c)
    todo, allp = [pid], []
    while todo:
        x = todo.pop()
        allp.append(x)
        todo += kids.get(x, [])
    for x in reversed(allp):
        try:
            os.kill(x, signal.SIGKILL)
        except ProcessLookupError:
            pass


def parse_stream(log):
    ev = {"agents": [], "skills": [], "tools": {}, "hook_ctx": [], "result": None, "cost": None,
          "writes": [], "errors": []}
    for line in Path(log).read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "system" and e.get("subtype") == "hook_response":
            if "agent-orchestrator" in (e.get("output") or "") + (e.get("stdout") or ""):
                ev["hook_ctx"].append(e.get("hook_event"))
        msg = e.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                name, inp = c.get("name"), c.get("input") or {}
                ev["tools"][name] = ev["tools"].get(name, 0) + 1
                if name in ("Agent", "Task"):
                    ev["agents"].append({"subagent_type": inp.get("subagent_type"),
                                         "description": inp.get("description"),
                                         "parent": e.get("parent_tool_use_id")})
                if name == "Skill":
                    ev["skills"].append(inp.get("skill") or inp.get("command"))
                if name in ("Write", "Edit") and inp.get("file_path"):
                    ev["writes"].append(inp["file_path"])
        if e.get("type") == "result":
            ev["result"] = e.get("result")
            ev["cost"] = e.get("total_cost_usd")
            if e.get("is_error"):
                ev["errors"].append(e.get("subtype"))
    return ev


def orch_state(cwd):
    runs = sorted((Path(cwd) / ".claude/orchestrator/runs").glob("*/run.json"))
    out = []
    for r in runs:
        d = json.loads(r.read_text())
        ev_path = r.parent / "events.jsonl"
        starts = stops = 0
        if ev_path.exists():
            for line in ev_path.read_text().splitlines():
                e = json.loads(line)
                starts += e["event"] == "SubagentStart"
                stops += e["event"] == "SubagentStop"
        out.append({"id": d["id"], "status": d["status"], "agents": list(d["names"].values()),
                    "tasks": {k: {"status": t["status"], "attempts": t["attempts"],
                                  "started": t.get("started")} for k, t in d["tasks"].items()},
                    "hook_spawns": [starts, stops]})
    return out


def run_checks(checks, cwd):
    results = []
    chk = f'"{sys.executable}" "{Path(__file__).resolve().parent / "check.py"}"'
    for c in checks:
        c = c.replace("{CHECK}", chk)
        p = subprocess.run(c, shell=True, cwd=cwd, capture_output=True, text=True, timeout=900)
        results.append({"check": c, "ok": p.returncode == 0,
                        "tail": (p.stdout + p.stderr).strip()[-600:]})
    return results


def recheck(name, sc, base):
    """Re-parse existing session logs and re-run checks (no new Claude session)."""
    wd = base / name
    report = {"scenario": name, "workdir": str(wd), "sessions": [], "rechecked": True}
    old = json.loads((base / f"{name}.report.json").read_text()) if (base / f"{name}.report.json").exists() else {}
    for i, log in enumerate(sorted(base.glob(f"{name}.session*.jsonl"))):
        ev = parse_stream(log)
        prev = (old.get("sessions") or [{}] * (i + 1))[i] if i < len(old.get("sessions", [])) else {}
        report["sessions"].append({"cost_usd": ev["cost"], "skills": ev["skills"], "agent_calls": ev["agents"],
                                   "tools": ev["tools"], "errors": ev["errors"],
                                   "result_tail": (ev["result"] or "")[-1500:],
                                   "orch_before": prev.get("orch_before", []), "orch_after": orch_state(wd)})
    (base / f"{name}.report.json").write_text(json.dumps(report, indent=2))
    report["checks"] = run_checks(sc.get("checks", []), wd)
    report["passed"] = all(c["ok"] for c in report["checks"])
    (base / f"{name}.report.json").write_text(json.dumps(report, indent=2))
    for c in report["checks"]:
        print(f"[{name}] {'PASS' if c['ok'] else 'FAIL'}: {c['check']}\n    {c['tail'][-300:]}", flush=True)
    return report


def scenario(name, sc, base):
    wd = base / name
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    for rel, content in sc.get("files", {}).items():
        (wd / rel).parent.mkdir(parents=True, exist_ok=True)
        (wd / rel).write_text(content)
    if sc.get("authorize"):  # simulates the user's own opt-in: project CLAUDE.md line
        subprocess.run([sys.executable, str(ROOT / "install.py"), "authorize", "--scope", "project",
                        "--project", str(wd)], capture_output=True)
    subprocess.run(["git", "init", "-q"], cwd=wd)
    subprocess.run(["git", "add", "-A"], cwd=wd)
    subprocess.run(["git", "-c", "user.email=e2e@local", "-c", "user.name=e2e", "commit", "-qm",
                    "fixture", "--allow-empty"], cwd=wd)
    if sc.get("init"):
        subprocess.run([sys.executable, str(ORCH), "init"], cwd=wd, capture_output=True,
                       env={**os.environ, "ORCH_PROJECT_DIR": str(wd)})
    report = {"scenario": name, "workdir": str(wd), "sessions": []}
    kill_when = None
    if sc.get("interrupt"):
        def kill_when(cwd):
            st = orch_state(cwd)
            if not st:
                return False
            ts = st[-1]["tasks"].values()
            done = sum(t["status"] == "done" for t in ts)
            starts, stops = st[-1]["hook_spawns"]
            # interrupt real in-flight work: some tasks done, more left, a subagent running now
            return done >= sc["interrupt"]["after_done"] and done < len(ts) and starts > stops
    prompts = [sc["prompt"]] + sc.get("followups", [])
    for i, prompt in enumerate(prompts):
        log = wd.parent / f"{name}.session{i + 1}.jsonl"
        before = orch_state(wd)
        killed, dur = run_claude(claude_cmd(prompt, sc.get("mode", "acceptEdits"), sc["budget"]), wd, log,
                                 kill_when if i == 0 else None, sc.get("timeout", 5400))
        ev = parse_stream(log)
        report["sessions"].append({"prompt": prompt[:120], "killed": killed, "seconds": int(dur),
                                   "cost_usd": ev["cost"], "skills": ev["skills"],
                                   "agent_calls": ev["agents"], "tools": ev["tools"],
                                   "hook_ctx": ev["hook_ctx"], "writes": ev["writes"][:40],
                                   "errors": ev["errors"],
                                   "result_tail": (ev["result"] or "")[-1500:],
                                   "orch_before": before, "orch_after": orch_state(wd)})
        print(f"[{name}] session {i + 1}: {int(dur)}s cost=${ev['cost']} agents={len(ev['agents'])} "
              f"killed={killed} skills={ev['skills']}", flush=True)
    (wd.parent / f"{name}.report.json").write_text(json.dumps(report, indent=2))  # checks read it
    report["checks"] = run_checks(sc.get("checks", []), wd)
    report["passed"] = all(c["ok"] for c in report["checks"])
    (wd.parent / f"{name}.report.json").write_text(json.dumps(report, indent=2))
    for c in report["checks"]:
        print(f"[{name}] {'PASS' if c['ok'] else 'FAIL'}: {c['check']}\n    {c['tail'][-300:]}", flush=True)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", help="scenario id (T1..T6) or 'all'")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--recheck", action="store_true", help="re-parse logs + re-run checks only")
    a = ap.parse_args()
    scen = json.loads(SCEN.read_text())
    names = list(scen) if a.which == "all" else [a.which]
    base = Path(a.workdir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    ok = True
    for n in names:
        ok &= (recheck if a.recheck else scenario)(n, scen[n], base)["passed"]
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
