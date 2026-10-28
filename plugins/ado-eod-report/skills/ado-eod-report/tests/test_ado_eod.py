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

    def call(self, method, url, body=None):
        if url.endswith("connectionData"):
            return {"authenticatedUser": {"id": "me-1", "providerDisplayName": "Quy"}}
        if url.endswith("wiql"):
            ids = [200, 101] if "NOT IN" in body["query"] else [101, 102]
            return {"workItems": [{"id": i} for i in ids]}
        if url.endswith("workitemsbatch"):
            return {"value": [self.items[i] for i in body["ids"]]}
        if "/updates" in url:
            return {"value": self.updates[int(url.split("/workItems/")[1].split("/")[0])]}
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

    def test_wiql_falls_back_when_ever_is_refused(self):
        client = FakeClient()
        real = client.call

        def call(method, url, body=None):
            if url.endswith("wiql") and "EVER" in body["query"]:
                raise eod.Fail("HTTP", "400 EVER not supported")
            return real(method, url, body)

        client.call = call
        self.assertEqual(eod.touched_ids(client, DAY), [101, 102])


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

    def test_missing_pat_is_one_error_line(self):
        code, out = self.run_main("check", "--org", "acme")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("ERROR NO_PAT:"))

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
