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

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
spec = importlib.util.spec_from_file_location("ado_eod", os.path.join(SCRIPTS, "ado_eod.py"))
eod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eod)

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

    def test_strip_html(self):
        self.assertEqual(eod.strip_html("<div>Fixed &amp; deployed</div><div>QA next</div>"),
                         "Fixed & deployed / QA next")
        self.assertTrue(eod.strip_html("x" * 500).endswith("…"))


class Sections(unittest.TestCase):
    def test_match_project_area_or_tag_as_whole_word(self):
        f = {"System.TeamProject": "Core", "System.AreaPath": "Core\\PXP\\Checkout"}
        self.assertEqual(eod.section_for(f, eod.DEFAULT_SECTIONS), "PXP")
        f = {"System.TeamProject": "Core", "System.Tags": "timetable; urgent"}
        self.assertEqual(eod.section_for(f, eod.DEFAULT_SECTIONS), "Timetable")

    def test_no_partial_word_match(self):
        f = {"System.TeamProject": "SPXPlatform"}
        self.assertEqual(eod.section_for(f, eod.DEFAULT_SECTIONS), "Others")


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
                                        "System.State": "Closed", "System.TeamProject": "PXP",
                                        "System.AssignedTo": {"id": "me-1", "displayName": "Quy"}},
                  "relations": [{"rel": "System.LinkTypes.Hierarchy-Reverse",
                                 "url": "https://dev.azure.com/acme/_apis/wit/workItems/100"},
                                {"rel": "ArtifactLink", "url": "vstfs:///Git/PullRequestId/p1%2Fr1%2F55"}]},
            102: {"id": 102, "fields": {"System.Title": "Someone else's", "System.WorkItemType": "Bug",
                                        "System.State": "Active", "System.TeamProject": "SXP",
                                        "System.AssignedTo": {"id": "x", "displayName": "X"}}},
            100: {"id": 100, "fields": {"System.Title": "Payments story", "System.WorkItemType": "User Story"}},
            200: {"id": 200, "fields": {"System.Title": "Next thing", "System.WorkItemType": "Task",
                                        "System.State": "New", "System.TeamProject": "Timetable"}},
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
                               "repository": {"id": "r1", "name": "web", "project": {"id": "p1", "name": "PXP"}}}
                              for pr in self.merged]}
        if url.endswith("/workitems"):
            pr = int(url.split("/pullRequests/")[1].split("/")[0])
            return {"value": [{"id": str(i)} for i in self.merged[pr]]}
        if "/pullrequests/" in url:
            return {"status": "completed", "title": "Retry fix", "repository": {"name": "web"}, "reviewers": [{"vote": 10}]}
        raise AssertionError(url)


class Collect(unittest.TestCase):
    def test_collect_end_to_end(self):
        out = eod.collect(FakeClient(), DAY, UTC, eod.DEFAULT_SECTIONS)
        self.assertEqual([i["id"] for i in out["items"]], [101])
        item = out["items"][0]
        self.assertEqual(item["section"], "PXP")
        self.assertEqual((item["state_start"], item["state_end"]), ("Active", "Closed"))
        self.assertEqual(item["parent"]["title"], "Payments story")
        self.assertEqual(item["prs"][0]["status"], "completed")
        self.assertEqual(item["url"], "https://dev.azure.com/acme/PXP/_workitems/edit/101")
        self.assertEqual([o["id"] for o in out["open_assigned"]], [200])
        self.assertEqual(out["open_assigned"][0]["section"], "Timetable")
        self.assertEqual(out["merged_prs"], [])
        self.assertEqual(item["merged_today"], [])

    def test_items_of_a_merged_pr_are_listed_though_untouched(self):
        client = FakeClient()
        client.items[300] = {"id": 300, "fields": {"System.Title": "Linked task", "System.WorkItemType": "Task",
                                                   "System.State": "New", "System.TeamProject": "PXP"}}
        client.updates[300] = [update(1, "2026-10-20T08:00:00Z", System_State={"newValue": "New"})]
        client.merged = {60: [300], 61: []}
        out = eod.collect(client, DAY, UTC, eod.DEFAULT_SECTIONS)
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
        out = eod.collect(client, DAY, UTC, eod.DEFAULT_SECTIONS)
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
        code, out = self.run_main("config", "--sections", '{"PXP": 1}')
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("ERROR BAD_ARGS:"))


if __name__ == "__main__":
    unittest.main()
