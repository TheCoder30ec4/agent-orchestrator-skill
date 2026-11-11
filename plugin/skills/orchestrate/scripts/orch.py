#!/usr/bin/env python3
"""agent-orchestrator CLI — deterministic utilities behind the `orchestrate` skill.

Claude does the thinking (requirements, team design, task decomposition).
This script does the bookkeeping that must be exact: validating and writing
native Claude Code subagent files, the agent registry, plan/DAG checks,
persistent run state, execution reports, and the lifecycle hooks.

Stdlib only. Python 3.8+.  Run `orch.py -h` or `orch.py <cmd> -h`.
"""
import argparse
import fnmatch
import json
import os
import re
import shutil
import string
import subprocess
import sys
import time
import uuid
from pathlib import Path

VERSION = "1.0.0"
SKILL_DIR = Path(__file__).resolve().parent.parent
IN_PLUGIN = (SKILL_DIR.parent.name == "skills" and
             (SKILL_DIR.parent.parent / ".claude-plugin" / "plugin.json").exists())
MARKER = "<!-- agent-orchestrator:generated"
REPORT_KEYS = ["status", "task", "completed", "files_changed", "dependencies",
               "issues", "remaining", "validation"]
TASK_STATES = ["pending", "in_progress", "done", "failed", "skipped"]

KNOWN_TOOLS = {
    "Agent", "Task", "AskUserQuestion", "Bash", "BashOutput", "Edit", "EnterPlanMode",
    "ExitPlanMode", "Glob", "Grep", "KillShell", "LS", "LSP", "Monitor", "MultiEdit",
    "NotebookEdit", "NotebookRead", "PowerShell", "Read", "SendMessage", "Skill",
    "TaskCreate", "TaskGet", "TaskList", "TaskStop", "TaskUpdate", "TodoWrite",
    "ToolSearch", "WebFetch", "WebSearch", "Write", "EnterWorktree", "ExitWorktree",
}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
MODELS = {"sonnet", "opus", "haiku", "fable", "inherit"}
PERMISSION_MODES = {"default", "manual", "acceptEdits", "auto", "dontAsk",
                    "bypassPermissions", "plan"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
COLORS = {"red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"}
MEMORY = {"user", "project", "local"}
FM_KEYS = {"name", "description", "tools", "disallowedTools", "model", "permissionMode",
           "maxTurns", "skills", "mcpServers", "hooks", "memory", "background", "effort",
           "isolation", "color", "initialPrompt", "omitClaudeMd", "autoCompactWindow",
           "experimental"}
SPEC_FM = ["tools", "disallowedTools", "model", "permissionMode", "maxTurns", "skills",
           "memory", "background", "effort", "isolation", "color"]
VAGUE = re.compile(r"\b(various|etc|stuff|things|as needed|and more|misc|miscellaneous|"
                   r"help with|general tasks|anything|whatever)\b", re.I)


class OrchError(Exception):
    pass


# ----------------------------------------------------------------- io helpers

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise OrchError(f"{path}: invalid JSON ({e})")


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)  # atomic: a crash never leaves half-written state


def load_input(arg):
    """Read JSON (or a markdown file holding an orch-plan block) from path or '-'."""
    text = sys.stdin.read() if arg == "-" else Path(arg).read_text()
    return text


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def find_project(start=None):
    env = os.environ.get("ORCH_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    p = Path(start or os.getcwd()).resolve()
    home = Path.home().resolve()
    for d in [p, *p.parents]:
        if d == home:
            break
        if (d / ".claude" / "orchestrator" / "runs").is_dir():
            return d
    return p


class Ctx:
    def __init__(self, project=None):
        self.project = Path(project).resolve() if project else find_project()
        self.orch = self.project / ".claude" / "orchestrator"
        self.cfgdir = config_dir()
        self.user_orch = self.cfgdir / "orchestrator"
        self.cfg = self._config()

    def _config(self):
        cfg = read_json(SKILL_DIR / "config" / "defaults.json", {})
        for p in (self.user_orch / "config.json", self.orch / "config.json"):
            cfg.update(read_json(p, {}) or {})
        return cfg

    # paths
    def runs(self):
        return self.orch / "runs"

    def run_dir(self, rid):
        return self.runs() / rid

    def agents_dir(self, scope):
        return (self.cfgdir if scope == "user" else self.project / ".claude") / "agents"

    def registry_path(self, scope):
        return (self.user_orch if scope == "user" else self.orch) / "registry.json"

    def active_id(self):
        try:
            return (self.orch / "active").read_text().strip() or None
        except FileNotFoundError:
            return None

    def set_active(self, rid):
        self.orch.mkdir(parents=True, exist_ok=True)
        if rid:
            (self.orch / "active").write_text(rid + "\n")
        elif (self.orch / "active").exists():
            (self.orch / "active").unlink()

    def load_run(self, rid=None):
        rid = rid or self.active_id()
        if not rid:
            raise OrchError("no active run (use --run ID or `orch.py run init`)")
        run = read_json(self.run_dir(rid) / "run.json")
        if run is None:
            raise OrchError(f"run '{rid}' not found")
        return run

    def save_run(self, run):
        run["updated"] = now()
        write_json(self.run_dir(run["id"]) / "run.json", run)

    def log(self, rid, event, **data):
        d = self.run_dir(rid)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "events.jsonl", "a") as f:
            f.write(json.dumps({"ts": now(), "event": event, **data}) + "\n")

    # registry
    def registry(self, scope):
        return read_json(self.registry_path(scope), {"agents": {}})

    def save_registry(self, scope, reg):
        write_json(self.registry_path(scope), reg)

    def find_registered(self, name):
        for scope in ("project", "user"):
            entry = self.registry(scope)["agents"].get(name)
            if entry:
                return scope, entry
        return None, None


# ------------------------------------------------------------ frontmatter I/O

