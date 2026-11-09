"""Collect one person's Azure DevOps work for a day or a week, for the EOD and weekly reports.

usage:
    python ado_eod.py check
    python ado_eod.py collect [--date today|yesterday|YYYY-MM-DD] [--days N] [--org URL]
    python ado_eod.py collect --week this|last|YYYY-MM-DD [--org URL]
    python ado_eod.py config [--org URL] [--sections JSON] [--timezone IANA] [--pat-stdin] [--show]

check    signs in and prints `OK <name> <email> <org> (via <PAT|az login>)`.
collect  prints one JSON document: every work item the PAT's owner changed that
         day (or that is assigned to them and changed state that day), what
         changed and when, its parent, linked PRs and their status, the report
         section it belongs to, and the open items still assigned to them.
         --days N widens the window to N days from --date; --week takes the
         Monday-to-Sunday week holding that date.
config   saves settings to <config dir>/config.json (the PAT to <config dir>/pat).

Sign-in: a PAT from AZURE_DEVOPS_EXT_PAT, then ADO_PAT, then <config dir>/pat.
Without one, or when Azure DevOps rejects it, the Azure CLI sign-in (`az login`)
is used instead. The config dir is ADO_EOD_HOME, or ~/.config/ado-eod-report. The organization
comes from --org, then the config, then the origin remote of the current
directory. PAT scopes: Work Items (Read & write); Code (Read) adds PR status.

Every failure prints one line `ERROR <CODE>: <message>` and exits 2. Codes:
NO_AUTH, NO_ORG, AUTH, FORBIDDEN, NOT_FOUND, NETWORK, BAD_ARGS.
GitHub (through the gh CLI's sign-in) is optional: when it fails, collect
still succeeds and says why in `warnings`.
"""
import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "7.1"
# The Azure DevOps application ID: `az` issues tokens for this resource.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
# With no sections configured, each item goes to a section named after its own
# Azure DevOps project, so nothing about any one team is built in.
OTHERS = "Others"
# Older versions saved these fixed sections on the first run. A config holding
# exactly them was never chosen by the user, so it is treated as unset.
LEGACY_SECTIONS = [
    {"name": "PXP", "match": ["PXP"]},
    {"name": "SXP", "match": ["SXP"]},
    {"name": "Timetable", "match": ["Timetable"], "style": "plain"},
    {"name": "Others", "match": []},
]
# States that mean the item needs nothing more from its owner. Processes name
# them differently, and WIQL has no state category, so this is a list.
CLOSED_STATES = ["Closed", "Done", "Removed", "Resolved", "Completed", "Cut", "Inactive"]
# Bookkeeping fields ADO rewrites on every save; they say nothing about the work.
NOISE_FIELDS = re.compile(
    r"^(System\.(Rev|ChangedDate|ChangedBy|AuthorizedDate|AuthorizedAs|RevisedDate|Watermark|PersonId|"
    r"IsDeleted|CommentCount|AreaId|IterationId|NodeName|AreaLevel\d|IterationLevel\d|TeamProject|"
    r"History|State|Reason|AssignedTo|BoardColumn|BoardColumnDone|CreatedDate|CreatedBy|Id|WorkItemType|"
    r"ExternalLinkCount|RelatedLinkCount|HyperLinkCount|AttachedFileCount|RemoteLinkCount|Parent)|"
    r"Microsoft\.VSTS\.Common\.(StateChangeDate|ActivatedDate|ActivatedBy|ClosedDate|ClosedBy|"
    r"ResolvedDate|ResolvedBy|ResolvedReason)|Microsoft\.VSTS\.Scheduling\.CompletedWork|"
    r"WEF_.*)$"
)
PR_LINK = re.compile(r"^vstfs:///Git/PullRequestId/([^%/]+)%2[Ff]([^%/]+)%2[Ff](\d+)$")


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --- settings -----------------------------------------------------------------

def config_dir() -> Path:
    return Path(os.environ.get("ADO_EOD_HOME") or Path.home() / ".config" / "ado-eod-report")


def load_config() -> dict:
    path = config_dir() / "config.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def load_pat():
    """(pat, where it came from), or (None, None)."""
    for var in ("AZURE_DEVOPS_EXT_PAT", "ADO_PAT"):
        if os.environ.get(var, "").strip():
            return os.environ[var].strip(), f"env {var}"
    path = config_dir() / "pat"
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip(), str(path)
    return None, None


