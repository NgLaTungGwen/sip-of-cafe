"""Unit tests for scripts/ado_eod.py. Run: python -m unittest discover -s tests

Each fix to the script should add a case here that fails without the fix.
"""
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

# A user's own sections, as `config --sections` would save them. The script
# itself has no built-in sections.
SECTIONS = [
    {"name": "ProjectA", "match": ["ProjectA"]},
    {"name": "ProjectB", "match": ["ProjectB"]},
    {"name": "ProjectC", "match": ["ProjectC"], "style": "plain"},
    {"name": "Others", "match": []},
]
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
spec = importlib.util.spec_from_file_location("ado_eod", os.path.join(SCRIPTS, "ado_eod.py"))
eod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eod)
# Tests never reach GitHub: each GitHub test passes its own search.
eod.gh_search = lambda q, limit=300: []

UTC = dt.timezone.utc
ME = {"id": "me-1", "name": "Quy", "email": "quy@example.com"}
DAY = dt.date(2026, 10, 28)
START = dt.datetime(2026, 10, 28, tzinfo=UTC)
END = START + dt.timedelta(days=1)


def update(rev, when, by="me-1", **fields):
    out = {"rev": rev, "revisedBy": {"id": by, "displayName": "Quy" if by == "me-1" else "Other"},
           "fields": {"System.ChangedDate": {"newValue": when}}}
    for name, value in fields.items():
        out["fields"][name.replace("_", ".")] = value
    return out


class Parsing(unittest.TestCase):
    def test_ado_timestamps_with_any_fraction(self):
        self.assertEqual(eod.parse_time("2026-10-28T09:12:33.1234567Z"),
                         dt.datetime(2026, 10, 28, 9, 12, 33, 123456, tzinfo=UTC))
        self.assertEqual(eod.parse_time("2026-10-28T09:12:33Z").hour, 9)
        self.assertEqual(eod.parse_time("2026-10-28T09:12:33+07:00").utcoffset(), dt.timedelta(hours=7))

    def test_org_from_remote(self):
        self.assertEqual(eod.org_from_remote("https://acme@dev.azure.com/acme/Proj/_git/repo"),
                         "https://dev.azure.com/acme")
        self.assertEqual(eod.org_from_remote("git@ssh.dev.azure.com:v3/acme/Proj/repo"), "https://dev.azure.com/acme")
        self.assertEqual(eod.org_from_remote("https://acme.visualstudio.com/Proj/_git/repo"),
                         "https://acme.visualstudio.com")
        self.assertEqual(eod.org_from_remote("git@github.com:a/b.git"), "")

    def test_normalize_org(self):
        self.assertEqual(eod.normalize_org("acme"), "https://dev.azure.com/acme")
        self.assertEqual(eod.normalize_org("https://dev.azure.com/acme/Proj/_boards"), "https://dev.azure.com/acme")

    def test_parse_day(self):
        self.assertEqual(eod.parse_day("2026-10-01", UTC), dt.date(2026, 10, 1))
        with self.assertRaises(eod.Fail):
            eod.parse_day("last friday", UTC)

    def test_parse_week_gives_the_monday(self):
        self.assertEqual(eod.parse_week("2026-10-30", UTC), dt.date(2026, 10, 26))
        self.assertEqual(eod.parse_week("2026-10-26", UTC), dt.date(2026, 10, 26))
        self.assertEqual(eod.parse_week("this", UTC).weekday(), 0)
        self.assertEqual(eod.parse_week("this", UTC) - eod.parse_week("last", UTC), dt.timedelta(days=7))
        with self.assertRaises(eod.Fail):
            eod.parse_week("next", UTC)

    def test_strip_html(self):
        self.assertEqual(eod.strip_html("<div>Fixed &amp; deployed</div><div>QA next</div>"),
                         "Fixed & deployed / QA next")
        self.assertTrue(eod.strip_html("x" * 500).endswith("…"))