def _scalar(v):
    v = v.strip()
    if v.startswith('"'):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v.strip('"')
    if v.startswith("'") and v.endswith("'") and len(v) > 1:
        return v[1:-1].replace("''", "'")
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    v = re.sub(r"\s+#.*$", "", v)
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_frontmatter(text):
    """Parse the YAML subset agent files use: scalars, quoted strings, block
    scalars (| >), lists, and opaque nested maps (kept as raw text).
    ponytail: hand-rolled subset parser; switch to PyYAML if specs need full YAML."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise OrchError("missing frontmatter (file must start with '---')")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise OrchError("unterminated frontmatter (no closing '---')")
    fm, body = lines[1:end], "\n".join(lines[end + 1:])
    data, i = {}, 0
    while i < len(fm):
        line = fm[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
        if not m or line[0] in " \t":
            raise OrchError(f"frontmatter line {i + 2}: cannot parse: {line!r}")
        key, val = m.group(1), (m.group(2) or "").strip()
        i += 1
        block = []
        while i < len(fm) and (not fm[i].strip() or fm[i][0] in " \t-"):
            block.append(fm[i])
            i += 1
        if val[:1] in ("|", ">"):
            parts = [b.strip() for b in block]
            data[key] = ("\n" if val[0] == "|" else " ").join(parts).strip()
        elif val:
            data[key] = _scalar(val)
        elif block and all(b.strip().startswith("- ") or not b.strip() for b in block):
            data[key] = [_scalar(b.strip()[2:]) for b in block if b.strip()]
        else:
            data[key] = {"_raw": "\n".join(block)}
    return data, body


def dump_frontmatter(fm):
    out = ["---"]
    for k, v in fm.items():
        if v is None or v == [] or v == "":
            continue
        if isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, int):
            out.append(f"{k}: {v}")
        elif isinstance(v, list):
            out.append(f"{k}: {json.dumps(', '.join(map(str, v)))}")
        else:
            out.append(f"{k}: {json.dumps(str(v))}")  # JSON strings are valid YAML
    out.append("---")
    return "\n".join(out)


def tool_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        # split on commas not inside parentheses: "Bash(git *), Read"
        return [t.strip() for t in re.split(r",(?![^()]*\))", v) if t.strip()]
    return [str(t).strip() for t in v]


# ----------------------------------------------------------------- validation

def validate_frontmatter(fm, cfg=None):
    cfg = cfg or {}
    errs, warns = [], []
    name = fm.get("name")
    if not name:
        errs.append("missing required field 'name'")
    elif not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", name):
        errs.append(f"invalid name {name!r}: use letters, digits and hyphens (max 64)")
    elif name != name.lower():
        warns.append(f"name {name!r}: lowercase-hyphenated names are conventional")
    desc = fm.get("description")
    if not desc or not isinstance(desc, str):
        errs.append("missing required field 'description'")
    elif len(desc) < 30:
        warns.append("description is very short; Claude uses it to decide when to delegate")
    for key in ("tools", "disallowedTools"):
        for t in tool_list(fm.get(key)):
            base = t.split("(", 1)[0]
            if base not in KNOWN_TOOLS and not base.startswith("mcp__"):
                warns.append(f"{key}: unknown tool {t!r} (typo, or newer than this validator?)")
    model = fm.get("model")
    if model is not None and model not in MODELS and not str(model).startswith("claude-"):
        errs.append(f"model {model!r} not one of {sorted(MODELS)} or a claude-* model id")
    pm = fm.get("permissionMode")
    if pm is not None:
        if pm not in PERMISSION_MODES:
            errs.append(f"permissionMode {pm!r} not one of {sorted(PERMISSION_MODES)}")
        elif pm == "bypassPermissions" and not cfg.get("allow_bypass_permissions"):
            errs.append("permissionMode bypassPermissions is blocked by config "
                        "(set allow_bypass_permissions=true to allow)")
    mt = fm.get("maxTurns")
    if mt is not None and (not isinstance(mt, int) or mt < 1):
        errs.append("maxTurns must be a positive integer")
    for key, allowed in (("effort", EFFORTS), ("color", COLORS), ("memory", MEMORY)):
        if fm.get(key) is not None and fm[key] not in allowed:
            errs.append(f"{key} {fm[key]!r} not one of {sorted(allowed)}")
    if fm.get("isolation") not in (None, "worktree"):
        errs.append("isolation must be 'worktree'")
    if fm.get("background") is not None and not isinstance(fm["background"], bool):
        errs.append("background must be true/false")
    for k in fm:
        if k not in FM_KEYS:
            warns.append(f"unknown frontmatter key {k!r} (Claude Code will ignore it)")
    return errs, warns


def lint_text(label, text):
    m = VAGUE.search(text or "")
    return [f"{label}: vague wording {m.group(0)!r} — say exactly what to do"] if m else []


def validate_spec(spec, cfg):
    errs, warns = [], []
    for key in ("name", "description", "objective"):
        if not str(spec.get(key) or "").strip():
            errs.append(f"spec missing '{key}'")
    for key in ("responsibilities", "outputs", "validation"):
        v = spec.get(key)
        if not isinstance(v, list) or not v:
            errs.append(f"spec '{key}' must be a non-empty list")
    for key in ("expertise", "inputs", "depends_on", "owns", "must_not_modify", "constraints"):
        if key in spec and not isinstance(spec[key], list):
            errs.append(f"spec '{key}' must be a list")
    if errs:
        return errs, warns
    base = spec["name"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", base):
        errs.append(f"agent name {base!r}: lowercase letters, digits, hyphens (max 41)")
    clash = set(spec.get("owns", [])) & set(spec.get("must_not_modify", []))
    if clash:
        errs.append(f"owns and must_not_modify both contain {sorted(clash)}")
    tools = set(t.split("(", 1)[0] for t in tool_list(spec.get("tools")))
    if spec.get("tools") and tools & WRITE_TOOLS and not spec.get("owns"):
        warns.append("agent has write tools but owns nothing: every write will be denied")
    if spec.get("owns") and spec.get("tools") and not tools & WRITE_TOOLS and "Bash" not in tools:
        warns.append("agent owns files but has no write tool")
    warns += lint_text("description", spec["description"])
    warns += lint_text("objective", spec["objective"])
    for r in spec["responsibilities"]:
        warns += lint_text("responsibility", r)
    if len(spec["responsibilities"]) > 12:
        warns.append("more than 12 responsibilities: consider splitting this agent")
    return errs, warns


# -------------------------------------------------------------- agent factory

def load_roles(ctx):
    roles = read_json(SKILL_DIR / "templates" / "roles.json", {})
    roles.update(read_json(ctx.user_orch / "roles.json", {}) or {})  # user custom templates
    roles.update(read_json(ctx.orch / "roles.json", {}) or {})
    return roles


def resolve_spec(ctx, spec):
    """Apply `extends: <seed role>`; spec fields override, lists replace."""
    spec = dict(spec)
    if spec.get("extends"):
        roles = load_roles(ctx)
        seed = roles.get(spec["extends"])
        if seed is None:
            raise OrchError(f"extends: unknown role template {spec['extends']!r} "
                            f"(known: {', '.join(sorted(roles))})")
        merged = dict(seed)
        merged.update({k: v for k, v in spec.items() if k != "extends"})
        merged["extends"] = spec["extends"]
        spec = merged
    spec.setdefault("role", spec.get("name", "").replace("-", " ").title())
    return spec


def full_name(ctx, base, scope, rid):
    if scope == "run":
        prefix = f"{ctx.cfg.get('temp_agent_prefix', 'orch')}-{rid}-"
        return base if base.startswith(prefix) else prefix + base
    return base


def bullets(items, empty="- (none)"):
    return "\n".join(f"- {x}" for x in items) if items else empty


def render_agent(ctx, spec, name, scope, rid, children=None, names=None):
    names = names or {}
    tools = tool_list(spec.get("tools"))
    if children and tools and not any(t.split("(")[0] in ("Agent", "Task") for t in tools):
        tools.append("Agent")
    fm = {"name": name}
    desc = spec["description"].strip()
    if scope == "run":
        desc = f"[orchestrator run {rid}] {desc} Invoked by the orchestrator only."
    fm["description"] = desc
    for k in SPEC_FM:
        if k in ("tools",):
            fm[k] = tools
        elif spec.get(k) is not None:
            fm[k] = spec[k]
    if "model" not in fm and ctx.cfg.get("default_model"):
        fm["model"] = ctx.cfg["default_model"]
    deps = [names.get(d, d) for d in spec.get("depends_on", [])]
    lead = ""
    if children:
        lead = ("\n## You lead sub-agents\nYou may delegate (Agent tool) only to: "
                + ", ".join(f"`{c}`" for c in children)
                + ". Give each a precise task, collect their ORCH_REPORT blocks, verify "
                  "their work, and fold it into your own report.\n")
    tmpl = string.Template((SKILL_DIR / "templates" / "agent.md.tmpl").read_text())
    body = tmpl.substitute(
        marker=f"{MARKER} run={rid or '-'} scope={scope} role={spec.get('extends') or spec['name']} -->",
        role=spec["role"],
        run_note=f" (run `{rid}`)" if rid else "",
        objective=spec["objective"].strip(),
        responsibilities=bullets(spec["responsibilities"]),
        expertise=bullets(spec.get("expertise", [])),
        inputs=bullets(spec.get("inputs", []), "- The task message from the orchestrator."),
        outputs=bullets(spec["outputs"]),
        owns=", ".join(f"`{g}`" for g in spec.get("owns", [])) or "nothing (read-only role)",
        must_not=", ".join(f"`{g}`" for g in spec.get("must_not_modify", [])) or
        "anything outside your ownership",
        dependencies=bullets([f"`{d}`" for d in deps], "- None."),
        constraints=bullets(spec.get("constraints", []), "- Follow the project's existing conventions."),
        validation=bullets(spec["validation"]),
        lead=lead,
    )
    return dump_frontmatter(fm) + "\n" + body


def is_ours(path):
    try:
        return MARKER in Path(path).read_text()
    except FileNotFoundError:
        return False


def create_agents(ctx, specs, scope="run", rid=None, force=False):
    """Validate a batch of specs as a unit; write nothing unless all pass."""
    if scope == "run" and not rid:
        raise OrchError("scope 'run' needs a run id (--run)")
    if scope not in ("run", "project", "user"):
        raise OrchError(f"unknown scope {scope!r}")
    specs = [resolve_spec(ctx, s) for s in specs]
    errs, warns = [], []
    bases = [s.get("name") for s in specs]
    dup = {b for b in bases if bases.count(b) > 1}
    if dup:
        errs.append(f"duplicate agent names in team: {sorted(dup)}")
    names = {b: full_name(ctx, b, scope, rid) for b in bases if b}
    for s in specs:
        e, w = validate_spec(s, ctx.cfg)
        errs += [f"{s.get('name')}: {x}" for x in e]
        warns += [f"{s.get('name')}: {x}" for x in w]
        for d in s.get("depends_on", []):
            if d not in names:
                errs.append(f"{s.get('name')}: depends_on unknown agent {d!r}")
        if s.get("parent") and s["parent"] not in names:
            errs.append(f"{s.get('name')}: parent unknown agent {s['parent']!r}")
    if errs:
        raise OrchError("invalid agent specs:\n  " + "\n  ".join(errs))
    # hierarchy depth (main session = 0, its direct agents = 1)
    by = {s["name"]: s for s in specs}
    depth_cap = min(int(ctx.cfg.get("max_depth", 2)),
                    int(os.environ.get("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", 3)))
    for s in specs:
        d, cur, seen = 1, s, set()
        while cur.get("parent"):
            if cur["name"] in seen:
                raise OrchError(f"parent cycle at {cur['name']}")
            seen.add(cur["name"])
            cur, d = by[cur["parent"]], d + 1
        if d > depth_cap:
            raise OrchError(f"{s['name']}: nesting depth {d} exceeds max_depth {depth_cap}")
    if scope == "run" and len(specs) > int(ctx.cfg.get("max_agents", 12)):
        raise OrchError(f"team has {len(specs)} agents; max_agents is {ctx.cfg['max_agents']}. "
                        "Merge roles or raise the limit in config.")
    warns += ownership_overlaps({s["name"]: s.get("owns", []) for s in specs})
    # render + overwrite checks before writing anything
    adir = ctx.agents_dir(scope)
    out = []
    for s in specs:
        name = names[s["name"]]
        path = adir / f"{name}.md"
        if path.exists():
            if not is_ours(path):
                raise OrchError(f"{path} exists and was not generated by agent-orchestrator; "
                                "refusing to overwrite (rename your agent or remove it yourself)")
            if not force:
                raise OrchError(f"{path} already exists (generated). Use --force to regenerate.")
        children = [names[c["name"]] for c in specs if c.get("parent") == s["name"]]
        text = render_agent(ctx, s, name, scope, rid, children, names)
        fm, _ = parse_frontmatter(text)
        e, w = validate_frontmatter(fm, ctx.cfg)
        if e:
            raise OrchError(f"{name}: generated frontmatter invalid: {e}")
        out.append((s, name, path, text))
    adir.mkdir(parents=True, exist_ok=True)
    reg_scope = "user" if scope == "user" else "project"
    reg = ctx.registry(reg_scope)
    for s, name, path, text in out:
        path.write_text(text)
        prev = reg["agents"].get(name, {})
        reg["agents"][name] = {
            "path": str(path), "scope": scope, "run": rid, "base": s["name"],
            "role": s["role"], "extends": s.get("extends"), "created": now(),
            "status": "available", "spec": s, "history": prev.get("history", []),
        }
    ctx.save_registry(reg_scope, reg)
    return [(name, str(path)) for _, name, path, _ in out], warns, names


def glob_prefix(g):
    return re.split(r"[*?\[]", g, maxsplit=1)[0]


def globs_overlap(a, b):
    """Conservative: two globs overlap if either matches the other or their
    literal prefixes nest.  ponytail: heuristic, false positives possible."""
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    pa, pb = glob_prefix(a), glob_prefix(b)
    if (pa == a) and (pb == b):  # both literal paths
        return a == b
    if pa == a:
        return fnmatch.fnmatch(a, b)
    if pb == b:
        return fnmatch.fnmatch(b, a)
    return pa.startswith(pb) or pb.startswith(pa)


def ownership_overlaps(owners):
    warns, keys = [], sorted(owners)
    for i, x in enumerate(keys):
        for y in keys[i + 1:]:
            for a in owners[x]:
                for b in owners[y]:
                    if globs_overlap(a, b):
                        warns.append(f"ownership overlap: {x} `{a}` vs {y} `{b}` — never run "
                                     "these two in the same wave")
    return warns


# ---------------------------------------------------------------------- plans

def extract_plan(text):
    """Accept raw JSON or markdown with a ```json orch-plan fenced block."""
    m = re.search(r"```(?:json)?\s*orch-plan\s*\n(.*?)\n```", text, re.S)
    raw = m.group(1) if m else text
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OrchError(f"plan is not valid JSON ({e}); expected a ```json orch-plan block")
    if not isinstance(plan, dict) or "tasks" not in plan:
        raise OrchError("plan must be an object with 'tasks' (and usually 'team')")
    team = plan.get("team") or {}
    plan["team"] = {"agents": team.get("agents", []) if isinstance(team, dict) else team}
    return plan


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def analyze_plan(tasks, agent_owns, cfg):
    """tasks: list of dicts. agent_owns: {agent base name: [globs]}."""
    errs, warns = [], []
    ids, titles = {}, {}
    for t in tasks:
        tid = t.get("id")
        if not tid or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(tid)):
            errs.append(f"task {tid!r}: id must be letters/digits/._-")
            continue
        if tid in ids:
            errs.append(f"duplicate task id {tid!r}")
        ids[tid] = t
        if not str(t.get("title") or "").strip():
            errs.append(f"{tid}: missing title")
        else:
            nt = norm_title(t["title"])
            if nt in titles:
                errs.append(f"{tid}: duplicate of task {titles[nt]} ({t['title']!r})")
            titles[nt] = tid
        agent = t.get("agent")
        if agent != "orchestrator" and agent not in agent_owns:
            errs.append(f"{tid}: agent {agent!r} is not in the team (or use 'orchestrator')")
    for tid, t in ids.items():
        for d in t.get("depends_on", []):
            if d not in ids:
                errs.append(f"{tid}: depends_on unknown task {d!r}")
    if errs:
        return {"errors": errs, "warnings": warns, "waves": [], "critical_path": []}
    # topological levels (Kahn) -> cycle detection + parallel waves
    level, remaining = {}, dict(ids)
    while remaining:
        ready = [tid for tid, t in remaining.items()
                 if all(d in level for d in t.get("depends_on", []))]
        if not ready:
            errs.append(f"dependency cycle among: {sorted(remaining)}")
            break
        for tid in ready:
            level[tid] = 1 + max([level[d] for d in ids[tid].get("depends_on", [])] or [-1])
            del remaining[tid]
    if errs:
        return {"errors": errs, "warnings": warns, "waves": [], "critical_path": []}
    order = [t["id"] for t in tasks]
    waves = []
    maxp = int(cfg.get("max_parallel", 5))
    for lv in range(max(level.values()) + 1):
        wave = [tid for tid in order if level[tid] == lv]
        for i in range(0, len(wave), maxp):  # split to respect max_parallel
            waves.append(wave[i:i + maxp])
    owns = lambda t: t.get("owns") or agent_owns.get(t["agent"], [])
    for wave in waves:
        for i, a in enumerate(wave):
            for b in wave[i + 1:]:
                for ga in owns(ids[a]):
                    for gb in owns(ids[b]):
                        if globs_overlap(ga, gb):
                            errs.append(f"tasks {a} and {b} would run in parallel but both own "
                                        f"`{ga}`/`{gb}`: add a depends_on or split ownership")
    # critical path = longest chain by weight (default 1)
    best = {}
    for tid in sorted(ids, key=lambda x: level[x]):
        deps = ids[tid].get("depends_on", [])
        prev = max(deps, key=lambda d: best[d][0]) if deps else None
        w = float(ids[tid].get("weight", 1))
        best[tid] = ((best[prev][0] if prev else 0) + w, (best[prev][1] if prev else []) + [tid])
    cp = max(best.values(), key=lambda x: x[0])[1] if best else []
    for tid, t in ids.items():
        if not t.get("acceptance"):
            warns.append(f"{tid}: no acceptance criteria — how will you verify it?")
    return {"errors": errs, "warnings": warns, "waves": waves, "critical_path": cp}


