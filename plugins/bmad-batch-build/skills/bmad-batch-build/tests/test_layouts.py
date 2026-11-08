"""The scripts on repositories that are not laid out like hive-factory.

Each case builds a small fake repository, puts a fake npm/bun on PATH that
only records its calls, and runs the real script. Run by scripts/self-test.sh.
"""
import json
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
TAB = chr(9)


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


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Profile(Layouts):
    """The project profile (profile.py) and the scripts that follow it."""

    def profile(self, *args):
        r = subprocess.run(["python", os.path.join(SCRIPTS, "profile.py"), *args], capture_output=True, text=True)
        return r.returncode, r.stdout.replace("\r", "")

    def ioc_like(self):
        write(os.path.join(self.repo, "app", "package.json"), json.dumps(
            {"name": "app", "scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint ."},
             "devDependencies": {"vitest": "2"}}))
        write(os.path.join(self.repo, "app", "package-lock.json"), "{}")

    def test_detect_an_app_folder_repository(self):
        self.ioc_like()
        self.profile("detect", self.repo, "--write")
        prof = json.load(open(os.path.join(self.repo, ".claude", "bmad-batch-project.json"), encoding="utf-8"))
        self.assertEqual(prof["install"], [{"dir": "app", "cmd": "npm ci --no-audit --no-fund --loglevel=error"}])
        self.assertEqual(prof["suite"], {"dir": "app", "cmd": "npm run test"})
        self.assertEqual(prof["targeted"]["test"], "npx vitest run <files> --reporter=dot")
        self.assertEqual(sorted(c["name"] for c in prof["checks"]), ["app-lint", "app-test", "app-typecheck"])
        self.assertIsNone(prof["confirmed"])

    def test_detect_finds_the_test_runner_in_a_workspace_member(self):
        write(os.path.join(self.repo, "package.json"), json.dumps(
            {"name": "mono", "workspaces": ["api", "web"], "scripts": {"test": "npm -w api test"}}))
        write(os.path.join(self.repo, "package-lock.json"), "{}")
        write(os.path.join(self.repo, "api", "package.json"), json.dumps(
            {"name": "api", "scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}}))
        self.profile("detect", self.repo, "--write")
        prof = json.load(open(os.path.join(self.repo, ".claude", "bmad-batch-project.json"), encoding="utf-8"))
        self.assertEqual(prof["targeted"]["dir"], "api")
        self.assertEqual(prof["targeted"]["test"], "npx jest <files>")

    def test_check_walks_none_unconfirmed_ok_stale(self):
        self.ioc_like()
        self.assertEqual(self.profile("check", self.repo)[1].split()[:2], ["PROFILE", "none"])
        self.profile("detect", self.repo, "--write")
        self.assertIn("PROFILE unconfirmed", self.profile("check", self.repo)[1])
        self.profile("confirm", self.repo)
        self.assertIn("PROFILE ok", self.profile("check", self.repo)[1])
        write(os.path.join(self.repo, "app", "package.json"), '{"name": "app", "scripts": {"test": "jest"}}')
        self.assertIn("PROFILE stale", self.profile("check", self.repo)[1])

    def test_install_and_verify_follow_the_profile(self):
        self.ioc_like()
        self.profile("detect", self.repo, "--write")
        path = os.path.join(self.repo, ".claude", "bmad-batch-project.json")
        prof = json.load(open(path, encoding="utf-8"))
        # As the orchestrator would after reading "use bun" in the repo's docs.
        prof["install"] = [{"dir": "app", "cmd": "bun install --frozen-lockfile"}]
        prof["checks"] = [{"name": "types", "dir": "app", "cmd": "bun run typecheck"}]
        json.dump(prof, open(path, "w", encoding="utf-8"))
        r, calls = self.run_script("install-deps.sh", self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c.split(" @ ")[0] for c in calls], ["bun install --frozen-lockfile"])
        os.remove(self.calls)
        r, calls = self.run_script("verify.sh", self.repo)
        self.assertEqual([c.split(" @ ")[0] for c in calls], ["bun run typecheck"])
        self.assertIn("types.rc:0", r.stdout)

    def prepared(self, with_profile):
        # With a profile the base is the repository's own default branch
        # (develop here); without one it stays origin/main, as it always was.
        remote = os.path.join(self.tmp, "remote.git")
        git("init", "-q", "--bare", "-b", "develop" if with_profile else "main", remote)
        git("clone", "-q", remote, self.repo)
        os.makedirs(os.path.join(self.repo, "_bmad", "custom"))
        self.ioc_like()
        write(os.path.join(self.repo, ".env"), "DATABASE_URL=postgres://u:p@db.example:5432/app\n")
        write(os.path.join(self.repo, ".gitignore"), ".env\n_bmad/\n.claude/\n")
        git("add", "-A", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=self.repo)
        git("push", "-q", "origin", "HEAD", cwd=self.repo)
        if with_profile:
            self.profile("detect", self.repo, "--write")
        r, _ = self.run_script("prepare-worktree.sh", self.repo, "u-1", "feat/u-1", "--no-install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        wt = os.path.join(self.repo, ".claude", "worktrees", "u-1")
        toml = open(os.path.join(wt, "_bmad", "custom", "bmad-build-auto.toml"), encoding="utf-8").read()
        return wt, toml, open(os.path.join(wt, ".env"), encoding="utf-8").read()

    def test_prepare_worktree_pins_the_profile_commands(self):
        wt, toml, env = self.prepared(with_profile=True)
        self.assertIn("&& npm run test'", toml)
        self.assertIn("/u-1/app && npx vitest run <files> --reporter=dot", toml)
        self.assertNotIn("/u-1 && npm test", toml)
        # The base is the profile's (origin/HEAD is develop here), and the
        # database URL is left alone: per_worktree is hive-factory's setup.
        self.assertIn("db.example:5432", env)
        branch = subprocess.run(["git", "-C", wt, "log", "-1", "--format=%D"], capture_output=True, text=True).stdout
        self.assertIn("develop", branch)

    def test_a_project_without_a_suite_gets_no_suite_instructions(self):
        import tomllib
        remote = os.path.join(self.tmp, "remote.git")
        git("init", "-q", "--bare", "-b", "main", remote)
        git("clone", "-q", remote, self.repo)
        os.makedirs(os.path.join(self.repo, "_bmad", "custom"))
        write(os.path.join(self.repo, "package.json"), "{}")
        write(os.path.join(self.repo, ".gitignore"), "_bmad/\n.claude/\n")
        git("add", "-A", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=self.repo)
        git("push", "-q", "origin", "main", cwd=self.repo)
        write(os.path.join(self.repo, ".claude", "bmad-batch-project.json"), json.dumps(
            {"base": "main", "install": [], "checks": [], "suite": None,
             "targeted": {"dir": ".", "test": None, "typecheck": "npm run typecheck"}}))
        r, _ = self.run_script("prepare-worktree.sh", self.repo, "u-1", "feat/u-1", "--no-install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        path = os.path.join(self.repo, ".claude", "worktrees", "u-1", "_bmad", "custom", "bmad-build-auto.toml")
        doc = tomllib.load(open(path, "rb"))["workflow"]
        facts = " ".join(doc["persistent_facts"])
        self.assertIn("no full test suite to run", facts)
        self.assertNotIn("npm test", facts)
        self.assertIn("runs no tests", doc["implementation_handoff"])
        self.assertIn("launch a new `haiku-implementer`", facts)

    def test_verify_apps_runs_the_apps_test_suite_unless_no_test(self):
        # hive-factory: root npm test never reaches apps/, PR Validation runs its bun test.
        write(os.path.join(self.repo, "package.json"), '{"name": "x"}')
        write(os.path.join(self.repo, "package-lock.json"), "{}")
        write(os.path.join(self.repo, "apps", "package.json"), '{"name": "web"}')
        write(os.path.join(self.repo, "apps", "bun.lock"), "")
        os.makedirs(os.path.join(self.repo, "apps", "node_modules"))
        r, calls = self.run_script("verify.sh", self.repo, "--apps")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        apps = [c.split(" @ ")[0] for c in calls if c.replace("\\", "/").endswith("/apps")]
        self.assertEqual(apps, ["bun run typecheck", "bun test"])
        self.assertIn("apps-test.rc:0", r.stdout)
        self.assertIn("apps.rc:0", r.stdout)
        os.remove(self.calls)
        r, calls = self.run_script("verify.sh", self.repo, "--apps", "--no-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c.split(" @ ")[0] for c in calls if c.startswith("bun ")], ["bun run typecheck"])
        self.assertNotIn("apps-test.rc", r.stdout)

    def fail_on(self, word):
        # The fake npm and bun exit 2 when their arguments contain <word>.
        for tool in ("npm", "bun"):
            write(os.path.join(self.bin, tool), f'#!/usr/bin/env bash\necho "{tool} $* @ $(pwd)" >> "{self.calls}"\n'
                  f'case "$*" in *{word}*) exit 2 ;; esac\n')
            os.chmod(os.path.join(self.bin, tool), 0o755)

    def test_verify_exits_1_when_a_check_fails(self):
        # sonar-zero-5: apps.rc:2, yet bg.sh wait printed "verify: ok", because
        # verify.sh always exited 0.
        write(os.path.join(self.repo, "package.json"), '{"name": "x"}')
        write(os.path.join(self.repo, "package-lock.json"), "{}")
        write(os.path.join(self.repo, "apps", "package.json"), '{"name": "web"}')
        os.makedirs(os.path.join(self.repo, "apps", "node_modules"))
        self.fail_on("typecheck")
        r, _ = self.run_script("verify.sh", self.repo, "--apps", "--no-test")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("apps.rc:2", r.stdout)
        self.fail_on("nothing-matches")
        r, _ = self.run_script("verify.sh", self.repo, "--apps", "--no-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_verify_apps_installs_apps_first_when_it_has_no_node_modules(self):
        write(os.path.join(self.repo, "package.json"), '{"name": "x"}')
        write(os.path.join(self.repo, "apps", "package.json"), '{"name": "web", "scripts": {"lint": "eslint ."}}')
        write(os.path.join(self.repo, "apps", "bun.lock"), "")
        r, calls = self.run_script("verify.sh", self.repo, "--apps", "--no-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        apps = [c.split(" @ ")[0] for c in calls if c.replace("\\", "/").endswith("/apps")]
        self.assertEqual(apps, ["bun install --frozen-lockfile", "bun run typecheck", "bun run lint"])
        self.assertIn("apps-lint.rc:0", r.stdout)

    def test_verify_runs_the_pipelines_ratchets_with_their_caps(self):
        # sonar-zero-5: api lint passed locally at 1548 warnings; PR Validation
        # caps it at API_MAX_WARNINGS=1546.
        write(os.path.join(self.repo, "package.json"), '{"name": "x"}')
        write(os.path.join(self.repo, "apps", "package.json"), '{"name": "web", "scripts": {"lint": "eslint ."}}')
        os.makedirs(os.path.join(self.repo, "apps", "node_modules"))
        write(os.path.join(self.repo, "pipeline", "pr-validation.yml"), """variables:
  - name: API_MAX_WARNINGS
    value: "1546"
  - name: WEB_MAX_WARNINGS
    value: "9"
jobs:
  - job: api
    steps:
      - script: npm --workspace @hive/api run lint -- --max-warnings $(API_MAX_WARNINGS)
        displayName: "api: eslint"
        workingDirectory: $(Build.SourcesDirectory)
      - script: bun run lint --max-warnings $(WEB_MAX_WARNINGS)
        workingDirectory: $(Build.SourcesDirectory)/apps
      - script: npm run knip -- --max-issues $(UNSET_CAP)
""")
        self.fail_on("1546")
        r, calls = self.run_script("verify.sh", self.repo, "--apps", "--no-test")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("ratchet-api-max-warnings.rc:2", r.stdout)
        self.assertIn("ratchet-web-max-warnings.rc:0", r.stdout)
        cmds = [c.split(" @ ")[0] for c in calls]
        # After the build, which type-aware lint needs; the apps cap replaces
        # the plain apps lint; a cap with no value is skipped.
        self.assertLess(cmds.index("npm run build"), cmds.index("npm --workspace @hive/api run lint -- --max-warnings 1546"))
        self.assertIn("bun run lint --max-warnings 9", cmds)
        self.assertNotIn("bun run lint", cmds)
        self.assertFalse(any("knip" in c for c in cmds))
        self.assertNotIn("apps-lint.rc", r.stdout)

    def test_ci_ratchets_reads_github_workflows(self):
        write(os.path.join(self.repo, "web", "package.json"), "{}")
        write(os.path.join(self.repo, ".github", "workflows", "ci.yml"), """env:
  WEB_MAX: 4
jobs:
  lint:
    steps:
      - name: lint
        working-directory: web
        run: npx eslint . --max-warnings ${{ env.WEB_MAX }}
""")
        r = subprocess.run(["python", os.path.join(SCRIPTS, "ci-ratchets.py"), self.repo], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "ratchet-web-max" + TAB + "web" + TAB + "npx eslint . --max-warnings 4", r.stderr)

    def test_verify_no_test_skips_the_suite(self):
        write(os.path.join(self.repo, "package.json"), '{"name": "x"}')
        write(os.path.join(self.repo, "package-lock.json"), "{}")
        r, calls = self.run_script("verify.sh", self.repo, "--no-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("npm test", [c.split(" @ ")[0] for c in calls])
        self.assertIn("npm run build", [c.split(" @ ")[0] for c in calls])

    def test_prepare_worktree_without_a_profile_keeps_the_original_commands(self):
        wt, toml, env = self.prepared(with_profile=False)
        self.assertIn("/u-1 && npm test'", toml)
        import tomllib
        tomllib.loads(toml)  # still valid TOML
        self.assertIn("127.0.0.1:55432", env)


class LinuxTests(unittest.TestCase):
    """sonar-zero-5: two Linux-only verifier tests (skipIf win32) failed first
    on CI, because the Windows verify host never runs them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.calls = os.path.join(self.tmp, "docker.txt").replace("\\", "/")
        write(os.path.join(self.bin, "docker"),
              f'#!/usr/bin/env bash\necho "$*" >> "{self.calls}"\n'
              '[ "$1" = info ] && exit 0\n'
              'case "$*" in *verifier.spec.ts*) [ -n "${FAKE_RED:-}" ] && { echo "FAIL x"; exit 1; } ;; esac\nexit 0\n')
        os.chmod(os.path.join(self.bin, "docker"), 0o755)
        self.repo = os.path.join(self.tmp, "repo").replace("\\", "/")
        write(os.path.join(self.repo, "package.json"), "{}")
        write(os.path.join(self.repo, "package-lock.json"), "{}")
        write(os.path.join(self.repo, "api", "package.json"), "{}")
        write(os.path.join(self.repo, "api", "test", "old.spec.ts"), "it.skipIf(process.platform === 'win32')('x', () => {})\n")
        git = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "base"], check=True)
        self.base = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.git = git

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_it(self, **env):
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], LINUX_TESTS_FORCE="1", **env)
        return subprocess.run([BASH, os.path.join(SCRIPTS, "linux-tests.sh"), self.repo, self.base],
                              capture_output=True, text=True, env=e)

    def commit(self, path, text):
        write(os.path.join(self.repo, path), text)
        subprocess.run(self.git + ["add", "-A"], check=True)
        subprocess.run(self.git + ["commit", "-qm", "change"], check=True)

    def test_no_platform_specific_change_runs_nothing(self):
        self.commit("api/test/plain.spec.ts", "it('x', () => {})\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("linux-tests: none", r.stdout)
        self.assertFalse(os.path.exists(self.calls))

    def test_changed_platform_specific_tests_run_in_their_package(self):
        self.commit("api/test/verifier.spec.ts", "it.skipIf(process.platform === 'win32')('kills', () => {})\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("linux-tests: ok (1 files)", r.stdout)
        run = open(self.calls).read().split("run --rm", 1)[1]
        self.assertIn("cd /w/api && su node -c 'npx vitest run test/verifier.spec.ts", run)
        self.assertNotIn("old.spec.ts", run)  # unchanged since the base
        self.assertIn("--exclude=.env", run)
        r = self.run_it(FAKE_RED="1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("linux-tests: failed", r.stdout)

    def test_skipped_without_docker(self):
        self.commit("api/test/verifier.spec.ts", "if (process.platform !== 'win32') it('x', () => {})\n")
        write(os.path.join(self.bin, "docker"), "#!/usr/bin/env bash\nexit 1\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("skipped (docker is not running)", r.stdout)


class HeavyQueue(unittest.TestCase):
    def env(self, **kw):
        tmp = tempfile.mkdtemp()
        return dict(os.environ, BMAD_RUNS_DIR=os.path.join(tmp, "s"), BMAD_HEAVY_DIR=os.path.join(tmp, "h"), **kw)

    def test_test_runs_in_one_worktree_take_turns(self):
        # sonar-zero-5: parallel coders in one worktree overwrote each other's
        # lcov and test-database rows. Two test commands in one worktree run
        # one after the other; a typecheck beside them does not wait.
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", repo], check=True)
        out = os.path.join(repo, "order.txt").replace("\\", "/")
        env = self.env()
        r = repo.replace("\\", "/")
        def go(tag, cmd):
            return subprocess.Popen([BASH, os.path.join(SCRIPTS, "heavy.sh"),
                                     f"cd '{r}' && {cmd.format(tag=tag, out=out)}"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
        # a holds the lock until c's typecheck has run (at most 60 s), so the
        # order no longer depends on how fast the machine starts c.
        a = go("a", "echo start-{tag} >> {out}; echo vitest >/dev/null; "
                    "n=0; until grep -q typecheck-c {out} || [ $n -ge 300 ]; do sleep 0.2; n=$((n+1)); done; "
                    "echo end-{tag} >> {out}")
        # b starts only once a holds the worktree's lock and has begun.
        lock = os.path.join(repo, ".git", "bmad", "test.lock")
        deadline = time.time() + 60
        while not (os.path.isdir(lock) and os.path.exists(out)) and time.time() < deadline:
            time.sleep(0.2)
        self.assertTrue(os.path.isdir(lock), "a never took the worktree's lock")
        b = go("b", "echo start-{tag} >> {out}; echo vitest >/dev/null; echo end-{tag} >> {out}")
        c = go("c", "echo typecheck-{tag} >> {out}")
        for p in (a, b, c):
            p.communicate(timeout=120)
        lines = open(out).read().split()
        self.assertEqual([x for x in lines if x[-1] in "ab"], ["start-a", "end-a", "start-b", "end-b"], lines)
        self.assertLess(lines.index("typecheck-c"), lines.index("end-a"), lines)
        self.assertFalse(os.path.exists(os.path.join(repo, ".git", "bmad", "test.lock")))

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
