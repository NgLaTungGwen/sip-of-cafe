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
            os.path.join(SKILL_DIR, "agents", f) for f in os.listdir(os.path.join(SKILL_DIR, "agents"))]
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
                                         "INTERFACES: none\nSKILL ISSUES: heavy.sh broke vitest; bg.sh wait too short"}]}},
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
    def pinned(self, with_api):
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
                            "--no-install", "--base", "HEAD"], capture_output=True, text=True)
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

    def test_build_is_not_banned_when_the_project_requires_it(self):
        # An unconditional "Never run npm run build" made workers skip a build
        # that the intent or AGENTS.md required (healthpro-seed-fix, healthpro-cyber).
        wt, toml = self.pinned(with_api=False)
        self.assertNotIn("Never run", toml)
        self.assertIn("unless the intent, the spec's Verification or AGENTS.md requires one", toml)


if __name__ == "__main__":
    unittest.main()
