"""Unit tests for the skill's Python scripts. Run by scripts/self-test.sh.

Each fix to a script should add a case here that fails without the fix.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


waves = load("plan_waves", "plan-waves.py")
journal = load("journal", "journal.py")
log_event = load("log_event", "log-event.py")


SKILL_DIR = os.path.dirname(SCRIPTS)
DASHBOARD_DIR = os.path.join(os.path.dirname(SKILL_DIR), "bmad-batch-dashboard")


def frontmatter_errors(text):
    """Problems a strict YAML parser (the `skills` installer) finds in a file's
    frontmatter. Claude Code reads frontmatter leniently, so a broken one still
    works locally and only fails on a fresh install."""
    if not text.startswith("---"):
        return ["no frontmatter"]
    head = text.split("---", 2)[1]
    try:
        import yaml
    except ImportError:
        # Without PyYAML, catch the mistake that happened: a quote inside a
        # single-quoted value (it must be doubled, '').
        bad = [k for k, v in (ln.split(":", 1) for ln in head.splitlines() if ":" in ln and not ln.startswith(" "))
               if v.strip().startswith("'") and "'" in v.strip()[1:-1].replace("''", "")]
        return [f"unescaped quote in {k}" for k in bad]
    try:
        data = yaml.safe_load(head)
    except yaml.YAMLError as exc:
        return [str(exc).splitlines()[0]]
    return [] if isinstance(data, dict) and data.get("name") and data.get("description") else ["no name or description"]


class Frontmatter(unittest.TestCase):
    def test_skill_and_agents_parse_strictly(self):
        files = [os.path.join(SKILL_DIR, "SKILL.md")] + [
            os.path.join(SKILL_DIR, "agents", f) for f in os.listdir(os.path.join(SKILL_DIR, "agents"))] + [
            os.path.join(DASHBOARD_DIR, "SKILL.md")]
        for f in files:
            self.assertEqual(frontmatter_errors(open(f, encoding="utf-8").read()), [], f)

    def test_a_quote_inside_single_quotes_is_caught(self):
        self.assertTrue(frontmatter_errors("---\nname: x\ndescription: 'the units' sizes'\n---\n"))


def unit(i, size="M", deps=()):
    return {"id": i, "title": f"u{i}", "size": size, "depends_on": list(deps)}


class PlanWaves(unittest.TestCase):
    def test_independent_units_run_in_parallel(self):
        self.assertEqual(waves.plan([unit(1), unit(2), unit(3)])["mode"], "parallel")

    def test_a_chain_runs_as_a_lane(self):
        self.assertEqual(waves.plan([unit(1), unit(2, deps=[1]), unit(3, deps=[2])])["mode"], "lane")

    def test_dependencies_make_waves(self):
        r = waves.plan([unit(1, "L"), unit(2, "L"), unit(3, deps=[1, 2]), unit(4, deps=[1]), unit(5, deps=[2])])
        self.assertEqual(r["mode"], "waves")
        self.assertEqual([w["units"] for w in r["waves"]], [[1, 2], [3, 4, 5]])
        self.assertLess(r["estimate"], r["lane"])

    def test_two_small_units_stay_in_one_lane(self):
        self.assertEqual(waves.plan([unit(1, "S"), unit(2, "S")])["mode"], "lane")

    def test_a_large_independent_group_is_capped_into_several_waves(self):
        r = waves.plan([unit(i) for i in range(1, 13)], max_par=4)
        self.assertEqual([w["units"] for w in r["waves"]], [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]])
        self.assertEqual(r["mode"], "waves")
        self.assertEqual(r["capped"], 4)
        self.assertIn("exceeds the cap of 4", waves.mode_line([unit(i) for i in range(1, 13)], r))

    def test_the_cap_tightens_with_other_active_runs(self):
        self.assertEqual([waves.cap_for(n) for n in (0, 1, 2, 5)], [4, 3, 2, 2])

    def test_a_cycle_is_rejected(self):
        with self.assertRaises(SystemExit):
            waves.plan([unit(1, deps=[2]), unit(2, deps=[1])])


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def call(i, name, inp, t):
    return {"type": "assistant", "timestamp": t,
            "message": {"id": f"m{i}", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}


def result(i, text, t, error=True):
    return {"type": "user", "timestamp": t,
            "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "is_error": error, "content": text}]}}


class Journal(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.session = os.path.join(self.dir, "s1.jsonl")
        os.makedirs(os.path.join(self.dir, "s1", "subagents"))
        t = "2026-10-26T10:00:00Z"
        write_jsonl(self.session, [
            {"type": "user", "timestamp": t, "message": {"content": "<command-name>/bmad-batch-build</command-name>"}},
            call(1, "Agent", {"description": "worker 1", "prompt": "x"}, t),
            call(2, "Bash", {"command": "bash check-unit.sh wt br spec"}, t),
            result(2, "Exit code 1\nNOT READY: status is 'missing'", t),
            call(3, "SendMessage", {"to": "aw1", "message": "go on"}, t),
            call(4, "Bash", {"command": "python cost-report.py --record run --root D:/repo"}, "2026-10-26T11:00:00Z"),
            # After --record: later work in the session, not the run's.
            call(5, "Bash", {"command": "grep x y"}, "2026-10-26T11:05:00Z"),
            result(5, "Exit code 1\nfatal: later investigation", "2026-10-26T11:05:00Z"),
        ])
        late = os.path.join(self.dir, "s1", "subagents", "agent-late")
        write_jsonl(late + ".jsonl", [
            {"type": "user", "timestamp": "2026-10-26T12:00:00Z", "message": {"content": "look into x"}},
            call(30, "Bash", {"command": "sleep 60"}, "2026-10-26T12:00:00Z"),
            result(30, "<tool_use_error>Blocked: sleep 60 followed by: x</tool_use_error>", "2026-10-26T12:00:00Z"),
        ])
        json.dump({"agentType": "general-purpose"}, open(late + ".meta.json", "w"))
        sub = os.path.join(self.dir, "s1", "subagents", "agent-aw1")
        write_jsonl(sub + ".jsonl", [
            {"type": "user", "timestamp": t, "message": {"content": "You are a Build Auto worker for one unit.\nUnit: 1"}},
            call(10, "Bash", {"command": "sleep 60; tail x.log"}, t),
            result(10, "<tool_use_error>Blocked: sleep 60 followed by: tail x.log</tool_use_error>", t),
            call(11, "Bash", {"command": "rm -rf node_modules"}, t),
            result(11, "Permission for this action was denied by the Claude Code auto mode classifier. "
                       "Reason: [Interfere With Workloads]. More text", t),
            call(12, "Bash", {"command": "cat > a <<'EOF'"}, t),
            result(12, "Exit code 2\n/usr/bin/bash: eval: line 1: unexpected EOF while looking for matching `)'", t),
            call(13, "Bash", {"command": "npm test"}, "2026-10-26T10:30:00Z"),
            result(13, "Exit code 1\nRangeError: options.minThreads and options.maxThreads must not conflict",
                   "2026-10-26T10:30:00Z"),
            {"type": "assistant", "timestamp": "2026-10-26T10:40:00Z", "message": {"id": "m99", "content": [
                {"type": "text", "text": "SPEC: /x/spec.md\nSTATUS: done\nBLOCKED: none\nHEAD: abc\n"
                                         "INTERFACES: none\nSKILL ISSUES: heavy.sh broke vitest | bg.sh wait too short; it returned early"}]}},
        ])
        json.dump({"agentType": "bmad-batch-worker", "toolUseId": "t1"}, open(sub + ".meta.json", "w"))
        self.store = os.path.join(self.dir, "store")
        os.makedirs(self.store)
        write_jsonl(os.path.join(self.store, "events.jsonl"), [
            {"end": "2026-10-26T10:20:00+00:00", "tool": "bg:test", "cwd": "D:/repo/.claude/worktrees/run-1",
             "rc": 1, "cmd": "npm test", "error": "RangeError: conflict"},
            {"end": "2026-10-26T10:20:00+00:00", "tool": "heavy", "cwd": "D:/other", "rc": 1, "cmd": "x", "error": "e"},
            # Another run in the same project, whose slug merely starts the same.
            {"end": "2026-10-26T10:20:00+00:00", "tool": "heavy", "cwd": "D:/repo/.claude/worktrees/runaway-1",
             "rc": 1, "cmd": "y", "error": "e"},
            {"end": "2026-10-20T10:20:00+00:00", "tool": "heavy", "cwd": "D:/repo", "rc": 1, "cmd": "old", "error": "e"},
        ])

    def test_buckets(self):
        j = journal.build(self.session, "run", "D:/repo", self.store)
        c = j["counts"]
        self.assertEqual(c.get("gate_failed"), 1)
        self.assertEqual(c.get("harness_block"), 1)
        self.assertEqual(c.get("classifier_denial"), 1)
        self.assertEqual(c.get("shell_syntax"), 1)
        self.assertEqual(c.get("command_failed"), 1)
        self.assertEqual(c.get("skill_issue"), 2)
        self.assertEqual(c.get("resume"), 1)
        # Only the event inside the run's window and root counts.
        self.assertEqual(c.get("wrapper_failed"), 1)

    def test_labels_and_reply(self):
        j = journal.build(self.session, "run", "D:/repo", self.store)
        whos = {it["who"] for it in j["items"]}
        self.assertIn("worker u1", whos)
        self.assertIn("resumed worker u1", {it["detail"] for it in j["items"] if it["bucket"] == "resume"})
        self.assertEqual(j["timeline"][0]["result"], "STATUS done")
        self.assertIn("Interfere With Workloads", journal.markdown(j))

    def test_missing_reply(self):
        sub = os.path.join(self.dir, "s1", "subagents", "agent-aw1.jsonl")
        lines = open(sub, encoding="utf-8").read().splitlines()[:-1]
        open(sub, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        j = journal.build(self.session, "run", "D:/repo", self.store)
        self.assertEqual(j["counts"].get("missing_reply"), 1)


class LogEvent(unittest.TestCase):
    def test_error_line_is_the_last_error(self):
        f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".log", encoding="utf-8")
        f.write("RUN v2\n\x1b[31mRangeError: options.minThreads conflict\x1b[0m\n  at x\n Tests no tests\n")
        f.close()
        self.assertEqual(log_event.error_line(f.name), "RangeError: options.minThreads conflict")


class CheckTestEnv(unittest.TestCase):
    def test_a_missing_root_fails_instead_of_passing_as_none(self):
        bash = os.environ.get("SELF_TEST_BASH") or shutil.which("bash")
        r = subprocess.run([bash, os.path.join(SCRIPTS, "check-test-env.sh"), "D:/no/such/root\r"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("BROKEN", r.stdout)


class HeavyShell(unittest.TestCase):
    def run_heavy(self, cmd):
        env = dict(os.environ, BMAD_RUNS_DIR=tempfile.mkdtemp(), BMAD_HEAVY_DIR=tempfile.mkdtemp())
        # self-test.sh passes its own bash: on Windows a bare "bash" can be WSL's.
        bash = os.environ.get("SELF_TEST_BASH") or shutil.which("bash")
        return subprocess.run([bash, os.path.join(SCRIPTS, "heavy.sh"), cmd], capture_output=True, text=True, env=env)

    def test_exit_code_passes_through(self):
        self.assertEqual(self.run_heavy("exit 3").returncode, 3)

    def test_vitest_minimum_not_above_maximum(self):
        out = self.run_heavy("echo $VITEST_MIN_THREADS $VITEST_MAX_THREADS $VITEST_MIN_FORKS $VITEST_MAX_FORKS").stdout
        lo_t, hi_t, lo_f, hi_f = map(int, out.split())
        self.assertLessEqual(lo_t, hi_t)
        self.assertLessEqual(lo_f, hi_f)


class PrepareWorktree(unittest.TestCase):
    def pinned(self, with_api, extra=()):
        bash = os.environ.get("SELF_TEST_BASH") or shutil.which("bash")
        root = tempfile.mkdtemp().replace("\\", "/")
        os.makedirs(os.path.join(root, "_bmad", "custom"))
        os.makedirs(os.path.join(root, ".claude", "skills"))
        if with_api:
            os.makedirs(os.path.join(root, "api"))
            open(os.path.join(root, "api", "package.json"), "w").close()
        open(os.path.join(root, "package.json"), "w").close()
        git = ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run(git + ["add", "package.json"] + (["api"] if with_api else []), check=True)
        subprocess.run(git + ["commit", "-q", "-m", "init"], check=True)
        r = subprocess.run([bash, os.path.join(SCRIPTS, "prepare-worktree.sh"), root, "u1", "b1",
                            "--no-install", "--base", "HEAD", *extra], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        wt = r.stdout.strip().splitlines()[-1]
        with open(os.path.join(wt, "_bmad", "custom", "bmad-build-auto.toml"), encoding="utf-8") as f:
            return wt, f.read()

    def test_handoff_names_no_api_dir_in_a_repo_without_one(self):
        wt, toml = self.pinned(with_api=False)
        self.assertNotIn(wt + "/api", toml)
        self.assertIn("cd " + wt + " && npx vitest run", toml)

    def test_handoff_uses_api_dir_when_the_repo_has_one(self):
        wt, toml = self.pinned(with_api=True)
        self.assertIn("cd " + wt + "/api && npx vitest run", toml)

    def test_fallback_agents_launch_the_coder_as_general_purpose(self):
        # A copied install's first run: the session has not loaded the run's
        # agents, so the coder runs as general-purpose on Haiku with the
        # agent's instructions read from the skill ("batch-build did not launch").
        _, toml = self.pinned(with_api=False, extra=("--agents", "general-purpose"))
        self.assertNotIn("general-purposehaiku-implementer", toml)
        self.assertIn("subagent_type: general-purpose", toml)
        self.assertIn("model: haiku", toml)
        self.assertIn("agents/haiku-implementer.md and follow it as your standing instructions", toml)
        _, toml = self.pinned(with_api=False)
        self.assertIn("subagent_type: haiku-implementer`)", toml)  # unchanged without fallback
        # A Windows session with only PowerShell runs the bash commands through bash -c.
        self.assertIn("run each one as bash -c '<command>' through PowerShell", toml)
        self.tomllib_ok(toml)

    def tomllib_ok(self, toml):
        try:
            import tomllib
        except ImportError:
            return
        tomllib.loads(toml)

    def test_build_is_not_banned_when_the_project_requires_it(self):
        # An unconditional "Never run npm run build" made workers skip a build
        # that the intent or AGENTS.md required (healthpro-seed-fix, healthpro-cyber).
        wt, toml = self.pinned(with_api=False)
        self.assertNotIn("Never run", toml)
        self.assertIn("unless the intent, the spec's Verification or AGENTS.md requires one", toml)


class Metrics(unittest.TestCase):
    """metrics.py export leaves nothing identifying in a bundle, and import dedupes."""

    ROW = {"session": "s1", "start": "2026-10-27T01:00:00Z", "usd": 12.5, "min_wall": 90, "workers": 3,
           "project": "ClientSecretProject", "slug": "client-feature-x",
           "mode": "Mode: waves (≈460 min). Order: 1,6 → {2,3}.",
           "hardware": {"host": "XT-0032-SECRET-HOST", "os": "Windows", "cpus": 12, "ram_gb": 32},
           "worker_spans": [{"wt": "client-feature-x-1", "unit": 1, "min": 20}],
           "unit_minutes": [{"id": 1, "size": "M", "min": 20}],
           "skill_issues": ["prepare failed in D:\\Clients\\Secret\\repo\\api",
                            "merged acme/feat-client-x into clientsecretproject main"],
           "journal": {"counts": {"tool_error": 2},
                       "outcome": {"pr": "https://dev.azure.com/acme/Clients/_git/x/pullrequest/7",
                                   "merged": "Client billing export (b1a00db4); Client audit log (2f1969fb)"},
                       "items": [{"bucket": "tool_error", "who": "worker u1", "count": 1,
                                  "detail": "cat /home/someone/secret/file.ts failed"}]}}
    EVENT = {"end": "2026-10-27T02:00:00+00:00", "tool": "bg:suite", "rc": 1, "sec": 300,
             "cwd": "D:/Clients/Secret/repo", "cmd": "cd D:/Clients/Secret/repo && npm test",
             "error": "npm error in D:/Clients/Secret/repo"}

    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.old = os.environ.get("BMAD_RUNS_DIR")
        os.environ["BMAD_RUNS_DIR"] = self.store
        for name, row in (("runs.jsonl", self.ROW), ("events.jsonl", self.EVENT)):
            with open(os.path.join(self.store, name), "w", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        self.m = load("metrics", "metrics.py")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BMAD_RUNS_DIR", None)
        else:
            os.environ["BMAD_RUNS_DIR"] = self.old
        shutil.rmtree(self.store, ignore_errors=True)

    def test_export_drops_identifying_data(self):
        for text in (False, True):
            b = self.m.bundle(text=text)
            dump = json.dumps(b)
            for secret in ("SECRET-HOST", "ClientSecretProject", "client-feature-x", "Clients", "/home/someone",
                           "npm test", "acme", "billing", "b1a00db4"):
                self.assertNotIn(secret.lower(), dump.lower(), f"{secret} leaked (include_text={text})")
            self.assertEqual(b["schema"], self.m.SCHEMA)
            run = b["runs"][0]
            self.assertEqual((run["usd"], run["mode"], run["hardware"]["cpus"]), (12.5, "waves", 12))
            self.assertTrue(run["pr_opened"])
            self.assertEqual(b["events"][0]["kind"], "test")
            self.assertEqual(run["installation_id"], b["installation_id"])
        self.assertIn("<path>", b["runs"][0]["skill_issues"][0])
        self.assertEqual(self.m.installation_id(), b["installation_id"])  # stable across calls

    def test_import_merges_and_dedupes(self):
        other = self.m.bundle()
        other["installation_id"] = "other"
        for r in other["runs"] + other["events"]:
            r["installation_id"] = "other"
        path = os.path.join(self.store, "other.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(other, fh)
        for _ in range(2):
            self.m.import_bundles([path])
        runs = self.m.read_jsonl(os.path.join(self.store, "metrics", "runs.jsonl"))
        events = self.m.read_jsonl(os.path.join(self.store, "metrics", "events.jsonl"))
        self.assertEqual(len(runs), 2)  # one per installation, not doubled
        self.assertEqual(len(events), 2)
        own = [r for r in runs if r["installation_id"] != "other"][0]
        self.assertEqual(own["project"], "ClientSecretProject")  # own rows keep their names


class Setup(unittest.TestCase):
    """setup.sh resolves a project without asking: ignore rule, base branch,
    tickets and PR mode from the remote, agents copied (BMAD already present
    here, so nothing is downloaded)."""

    def test_setup_on_a_github_project(self):
        bash = os.environ.get("SELF_TEST_BASH") or shutil.which("bash")
        root = tempfile.mkdtemp().replace("\\", "/")
        os.makedirs(os.path.join(root, "_bmad", "scripts"))
        open(os.path.join(root, "_bmad", "scripts", "render_skill.py"), "w").close()
        os.makedirs(os.path.join(root, ".claude", "skills", "bmad-build-auto"))
        open(os.path.join(root, ".claude", "skills", "bmad-build-auto", "SKILL.md"), "w").close()
        subprocess.run(["git", "init", "-q", "-b", "trunk", root], check=True)
        subprocess.run(["git", "-C", root, "remote", "add", "origin", "https://github.com/acme/app.git"], check=True)
        subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        r = subprocess.run([bash, os.path.join(SCRIPTS, "setup.sh"), root], capture_output=True, text=True)
        out = r.stdout
        self.assertEqual(r.returncode, 0, out + r.stderr)
        self.assertIn("SETUP ok", out)
        self.assertRegex(out, r"PYTHON python")
        # The plugin ships the agents itself; a copied install gets them in the project.
        plugin = os.path.exists(os.path.join(SKILL_DIR, "..", "..", ".claude-plugin", "plugin.json"))
        self.assertIn("AGENTS plugin" if plugin else "AGENTS copied", out)
        self.assertEqual(os.path.exists(os.path.join(root, ".claude", "agents", "bmad-batch-worker.md")), not plugin)
        self.assertIn("IGNORE added", out)
        self.assertIn("BASE trunk", out)
        self.assertIn("REMOTE github", out)
        self.assertIn("TICKETS off", out)
        r = subprocess.run([bash, os.path.join(SCRIPTS, "setup.sh"), root], capture_output=True, text=True)
        self.assertIn("IGNORE ok", r.stdout)  # the rule is added once
        self.assertIn("AGENTS plugin" if plugin else "AGENTS project", r.stdout)
        shutil.rmtree(root, ignore_errors=True)

    def test_ado_remote_parsing(self):
        tickets = load("ado_tickets", "ado_tickets.py")
        root = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", root], check=True)
        cases = {
            "https://org1@dev.azure.com/org1/My%20Project/_git/repo": ("https://dev.azure.com/org1", "My Project"),
            "git@ssh.dev.azure.com:v3/org2/Proj/repo": ("https://dev.azure.com/org2", "Proj"),
            "https://org3.visualstudio.com/DefaultCollection/P3/_git/r": ("https://org3.visualstudio.com", "P3"),
            "https://github.com/acme/app.git": (None, None),
        }
        old = os.getcwd()
        try:
            os.chdir(root)
            for url, want in cases.items():
                subprocess.run(["git", "remote", "remove", "origin"], capture_output=True)
                subprocess.run(["git", "remote", "add", "origin", url], check=True)
                self.assertEqual(tickets.ado_remote(), want, url)
        finally:
            os.chdir(old)
            shutil.rmtree(root, ignore_errors=True)


class RunSkillName(unittest.TestCase):
    """Only the batch build itself counts as a run: its plugin form does, the
    dashboard (a sibling skill in the same plugin) does not."""

    def test_names(self):
        for mod in (load("cost_report", "cost-report.py"), load("install_skill", "install-skill.py")):
            self.assertTrue(mod.is_run_skill("bmad-batch-build"))
            self.assertTrue(mod.is_run_skill("bmad-batch-build:bmad-batch-build"))
            self.assertFalse(mod.is_run_skill("bmad-batch-build:bmad-batch-dashboard"))
            self.assertFalse(mod.is_run_skill("bmad-batch-dashboard"))
            line = json.dumps({"type": "user", "message": {"content": "<command-name>/bmad-batch-build:bmad-batch-dashboard</command-name>"}})
            self.assertFalse(mod.invokes(line))
            line = json.dumps({"type": "user", "message": {"content": "<command-name>/bmad-batch-build</command-name>"}})
            self.assertTrue(mod.invokes(line))


class Dashboard(unittest.TestCase):
    """dashboard.py embeds the dataset safely and the page script parses."""

    def test_build(self):
        store = tempfile.mkdtemp()
        old = os.environ.get("BMAD_RUNS_DIR")
        os.environ["BMAD_RUNS_DIR"] = store
        try:
            os.makedirs(os.path.join(store, "metrics"))
            row = {"installation_id": "i1", "session": "s1", "start": "2026-10-27T01:00:00Z", "usd": 3.5,
                   "slug": "evil</script><!--x", "usd_by_role": {"main": 1.5, "worker": 2}, "min_wall": 30}
            with open(os.path.join(store, "metrics", "runs.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            spec = importlib.util.spec_from_file_location("dashboard", os.path.join(DASHBOARD_DIR, "scripts", "dashboard.py"))
            dash = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(dash)
            out = os.path.join(store, "d.html")
            dash.build(out)
            html = open(out, encoding="utf-8").read()
            self.assertEqual(html.count("</script>"), 3)  # d3, the data, the page script
            self.assertNotIn("<!--x", html)
            data = html.split('<script id="dash-data" type="application/json">', 1)[1].split("</script>", 1)[0]
            self.assertEqual(json.loads(data)["runs"][0]["slug"], row["slug"])
            node = shutil.which("node")
            if node:
                page = os.path.join(store, "page.js")
                with open(page, "w", encoding="utf-8") as fh:
                    fh.write(html.rsplit("<script>", 1)[1].split("</script>", 1)[0])
                r = subprocess.run([node, "--check", page], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            if old is None:
                os.environ.pop("BMAD_RUNS_DIR", None)
            else:
                os.environ["BMAD_RUNS_DIR"] = old
            shutil.rmtree(store, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
