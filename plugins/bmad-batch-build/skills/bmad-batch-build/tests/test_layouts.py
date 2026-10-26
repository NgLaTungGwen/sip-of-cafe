"""The scripts on repositories that are not laid out like hive-factory.

Each case builds a small fake repository, puts a fake npm/bun on PATH that
only records its calls, and runs the real script. Run by scripts/self-test.sh.
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SKILL, "scripts")
PLUGIN = os.path.dirname(os.path.dirname(SKILL))
BASH = os.environ.get("SELF_TEST_BASH") or shutil.which("bash")


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class Layouts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        self.bin = os.path.join(self.tmp, "bin")
        self.calls = os.path.join(self.tmp, "calls.txt")
        for tool in ("npm", "bun", "pnpm", "yarn"):
            write(os.path.join(self.bin, tool), f'#!/usr/bin/env bash\necho "{tool} $* @ $(pwd)" >> "{self.calls}"\n')
            os.chmod(os.path.join(self.bin, tool), 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   BMAD_RUNS_DIR=os.path.join(self.tmp, "store"), BMAD_HEAVY_DIR=os.path.join(self.tmp, "heavy"))
        r = subprocess.run([BASH, os.path.join(SCRIPTS, args[0]), *args[1:]], capture_output=True, text=True, env=env)
        calls = open(self.calls, encoding="utf-8").read().splitlines() if os.path.exists(self.calls) else []
        return r, calls

    def test_root_lockfile_installs_once_at_the_root(self):
        # hive-factory and HealthPro: the original behaviour, unchanged.
        write(os.path.join(self.repo, "package.json"), '{"name": "x"}')
        write(os.path.join(self.repo, "package-lock.json"), "{}")
        write(os.path.join(self.repo, "app", "package-lock.json"), "{}")
        r, calls = self.run_script("install-deps.sh", self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c.split(" @ ")[0] for c in calls], ["npm ci --no-audit --no-fund --loglevel=error"])

    def test_packages_without_a_root_lockfile_install_where_their_lockfiles_are(self):
        # ioc: packages in app/, no lockfile at the root; plus a bun package.
        write(os.path.join(self.repo, "app", "package.json"), '{"name": "app"}')
        write(os.path.join(self.repo, "app", "package-lock.json"), "{}")
        write(os.path.join(self.repo, "web", "bun.lock"), "")
        write(os.path.join(self.repo, "app", "node_modules", "dep", "package-lock.json"), "{}")
        r, calls = self.run_script("install-deps.sh", self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = sorted((c.split(" @ ")[0], os.path.basename(c.split(" @ ")[1])) for c in calls)
        self.assertEqual(got, [("bun install --frozen-lockfile", "web"),
                               ("npm ci --no-audit --no-fund --loglevel=error", "app")])

    def test_verify_runs_each_package_folders_own_scripts(self):
        write(os.path.join(self.repo, "app", "package.json"),
              '{"name": "app", "scripts": {"test": "x", "typecheck": "x"}}')
        write(os.path.join(self.repo, "app", "package-lock.json"), "{}")
        r, calls = self.run_script("verify.sh", self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(c.split(" @ ")[0] for c in calls), ["npm run test", "npm run typecheck"])
        self.assertIn("app-test.rc:0", r.stdout)


class HeavyQueue(unittest.TestCase):
    def env(self, **kw):
        tmp = tempfile.mkdtemp()
        return dict(os.environ, BMAD_RUNS_DIR=os.path.join(tmp, "s"), BMAD_HEAVY_DIR=os.path.join(tmp, "h"), **kw)

    def test_a_hung_command_is_stopped_at_the_time_limit(self):
        r = subprocess.run([BASH, os.path.join(SCRIPTS, "heavy.sh"), "sleep 30"], capture_output=True, text=True,
                           env=self.env(BMAD_HEAVY_TIMEOUT="2"), timeout=60)
        self.assertEqual(r.returncode, 124)
        self.assertIn("stopped after 2s", r.stdout + r.stderr)

    def test_max_wait_counts_real_time(self):
        # A queue that never frees memory: the floor is higher than any machine
        # has, and another live command holds a slot. The waiter must still
        # start once max_wait seconds have really passed.
        env = self.env(BMAD_HEAVY_MAX_WAIT="6", BMAD_HEAVY_FLOOR_MB="99999999")
        d = env["BMAD_HEAVY_DIR"]
        os.makedirs(d)
        # The blocker registers itself from bash, so the queue sees its own pid.
        blocker = subprocess.Popen([BASH, "-c", f'echo "$$ 1 2500 9999999999" > "{d}/run.$$"; sleep 60'])
        try:
            for _ in range(100):
                if any(f.startswith("run.") for f in os.listdir(d)):
                    break
                time.sleep(0.1)
            r = subprocess.run([BASH, os.path.join(SCRIPTS, "heavy.sh"), "echo started"], capture_output=True,
                               text=True, env=env, timeout=60)
        finally:
            blocker.kill()
        self.assertIn("starting anyway", r.stderr)
        self.assertIn("started", r.stdout)


class PluginAgents(unittest.TestCase):
    def test_plugin_root_agents_match_the_skills_copy(self):
        # The plugin registers agents/ at its root; `npx skills add` copies
        # only the skill folder, which keeps its own copy. They must not drift.
        root, skill = os.path.join(PLUGIN, "agents"), os.path.join(SKILL, "agents")
        self.assertTrue(os.path.isdir(root), "plugin has no agents/ folder at its root")
        self.assertEqual(sorted(os.listdir(root)), sorted(os.listdir(skill)))
        for f in os.listdir(skill):
            a = open(os.path.join(root, f), encoding="utf-8").read().replace("\r\n", "\n")
            b = open(os.path.join(skill, f), encoding="utf-8").read().replace("\r\n", "\n")
            self.assertEqual(a, b, f)


if __name__ == "__main__":
    unittest.main()