class Sections(unittest.TestCase):
    def test_match_project_area_or_tag_as_whole_word(self):
        f = {"System.TeamProject": "Core", "System.AreaPath": "Core\\ProjectA\\Checkout"}
        self.assertEqual(eod.section_for(f, SECTIONS), "ProjectA")
        f = {"System.TeamProject": "Core", "System.Tags": "projectc; urgent"}
        self.assertEqual(eod.section_for(f, SECTIONS), "ProjectC")

    def test_no_partial_word_match(self):
        f = {"System.TeamProject": "ProjectAlpha"}
        self.assertEqual(eod.section_for(f, SECTIONS), "Others")


    def test_no_config_groups_by_project(self):
        self.assertEqual(eod.section_for({"System.TeamProject": "Contoso Web"}, []), "Contoso Web")
        self.assertEqual(eod.section_for({}, []), "Others")

    def test_section_list_by_project_is_sorted_with_others_last(self):
        out = eod.section_list([], ["zeta", "Others", "Alpha", "zeta"])
        self.assertEqual([s["name"] for s in out], ["Alpha", "zeta", "Others"])

    def test_section_list_keeps_config_order_and_style(self):
        out = eod.section_list(SECTIONS, ["ProjectA"])
        self.assertEqual([s["name"] for s in out], ["ProjectA", "ProjectB", "ProjectC", "Others"])
        self.assertEqual(out[2]["style"], "plain")

    def test_old_fixed_sections_are_dropped_on_first_collect(self):
        saved = {}
        cfg = {"sections": eod.LEGACY_SECTIONS}
        with mock.patch.object(eod, "connect", return_value=(FakeClient(), ME)),                 mock.patch.object(eod, "load_config", return_value=cfg),                 mock.patch.object(eod, "save_config", side_effect=saved.update):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(eod.main(["collect", "--date", DAY.isoformat()]), 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["first_run"])
        self.assertEqual((saved["sections"], out["sections_from"]), ([], "project"))
        self.assertIn("ProjectA", [s["name"] for s in out["sections"]])
        self.assertEqual(out["items"][0]["section"], "ProjectA")


class Summarize(unittest.TestCase):
    def test_window_state_and_events(self):
        updates = [
            update(1, "2026-10-20T08:00:00Z", System_State={"newValue": "New"}),
            update(2, "2026-10-28T03:00:00Z", System_State={"oldValue": "New", "newValue": "Active"},
                   Microsoft_VSTS_Scheduling_CompletedWork={"oldValue": None, "newValue": 3}),
            update(3, "2026-10-28T10:00:00Z", by="qa-2", System_History={"newValue": "<p>Looks good</p>"},
                   Microsoft_VSTS_Common_Priority={"oldValue": 2, "newValue": 1}),
            update(4, "2026-10-29T01:00:00Z", System_State={"oldValue": "Active", "newValue": "Closed"}),
        ]
        s = eod.summarize(updates, ME, START, END, UTC)
        self.assertEqual((s["state_start"], s["state_end"]), ("New", "Active"))
        self.assertEqual(len(s["events"]), 2)
        self.assertEqual(s["events"][0]["state"], ["New", "Active"])
        self.assertEqual(s["events"][0]["completed_work"], [None, 3])
        self.assertTrue(s["events"][0]["by_me"])
        self.assertEqual(s["events"][1]["comment"], "Looks good")
        self.assertEqual(s["events"][1]["fields"], ["Priority"])
        self.assertFalse(s["events"][1]["by_me"])

    def test_created_today_has_no_start_state(self):
        s = eod.summarize([update(1, "2026-10-28T05:00:00Z", System_State={"newValue": "New"})], ME, START, END, UTC)
        self.assertIsNone(s["state_start"])
        self.assertEqual(s["state_end"], "New")
        self.assertTrue(s["events"][0]["created"])

    def test_relevant_only_when_mine(self):
        others = {"events": [{"by_me": False, "state": ["Resolved", "Closed"]}]}
        self.assertTrue(eod.relevant({"System.AssignedTo": {"id": "me-1"}}, others, ME))
        self.assertFalse(eod.relevant({"System.AssignedTo": {"id": "x"}}, others, ME))
        self.assertFalse(eod.relevant({"System.AssignedTo": {"id": "me-1"}},
                                      {"events": [{"by_me": False, "fields": ["Priority"]}]}, ME))