def render_tree(agents, title="Orchestrator (main session)"):
    """Visual team tree. `parent` = real nested delegation; `group` = label only."""
    lines = [title]
    kids = {}
    for a in agents:
        kids.setdefault(a.get("parent"), []).append(a)

    def label(a):
        extra = f" — {a.get('role')}" if a.get("role") else ""
        return f"{a['name']}{extra}"

    def walk(nodes, prefix, grouped=False):
        groups, order = {}, []
        for a in nodes:
            g = None if (grouped or a.get("parent")) else a.get("group")
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append(a)
        items = []
        for g in order:
            if g:
                items.append(("group", g, groups[g]))
            else:
                items += [("agent", a, None) for a in groups[g]]
        for i, (kind, x, members) in enumerate(items):
            last = i == len(items) - 1
            lines.append(prefix + ("└── " if last else "├── ") +
                         (f"{x} [group]" if kind == "group" else label(x)))
            nxt = prefix + ("    " if last else "│   ")
            if kind == "group":
                walk(members, nxt, True)
            elif kids.get(x["name"]):
                walk(kids[x["name"]], nxt)

    walk(kids.get(None, []), "")
    return "\n".join(lines)


def plan_summary(plan, analysis):
    out = [render_tree(plan["team"]["agents"]), ""]
    for i, w in enumerate(analysis["waves"], 1):
        out.append(f"Wave {i}: " + ", ".join(w) + ("  (parallel)" if len(w) > 1 else ""))
    if analysis["critical_path"]:
        out.append("Critical path: " + " -> ".join(analysis["critical_path"]))
    return "\n".join(out)


