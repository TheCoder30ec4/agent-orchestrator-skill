"""Unit tests for orch.py (stdlib unittest).  Run: python3 -m unittest discover tests"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugin" / "skills" / "orchestrate" / "scripts" / "orch.py"
sys.path.insert(0, str(SCRIPT.parent))
import orch  # noqa: E402

TEAM = [
    {"name": "architect", "extends": "architect", "owns": ["contracts/**"]},
    {"name": "api", "extends": "backend", "owns": ["backend/**"], "depends_on": ["architect"],
     "group": "Backend"},
    {"name": "ui", "extends": "frontend", "owns": ["frontend/**"], "depends_on": ["architect"]},
]
TASKS = [
    {"id": "T1", "title": "Contracts", "agent": "architect", "acceptance": ["valid"]},
    {"id": "T2", "title": "API", "agent": "api", "depends_on": ["T1"], "acceptance": ["tests"]},
    {"id": "T3", "title": "UI", "agent": "ui", "depends_on": ["T1"], "acceptance": ["build"]},
    {"id": "T4", "title": "Integrate", "agent": "orchestrator", "depends_on": ["T2", "T3"],
     "acceptance": ["e2e"]},
]
REPORT = {"status": "done", "task": "T1", "completed": ["c"], "files_changed": ["contracts/a.yaml"],
          "dependencies": [], "issues": [], "remaining": [], "validation": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "proj"
        self.cfg = Path(self.tmp.name) / "cfg"
        self.proj.mkdir()
        self.cfg.mkdir()
        self.env = {"ORCH_PROJECT_DIR": str(self.proj), "CLAUDE_CONFIG_DIR": str(self.cfg)}
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def cli(self, *args, stdin=None):
        """Run in-process; returns (rc, stdout, stderr)."""
        o, e = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin or "")
        try:
            with redirect_stdout(o), redirect_stderr(e):
                rc = orch.main(list(args))
        finally:
            sys.stdin = old_stdin
        return rc, o.getvalue(), e.getvalue()

    def plan_file(self, team=TEAM, tasks=TASKS):
        p = self.proj / "plan.md"
        p.write_text("# Plan\n\n```json orch-plan\n" +
                     json.dumps({"title": "Demo", "team": {"agents": team}, "tasks": tasks}) + "\n```\n")
        return str(p)

    def init_run(self, **kw):
        rc, out, err = self.cli("run", "init", "--from-plan", self.plan_file(**kw), "--id", "demo")
        self.assertEqual(rc, 0, err)
        return out


class FrontmatterTests(Base):
    def test_parse_existing_style_agent(self):
        text = ('---\nname: tester\ndescription: "Line one.\\nLine two with \\"quotes\\""\n'
                'model: haiku\ncolor: blue\ntools:\n  - Read\n  - Bash\nnotes: >\n  folded\n  text\n---\nBody')
        fm, body = orch.parse_frontmatter(text)
        self.assertEqual(fm["description"], 'Line one.\nLine two with "quotes"')
        self.assertEqual(fm["tools"], ["Read", "Bash"])
        self.assertEqual(fm["notes"], "folded text")
        self.assertEqual(body, "Body")

    def test_roundtrip_dump(self):
        fm = {"name": "a", "description": 'x: "y"', "tools": ["Read", "Bash(git *)"], "maxTurns": 5}
        parsed, _ = orch.parse_frontmatter(orch.dump_frontmatter(fm) + "\nbody")
        self.assertEqual(parsed["description"], 'x: "y"')
        self.assertEqual(orch.tool_list(parsed["tools"]), ["Read", "Bash(git *)"])
        self.assertEqual(parsed["maxTurns"], 5)

    def test_missing_frontmatter(self):
        with self.assertRaises(orch.OrchError):
            orch.parse_frontmatter("no frontmatter")

    def test_validate_frontmatter_errors(self):
        errs, warns = orch.validate_frontmatter(
            {"name": "bad name", "model": "gpt-4", "permissionMode": "bypassPermissions",
             "tools": "Read, Wrte", "effort": "huge", "maxTurns": 0})
        joined = " ".join(errs)
        for needle in ("invalid name", "model", "bypassPermissions", "effort", "maxTurns", "description"):
            self.assertIn(needle, joined)
        self.assertTrue(any("Wrte" in w for w in warns))

    def test_mcp_and_scoped_tools_ok(self):
        errs, warns = orch.validate_frontmatter(
            {"name": "ok", "description": "a sufficiently long description of the agent",
             "tools": "mcp__github__search, Bash(npm test *), Agent(worker)"})
        self.assertEqual(errs, [])
        self.assertEqual(warns, [])


class FactoryTests(Base):
    def test_create_validate_and_no_overwrite(self):
        spec = json.dumps({"name": "csv-reporter", "extends": "generalist", "owns": ["report.py"]})
        rc, out, err = self.cli("agent", "create", "--scope", "project", "--spec", "-", stdin=spec)
        self.assertEqual(rc, 0, err)
        path = self.proj / ".claude" / "agents" / "csv-reporter.md"
        self.assertTrue(path.exists())
        rc, out, _ = self.cli("agent", "validate", str(path))
        self.assertEqual(rc, 0, out)
        # second create without --force refuses; with --force regenerates
        rc, _, err = self.cli("agent", "create", "--scope", "project", "--spec", "-", stdin=spec)
        self.assertEqual(rc, 1)
        self.assertIn("--force", err)
        rc, _, _ = self.cli("agent", "create", "--scope", "project", "--force", "--spec", "-", stdin=spec)
        self.assertEqual(rc, 0)

    def test_never_overwrites_handwritten_agent(self):
        d = self.proj / ".claude" / "agents"
        d.mkdir(parents=True)
        (d / "mine.md").write_text("---\nname: mine\ndescription: hand written agent here\n---\nhi")
        spec = json.dumps({"name": "mine", "extends": "generalist"})
        rc, _, err = self.cli("agent", "create", "--scope", "project", "--force", "--spec", "-", stdin=spec)
        self.assertEqual(rc, 1)
        self.assertIn("not generated", err)
        self.assertIn("hand written", (d / "mine.md").read_text())

    def test_user_scope_goes_to_config_dir(self):
        spec = json.dumps({"name": "reviewer", "extends": "security"})
        rc, _, err = self.cli("agent", "create", "--scope", "user", "--spec", "-", stdin=spec)
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.cfg / "agents" / "reviewer.md").exists())
        self.assertIn("reviewer", json.loads((self.cfg / "orchestrator" / "registry.json").read_text())["agents"])

    def test_invalid_spec_writes_nothing(self):
        specs = json.dumps({"agents": [
            {"name": "good", "extends": "generalist"},
            {"name": "bad", "description": "x", "objective": "y", "responsibilities": [],
             "outputs": ["o"], "validation": ["v"]}]})
        rc, _, err = self.cli("agent", "create", "--scope", "project", "--spec", "-", stdin=specs)
        self.assertEqual(rc, 1)
        self.assertIn("responsibilities", err)
        self.assertFalse((self.proj / ".claude" / "agents" / "good.md").exists())

    def test_unknown_role_and_owns_conflict(self):
        with self.assertRaises(orch.OrchError):
            orch.resolve_spec(orch.Ctx(), {"name": "x", "extends": "nope"})
        e, _ = orch.validate_spec(dict(orch.resolve_spec(orch.Ctx(), {"name": "x", "extends": "generalist"}),
                                       owns=["a/**"], must_not_modify=["a/**"]), {})
        self.assertTrue(any("both contain" in x for x in e))

    def test_vague_lint(self):
        _, w = orch.validate_spec({"name": "v", "description": "Helps with various things",
                                   "objective": "do stuff", "responsibilities": ["etc"],
                                   "outputs": ["o"], "validation": ["v"]}, {})
        self.assertTrue(any("vague" in x for x in w))

    def test_parent_adds_agent_tool_and_depth_limit(self):
        ctx = orch.Ctx()
        specs = [{"name": "lead", "extends": "backend"},
                 {"name": "worker", "extends": "backend", "parent": "lead"}]
        created, _, names = orch.create_agents(ctx, specs, "run", "r1")
        lead = (self.proj / ".claude" / "agents" / "orch-r1-lead.md").read_text()
        fm, body = orch.parse_frontmatter(lead)
        self.assertIn("Agent", orch.tool_list(fm["tools"]))
        self.assertIn("orch-r1-worker", body)
        deep = [{"name": "a", "extends": "backend"}, {"name": "b", "extends": "backend", "parent": "a"},
                {"name": "c", "extends": "backend", "parent": "b"}]
        with self.assertRaises(orch.OrchError) as cm:
            orch.create_agents(ctx, deep, "run", "r2")
        self.assertIn("max_depth", str(cm.exception))

    def test_max_agents(self):
        ctx = orch.Ctx()
        ctx.cfg["max_agents"] = 2
        with self.assertRaises(orch.OrchError):
            orch.create_agents(ctx, [{"name": f"a{i}", "extends": "generalist"} for i in range(3)], "run", "r")


class PlanTests(Base):
    def analyze(self, tasks, owns=None, cfg=None):
        owns = owns or {"architect": ["contracts/**"], "api": ["backend/**"], "ui": ["frontend/**"]}
        return orch.analyze_plan(tasks, owns, cfg or {"max_parallel": 5})

    def test_waves_and_critical_path(self):
        an = self.analyze(TASKS)
        self.assertEqual(an["errors"], [])
        self.assertEqual(an["waves"], [["T1"], ["T2", "T3"], ["T4"]])
        self.assertEqual(an["critical_path"][0], "T1")
        self.assertEqual(an["critical_path"][-1], "T4")

    def test_cycle_unknown_dup(self):
        cyc = [{"id": "A", "title": "a", "agent": "api", "depends_on": ["B"]},
               {"id": "B", "title": "b", "agent": "api", "depends_on": ["A"]}]
        self.assertTrue(any("cycle" in e for e in self.analyze(cyc)["errors"]))
        unk = [{"id": "A", "title": "a", "agent": "ghost", "depends_on": ["Z"]}]
        errs = self.analyze(unk)["errors"]
        self.assertTrue(any("ghost" in e for e in errs))
        self.assertTrue(any("Z" in e for e in errs))
        dup = [{"id": "A", "title": "Build API", "agent": "api"}, {"id": "B", "title": "build  api!", "agent": "api"}]
        self.assertTrue(any("duplicate" in e for e in self.analyze(dup)["errors"]))

    def test_parallel_ownership_conflict(self):
        tasks = [{"id": "A", "title": "a", "agent": "api"},
                 {"id": "B", "title": "b", "agent": "ui", "owns": ["backend/models/**"]}]
        self.assertTrue(any("parallel" in e for e in self.analyze(tasks)["errors"]))
        tasks[1]["depends_on"] = ["A"]  # serializing resolves it
        self.assertEqual(self.analyze(tasks)["errors"], [])

    def test_max_parallel_splits_waves(self):
        owns = {f"a{i}": [f"d{i}/**"] for i in range(5)}
        tasks = [{"id": f"T{i}", "title": f"t{i}", "agent": f"a{i}"} for i in range(5)]
        an = orch.analyze_plan(tasks, owns, {"max_parallel": 2})
        self.assertEqual([len(w) for w in an["waves"]], [2, 2, 1])

    def test_glob_overlap(self):
        self.assertTrue(orch.globs_overlap("src/**", "src/api/**"))
        self.assertFalse(orch.globs_overlap("frontend/**", "backend/**"))
        self.assertTrue(orch.globs_overlap("README.md", "*.md"))
        self.assertFalse(orch.globs_overlap("README.md", "docs/**"))

    def test_plan_check_cli(self):
        rc, out, err = self.cli("plan", "--check", self.plan_file())
        self.assertEqual(rc, 0, err)
        self.assertIn("Wave 2: T2, T3  (parallel)", out)
        self.assertIn("Backend [group]", out)
        rc, out, _ = self.cli("plan", "--check", self.plan_file(tasks=TASKS + [TASKS[0]]))
        self.assertEqual(rc, 1)


class RunTests(Base):
    def test_full_lifecycle_and_resume_semantics(self):
        out = self.init_run()
        self.assertIn('subagent_type "orch-demo-api"', out)
        self.assertEqual(self.cli("task", "start", "T2")[0], 1)  # deps not done
        self.assertEqual(self.cli("task", "start", "T1")[0], 0)
        rc, out, _ = self.cli("task", "done", "T1", "--report", "-",
                              stdin="text\n```ORCH_REPORT\n" + json.dumps(REPORT) + "\n```\n")
        self.assertEqual(rc, 0)
        self.assertIn("ready now: T2, T3", out)
        rc, _, err = self.cli("task", "start", "T1")  # never re-run done work
        self.assertEqual(rc, 1)
        self.assertIn("already done", err)
        # simulate interruption: T2 in progress, new process sees it
        self.cli("task", "start", "T2")
        rc, out, _ = self.cli("status", "--json")
        st = json.loads(out)
        self.assertEqual(st["tasks"]["T1"], "done")
        self.assertEqual(st["tasks"]["T2"], "in_progress")
        self.assertEqual(st["tasks"]["T3"], "ready")
        # session hook announces the unfinished run
        res = subprocess.run([sys.executable, str(SCRIPT), "hook", "session"], input="{}",
                             capture_output=True, text=True, env={**os.environ, **self.env})
        self.assertIn("T2", json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"])
        rc, out, _ = self.cli("run", "close", "--status", "aborted")
        self.assertEqual(rc, 0)
        self.assertFalse((self.proj / ".claude" / "agents" / "orch-demo-api.md").exists())
        self.assertTrue((self.proj / ".claude" / "orchestrator" / "runs" / "demo" / "report.md").exists())

    def test_retry_limit(self):
        self.init_run()
        for _ in range(3):  # max_retries=2 -> 3 attempts
            self.assertEqual(self.cli("task", "start", "T1")[0], 0)
            self.cli("task", "fail", "T1", "--reason", "boom")
            self.cli("task", "retry", "T1", "--reason", "again")
        rc, _, err = self.cli("task", "start", "T1")
        self.assertEqual(rc, 1)
        self.assertIn("max attempts", err)

    def test_report_ownership_and_missing_fields(self):
        self.init_run()
        self.cli("task", "start", "T1")
        bad = dict(REPORT, files_changed=["backend/hack.py"])
        del bad["issues"]
        rc, out, _ = self.cli("task", "done", "T1", "--report", "-", stdin=json.dumps(bad))
        self.assertEqual(rc, 0)
        self.assertIn("missing fields: issues", out)
        self.assertIn("outside architect's ownership", out)

    def test_task_add_rejects_duplicates(self):
        self.init_run()
        self.assertEqual(self.cli("task", "add", "T9", "--title", "contracts", "--agent", "api")[0], 1)
        self.assertEqual(self.cli("task", "add", "T9", "--title", "Fix UI mismatch", "--agent", "ui",
                                  "--depends-on", "T3")[0], 0)

    def test_second_active_run_blocked(self):
        self.init_run()
        rc, _, err = self.cli("run", "init", "--from-plan", self.plan_file(), "--id", "other")
        self.assertEqual(rc, 1)
        self.assertIn("still active", err)

    def test_promote_and_stats(self):
        self.init_run()
        self.cli("task", "start", "T1")
        self.cli("task", "fail", "T1")
        self.cli("task", "retry", "T1")
        self.cli("task", "start", "T1")
        self.cli("task", "fail", "T1")
        rc, out, _ = self.cli("stats")
        self.assertIn("Repeatedly failing roles", out)
        rc, out, err = self.cli("agent", "promote", "orch-demo-api", "--to", "user", "--as", "api-dev")
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.cfg / "agents" / "api-dev.md").exists())

    def test_lessons(self):
        self.cli("lessons", "add", "--tag", "web", "contracts", "first")
        rc, out, _ = self.cli("lessons", "list", "--tag", "web")
        self.assertIn("contracts first", out)


class HookTests(Base):
    def hook(self, event, payload, extra_env=None):
        res = subprocess.run([sys.executable, str(SCRIPT), "hook", event], input=json.dumps(payload),
                             capture_output=True, text=True,
                             env={**os.environ, **self.env, **(extra_env or {})})
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["hookSpecificOutput"] if res.stdout.strip() else None

    def test_plan_mode_injection(self):
        out = self.hook("prompt", {"permission_mode": "plan", "prompt": "build a SaaS"})
        self.assertIn("orchestrate", out["additionalContext"])
        self.assertIsNone(self.hook("prompt", {"permission_mode": "default", "prompt": "fix typo"}))
        nudge = self.hook("prompt", {"permission_mode": "default",
                                     "prompt": "Build a task app with auth, a database, a React UI and Docker"})
        self.assertIn("sizing step", nudge["additionalContext"])
        self.assertIsNone(self.hook("prompt", {"permission_mode": "default", "prompt": "/help me build an app with many parts now"}))
        self.assertIsNone(self.hook("prompt", {"permission_mode": "plan", "prompt": "x [no-orchestrate]"}))

    def test_ownership_enforcement(self):
        self.init_run()
        base = {"tool_name": "Write", "agent_type": "orch-demo-api", "cwd": str(self.proj)}
        deny = self.hook("pretool", dict(base, tool_input={"file_path": "frontend/x.js"}))
        self.assertEqual(deny["permissionDecision"], "deny")
        self.assertIsNone(self.hook("pretool", dict(base, tool_input={"file_path": "backend/x.py"})))
        abs_ok = str(self.proj / "backend" / "y.py")
        self.assertIsNone(self.hook("pretool", dict(base, tool_input={"file_path": abs_ok})))
        scratch = Path(self.tmp.name) / "scratch"
        self.assertIsNone(self.hook("pretool", dict(base, scratchpad_dir=str(scratch),
                                                    tool_input={"file_path": str(scratch / "t.txt")})))
        outside = self.hook("pretool", dict(base, tool_input={"file_path": str(Path(self.tmp.name) / "x")}))
        self.assertEqual(outside["permissionDecision"], "deny")
        # main session and foreign agents are not restricted
        self.assertIsNone(self.hook("pretool", {"tool_name": "Write", "cwd": str(self.proj),
                                                "tool_input": {"file_path": "frontend/x.js"}}))
        self.assertIsNone(self.hook("pretool", dict(base, agent_type="Explore",
                                                    tool_input={"file_path": "frontend/x.js"})))
        events = (self.proj / ".claude/orchestrator/runs/demo/events.jsonl").read_text()
        self.assertIn("ownership_denied", events)

    def test_fallback_general_purpose_identified(self):
        self.init_run()
        tdir = Path(self.tmp.name) / "transcripts" / "sess"
        (tdir / "subagents").mkdir(parents=True)
        (tdir / "subagents" / "agent-abc.jsonl").write_text(json.dumps(
            {"message": {"content": '<agent-definition name="orch-demo-ui">...'}}) + "\n")
        base = {"tool_name": "Write", "agent_type": "general-purpose", "agent_id": "abc",
                "transcript_path": str(tdir) + ".jsonl", "cwd": str(self.proj)}
        deny = self.hook("pretool", dict(base, tool_input={"file_path": "backend/x.py"}))
        self.assertEqual(deny["permissionDecision"], "deny")
        self.assertIsNone(self.hook("pretool", dict(base, tool_input={"file_path": "frontend/x.js"})))
        other = dict(base, agent_id="zzz")  # unrelated general-purpose agent: unrestricted
        self.assertIsNone(self.hook("pretool", dict(other, tool_input={"file_path": "backend/x.py"})))

    def test_core_protection(self):
        target = SCRIPT.parent.parent / "SKILL.md"
        out = self.hook("pretool", {"tool_name": "Edit", "cwd": str(self.proj),
                                    "tool_input": {"file_path": str(target)}})
        self.assertEqual(out["permissionDecision"], "ask")

    def test_subagent_logging(self):
        self.init_run()
        self.hook("subagent", {"hook_event_name": "SubagentStart", "agent_id": "a1",
                               "agent_type": "orch-demo-api"})
        self.hook("subagent", {"hook_event_name": "SubagentStop", "agent_id": "a1",
                               "agent_type": "orch-demo-api"})
        rc, out, _ = self.cli("status")
        self.assertIn("1 started, 1 finished", out)

    def test_hooks_never_crash(self):
        res = subprocess.run([sys.executable, str(SCRIPT), "hook", "pretool"], input="not json",
                             capture_output=True, text=True, env={**os.environ, **self.env})
        self.assertEqual(res.returncode, 0)
        (self.proj / ".claude/orchestrator").mkdir(parents=True)
        (self.proj / ".claude/orchestrator/active").write_text("ghost\n")
        self.assertIsNone(self.hook("session", {}))
        self.assertIsNone(self.hook("pretool", {"tool_name": "Write", "agent_type": "x",
                                                "tool_input": {"file_path": "a"}}))


class EnvTests(Base):
    def test_env_json(self):
        rc, out, _ = self.cli("env", "--json")
        self.assertEqual(rc, 0)
        info = json.loads(out)
        self.assertIn("capabilities", info)
        self.assertEqual(info["config"]["max_retries"], 2)

    def test_config_override(self):
        d = self.proj / ".claude" / "orchestrator"
        d.mkdir(parents=True)
        (d / "config.json").write_text('{"max_agents": 3}')
        self.assertEqual(orch.Ctx().cfg["max_agents"], 3)


class PackagingTests(unittest.TestCase):
    def test_hooks_json_points_at_script(self):
        hooks = json.loads((ROOT / "plugin/hooks/hooks.json").read_text())["hooks"]
        for ev in ("UserPromptSubmit", "SessionStart", "PreToolUse", "SubagentStart", "SubagentStop"):
            cmd = hooks[ev][0]["hooks"][0]["command"]
            self.assertIn("skills/orchestrate/scripts/orch.py", cmd)
        self.assertTrue(SCRIPT.exists())

    def test_seed_roles_are_valid_specs(self):
        roles = json.loads((SCRIPT.parent.parent / "templates/roles.json").read_text())
        for name, spec in roles.items():
            errs, _ = orch.validate_spec(dict(spec, name=name), {})
            self.assertEqual(errs, [], name)

    def test_skill_md_size(self):
        text = (SCRIPT.parent.parent / "SKILL.md").read_text()
        fm, _ = orch.parse_frontmatter(text)
        self.assertEqual(fm["name"], "orchestrate")
        self.assertLess(len(fm["description"]), 1536)
        self.assertLess(len(text.splitlines()), 500)


if __name__ == "__main__":
    unittest.main()