class FakeClient(eod.Client):
    """Answers the calls collect() makes from canned data."""

    def __init__(self):
        super().__init__("https://dev.azure.com/acme", "pat")
        self.items = {
            101: {"id": 101, "fields": {"System.Title": "Checkout retries", "System.WorkItemType": "Task",
                                        "System.State": "Closed", "System.TeamProject": "ProjectA",
                                        "System.AssignedTo": {"id": "me-1", "displayName": "Quy"}},
                  "relations": [{"rel": "System.LinkTypes.Hierarchy-Reverse",
                                 "url": "https://dev.azure.com/acme/_apis/wit/workItems/100"},
                                {"rel": "ArtifactLink", "url": "vstfs:///Git/PullRequestId/p1%2Fr1%2F55"}]},
            102: {"id": 102, "fields": {"System.Title": "Someone else's", "System.WorkItemType": "Bug",
                                        "System.State": "Active", "System.TeamProject": "ProjectB",
                                        "System.AssignedTo": {"id": "x", "displayName": "X"}}},
            100: {"id": 100, "fields": {"System.Title": "Payments story", "System.WorkItemType": "User Story"}},
            200: {"id": 200, "fields": {"System.Title": "Next thing", "System.WorkItemType": "Task",
                                        "System.State": "New", "System.TeamProject": "ProjectC"}},
        }
        self.updates = {
            101: [update(1, "2026-10-27T08:00:00Z", System_State={"newValue": "Active"}),
                  update(2, "2026-10-28T09:00:00Z", System_State={"oldValue": "Active", "newValue": "Closed"})],
            102: [update(1, "2026-10-28T09:00:00Z", by="x", System_State={"newValue": "Active"})],
        }
        self.merged = {}  # PR id -> linked work item ids

    def call(self, method, url, body=None):
        if "connectionData" in url:
            return {"authenticatedUser": {"id": "me-1", "providerDisplayName": "Quy"}}
        if url.endswith("wiql"):
            ids = [200, 101] if "NOT IN" in body["query"] else [101, 102]
            return {"workItems": [{"id": i} for i in ids]}
        if url.endswith("workitemsbatch"):
            return {"value": [self.items[i] for i in body["ids"]]}
        if "/updates" in url:
            return {"value": self.updates[int(url.split("/workItems/")[1].split("/")[0])]}
        if url.startswith("_apis/git/pullrequests?"):
            self.pr_query = url
            return {"value": [{"pullRequestId": pr, "title": f"PR {pr}",
                               "repository": {"id": "r1", "name": "web", "project": {"id": "p1", "name": "ProjectA"}}}
                              for pr in self.merged]}
        if url.endswith("/workitems"):
            pr = int(url.split("/pullRequests/")[1].split("/")[0])
            return {"value": [{"id": str(i)} for i in self.merged[pr]]}
        if "/pullrequests/" in url:
            return {"status": "completed", "title": "Retry fix", "repository": {"name": "web"}, "reviewers": [{"vote": 10}]}
        raise AssertionError(url)