# ----------------------------------------------------------------- run state

def new_run_id(title):
    slug = re.sub(r"[^a-z0-9]+", "", (title or "run").lower())[:10] or "run"
    return f"{slug}-{uuid.uuid4().hex[:4]}"


def init_run(ctx, plan, title=None, rid=None, plan_file=None, force=False):
    title = title or plan.get("title") or "untitled"
    rid = rid or new_run_id(title)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,20}", rid):
        raise OrchError("run id: lowercase letters/digits/hyphens, max 21 chars")
    if ctx.run_dir(rid).exists():
        raise OrchError(f"run {rid} already exists")
    active = ctx.active_id()
    if active and not force:
        r = read_json(ctx.run_dir(active) / "run.json", {})
        if r.get("status") == "active":
            raise OrchError(f"run {active} is still active; resume it, or `run close` it first "
                            "(or pass --force to start another)")
    agents = [resolve_spec(ctx, a) for a in plan["team"]["agents"]]
    analysis = analyze_plan(plan["tasks"], {a["name"]: a.get("owns", []) for a in agents}, ctx.cfg)
    if analysis["errors"]:
        raise OrchError("invalid plan:\n  " + "\n  ".join(analysis["errors"]))
    created, warns, names = create_agents(ctx, agents, "run", rid) if agents else ([], [], {})
    run = {
        "id": rid, "title": title, "created": now(), "status": "active",
        "plan_file": str(plan_file) if plan_file else None,
        "limits": {k: ctx.cfg.get(k) for k in ("max_agents", "max_parallel", "max_depth", "max_retries")},
        "team": {"agents": agents}, "names": names, "waves": analysis["waves"],
        "critical_path": analysis["critical_path"], "order": [t["id"] for t in plan["tasks"]],
        "tasks": {t["id"]: {
            "title": t["title"], "agent": t["agent"], "depends_on": t.get("depends_on", []),
            "owns": t.get("owns"), "description": t.get("description", ""),
            "acceptance": t.get("acceptance", []), "status": "pending", "attempts": 0,
            "history": []} for t in plan["tasks"]},
    }
    ctx.save_run(run)
    if plan_file:
        shutil.copy(plan_file, ctx.run_dir(rid) / "plan.md")
    ctx.set_active(rid)
    ctx.log(rid, "run_init", agents=[n for n, _ in created], tasks=len(plan["tasks"]))
    return run, warns + analysis["warnings"]


def task_view(run):
    tasks = run["tasks"]
    view = {}
    for tid in run["order"]:
        t = tasks[tid]
        st = t["status"]
        if st == "pending":
            deps = [tasks[d]["status"] for d in t["depends_on"]]
            if any(d in ("failed", "skipped") for d in deps):
                st = "blocked"
            elif all(d == "done" for d in deps):
                st = "ready"
        view[tid] = st
    return view


def agent_status(run, base):
    sts = [t["status"] for t in run["tasks"].values() if t["agent"] == base]
    if not sts:
        return "IDLE"
    if "in_progress" in sts:
        return "RUNNING"
    if "failed" in sts:
        return "FAILED"
    if all(s in ("done", "skipped") for s in sts):
        return "COMPLETED"
    return "PENDING"


def spawn_events(ctx, rid):
    p = ctx.run_dir(rid) / "events.jsonl"
    starts, stops = [], []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event") == "SubagentStart":
                starts.append(e)
            elif e.get("event") == "SubagentStop":
                stops.append(e)
    return starts, stops


def status_text(ctx, run, full=False):
    view = task_view(run)
    total = len(view)
    done = sum(1 for s in view.values() if s in ("done", "skipped"))
    pct = int(100 * done / total) if total else 100
    starts, stops = spawn_events(ctx, run["id"])
    out = [f"PROJECT: {run['title']}   (run {run['id']}, {run['status'].upper()})",
           f"Progress: {done}/{total} tasks ({pct}%)   "
           f"Verified subagent spawns (hook log): {len(starts)} started, {len(stops)} finished", ""]
    out.append("| Agent | Responsibility | Status |")
    out.append("|---|---|---|")
    bases = [a["name"] for a in run["team"]["agents"]]
    if any(t["agent"] == "orchestrator" for t in run["tasks"].values()):
        bases.append("orchestrator")
    for a in run["team"]["agents"]:
        out.append(f"| {run['names'].get(a['name'], a['name'])} | {a.get('role', '')} | "
                   f"{agent_status(run, a['name'])} |")
    if "orchestrator" in bases:
        out.append(f"| orchestrator | integration / validation | {agent_status(run, 'orchestrator')} |")
    out += ["", "| Task | Title | Agent | Status | Attempts | Depends on |", "|---|---|---|---|---|---|"]
    for tid in run["order"]:
        t = run["tasks"][tid]
        out.append(f"| {tid} | {t['title']} | {t['agent']} | {view[tid].upper()} | "
                   f"{t['attempts']} | {', '.join(t['depends_on']) or '-'} |")
    groups = {s: [t for t, v in view.items() if v == s] for s in
              ("ready", "in_progress", "failed", "blocked")}
    out.append("")
    out.append("Ready to start: " + (", ".join(groups["ready"]) or "-"))
    out.append("In progress:    " + (", ".join(groups["in_progress"]) or "-"))
    out.append("Failed:         " + (", ".join(groups["failed"]) or "-"))
    out.append("Blocked:        " + (", ".join(groups["blocked"]) or "-"))
    if full:
        out += ["", "## Task reports"]
        for tid in run["order"]:
            t = run["tasks"][tid]
            out.append(f"\n### {tid} — {t['title']} [{t['status']}]")
            rep = t.get("report")
            if rep:
                for k in REPORT_KEYS:
                    if k in rep:
                        out.append(f"- **{k}**: {json.dumps(rep[k]) if not isinstance(rep[k], str) else rep[k]}")
            for h in t["history"]:
                out.append(f"- {h['ts']} {h['event']}" + (f": {h['note']}" if h.get("note") else ""))
            if t.get("ownership_warnings"):
                out.append("- ownership warnings: " + "; ".join(t["ownership_warnings"]))
    return "\n".join(out)


def parse_report(text):
    m = re.search(r"```\s*ORCH_REPORT\s*\n(.*?)\n```", text, re.S) or \
        re.search(r"```json\s*\n(.*?)\n```", text, re.S)
    raw = m.group(1) if m else text.strip()
    try:
        rep = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OrchError(f"report is not valid JSON ({e}); expected an ORCH_REPORT block")
    if not isinstance(rep, dict):
        raise OrchError("report must be a JSON object")
    return rep, [k for k in REPORT_KEYS if k not in rep]


def record_history(ctx, run, base, tid, result):
    name = run["names"].get(base)
    if not name:
        return
    reg = ctx.registry("project")
    entry = reg["agents"].get(name)
    if entry:
        entry.setdefault("history", []).append({"run": run["id"], "task": tid, "result": result, "ts": now()})
        entry["status"] = {"done": "completed", "failed": "failed", "start": "active"}.get(result, result)
        ctx.save_registry("project", reg)


def owned_by(path, globs):
    return any(fnmatch.fnmatch(path, g) or path == g.rstrip("/") or
               (g.endswith("/") and path.startswith(g)) for g in globs)


# ---------------------------------------------------------------- commands

def out(obj, as_json):
    print(json.dumps(obj, indent=2) if as_json else obj)