def save_pat(pat: str) -> Path:
    config_dir().mkdir(parents=True, exist_ok=True)
    path = config_dir() / "pat"
    path.write_text(pat.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def org_tenant(org: str) -> str:
    """The Entra tenant that backs the organization. Azure DevOps names it in
    X-VSS-ResourceTenant even on an unauthenticated request; "" when unknown."""
    # Without this header ADO redirects to its sign-in page, which drops the tenant.
    req = urllib.request.Request(f"{org}/_apis/connectionData?api-version={API}",
                                 headers={"X-TFS-FedAuthRedirect": "Suppress"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as e:
        headers = e.headers
    except (urllib.error.URLError, OSError):
        return ""
    tenant = (headers.get("X-VSS-ResourceTenant") or "").split(",")[0].strip()
    return "" if tenant in ("", "00000000-0000-0000-0000-000000000000") else tenant


def az_token(tenant: str = ""):
    """An Azure DevOps access token from the Azure CLI sign-in, or None when az
    is missing or not signed in. With a tenant, the token comes from it: az's
    default subscription may sit in another tenant than the organization."""
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        return None
    cmd = [az, "account", "get-access-token", "--resource", ADO_RESOURCE,
           "--query", "accessToken", "-o", "tsv", "--only-show-errors"]
    for extra in ([["--tenant", tenant]] if tenant else []) + [[]]:
        try:
            result = subprocess.run(cmd + extra, capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def normalize_org(org: str) -> str:
    org = org.strip().rstrip("/")
    if not org:
        return ""
    if "://" not in org:
        return f"https://dev.azure.com/{org}"
    m = re.match(r"(https://dev\.azure\.com/[^/]+)", org)
    if m:
        return m.group(1)
    m = re.match(r"(https://[^./]+\.visualstudio\.com)", org)
    return m.group(1) if m else org


def org_from_remote(url: str) -> str:
    for pattern, org in ((r"https://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/", "https://dev.azure.com/{}"),
                         (r"(?:git@)?ssh\.dev\.azure\.com:v3/([^/]+)/", "https://dev.azure.com/{}"),
                         (r"https://(?:[^@/]+@)?([^./]+)\.visualstudio\.com/", "https://{}.visualstudio.com")):
        m = re.match(pattern, url.strip())
        if m:
            return org.format(m.group(1))
    return ""


def resolve_org(arg: str, cfg: dict) -> str:
    if arg:
        return normalize_org(arg)
    if cfg.get("org"):
        return normalize_org(cfg["org"])
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout
    except OSError:
        url = ""
    return org_from_remote(url)


def timezone(cfg: dict):
    name = cfg.get("timezone")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            raise Fail("BAD_ARGS", f"unknown timezone {name!r} in config")
    return dt.datetime.now().astimezone().tzinfo


def parse_day(text: str, tz) -> dt.date:
    today = dt.datetime.now(tz).date()
    text = (text or "today").strip().lower()
    if text == "today":
        return today
    if text == "yesterday":
        return today - dt.timedelta(days=1)
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise Fail("BAD_ARGS", f"--date must be today, yesterday or YYYY-MM-DD, got {text!r}")


def parse_week(text: str, tz) -> dt.date:
    """The Monday of the week named by this, last or a YYYY-MM-DD inside it."""
    text = (text or "this").strip().lower()
    today = dt.datetime.now(tz).date()
    if text == "this":
        day = today
    elif text == "last":
        day = today - dt.timedelta(days=7)
    else:
        try:
            day = dt.date.fromisoformat(text)
        except ValueError:
            raise Fail("BAD_ARGS", f"--week must be this, last or YYYY-MM-DD, got {text!r}")
    return day - dt.timedelta(days=day.weekday())


def parse_time(value: str) -> dt.datetime:
    """ADO timestamps: 2026-10-28T09:12:33.1234567Z, with 0-7 fraction digits.
    fromisoformat only takes 'Z' and long fractions from Python 3.11."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", value or "")
    if not m:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    base = dt.datetime.fromisoformat(m.group(1))
    micro = int((m.group(2) or "0")[:6].ljust(6, "0"))
    zone = m.group(3) or "Z"
    offset = dt.timezone.utc if zone == "Z" else dt.datetime.strptime(zone.replace(":", ""), "%z").tzinfo
    return base.replace(microsecond=micro, tzinfo=offset)


# --- HTTP ---------------------------------------------------------------------

class Client:
    def __init__(self, org: str, pat: str = None, token: str = None):
        self.org = org
        if pat:
            self.auth, self.via = "Basic " + base64.b64encode(f":{pat}".encode()).decode(), "PAT"
        else:
            self.auth, self.via = f"Bearer {token}", "az login"

    def call(self, method: str, url: str, body=None):
        if not url.startswith("http"):
            url = f"{self.org}/{url}"
        if "api-version=" not in url:
            url += ("&" if "?" in url else "?") + f"api-version={API}"
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    text = resp.read().decode("utf-8")
                    # A wrong or expired PAT gets 203 with a sign-in page, not 401.
                    if resp.status == 203 or not text.lstrip().startswith(("{", "[")):
                        raise Fail("AUTH", f"Azure DevOps did not accept the {self.via} sign-in "
                                           "(expired, revoked, or not a member of this organization)")
                    return json.loads(text)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                if e.code == 401:
                    raise Fail("AUTH", f"Azure DevOps rejected the {self.via} sign-in (401): "
                                       "expired, revoked, or not a member of this organization")
                if e.code == 403:
                    raise Fail("FORBIDDEN", f"the {self.via} sign-in has no access to {url.split('?')[0]}")
                if e.code == 404:
                    raise Fail("NOT_FOUND", f"not found: {url.split('?')[0]}")
                raise Fail("HTTP", f"{e.code} from {url.split('?')[0]}: {message_of(detail)}")
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise Fail("NETWORK", f"cannot reach {self.org}: {e.reason}")
        raise Fail("NETWORK", f"gave up on {url.split('?')[0]}")


def message_of(detail: str) -> str:
    try:
        return json.loads(detail).get("message", detail)[:300]
    except ValueError:
        return detail[:300]


# --- collecting ---------------------------------------------------------------

def whoami(client: Client) -> dict:
    user = client.call("GET", f"_apis/connectionData?api-version={API}-preview")["authenticatedUser"]
    email = (user.get("properties", {}).get("Account", {}) or {}).get("$value", "")
    return {"id": user["id"], "name": user.get("providerDisplayName", ""), "email": email}


def wiql(client: Client, query: str) -> list:
    return [w["id"] for w in client.call("POST", "_apis/wit/wiql", {"query": query}).get("workItems", [])]


def touched_ids(client: Client, day: dt.date) -> list:
    # One day early: WIQL dates use the profile's time zone, not ours. The
    # exact window is applied to each update afterwards.
    since = (day - dt.timedelta(days=1)).isoformat()
    base = f"SELECT [System.Id] FROM WorkItems WHERE [System.ChangedDate] >= '{since}' AND "
    try:
        return wiql(client, base + "([System.ChangedBy] EVER @Me OR [System.AssignedTo] EVER @Me)")
    except Fail as e:
        if e.code != "HTTP":
            raise
        # Collections where EVER is refused: last changer or assignee only.
        return wiql(client, base + "([System.ChangedBy] = @Me OR [System.AssignedTo] = @Me)")


def open_assigned_ids(client: Client) -> list:
    states = ", ".join(f"'{s}'" for s in CLOSED_STATES)
    return wiql(client, "SELECT [System.Id] FROM WorkItems WHERE [System.AssignedTo] = @Me "
                        f"AND [System.State] NOT IN ({states}) "
                        "AND [System.WorkItemType] NOT IN ('Epic', 'Feature') "
                        "ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.ChangedDate] DESC")


def fetch_items(client: Client, ids: list, relations: bool) -> dict:
    out = {}
    for i in range(0, len(ids), 200):
        body = {"ids": ids[i:i + 200], "errorPolicy": "Omit"}
        if relations:
            body["$expand"] = "Relations"
        else:
            body["fields"] = ["System.Id", "System.Title", "System.WorkItemType", "System.State",
                              "System.TeamProject", "System.AreaPath", "System.IterationPath", "System.Tags",
                              "Microsoft.VSTS.Common.Priority"]
        for item in client.call("POST", "_apis/wit/workitemsbatch", body).get("value", []):
            if item:
                out[item["id"]] = item
    return out


def fetch_updates(client: Client, wid: int) -> list:
    updates, skip = [], 0
    while True:
        page = client.call("GET", f"_apis/wit/workItems/{wid}/updates?$top=200&$skip={skip}").get("value", [])
        updates += page
        if len(page) < 200:
            return updates
        skip += 200


def strip_html(text: str, limit: int = 400) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", " / ", text).strip(" /")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def person(value) -> str:
    if isinstance(value, dict):
        return value.get("displayName", "")
    return str(value or "").split(" <")[0]


def summarize(updates: list, me: dict, start: dt.datetime, end: dt.datetime, tz) -> dict:
    """What happened to one item inside [start, end): the events, and its state
    before and after the window."""
    state = None
    state_start, started = None, False
    events = []
    for u in sorted(updates, key=lambda u: u.get("rev", u.get("id", 0))):
        fields = u.get("fields", {})
        when_raw = (fields.get("System.ChangedDate") or {}).get("newValue") or u.get("revisedDate", "")
        when = parse_time(when_raw)
        if "System.State" in fields:
            new_state = fields["System.State"].get("newValue")
        else:
            new_state = state
        if when < start:
            state = new_state
            continue
        if when >= end:
            break
        if not started:
            state_start, started = state, True
        by = u.get("revisedBy", {}) or {}
        local = when.astimezone(tz)
        event = {"date": local.date().isoformat(), "time": local.strftime("%H:%M"), "by_me": by.get("id") == me["id"],
                 "by": by.get("displayName", "")}
        if u.get("rev") == 1:
            event["created"] = True
        if "System.State" in fields:
            event["state"] = [fields["System.State"].get("oldValue"), new_state]
        if "System.Reason" in fields and fields["System.Reason"].get("newValue"):
            event["reason"] = fields["System.Reason"]["newValue"]
        if "System.AssignedTo" in fields:
            event["assigned"] = [person(fields["System.AssignedTo"].get("oldValue")),
                                 person(fields["System.AssignedTo"].get("newValue"))]
        if "System.BoardColumn" in fields:
            event["board"] = [fields["System.BoardColumn"].get("oldValue"), fields["System.BoardColumn"].get("newValue")]
        if "Microsoft.VSTS.Scheduling.CompletedWork" in fields:
            f = fields["Microsoft.VSTS.Scheduling.CompletedWork"]
            event["completed_work"] = [f.get("oldValue"), f.get("newValue")]
        if (fields.get("System.History") or {}).get("newValue"):
            event["comment"] = strip_html(fields["System.History"]["newValue"])
        other = sorted({name.rsplit(".", 1)[-1] for name in fields if not NOISE_FIELDS.match(name)})
        if other:
            event["fields"] = other
        added = (u.get("relations") or {}).get("added") or []
        if any(PR_LINK.match(r.get("url", "")) for r in added):
            event["pr_linked"] = True
        if any(r.get("rel") == "System.LinkTypes.Hierarchy-Forward" for r in added):
            event["children_added"] = sum(r.get("rel") == "System.LinkTypes.Hierarchy-Forward" for r in added)
        events.append(event)
        state = new_state
    if not started:
        state_start = state
    return {"events": events, "state_start": state_start, "state_end": state}


def relevant(item_fields: dict, summary: dict, me: dict) -> bool:
    """The day's work of this person: they changed the item, or it is theirs and
    someone else moved its state (QA closing it, a reviewer reopening it)."""
    if any(e["by_me"] for e in summary["events"]):
        return True
    mine = (item_fields.get("System.AssignedTo") or {}).get("id") == me["id"]
    return mine and any("state" in e for e in summary["events"])


def section_for(fields: dict, sections: list) -> str:
    if not sections:
        return fields.get("System.TeamProject") or OTHERS
    hay = [fields.get("System.TeamProject", ""), fields.get("System.AreaPath", ""),
           fields.get("System.IterationPath", "")] + split_tags(fields.get("System.Tags"))
    for s in sections:
        for m in s.get("match", []):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(m) + r"(?![A-Za-z0-9])"
            if any(re.search(pattern, h or "", re.I) for h in hay):
                return s["name"]
    fallback = [s["name"] for s in sections if not s.get("match")]
    return fallback[-1] if fallback else OTHERS


def section_list(sections: list, used: list) -> list:
    """The report's sections in order, with their style. Configured sections keep
    their order; by-project sections are sorted, with Others last."""
    if sections:
        names = [s["name"] for s in sections]
        out = [{"name": s["name"], "style": s.get("style", "stories")} for s in sections]
        extra = sorted({u for u in used if u not in names and u != OTHERS}, key=str.lower)
        catch_all = next((i for i, s in enumerate(sections) if not s.get("match")), len(out))
        out[catch_all:catch_all] = [{"name": n, "style": "stories"} for n in extra]
        if OTHERS in used and OTHERS not in names:
            out.append({"name": OTHERS, "style": "stories"})
        return out
    names = sorted({u for u in used if u != OTHERS}, key=str.lower) + ([OTHERS] if OTHERS in used else [])
    return [{"name": n, "style": "stories"} for n in names]


def split_tags(tags) -> list:
    return [t.strip() for t in (tags or "").split(";") if t.strip()]


def is_blocked(fields: dict) -> bool:
    return (str(fields.get("Microsoft.VSTS.CMMI.Blocked", "")).lower() == "yes"
            or "block" in str(fields.get("System.State", "")).lower()
            or any("block" in t.lower() for t in split_tags(fields.get("System.Tags"))))


def pr_refs(item: dict) -> list:
    refs = []
    for r in item.get("relations") or []:
        m = PR_LINK.match(r.get("url", ""))
        if m:
            refs.append((urllib.parse.unquote(m.group(1)), urllib.parse.unquote(m.group(2)), int(m.group(3))))
    return refs


def parent_id(item: dict):
    for r in item.get("relations") or []:
        if r.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            return int(r["url"].rstrip("/").rsplit("/", 1)[-1])
    return None


def child_ids(item: dict) -> list:
    return [int(r["url"].rstrip("/").rsplit("/", 1)[-1]) for r in item.get("relations") or []
            if r.get("rel") == "System.LinkTypes.Hierarchy-Forward"]


def children_of(item: dict, known: dict, merged_ids: set):
    """How far a story's tasks are, counting the ones nobody touched in the
    window: a merged PR (merged_ids) counts its task as done. Removed and cut
    tasks don't count."""
    kids = [known[c] for c in child_ids(item) if c in known]
    if not kids:
        return None
    kids = [k for k in kids if k["fields"].get("System.State") not in ("Removed", "Cut")]
    done = lambda k: k["id"] in merged_ids or k["fields"].get("System.State") in CLOSED_STATES
    return {"total": len(kids), "done": sum(map(done, kids)),
            "open": [{"id": k["id"], "type": k["fields"].get("System.WorkItemType"),
                      "title": k["fields"].get("System.Title"), "state": k["fields"].get("System.State")}
                     for k in kids if not done(k)]}


def fetch_pr(client: Client, project: str, repo: str, pr: int) -> dict:
    try:
        data = client.call("GET", f"{urllib.parse.quote(project)}/_apis/git/repositories/{repo}/pullrequests/{pr}")
    except Fail as e:
        return {"id": pr, "status": "unknown", "error": e.code}
    status = data.get("status", "")
    if status == "active" and data.get("isDraft"):
        status = "draft"
    votes = [r.get("vote", 0) for r in data.get("reviewers", [])]
    return {"id": pr, "title": data.get("title", ""), "status": status,
            "repo": (data.get("repository") or {}).get("name", ""),
            "approved": sum(v >= 5 for v in votes), "waiting_or_rejected": sum(v < 0 for v in votes)}


def merged_prs(client: Client, me: dict, start: dt.datetime, end: dt.datetime) -> list:
    """This person's pull requests completed inside [start, end), across the
    organization, with the work items linked to each. A merge often leaves
    those items untouched on the board, so they are found from the PR side."""
    utc = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({
        "searchCriteria.creatorId": me["id"], "searchCriteria.status": "completed",
        "searchCriteria.queryTimeRangeType": "closed",
        "searchCriteria.minTime": utc(start), "searchCriteria.maxTime": utc(end), "$top": 200})
    out = []
    for p in client.call("GET", f"_apis/git/pullrequests?{query}").get("value", []):
        repo = p.get("repository") or {}
        project = (repo.get("project") or {}).get("id", "")
        links = client.call("GET", f"{project}/_apis/git/repositories/{repo.get('id')}"
                                   f"/pullRequests/{p['pullRequestId']}/workitems").get("value", [])
        out.append({"id": p["pullRequestId"], "title": p.get("title", ""), "repo": repo.get("name", ""),
                    "project": (repo.get("project") or {}).get("name", ""),
                    "work_items": sorted(int(w["id"]) for w in links)})
    return sorted(out, key=lambda p: p["id"])


def web_url(org: str, fields: dict, wid: int) -> str:
    return f"{org}/{urllib.parse.quote(fields.get('System.TeamProject', ''))}/_workitems/edit/{wid}"


def collect(client: Client, day: dt.date, tz, sections: list, me: dict = None, days: int = 1) -> dict:
    """The work of [day, day + days); merged_today means merged inside that window."""
    me = me or whoami(client)
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end = start + dt.timedelta(days=days)
    warnings = []

    try:
        merged = merged_prs(client, me, start, end)
    except Fail as e:
        if e.code not in ("FORBIDDEN", "AUTH", "HTTP"):
            raise
        merged = []
        warnings.append("Merged PRs unknown: the sign-in can't read code (a PAT needs the Code (Read) scope)")
    merged_ids = {wid for p in merged for wid in p["work_items"]}

    ids = sorted(set(touched_ids(client, day)) | merged_ids)
    items = fetch_items(client, ids, relations=True) if ids else {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        updates = dict(zip(items, pool.map(lambda wid: fetch_updates(client, wid), items)))

    worked = {}
    for wid, item in items.items():
        summary = summarize(updates[wid], me, start, end, tz)
        if wid in merged_ids or (summary["events"] and relevant(item["fields"], summary, me)):
            worked[wid] = (item, summary)

    parents = {parent_id(item) for item, _ in worked.values()} - {None}
    parent_items = fetch_items(client, sorted(parents - set(items)), relations=True) if parents else {}
    parent_items.update({pid: items[pid] for pid in parents if pid in items})
    kids = {c for i in list(parent_items.values()) + [i for i, _ in worked.values()] for c in child_ids(i)}
    known = dict(items)
    known.update(fetch_items(client, sorted(kids - set(items)), relations=False) if kids - set(items) else {})
    pr_jobs = [(wid, ref) for wid, (item, _) in worked.items() for ref in pr_refs(item)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        prs = dict(zip(pr_jobs, pool.map(lambda job: fetch_pr(client, *job[1]), pr_jobs)))
    if any(p.get("error") == "FORBIDDEN" for p in prs.values()):
        warnings.append("PR status unknown: the sign-in can't read code (a PAT needs the Code (Read) scope)")

    merged_ids |= {wid for (wid, _), pr in prs.items() if pr.get("status") == "completed"}
    out_items = []
    for wid, (item, summary) in worked.items():
        f = item["fields"]
        pid = parent_id(item)
        parent = parent_items.get(pid, {}).get("fields", {})
        assignee = f.get("System.AssignedTo") or {}
        out_items.append({
            "id": wid,
            "type": f.get("System.WorkItemType"),
            "title": f.get("System.Title"),
            "section": section_for(f, sections),
            "project": f.get("System.TeamProject"),
            "area": f.get("System.AreaPath"),
            "iteration": f.get("System.IterationPath"),
            "state_start": summary["state_start"],
            "state_end": summary["state_end"],
            "state_now": f.get("System.State"),
            "assigned_to": person(assignee),
            "assigned_to_me": assignee.get("id") == me["id"],
            "tags": split_tags(f.get("System.Tags")),
            "priority": f.get("Microsoft.VSTS.Common.Priority"),
            "remaining_work": f.get("Microsoft.VSTS.Scheduling.RemainingWork"),
            "blocked": is_blocked(f),
            "parent": {"id": pid, "type": parent.get("System.WorkItemType"), "title": parent.get("System.Title"),
                       "state": parent.get("System.State"),
                       "children": children_of(parent_items.get(pid, {}), known, merged_ids)}
            if pid else None,
            "children": children_of(item, known, merged_ids),
            "prs": [prs[(wid, ref)] for ref in pr_refs(item)],
            "merged_today": [p["id"] for p in merged if wid in p["work_items"]],
            "events": summary["events"],
            "url": web_url(client.org, f, wid),
        })
    out_items.sort(key=lambda i: (i["section"], i["parent"]["id"] if i["parent"] else i["id"], i["id"]))

    open_ids = [i for i in open_assigned_ids(client) if i not in worked][:15]
    open_items = fetch_items(client, open_ids, relations=False) if open_ids else {}
    open_out = []
    for wid in open_ids:
        if wid in open_items:
            f = open_items[wid]["fields"]
            open_out.append({"id": wid, "type": f.get("System.WorkItemType"), "title": f.get("System.Title"),
                             "state": f.get("System.State"), "section": section_for(f, sections),
                             "priority": f.get("Microsoft.VSTS.Common.Priority"), "blocked": is_blocked(f)})

    return {"date": day.isoformat(), "to": (day + dt.timedelta(days=days - 1)).isoformat(), "days": days,
            "timezone": str(tz), "org": client.org, "me": me,
            "sections": section_list(sections, [i["section"] for i in out_items + open_out]),
            "sections_from": "config" if sections else "project", "items": out_items, "merged_prs": merged,
            "open_assigned": open_out, "warnings": warnings}


# --- commands -----------------------------------------------------------------

# ---------------------------------------------------------------- GitHub
# GitHub work comes from the `gh` CLI's own sign-in, so it needs no token here.
# It is a second source next to Azure DevOps: issues play the part of work
# items (an issue with sub-issues is a story, a sub-issue its task), and PRs
# play the part of ADO PRs.

GH_FIELDS = """
  ... on PullRequest { number title url state isDraft mergedAt updatedAt reviewDecision
    repository { nameWithOwner } labels(first: 20) { nodes { name } }
    closingIssuesReferences(first: 20) { nodes { number repository { nameWithOwner } } } }
  ... on Issue { number title url state stateReason closedAt updatedAt
    repository { nameWithOwner } labels(first: 20) { nodes { name } }
    parent { number title state subIssues(first: 100) { nodes { number title state } } }
    subIssues(first: 100) { nodes { number title state } } }
"""
GH_QUERY = ("query($q: String!, $after: String) { search(query: $q, type: ISSUE, first: 100, after: $after) {"
            " pageInfo { hasNextPage endCursor } nodes {" + GH_FIELDS + "} } }")


def gh_search(q: str, limit: int = 300) -> list:
    """Every node a GitHub search returns, through `gh api graphql`."""
    gh = shutil.which("gh")
    if not gh:
        raise Fail("GITHUB", "the GitHub CLI (gh) is not installed")
    nodes, after = [], None
    while len(nodes) < limit:
        cmd = [gh, "api", "graphql", "-f", f"query={GH_QUERY}", "-f", f"q={q}"]
        if after:
            cmd += ["-f", f"after={after}"]
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=90)
        except subprocess.TimeoutExpired:
            raise Fail("GITHUB", "GitHub search timed out")
        if run.returncode != 0:
            lines = (run.stderr or run.stdout or "").strip().splitlines()
            raise Fail("GITHUB", lines[-1] if lines else "gh api graphql failed (run `gh auth login`)")
        page = json.loads(run.stdout)["data"]["search"]
        nodes += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return nodes[:limit]


def gh_section(repo: str, labels: list, sections: list, mapping: dict) -> str:
    """The section of a GitHub repo: the user's mapping, else a configured
    section whose match words appear in the repo or a label, else the repo's
    own name. Nothing is built in."""
    if repo in mapping:
        return mapping[repo]
    short = repo.split("/")[-1]
    for s in sections or []:
        for m in s.get("match", []):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(m) + r"(?![A-Za-z0-9])"
            if any(re.search(pattern, h or "", re.I) for h in [repo, short] + labels):
                return s["name"]
    return short


def gh_state(state: str) -> str:
    return {"OPEN": "Open", "CLOSED": "Closed", "MERGED": "Merged"}.get(state, state.title())


def gh_children(nodes: list):
    if not nodes:
        return None
    return {"total": len(nodes), "done": sum(n["state"] == "CLOSED" for n in nodes),
            "open": [{"id": n["number"], "title": n["title"], "state": gh_state(n["state"])}
                     for n in nodes if n["state"] != "CLOSED"]}


def gh_labels(node: dict) -> list:
    return [l["name"] for l in (node.get("labels") or {}).get("nodes", [])]


def collect_github(start: dt.datetime, end: dt.datetime, sections: list, gh_cfg: dict, search=None) -> dict:
    """GitHub issues and PRs of the signed-in gh user in [start, end)."""
    span = f"{start.isoformat()}..{(end - dt.timedelta(seconds=1)).isoformat()}"
    skip = " ".join(f"-repo:{r}" for r in gh_cfg.get("exclude", []))
    mapping = gh_cfg.get("sections", {})
    run = search or gh_search

    def find(q):
        return run(f"{q} {skip}".strip())

    def section(repo, node):
        return gh_section(repo, gh_labels(node), sections, mapping)

    def pr_out(n):
        status = ("completed" if n["state"] == "MERGED" else "abandoned" if n["state"] == "CLOSED"
                  else "draft" if n.get("isDraft") else "active")
        repo = n["repository"]["nameWithOwner"]
        return {"repo": repo, "id": n["number"], "title": n["title"], "status": status, "url": n["url"],
                "merged_at": n.get("mergedAt"), "review": n.get("reviewDecision"),
                "issues": [i["number"] for i in (n.get("closingIssuesReferences") or {}).get("nodes", [])
                           if i["repository"]["nameWithOwner"] == repo],
                "section": section(repo, n)}

    merged = [pr_out(n) for n in find(f"is:pr author:@me merged:{span}")]
    open_prs = [pr_out(n) for n in find(f"is:pr is:open author:@me updated:{span}")]
    prs_by_issue = {}
    for p in merged + open_prs:
        for i in p["issues"]:
            prs_by_issue.setdefault((p["repo"], i), []).append({k: p[k] for k in ("id", "title", "status", "url")})

    issues = {}
    for q in (f"is:issue author:@me closed:{span}", f"is:issue assignee:@me closed:{span}",
              f"is:issue is:open assignee:@me updated:{span}", f"is:issue is:open author:@me updated:{span}"):
        for n in find(q):
            issues[(n["repository"]["nameWithOwner"], n["number"])] = n
    items = []
    for (repo, num), n in sorted(issues.items()):
        par = n.get("parent")
        items.append({
            "source": "github", "repo": repo, "id": num, "type": "Issue", "title": n["title"],
            "state_end": gh_state(n["state"]), "state_reason": n.get("stateReason"),
            "closed_at": n.get("closedAt"), "labels": gh_labels(n), "section": section(repo, n),
            "parent": ({"id": par["number"], "type": "Issue", "title": par["title"], "state": gh_state(par["state"]),
                        "children": gh_children((par.get("subIssues") or {}).get("nodes", []))} if par else None),
            "children": gh_children((n.get("subIssues") or {}).get("nodes", [])),
            "prs": prs_by_issue.get((repo, num), []),
            "blocked": any("block" in l.lower() for l in gh_labels(n)),
            "url": n["url"],
        })

    open_assigned = [{"repo": n["repository"]["nameWithOwner"], "id": n["number"], "type": "Issue",
                      "title": n["title"], "state": "Open", "section": section(n["repository"]["nameWithOwner"], n)}
                     for n in find("is:issue is:open assignee:@me sort:updated-desc")
                     if (n["repository"]["nameWithOwner"], n["number"]) not in issues][:15]
    return {"items": items, "merged_prs": merged, "open_prs": open_prs, "open_assigned": open_assigned}


def connect(args, cfg):
    """(client, me) signed in with the PAT, or with `az login` when there is no
    PAT or Azure DevOps rejects it."""
    org = resolve_org(getattr(args, "org", ""), cfg)
    if not org:
        raise Fail("NO_ORG", "no organization: save one with `config --org https://dev.azure.com/<org>`")
    pat, _ = load_pat()
    failure = None
    if pat:
        client = Client(org, pat=pat)
        try:
            return client, whoami(client)
        except Fail as e:
            if e.code != "AUTH":
                raise
            failure = e
    tenant = org_tenant(org)
    login = f"az login --tenant {tenant} --scope {ADO_RESOURCE}/.default" if tenant else "az login"
    token = az_token(tenant)
    if token:
        client = Client(org, token=token)
        try:
            return client, whoami(client)
        except Fail as e:
            if e.code != "AUTH":
                raise
            failure = Fail("AUTH", f"{failure or e}; for az, sign in to the organization's tenant: `{login}`")
    if failure:
        raise failure
    if shutil.which("az") or shutil.which("az.cmd"):
        raise Fail("NO_AUTH", f"no PAT, and az has no sign-in for this organization: run `{login}`, "
                              f"or set AZURE_DEVOPS_EXT_PAT / write a PAT to {config_dir() / 'pat'}")
    raise Fail("NO_AUTH", f"no sign-in: set AZURE_DEVOPS_EXT_PAT, write a PAT to {config_dir() / 'pat'}, "
                          "or install the Azure CLI and run `az login`")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("--org", default="")
    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--date", default="today")
    p_collect.add_argument("--days", type=int, default=1, help="window length in days, from --date")
    p_collect.add_argument("--week", help="this, last or YYYY-MM-DD: the Monday-to-Sunday week holding it")
    p_collect.add_argument("--org", default="")
    p_collect.add_argument("--no-github", action="store_true", help="leave out GitHub for this run")
    p_config = sub.add_parser("config")
    p_config.add_argument("--org")
    p_config.add_argument("--sections", help='JSON list: [{"name": "Team A", "match": ["ProjectA"]}, ...]; [] groups by project')
    p_config.add_argument("--timezone")
    p_config.add_argument("--name", help="your name as it goes on the report")
    p_config.add_argument("--role", help="your role or team, shown under your name; empty clears it")
    p_config.add_argument("--pat-stdin", action="store_true", help="read the PAT from stdin and save it")
    p_config.add_argument("--show", action="store_true")
    p_config.add_argument("--github", choices=["on", "off"], help="include GitHub issues and PRs (default on)")
    p_config.add_argument("--github-exclude", help="comma-separated owner/repo list to leave out; empty clears it")
    p_config.add_argument("--github-sections", help='JSON map {"owner/repo": "Section"}')
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        if args.cmd == "config":
            if args.org:
                cfg["org"] = normalize_org(args.org)
            if args.sections:
                try:
                    sections = json.loads(args.sections)
                    assert isinstance(sections, list) and all(s.get("name") for s in sections)
                except (ValueError, AssertionError, AttributeError):
                    raise Fail("BAD_ARGS", "--sections must be a JSON list of {\"name\": ..., \"match\": [...]}")
                cfg["sections"] = sections
            gh_cfg = cfg.setdefault("github", {})
            if args.github:
                gh_cfg["enabled"] = args.github == "on"
            if args.github_exclude is not None:
                gh_cfg["exclude"] = [r.strip() for r in args.github_exclude.split(",") if r.strip()]
            if args.github_sections:
                try:
                    mapping = json.loads(args.github_sections)
                    assert isinstance(mapping, dict) and all(isinstance(v, str) for v in mapping.values())
                except (ValueError, AssertionError):
                    raise Fail("BAD_ARGS", '--github-sections must be a JSON map {"owner/repo": "Section"}')
                gh_cfg["sections"] = mapping
            if not gh_cfg:
                cfg.pop("github")
            if args.name is not None or args.role is not None:
                profile = cfg.setdefault("profile", {})
                for key, value in (("name", args.name), ("role", args.role)):
                    if value is not None:
                        profile[key] = value.strip()
                if not profile.get("name"):
                    raise Fail("BAD_ARGS", "--name is required: the report needs your name")
            if args.timezone:
                cfg["timezone"] = args.timezone
                timezone(cfg)
            if args.pat_stdin:
                pat = sys.stdin.read().strip()
                if not pat:
                    raise Fail("BAD_ARGS", "no PAT on stdin")
                print(f"PAT saved to {save_pat(pat)}")
            save_config(cfg)
            _, source = load_pat()
            shown = dict(cfg, sections=cfg.get("sections") or "one per Azure DevOps project",
                         pat=source or "none (az login is used if signed in)")
            print(json.dumps(shown, indent=2))
            return 0

        client, me = connect(args, cfg)
        if args.cmd == "check":
            print(f"OK {me['name']} <{me['email']}> {client.org} (via {client.via})")
            profile = cfg.get("profile") or {}
            if profile.get("name"):
                print(f"PROFILE {profile['name']}" + (f" | {profile['role']}" if profile.get("role") else ""))
            else:
                print("PROFILE NONE")
            return 0

        tz = timezone(cfg)
        if args.week:
            day, days = parse_week(args.week, tz), 7
        else:
            day, days = parse_day(args.date, tz), args.days
        if not 1 <= days <= 31:
            raise Fail("BAD_ARGS", f"--days must be 1 to 31, got {days}")
        first_run = "sections" not in cfg or cfg["sections"] == LEGACY_SECTIONS
        if first_run:
            cfg["sections"] = []
            save_config(cfg)
        result = collect(client, day, tz, cfg["sections"], me, days)
        result["first_run"] = first_run
        result["profile"] = cfg.get("profile") or None
        result["github"] = None
        gh_cfg = cfg.get("github", {})
        if gh_cfg.get("enabled", True) and not args.no_github:
            start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
            try:
                result["github"] = collect_github(start, start + dt.timedelta(days=days), cfg["sections"], gh_cfg)
            except Fail as e:
                result["warnings"].append(f"GitHub left out: {e}")
            g = result["github"]
            if g:
                used = [s["name"] for s in result["sections"]] + [
                    x["section"] for x in g["items"] + g["merged_prs"] + g["open_prs"] + g["open_assigned"]]
                result["sections"] = section_list(cfg["sections"], used)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    except Fail as e:
        print(f"ERROR {e.code}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