class Collect(unittest.TestCase):
    def test_collect_end_to_end(self):
        out = eod.collect(FakeClient(), DAY, UTC, SECTIONS)
        self.assertEqual([i["id"] for i in out["items"]], [101])
        item = out["items"][0]
        self.assertEqual(item["section"], "ProjectA")
        self.assertEqual((item["state_start"], item["state_end"]), ("Active", "Closed"))
        self.assertEqual(item["parent"]["title"], "Payments story")
        self.assertEqual(item["prs"][0]["status"], "completed")
        self.assertEqual(item["url"], "https://dev.azure.com/acme/ProjectA/_workitems/edit/101")
        self.assertEqual([o["id"] for o in out["open_assigned"]], [200])
        self.assertEqual(out["open_assigned"][0]["section"], "ProjectC")
        self.assertEqual(out["merged_prs"], [])
        self.assertEqual(item["merged_today"], [])

    def test_story_progress_counts_untouched_tasks(self):
        client = FakeClient()
        kid = lambda i: {"rel": "System.LinkTypes.Hierarchy-Forward", "url": f"https://dev.azure.com/acme/_apis/wit/workItems/{i}"}
        client.items[100]["relations"] = [kid(101), kid(150), kid(151)]
        client.items[150] = {"id": 150, "fields": {"System.Title": "Untouched task", "System.WorkItemType": "Task",
                                                   "System.State": "New"}}
        client.items[151] = {"id": 151, "fields": {"System.Title": "Dropped", "System.WorkItemType": "Task",
                                                   "System.State": "Removed"}}
        client.items[101]["fields"]["System.State"] = "Active"  # still open on the board, but its PR is completed
        out = eod.collect(client, DAY, UTC, SECTIONS)
        children = out["items"][0]["parent"]["children"]
        self.assertEqual((children["total"], children["done"]), (2, 1))
        self.assertEqual([o["id"] for o in children["open"]], [150])
        self.assertIsNone(out["items"][0]["children"])

    def test_week_window_takes_every_day_of_it(self):
        client = FakeClient()
        client.updates[101].append(update(3, "2026-10-31T09:00:00Z", System_History={"newValue": "Deployed to QA"}))
        day_only = eod.collect(client, DAY, UTC, SECTIONS)
        self.assertEqual(len(day_only["items"][0]["events"]), 1)
        week = eod.collect(client, dt.date(2026, 10, 26), UTC, SECTIONS, days=7)
        events = week["items"][0]["events"]
        self.assertEqual([e["date"] for e in events], ["2026-10-27", "2026-10-28", "2026-10-31"])
        self.assertEqual((week["items"][0]["state_start"], week["to"], week["days"]), (None, "2026-11-01", 7))
        self.assertIn("searchCriteria.maxTime=2026-11-02T00%3A00%3A00Z", client.pr_query)

    def test_cli_week_and_days_bounds(self):
        with mock.patch.object(eod, "connect", return_value=(FakeClient(), ME)),                 mock.patch.object(eod, "load_config", return_value={"sections": SECTIONS}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(eod.main(["collect", "--week", "2026-10-29"]), 0)
            out = json.loads(buf.getvalue())
            self.assertEqual((out["date"], out["to"]), ("2026-10-26", "2026-11-01"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(eod.main(["collect", "--days", "0"]), 2)
            self.assertIn("BAD_ARGS", buf.getvalue())

    def test_items_of_a_merged_pr_are_listed_though_untouched(self):
        client = FakeClient()
        client.items[300] = {"id": 300, "fields": {"System.Title": "Linked task", "System.WorkItemType": "Task",
                                                   "System.State": "New", "System.TeamProject": "ProjectA"}}
        client.updates[300] = [update(1, "2026-10-20T08:00:00Z", System_State={"newValue": "New"})]
        client.merged = {60: [300], 61: []}
        out = eod.collect(client, DAY, UTC, SECTIONS)
        linked = next(i for i in out["items"] if i["id"] == 300)
        self.assertEqual((linked["state_end"], linked["events"], linked["merged_today"]), ("New", [], [60]))
        self.assertEqual([(p["id"], p["work_items"]) for p in out["merged_prs"]], [(60, [300]), (61, [])])
        self.assertIn("searchCriteria.creatorId=me-1", client.pr_query)
        self.assertIn("searchCriteria.minTime=2026-10-28T00%3A00%3A00Z", client.pr_query)

    def test_merged_pr_search_refused_is_a_warning(self):
        client = FakeClient()
        real = client.call

        def call(method, url, body=None):
            if url.startswith("_apis/git/pullrequests?"):
                raise eod.Fail("FORBIDDEN", "no code scope")
            return real(method, url, body)

        client.call = call
        out = eod.collect(client, DAY, UTC, SECTIONS)
        self.assertEqual([i["id"] for i in out["items"]], [101])
        self.assertTrue(any("Merged PRs unknown" in w for w in out["warnings"]))

    def test_wiql_falls_back_when_ever_is_refused(self):
        client = FakeClient()
        real = client.call

        def call(method, url, body=None):
            if url.endswith("wiql") and "EVER" in body["query"]:
                raise eod.Fail("HTTP", "400 EVER not supported")
            return real(method, url, body)

        client.call = call
        self.assertEqual(eod.touched_ids(client, DAY), [101, 102])


class SignIn(unittest.TestCase):
    """The PAT wins; `az login` is used without one, or when the PAT is rejected."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"ADO_EOD_HOME": self.home}, clear=False)
        self.env.start()
        for var in ("AZURE_DEVOPS_EXT_PAT", "ADO_PAT"):
            os.environ.pop(var, None)
        self.args = mock.Mock(org="acme")
        self.tenant = mock.patch.object(eod, "org_tenant", return_value="t-1")
        self.tenant.start()

    def tearDown(self):
        self.tenant.stop()
        self.env.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def whoami(self, rejected=()):
        def fake(client):
            if client.via in rejected:
                raise eod.Fail("AUTH", f"Azure DevOps rejected the {client.via} sign-in")
            return ME
        return mock.patch.object(eod, "whoami", side_effect=fake)

    def test_pat_first_without_calling_az(self):
        os.environ["ADO_PAT"] = "p"
        with self.whoami(), mock.patch.object(eod, "az_token") as az:
            client, me = eod.connect(self.args, {})
        self.assertEqual(client.via, "PAT")
        self.assertTrue(client.auth.startswith("Basic "))
        az.assert_not_called()

    def test_az_login_without_pat(self):
        with self.whoami(), mock.patch.object(eod, "az_token", return_value="tok"):
            client, me = eod.connect(self.args, {})
        self.assertEqual((client.via, client.auth), ("az login", "Bearer tok"))
        self.assertEqual(me, ME)

    def test_az_login_when_pat_rejected(self):
        os.environ["ADO_PAT"] = "expired"
        with self.whoami(rejected=("PAT",)), mock.patch.object(eod, "az_token", return_value="tok"):
            client, _ = eod.connect(self.args, {})
        self.assertEqual(client.via, "az login")

    def test_rejected_pat_without_az_reports_the_pat(self):
        os.environ["ADO_PAT"] = "expired"
        with self.whoami(rejected=("PAT",)), mock.patch.object(eod, "az_token", return_value=None):
            with self.assertRaises(eod.Fail) as e:
                eod.connect(self.args, {})
        self.assertEqual(e.exception.code, "AUTH")
        self.assertIn("PAT", str(e.exception))

    def test_both_rejected_names_both(self):
        os.environ["ADO_PAT"] = "expired"
        with self.whoami(rejected=("PAT", "az login")), mock.patch.object(eod, "az_token", return_value="tok"):
            with self.assertRaises(eod.Fail) as e:
                eod.connect(self.args, {})
        self.assertIn("az login --tenant t-1", str(e.exception))

    def test_az_token_asked_for_the_org_tenant(self):
        with self.whoami(), mock.patch.object(eod, "az_token", return_value="tok") as az:
            eod.connect(self.args, {})
        az.assert_called_once_with("t-1")

    def test_az_token_tries_tenant_then_default(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            ok = "--tenant" not in cmd
            return mock.Mock(returncode=0 if ok else 1, stdout="tok\n" if ok else "")
        with mock.patch.object(eod.shutil, "which", return_value="/usr/bin/az"), \
                mock.patch.object(eod.subprocess, "run", side_effect=run):
            self.assertEqual(eod.az_token("t-1"), "tok")
        self.assertEqual(["--tenant" in c for c in calls], [True, False])


class Commands(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"ADO_EOD_HOME": self.home}, clear=False)
        self.env.start()
        for var in ("AZURE_DEVOPS_EXT_PAT", "ADO_PAT"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def run_main(self, *argv, stdin=""):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("sys.stdin", io.StringIO(stdin)):
            code = eod.main(list(argv))
        return code, buf.getvalue()

    def test_no_pat_and_no_az_is_one_error_line(self):
        with mock.patch.object(eod, "az_token", return_value=None), \
                mock.patch.object(eod, "org_tenant", return_value=""):
            code, out = self.run_main("check", "--org", "acme")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("ERROR NO_AUTH:"))

    def test_config_saves_pat_and_never_prints_it(self):
        code, out = self.run_main("config", "--org", "acme", "--pat-stdin", stdin="secret-pat\n")
        self.assertEqual(code, 0)
        self.assertNotIn("secret-pat", out)
        self.assertEqual(eod.load_pat()[0], "secret-pat")
        if os.name == "posix":
            self.assertEqual(os.stat(os.path.join(self.home, "pat")).st_mode & 0o777, 0o600)
        with open(os.path.join(self.home, "config.json")) as f:
            self.assertEqual(json.load(f)["org"], "https://dev.azure.com/acme")

    def test_bad_sections_rejected(self):
        code, out = self.run_main("config", "--sections", '{"Team A": 1}')
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("ERROR BAD_ARGS:"))


if __name__ == "__main__":
    unittest.main()


class SkillDocPathsTest(unittest.TestCase):
    """Every `<eod>/...` path the skill docs name must exist, so a moved or renamed
    file breaks a test instead of silently breaking the weekly or EOD report."""

    def test_eod_paths_exist(self):
        import glob
        import re
        eod_dir = os.path.dirname(SCRIPTS)
        docs = [os.path.join(eod_dir, "SKILL.md"),
                os.path.join(os.path.dirname(eod_dir), "ado-weekly-report", "SKILL.md")]
        docs += glob.glob(os.path.join(eod_dir, "references", "*.md"))
        seen = 0
        for doc in docs:
            with open(doc, encoding="utf-8") as f:
                for rel in re.findall(r"<eod>/([\w./-]+)", f.read()):
                    seen += 1
                    path = os.path.join(eod_dir, rel.rstrip("."))
                    self.assertTrue(os.path.exists(path), f"{os.path.basename(doc)} names missing <eod>/{rel}")
        self.assertGreater(seen, 10)


def gh_node(repo, number, title, state="CLOSED", kind="Issue", **extra):
    node = {"number": number, "title": title, "state": state, "url": f"https://github.com/{repo}/x/{number}",
            "repository": {"nameWithOwner": repo}, "labels": {"nodes": []}}
    if kind == "Issue":
        node.update(stateReason="COMPLETED" if state == "CLOSED" else None, closedAt="2026-10-28T03:00:00Z",
                    parent=None, subIssues={"nodes": []})
    else:
        node.update(isDraft=False, mergedAt="2026-10-28T03:00:00Z" if state == "MERGED" else None,
                    reviewDecision=None, closingIssuesReferences={"nodes": []})
    node.update(extra)
    return node


class GitHub(unittest.TestCase):
    START = dt.datetime(2026, 10, 26, tzinfo=UTC)

    def fake(self, queries):
        repo = "acme/web"
        story = gh_node(repo, 1, "Checkout story", subIssues={"nodes": [
            {"number": 2, "title": "Retry", "state": "CLOSED"}, {"number": 3, "title": "Docs", "state": "OPEN"}]})
        task = gh_node(repo, 2, "Retry", parent={"number": 1, "title": "Checkout story", "state": "CLOSED",
                                                 "subIssues": story["subIssues"]})
        pr = gh_node(repo, 9, "Retry checkout", state="MERGED", kind="PR",
                     closingIssuesReferences={"nodes": [{"number": 2, "repository": {"nameWithOwner": repo}}]})
        open_issue = gh_node("acme/api", 5, "Rate limits", state="OPEN")

        def search(q):
            queries.append(q)
            if "is:pr" in q:
                return [pr] if "merged:" in q else []
            if "closed:" in q:
                return [story, task]
            if "sort:updated-desc" in q:
                return [open_issue, task]
            return []
        return search

    def test_issues_prs_children_and_sections(self):
        queries = []
        g = eod.collect_github(self.START, self.START + dt.timedelta(days=7), SECTIONS,
                               {"exclude": ["acme/private"], "sections": {"acme/api": "ProjectB"}},
                               search=self.fake(queries))
        self.assertTrue(all("-repo:acme/private" in q for q in queries))
        self.assertIn("merged:2026-10-26T00:00:00+00:00..2026-11-01T23:59:59+00:00", queries[0])
        story, task = g["items"]
        self.assertEqual((story["children"]["done"], story["children"]["total"]), (1, 2))
        self.assertEqual(story["children"]["open"][0]["id"], 3)
        self.assertEqual(task["parent"]["children"]["total"], 2)
        self.assertEqual([p["id"] for p in task["prs"]], [9])
        self.assertEqual(g["merged_prs"][0]["issues"], [2])
        self.assertEqual(story["section"], "web")             # no section matches: the repo's own name
        self.assertEqual(g["open_assigned"], [dict(g["open_assigned"][0], id=5)])
        self.assertEqual(g["open_assigned"][0]["section"], "ProjectB")   # mapped in github.sections

    def test_section_by_match_word_or_label(self):
        self.assertEqual(eod.gh_section("acme/ProjectA-site", [], SECTIONS, {}), "ProjectA")
        self.assertEqual(eod.gh_section("acme/site", ["projectc"], SECTIONS, {}), "ProjectC")
        self.assertEqual(eod.gh_section("acme/site", [], [], {}), "site")

    def test_repo_sections_go_before_the_catch_all(self):
        names = [s["name"] for s in eod.section_list(SECTIONS, ["web", "ProjectA"])]
        self.assertEqual(names, ["ProjectA", "ProjectB", "ProjectC", "web", "Others"])

    def test_github_failure_is_a_warning_not_an_error(self):
        def broken(*a, **k):
            raise eod.Fail("GITHUB", "the GitHub CLI (gh) is not installed")
        with mock.patch.object(eod, "connect", return_value=(FakeClient(), ME)), \
                mock.patch.object(eod, "load_config", return_value={"sections": SECTIONS}), \
                mock.patch.object(eod, "collect_github", side_effect=broken):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(eod.main(["collect", "--date", DAY.isoformat()]), 0)
        out = json.loads(buf.getvalue())
        self.assertIsNone(out["github"])
        self.assertIn("GitHub left out: the GitHub CLI (gh) is not installed", out["warnings"])

    def test_no_github_flag_and_config_off_skip_it(self):
        for argv, cfg in ((["collect", "--no-github"], {"sections": SECTIONS}),
                          (["collect"], {"sections": SECTIONS, "github": {"enabled": False}})):
            with mock.patch.object(eod, "connect", return_value=(FakeClient(), ME)), \
                    mock.patch.object(eod, "load_config", return_value=cfg), \
                    mock.patch.object(eod, "collect_github") as gh:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(eod.main(argv + ["--date", DAY.isoformat()]), 0)
                gh.assert_not_called()


class Profile(unittest.TestCase):
    """The report names who it is for: `check` says whether that is saved yet."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"ADO_EOD_HOME": self.home}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.home, True)

    def run_main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(eod, "connect", return_value=(FakeClient(), ME)):
            code = eod.main(list(argv))
        return code, buf.getvalue()

    def test_check_says_profile_none_then_the_saved_name_and_role(self):
        code, out = self.run_main("check")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[1], "PROFILE NONE")
        self.assertEqual(self.run_main("config", "--name", "Lan Tran", "--role", "Backend engineer")[0], 0)
        self.assertEqual(self.run_main("check")[1].splitlines()[1], "PROFILE Lan Tran | Backend engineer")
        self.run_main("config", "--role", "")
        self.assertEqual(self.run_main("check")[1].splitlines()[1], "PROFILE Lan Tran")

    def test_role_alone_needs_a_name(self):
        code, out = self.run_main("config", "--role", "QA")
        self.assertEqual(code, 2)
        self.assertIn("ERROR BAD_ARGS", out)

    def test_collect_carries_the_profile(self):
        self.run_main("config", "--name", "Lan Tran", "--sections", "[]")
        code, out = self.run_main("collect", "--no-github", "--date", DAY.isoformat())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["profile"], {"name": "Lan Tran"})