def cmd_env(ctx, a):
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    inside = os.environ.get("CLAUDECODE") == "1"
    surface = {"cli": "Claude Code CLI", "claude-vscode": "Claude Code VS Code extension",
               "claude-desktop": "Claude Desktop (Code tab)"}.get(
        entry, f"Claude Code ({entry})" if entry else
        ("Claude Code (entrypoint unknown)" if inside else "not inside Claude Code"))
    settings = []
    for p in (ctx.cfgdir / "settings.json", ctx.project / ".claude" / "settings.json",
              ctx.project / ".claude" / "settings.local.json"):
        s = read_json(p, None) if p.exists() else None
        if isinstance(s, dict):
            settings.append((p, s))
    teams = os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") == "1" or any(
        str((s.get("env") or {}).get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")) == "1" for _, s in settings)
    plugin = any(any(k.startswith("agent-orchestrator@") and v for k, v in
                     (s.get("enabledPlugins") or {}).items()) for _, s in settings)
    hooks_in_settings = any("orch.py" in json.dumps(s.get("hooks", {})) for _, s in settings)
    via = "plugin" if IN_PLUGIN else "skill directory"
    git = run(["git", "-C", str(ctx.project), "rev-parse", "--is-inside-work-tree"]) == "true"
    depth = os.environ.get("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", "3 (default)")
    agents_dir = ctx.project / ".claude" / "agents"
    info = {
        "orchestrator_version": VERSION, "python": sys.version.split()[0],
        "claude_version": run(["claude", "--version"]) or "unknown (claude not on PATH)",
        "surface": surface, "entrypoint": entry or None, "inside_claude_code": inside,
        "project": str(ctx.project), "skill_dir": str(SKILL_DIR), "installed_via": via,
        "plugin_enabled": plugin, "hooks_in_settings": hooks_in_settings,
        "agent_teams_flag": teams, "max_spawn_depth": depth, "git_repo": git,
        "project_agents_dir_exists": agents_dir.is_dir(),
        "config": ctx.cfg,
    }
    caps = [
        ("Subagents (Agent tool)", "yes" if inside else "no",
         "claude.ai / no Agent tool: run the plan yourself role-by-role and SAY no agents were spawned"),
        ("Runtime agent files hot-reload", "yes" if agents_dir.is_dir() else "after restart",
         "if a generated agent is 'not found': spawn general-purpose with `orch.py agent prompt NAME`"),
        ("Nested delegation", f"depth {depth}", "flatten: orchestrator delegates directly"),
        ("Agent teams", "flag on (interactive sessions only)" if teams else "off",
         "subagents + orch.py task state (teams are unavailable in -p, SDK, Desktop)"),
        ("Plan Mode auto-trigger hook", "yes" if (plugin or hooks_in_settings or IN_PLUGIN) else "not detected",
         "invoke /orchestrate manually or install hooks: install.py"),
        ("Worktree isolation", "yes" if git else "no (not a git repo)", "rely on file ownership"),
        ("Ownership enforcement hook", "yes" if (plugin or hooks_in_settings or IN_PLUGIN) else "not detected",
         "ownership is instruction-only; audit with `task done` report"),
    ]
    if a.json:
        info["capabilities"] = [{"capability": c, "status": s, "fallback": f} for c, s, f in caps]
        return out(info, True)
    lines = [f"agent-orchestrator {VERSION} | {surface} | {info['claude_version']} | python {info['python']}",
             f"project: {ctx.project}  (git: {'yes' if git else 'no'})",
             f"skill: {SKILL_DIR} (via {via})", "",
             "| Capability | Status | Fallback |", "|---|---|---|"]
    lines += [f"| {c} | {s} | {f} |" for c, s, f in caps]
    lines.append("")
    lines.append("limits: " + ", ".join(f"{k}={ctx.cfg.get(k)}" for k in
                                        ("max_agents", "max_parallel", "max_depth", "max_retries")))
    print("\n".join(lines))


def cmd_init(ctx, a):
    for d in (ctx.runs(), ctx.project / ".claude" / "agents"):
        d.mkdir(parents=True, exist_ok=True)
    print(f"initialized {ctx.orch} and {ctx.project / '.claude' / 'agents'}")
    print("note: if .claude/agents/ did not exist when this session started, generated agents "
          "may need a restart (or use the general-purpose fallback).")


def cmd_agent(ctx, a):
    if a.action == "create":
        data = json.loads(load_input(a.spec))
        specs = data["agents"] if isinstance(data, dict) and "agents" in data else \
            (data if isinstance(data, list) else [data])
        rid = a.run or (ctx.active_id() if a.scope == "run" else None)
        created, warns, _ = create_agents(ctx, specs, a.scope, rid, a.force)
        for n, p in created:
            print(f"created {n}  ->  {p}")
        for w in warns:
            print(f"warning: {w}")
    elif a.action == "validate":
        targets = list(a.files) + list(a.names)
        if a.all or not targets:
            for d in (ctx.agents_dir("project"), ctx.agents_dir("user")):
                targets += sorted(str(p) for p in d.glob("*.md")) if d.is_dir() else []
        bad = 0
        for f in targets:
            p = Path(f)
            if not p.exists():
                scope, entry = ctx.find_registered(f)
                p = Path(entry["path"]) if entry else p
            try:
                fm, body = parse_frontmatter(p.read_text())
                e, w = validate_frontmatter(fm, ctx.cfg)
                if not body.strip():
                    w.append("empty body: the agent has no system prompt")
                w += lint_text("description", fm.get("description", "") if isinstance(fm.get("description"), str) else "")
            except (OrchError, OSError) as ex:
                e, w = [str(ex)], []
            bad += bool(e)
            print(f"{'FAIL' if e else 'OK  '} {p}")
            for x in e:
                print(f"   error: {x}")
            for x in w:
                print(f"   warning: {x}")
        if bad:
            raise OrchError(f"{bad} invalid agent definition(s)")
    elif a.action == "list":
        rows = []
        for scope in ("project", "user"):
            for n, e in sorted(ctx.registry(scope)["agents"].items()):
                fails = sum(1 for h in e.get("history", []) if h["result"] == "failed")
                ok = sum(1 for h in e.get("history", []) if h["result"] == "done")
                exists = Path(e["path"]).exists()
                rows.append({"name": n, "scope": e["scope"], "run": e.get("run"), "role": e["role"],
                             "status": e["status"] if exists else "removed", "done": ok, "failed": fails})
        if a.json:
            return out(rows, True)
        print("| Agent | Scope | Run | Role | Status | Done | Failed |\n|---|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['name']} | {r['scope']} | {r['run'] or '-'} | {r['role']} | {r['status']} | "
                  f"{r['done']} | {r['failed']} |")
        for d in (ctx.agents_dir("project"), ctx.agents_dir("user")):
            if d.is_dir():
                others = [p.stem for p in d.glob("*.md") if not is_ours(p)]
                if others:
                    print(f"\nhand-written agents in {d} (not managed, never modified): {', '.join(sorted(others))}")
    elif a.action == "remove":
        for name in a.names:
            scope, entry = ctx.find_registered(name)
            if not entry:
                raise OrchError(f"{name} is not a registered generated agent; not touching it")
            p = Path(entry["path"])
            if p.exists() and not is_ours(p):
                raise OrchError(f"{p} lost its generated marker (hand-edited?); remove it yourself")
            if entry["scope"] != "run" and not a.force:
                raise OrchError(f"{name} is a reusable {entry['scope']}-scope agent; pass --force")
            if p.exists():
                p.unlink()
            reg = ctx.registry(scope)
            reg["agents"][name]["status"] = "removed"
            ctx.save_registry(scope, reg)
            print(f"removed {p}")
    elif a.action == "prompt":
        scope, entry = ctx.find_registered(a.names[0])
        p = Path(entry["path"]) if entry else Path(a.names[0])
        if not p.exists():
            raise OrchError(f"agent {a.names[0]!r} not found")
        fm, body = parse_frontmatter(p.read_text())
        body = re.sub(r"^<!-- agent-orchestrator:generated.*?-->\n?", "", body.lstrip(), flags=re.S)
        print(f"<agent-definition name=\"{fm['name']}\">\n{body.strip()}\n</agent-definition>\n\n"
              "Follow the agent definition above as your role for this task.")
    elif a.action == "promote":
        scope, entry = ctx.find_registered(a.names[0])
        if not entry:
            raise OrchError(f"{a.names[0]} is not a registered generated agent")
        spec = dict(entry["spec"])
        spec["name"] = a.as_name or entry["base"]
        spec.pop("depends_on", None)
        spec.pop("parent", None)
        created, warns, _ = create_agents(ctx, [spec], a.to, None, a.force)
        for n, p in created:
            print(f"promoted {a.names[0]} -> {n} ({a.to} scope) at {p}")
        for w in warns:
            print(f"warning: {w}")


def cmd_team(ctx, a):
    data = json.loads(load_input(a.spec))
    specs = data["agents"] if isinstance(data, dict) else data
    rid = a.run or ctx.active_id()
    created, warns, names = create_agents(ctx, specs, "run", rid, a.force)
    if rid and ctx.run_dir(rid).exists():
        run = ctx.load_run(rid)
        known = {x["name"] for x in run["team"]["agents"]}
        run["team"]["agents"] += [resolve_spec(ctx, s) for s in specs if s["name"] not in known]
        run["names"].update(names)
        ctx.save_run(run)
        write_json(ctx.run_dir(rid) / "team.json", run["team"])
    for n, p in created:
        print(f"created {n}  ->  {p}")
    for w in warns:
        print(f"warning: {w}")
    print()
    print(render_tree([resolve_spec(ctx, s) for s in specs]))


def cmd_plan(ctx, a):
    if a.run or not a.file:
        run = ctx.load_run(a.run)
        plan = {"team": run["team"], "tasks": [dict(id=k, **run["tasks"][k]) for k in run["order"]]}
    else:
        plan = extract_plan(load_input(a.file))
    agents = [resolve_spec(ctx, s) for s in plan["team"]["agents"]]
    plan["team"]["agents"] = agents
    errs = []
    for s in agents:
        e, w = validate_spec(s, ctx.cfg)
        errs += [f"agent {s.get('name')}: {x}" for x in e]
    if len(agents) > int(ctx.cfg.get("max_agents", 12)):
        errs.append(f"team has {len(agents)} agents; max_agents is {ctx.cfg['max_agents']}")
    an = analyze_plan(plan["tasks"], {s["name"]: s.get("owns", []) for s in agents}, ctx.cfg)
    an["errors"] = errs + an["errors"]
    an["warnings"] += ownership_overlaps({s["name"]: s.get("owns", []) for s in agents})
    if a.json:
        out(an, True)
    else:
        print(plan_summary(plan, an) if not an["errors"] else "PLAN INVALID")
        for e in an["errors"]:
            print(f"error: {e}")
        for w in an["warnings"]:
            print(f"warning: {w}")
        if a.mermaid and not an["errors"]:
            print("\n```mermaid\nflowchart LR")
            for t in plan["tasks"]:
                print(f"  {t['id'].replace('.', '_').replace('-', '_')}[\"{t['id']}: {t['title']} ({t['agent']})\"]")
                for d in t.get("depends_on", []):
                    print(f"  {d.replace('.', '_').replace('-', '_')} --> {t['id'].replace('.', '_').replace('-', '_')}")
            print("```")
    if an["errors"]:
        raise OrchError(f"{len(an['errors'])} plan error(s)")


def cmd_run(ctx, a):
    if a.action == "init":
        if a.from_plan:
            plan = extract_plan(load_input(a.from_plan))
        else:
            raise OrchError("run init needs --from-plan FILE (markdown with orch-plan block, or JSON)")
        run, warns = init_run(ctx, plan, a.title, a.id, a.from_plan if a.from_plan != "-" else None, a.force)
        print(f"run {run['id']} initialized and active: {len(run['tasks'])} tasks, "
              f"{len(run['team']['agents'])} agents")
        for base, name in run["names"].items():
            print(f"  agent {base:<18} -> subagent_type \"{name}\"")
        print(plan_summary(run, {"waves": run["waves"], "critical_path": run["critical_path"]}))
        for w in warns:
            print(f"warning: {w}")
    elif a.action == "close":
        run = ctx.load_run(a.run)
        removed = []
        if not a.keep_agents:
            reg = ctx.registry("project")
            for name in run["names"].values():
                e = reg["agents"].get(name)
                if e and e["scope"] == "run" and Path(e["path"]).exists() and is_ours(e["path"]):
                    Path(e["path"]).unlink()
                    e["status"] = "removed"
                    removed.append(name)
            ctx.save_registry("project", reg)
        run["status"] = a.status
        ctx.save_run(run)
        (ctx.run_dir(run["id"]) / "report.md").write_text(status_text(ctx, run, True) + "\n")
        if ctx.active_id() == run["id"]:
            ctx.set_active(None)
        ctx.log(run["id"], "run_close", status=a.status, removed=removed)
        print(f"run {run['id']} closed as {a.status}; report: {ctx.run_dir(run['id']) / 'report.md'}")
        if removed:
            print("removed temporary agents: " + ", ".join(removed))
    elif a.action == "list":
        active = ctx.active_id()
        for d in sorted(ctx.runs().glob("*/run.json")) if ctx.runs().exists() else []:
            r = read_json(d, {})
            v = task_view(r)
            done = sum(1 for s in v.values() if s in ("done", "skipped"))
            print(f"{'*' if r['id'] == active else ' '} {r['id']:<16} {r['status']:<10} "
                  f"{done}/{len(v)} tasks  {r['title']}")
    elif a.action == "activate":
        ctx.load_run(a.run)
        ctx.set_active(a.run)
        print(f"active run: {a.run}")


def cmd_task(ctx, a):
    run = ctx.load_run(a.run)
    if a.action == "add":
        if a.id in run["tasks"]:
            raise OrchError(f"task {a.id} already exists")
        nt = norm_title(a.title)
        dup = [k for k, t in run["tasks"].items() if norm_title(t["title"]) == nt]
        if dup:
            raise OrchError(f"duplicate of task {dup[0]} ({a.title!r})")
        bases = {x["name"] for x in run["team"]["agents"]} | {"orchestrator"}
        if a.agent not in bases:
            raise OrchError(f"agent {a.agent!r} not in team ({', '.join(sorted(bases))})")
        for d in a.depends_on:
            if d not in run["tasks"]:
                raise OrchError(f"unknown dependency {d}")
        run["tasks"][a.id] = {"title": a.title, "agent": a.agent, "depends_on": a.depends_on,
                              "owns": a.owns or None, "description": a.description or "",
                              "acceptance": a.acceptance, "status": "pending", "attempts": 0,
                              "history": [{"ts": now(), "event": "added"}]}
        run["order"].append(a.id)
        ctx.save_run(run)
        ctx.log(run["id"], "task_add", task=a.id)
        print(f"added task {a.id} -> {a.agent}")
        return
    if a.id not in run["tasks"]:
        raise OrchError(f"unknown task {a.id!r}")
    t = run["tasks"][a.id]
    limit = 1 + int(ctx.cfg.get("max_retries", 2))
    if a.action == "start":
        if t["status"] == "done":
            raise OrchError(f"{a.id} is already done (completed {t.get('finished')}); not re-running")
        if t["status"] == "in_progress" and not a.force:
            raise OrchError(f"{a.id} is already in progress (started {t.get('started')}); "
                            "use --force only if that attempt is dead")
        pend = [d for d in t["depends_on"] if run["tasks"][d]["status"] != "done"]
        if pend and not a.force:
            raise OrchError(f"{a.id} waits on unfinished dependencies: {', '.join(pend)}")
        if t["attempts"] >= limit:
            raise OrchError(f"{a.id} reached max attempts ({limit}); escalate to the user")
        t.update(status="in_progress", started=now())
        t["attempts"] += 1
        t["history"].append({"ts": now(), "event": f"start (attempt {t['attempts']})"})
        record_history(ctx, run, t["agent"], a.id, "start")
        msg = f"{a.id} started (attempt {t['attempts']}/{limit})"
        name = run["names"].get(t["agent"])
        if name:
            msg += f" — delegate with subagent_type \"{name}\""
    elif a.action == "done":
        warns = []
        if a.report:
            rep, missing = parse_report(load_input(a.report))
            t["report"] = rep
            if missing:
                warns.append(f"report missing fields: {', '.join(missing)}")
            if str(rep.get("status", "")).lower() not in ("done", "completed", "complete", "success"):
                warns.append(f"agent reported status {rep.get('status')!r}, not done — "
                             "verify before accepting")
            globs = t.get("owns") or next((x.get("owns", []) for x in run["team"]["agents"]
                                           if x["name"] == t["agent"]), [])
            files = rep.get("files_changed") or []
            if isinstance(files, list) and t["agent"] != "orchestrator":
                outside = [f for f in files if isinstance(f, str) and
                           not owned_by(os.path.relpath(f, ctx.project) if os.path.isabs(f) else f, globs)]
                if outside:
                    w = f"files outside {t['agent']}'s ownership: {', '.join(outside)}"
                    t["ownership_warnings"] = [w]
                    warns.append(w)
        elif not a.no_report:
            raise OrchError("task done needs --report FILE|- (the agent's ORCH_REPORT) or --no-report")
        t.update(status="done", finished=now())
        t["history"].append({"ts": now(), "event": "done", "note": a.note or ""})
        record_history(ctx, run, t["agent"], a.id, "done")
        msg = f"{a.id} done" + "".join(f"\nwarning: {w}" for w in warns)
        nxt = [k for k, v in task_view(run).items() if v == "ready"]
        msg += f"\nready now: {', '.join(nxt) or '-'}"
    elif a.action == "fail":
        t.update(status="failed", finished=now())
        t["history"].append({"ts": now(), "event": "failed", "note": a.reason or ""})
        record_history(ctx, run, t["agent"], a.id, "failed")
        left = limit - t["attempts"]
        msg = f"{a.id} failed ({left} attempt(s) left)" + \
              ("; `task retry` with feedback" if left > 0 else "; retry limit reached — escalate to user")
    elif a.action == "retry":
        if t["status"] not in ("failed", "in_progress"):
            raise OrchError(f"{a.id} is {t['status']}; only failed/in_progress tasks can be retried")
        if t["attempts"] >= limit:
            raise OrchError(f"{a.id} reached max attempts ({limit}); escalate to the user")
        t["status"] = "pending"
        t["history"].append({"ts": now(), "event": "retry", "note": a.reason or ""})
        msg = f"{a.id} reset to pending (attempts so far {t['attempts']}/{limit})"
    elif a.action == "skip":
        t.update(status="skipped", finished=now())
        t["history"].append({"ts": now(), "event": "skipped", "note": a.reason or ""})
        msg = f"{a.id} skipped"
    ctx.save_run(run)
    ctx.log(run["id"], f"task_{a.action}", task=a.id)
    print(msg)


def cmd_status(ctx, a):
    run = ctx.load_run(a.run)
    if a.json:
        v = task_view(run)
        return out({"run": run["id"], "title": run["title"], "status": run["status"],
                    "tasks": v, "agents": {run["names"].get(x["name"], x["name"]): agent_status(run, x["name"])
                                           for x in run["team"]["agents"]}}, True)
    text = status_text(ctx, run, a.full)
    if a.full:
        (ctx.run_dir(run["id"]) / "report.md").write_text(text + "\n")
        text += f"\n\n(report written to {ctx.run_dir(run['id']) / 'report.md'})"
    print(text)


def cmd_stats(ctx, a):
    roles = {}
    for p in sorted(ctx.runs().glob("*/run.json")) if ctx.runs().exists() else []:
        r = read_json(p, {})
        role_of = {x["name"]: x.get("extends") or x["name"] for x in r.get("team", {}).get("agents", [])}
        for t in r.get("tasks", {}).values():
            key = role_of.get(t["agent"], t["agent"])
            s = roles.setdefault(key, {"tasks": 0, "done": 0, "failed_attempts": 0, "retried": 0})
            s["tasks"] += 1
            s["done"] += t["status"] == "done"
            s["failed_attempts"] += sum(1 for h in t["history"] if h["event"] == "failed")
            s["retried"] += t["attempts"] > 1
    flagged = [k for k, s in roles.items() if s["failed_attempts"] >= 2 and
               s["failed_attempts"] >= s["tasks"] / 2]
    lint = []
    reg = ctx.registry("project")
    for n, e in reg["agents"].items():
        if Path(e["path"]).exists():
            _, w = validate_spec(e["spec"], ctx.cfg)
            lint += [f"{n}: {x}" for x in w]
    if a.json:
        return out({"roles": roles, "flagged": flagged, "lint": lint}, True)
    print("| Role | Tasks | Done | Failed attempts | Retried |\n|---|---|---|---|---|")
    for k, s in sorted(roles.items()):
        print(f"| {k} | {s['tasks']} | {s['done']} | {s['failed_attempts']} | {s['retried']} |")
    if flagged:
        print("\nRepeatedly failing roles (tighten their instructions, scope or tools): " + ", ".join(flagged))
    if lint:
        print("\nAgent definition lint:\n  " + "\n  ".join(lint))
    lessons = ctx.user_orch / "lessons.jsonl"
    if lessons.exists():
        print(f"\n{len(lessons.read_text().splitlines())} recorded lesson(s): `orch.py lessons list`")


def cmd_lessons(ctx, a):
    p = ctx.user_orch / "lessons.jsonl"
    if a.action == "add":
        text = " ".join(a.text).strip()
        if not text:
            raise OrchError("lesson text is empty")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"ts": now(), "tags": a.tag, "project": str(ctx.project),
                                "lesson": text}) + "\n")
        print("lesson recorded")
    else:
        rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        if a.tag:
            rows = [r for r in rows if set(a.tag) & set(r.get("tags", []))]
        for r in rows[-a.limit:]:
            print(f"- [{', '.join(r.get('tags', [])) or 'general'}] {r['lesson']}")
        if not rows:
            print("(no lessons recorded yet)")


# ---------------------------------------------------------------------- hooks

def hook_output(event, **fields):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))


def skill_ref():
    return "agent-orchestrator:orchestrate" if IN_PLUGIN else "orchestrate"


def cmd_hook(ctx_unused, a):
    """Hooks must never break the session: any internal error -> silent exit 0."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    if os.environ.get("ORCH_HOOK_LOG"):  # troubleshooting: record every hook invocation
        with open(os.environ["ORCH_HOOK_LOG"], "a") as f:
            f.write(json.dumps({"ts": now(), "event": a.event, "payload": payload}) + "\n")
    try:
        project =os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
        ctx = Ctx(find_project(project) if project and not os.environ.get("ORCH_PROJECT_DIR") else None)
        globals()[f"hook_{a.event}"](ctx, payload)
    except Exception as e:  # noqa: BLE001 — a hook crash must not block the user
        print(f"agent-orchestrator hook {a.event}: {e}", file=sys.stderr)


MULTIPART = re.compile(r"\b(build|implement|develop|create|design|research|report|application|app|"
                       r"system|platform|pipeline|migrat\w*|refactor|end-to-end|full[- ]stack|"
                       r"service|api|saas|team|agents?)\b", re.I)


def hook_prompt(ctx, p):
    prompt = p.get("prompt") or ""
    if "[no-orchestrate]" in prompt or prompt.lstrip().startswith("/"):
        return
    orch = f'python3 "{Path(__file__).resolve()}"'
    if p.get("permission_mode") == "plan":
        if not ctx.cfg.get("plan_mode_trigger", True):
            return
        return hook_output("UserPromptSubmit", additionalContext=(
            f"[agent-orchestrator] Plan Mode is active. Use the `{skill_ref()}` skill workflow for this "
            "request (load it with the Skill tool if it is not already loaded in this session). "
            "Size the effort first: if the request is trivial, say so and plan it directly without a team; "
            "otherwise design the smallest effective agent team and write the plan (with its "
            f"```json orch-plan block) to the plan file. Orchestrator CLI: {orch}. "
            "Do not create agent files until the plan is approved."))
    # Outside Plan Mode: a soft nudge for prompts that look like multi-part work.
    # ponytail: keyword+length heuristic; the skill itself makes the real sizing decision.
    if not ctx.cfg.get("suggest_outside_plan_mode", True):
        return
    if len(prompt.split()) < int(ctx.cfg.get("suggest_min_words", 10)) or not MULTIPART.search(prompt):
        return
    hook_output("UserPromptSubmit", additionalContext=(
        f"[agent-orchestrator] This request may be multi-part work. Before starting, load the "
        f"`{skill_ref()}` skill with the Skill tool and follow its sizing step: it decides between "
        "doing the work directly, one specialist, or a team. Do not skip loading it based on your own "
        "estimate of effort; the user installed it to get specialist teams, validation and resumable "
        "state for multi-component work. Small single-area tasks stay direct under the skill too."))


def hook_session(ctx, p):
    rid = ctx.active_id()
    if not rid:
        return
    run = read_json(ctx.run_dir(rid) / "run.json")
    if not run or run.get("status") != "active":
        return
    v = task_view(run)
    done = sum(1 for s in v.values() if s in ("done", "skipped"))
    if done == len(v):
        return
    orch = f'python3 "{Path(__file__).resolve()}"'
    stale = [k for k, s in v.items() if s == "in_progress"]
    hook_output("SessionStart", additionalContext=(
        f"[agent-orchestrator] Unfinished orchestration run `{rid}` ({run['title']}): "
        f"{done}/{len(v)} tasks done" + (f"; interrupted while in progress: {', '.join(stale)}" if stale else "")
        + f". If the user wants to continue it, run `{orch} status` and resume with the "
        f"`{skill_ref()}` skill (resume section): do not repeat done tasks."))


def hook_pretool(ctx, p):
    tool = p.get("tool_name")
    if tool not in WRITE_TOOLS:
        return
    ti = p.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("notebook_path")
    if not target:
        return
    cwd = Path(p.get("cwd") or ctx.project)
    path = (cwd / target).resolve() if not os.path.isabs(target) else Path(target).resolve()
    # 1. core protection: the orchestrator's own skill/plugin files
    core = SKILL_DIR.parent.parent if IN_PLUGIN else SKILL_DIR
    if ctx.cfg.get("protect_core", True) and (path == core or core in path.parents):
        return hook_output("PreToolUse", permissionDecision="ask", permissionDecisionReason=(
            "agent-orchestrator: this edits the orchestration system itself. Core changes need "
            "explicit user approval."))
    # 2. file ownership for orchestrator-managed subagents
    agent = p.get("agent_type")
    if not agent or not ctx.cfg.get("enforce_ownership", True):
        return
    rid = ctx.active_id()
    run = read_json(ctx.run_dir(rid) / "run.json") if rid else None
    if not run:
        return
    base = next((b for b, n in run["names"].items() if n == agent), None)
    if base is None:
        base = fallback_identity(ctx, rid, run, p)
    if base is None:
        return  # not one of ours (Explore, user agents, unrelated general-purpose work)
    spec = next(x for x in run["team"]["agents"] if x["name"] == base)
    scratch = p.get("scratchpad_dir")
    if scratch and (path == Path(scratch).resolve() or Path(scratch).resolve() in path.parents):
        return  # the session's own temp space is never project ownership
    try:
        rel = path.relative_to(ctx.project).as_posix()
    except ValueError:
        rel = None
    if rel is None:
        reason = f"{path} is outside the project"
    elif owned_by(rel, spec.get("must_not_modify", [])):
        reason = f"`{rel}` is in {base}'s must_not_modify list"
    elif not owned_by(rel, spec.get("owns", [])):
        reason = (f"`{rel}` is outside {base}'s ownership "
                  f"({', '.join(spec.get('owns', [])) or 'read-only'})")
    else:
        return
    ctx.log(rid, "ownership_denied", agent=agent, path=str(rel or path))
    hook_output("PreToolUse", permissionDecision="deny", permissionDecisionReason=(
        f"agent-orchestrator: {reason}. Do not work around this; list the needed change under "
        "`issues` in your ORCH_REPORT so the orchestrator can route it to the owner."))


def fallback_identity(ctx, rid, run, p):
    """A general-purpose agent running one of our definitions (the 'agent not found' fallback):
    identify it from the generated agent name in its first prompt so ownership still applies.
    ponytail: relies on the observed transcript layout <session>/subagents/agent-<id>.jsonl
    (undocumented); if it changes, fallback agents just go unrestricted, as before."""
    aid, tp = p.get("agent_id"), p.get("transcript_path")
    if not aid or not tp:
        return None
    cache_path = ctx.run_dir(rid) / "fallback_agents.json"
    cache = read_json(cache_path, {}) or {}
    if aid in cache:
        return cache[aid]
    sub = Path(tp[:-6] if tp.endswith(".jsonl") else tp) / "subagents" / f"agent-{aid}.jsonl"
    try:
        with open(sub) as f:
            first = f.readline()
    except OSError:
        return None
    found = None
    for b, n in sorted(run["names"].items(), key=lambda kv: -len(kv[1])):
        if n in first:
            found = b
            break
    cache[aid] = found
    write_json(cache_path, cache)
    return found


def hook_subagent(ctx, p):
    rid = ctx.active_id()
    if not rid or not ctx.run_dir(rid).exists():
        return
    ctx.log(rid, p.get("hook_event_name", "Subagent"), agent_id=p.get("agent_id"),
            agent_type=p.get("agent_type"), session=p.get("session_id"))


# ------------------------------------------------------------------------ cli

def build_parser():
    ap = argparse.ArgumentParser(prog="orch.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", help="project root (default: auto-detect / cwd)")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("env", help="detect environment and report capabilities")
    s.add_argument("--json", action="store_true")
    sub.add_parser("init", help="create .claude/orchestrator and .claude/agents")

    s = sub.add_parser("agent", help="agent factory: create|validate|list|remove|prompt|promote")
    s.add_argument("action", choices=["create", "validate", "list", "remove", "prompt", "promote"])
    s.add_argument("names", nargs="*", help="agent names (remove/prompt/promote)")
    s.add_argument("--spec", default="-", help="JSON spec file or - for stdin (create)")
    s.add_argument("--scope", default="run", choices=["run", "project", "user"])
    s.add_argument("--run")
    s.add_argument("--force", action="store_true")
    s.add_argument("--files", nargs="*", default=[], help="agent files to validate")
    s.add_argument("--all", action="store_true", help="validate all project+user agents")
    s.add_argument("--json", action="store_true")
    s.add_argument("--to", default="project", choices=["project", "user"], help="promote target")
    s.add_argument("--as", dest="as_name", help="promote: new reusable name")

    s = sub.add_parser("team", help="create a run's agent team from a team spec")
    s.add_argument("action", choices=["create"])
    s.add_argument("--spec", default="-")
    s.add_argument("--run")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("plan", help="validate a plan: DAG, waves, critical path, ownership")
    s.add_argument("--check", dest="file", help="plan markdown (orch-plan block) or JSON; - for stdin")
    s.add_argument("--run", help="render an existing run's plan instead")
    s.add_argument("--mermaid", action="store_true")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("run", help="run lifecycle: init|close|list|activate")
    s.add_argument("action", choices=["init", "close", "list", "activate"])
    s.add_argument("--from-plan")
    s.add_argument("--title")
    s.add_argument("--id")
    s.add_argument("--run")
    s.add_argument("--status", default="completed", choices=["completed", "aborted", "failed"])
    s.add_argument("--keep-agents", action="store_true")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("task", help="task state: add|start|done|fail|retry|skip")
    s.add_argument("action", choices=["add", "start", "done", "fail", "retry", "skip"])
    s.add_argument("id")
    s.add_argument("--run")
    s.add_argument("--report", help="ORCH_REPORT text/JSON file, or - for stdin")
    s.add_argument("--no-report", action="store_true")
    s.add_argument("--reason")
    s.add_argument("--note")
    s.add_argument("--force", action="store_true")
    s.add_argument("--title")
    s.add_argument("--agent")
    s.add_argument("--depends-on", nargs="*", default=[])
    s.add_argument("--owns", nargs="*", default=[])
    s.add_argument("--acceptance", nargs="*", default=[])
    s.add_argument("--description")

    s = sub.add_parser("status", help="execution report for a run")
    s.add_argument("--run")
    s.add_argument("--full", action="store_true", help="include reports; writes report.md")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("stats", help="cross-run quality stats and agent lint")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("lessons", help="record/list reusable execution lessons")
    s.add_argument("action", choices=["add", "list"])
    s.add_argument("text", nargs="*")
    s.add_argument("--tag", action="append", default=[], help="repeatable: --tag web --tag auth")
    s.add_argument("--limit", type=int, default=30)

    s = sub.add_parser("hook", help="(internal) Claude Code hook entrypoints")
    s.add_argument("event", choices=["prompt", "session", "pretool", "subagent"])
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "task" and a.action == "add" and not (a.title and a.agent):
        print("error: task add needs --title and --agent", file=sys.stderr)
        return 2
    try:
        if a.cmd == "hook":
            cmd_hook(None, a)
            return 0
        ctx = Ctx(a.project)
        {"env": cmd_env, "init": cmd_init, "agent": cmd_agent, "team": cmd_team, "plan": cmd_plan,
         "run": cmd_run, "task": cmd_task, "status": cmd_status, "stats": cmd_stats,
         "lessons": cmd_lessons}[a.cmd](ctx, a)
        return 0
    except OrchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
