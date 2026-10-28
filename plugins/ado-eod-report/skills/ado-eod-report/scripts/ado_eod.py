"""Collect one person's Azure DevOps work for a day, for the EOD report.

usage:
    python ado_eod.py check
    python ado_eod.py collect [--date today|yesterday|YYYY-MM-DD] [--org URL]
    python ado_eod.py config [--org URL] [--sections JSON] [--timezone IANA] [--pat-stdin] [--show]

check    signs in with the PAT and prints `OK <name> <org>`.
collect  prints one JSON document: every work item the PAT's owner changed that
         day (or that is assigned to them and changed state that day), what
         changed and when, its parent, linked PRs and their status, the report
         section it belongs to, and the open items still assigned to them.
config   saves settings to <config dir>/config.json (the PAT to <config dir>/pat).

The PAT comes from AZURE_DEVOPS_EXT_PAT, then ADO_PAT, then <config dir>/pat.
The config dir is ADO_EOD_HOME, or ~/.config/ado-eod-report. The organization
comes from --org, then the config, then the origin remote of the current
directory. Scopes: Work Items (Read & write); Code (Read) adds PR status.

Every failure prints one line `ERROR <CODE>: <message>` and exits 2. Codes:
NO_PAT, NO_ORG, AUTH, FORBIDDEN, NOT_FOUND, NETWORK, BAD_ARGS.
"""
import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "7.1"
DEFAULT_SECTIONS = [
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
    def __init__(self, org: str, pat: str):
        self.org = org
        self.auth = "Basic " + base64.b64encode(f":{pat}".encode()).decode()

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
                        raise Fail("AUTH", "Azure DevOps did not accept the PAT (expired, revoked or wrong organization)")
                    return json.loads(text)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                if e.code == 401:
                    raise Fail("AUTH", "Azure DevOps rejected the PAT (401): expired, revoked or for another organization")
                if e.code == 403:
                    raise Fail("FORBIDDEN", f"the PAT lacks a scope for {url.split('?')[0]}")
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
    user = client.call("GET", "_apis/connectionData")["authenticatedUser"]
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
        event = {"time": when.astimezone(tz).strftime("%H:%M"), "by_me": by.get("id") == me["id"],
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
    hay = [fields.get("System.TeamProject", ""), fields.get("System.AreaPath", ""),
           fields.get("System.IterationPath", "")] + split_tags(fields.get("System.Tags"))
    for s in sections:
        for m in s.get("match", []):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(m) + r"(?![A-Za-z0-9])"
            if any(re.search(pattern, h or "", re.I) for h in hay):
                return s["name"]
    fallback = [s["name"] for s in sections if not s.get("match")]
    return fallback[-1] if fallback else "Others"


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


def web_url(org: str, fields: dict, wid: int) -> str:
    return f"{org}/{urllib.parse.quote(fields.get('System.TeamProject', ''))}/_workitems/edit/{wid}"


def collect(client: Client, day: dt.date, tz, sections: list) -> dict:
    me = whoami(client)
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end = start + dt.timedelta(days=1)
    warnings = []

    ids = touched_ids(client, day)
    items = fetch_items(client, ids, relations=True) if ids else {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        updates = dict(zip(items, pool.map(lambda wid: fetch_updates(client, wid), items)))

    worked = {}
    for wid, item in items.items():
        summary = summarize(updates[wid], me, start, end, tz)
        if summary["events"] and relevant(item["fields"], summary, me):
            worked[wid] = (item, summary)

    parents = {parent_id(item) for item, _ in worked.values()} - {None}
    parent_items = fetch_items(client, sorted(parents), relations=False) if parents else {}
    pr_jobs = [(wid, ref) for wid, (item, _) in worked.items() for ref in pr_refs(item)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        prs = dict(zip(pr_jobs, pool.map(lambda job: fetch_pr(client, *job[1]), pr_jobs)))
    if any(p.get("error") == "FORBIDDEN" for p in prs.values()):
        warnings.append("PR status unknown: the PAT has no Code (Read) scope")

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
            "parent": {"id": pid, "type": parent.get("System.WorkItemType"), "title": parent.get("System.Title")}
            if pid else None,
            "prs": [prs[(wid, ref)] for ref in pr_refs(item)],
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

    return {"date": day.isoformat(), "timezone": str(tz), "org": client.org, "me": me,
            "sections": [s["name"] for s in sections], "items": out_items,
            "open_assigned": open_out, "warnings": warnings}


# --- commands -----------------------------------------------------------------

def client_from(args, cfg) -> Client:
    pat, _ = load_pat()
    if not pat:
        raise Fail("NO_PAT", f"no PAT: set AZURE_DEVOPS_EXT_PAT or write it to {config_dir() / 'pat'}")
    org = resolve_org(getattr(args, "org", ""), cfg)
    if not org:
        raise Fail("NO_ORG", "no organization: save one with `config --org https://dev.azure.com/<org>`")
    return Client(org, pat)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("--org", default="")
    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--date", default="today")
    p_collect.add_argument("--org", default="")
    p_config = sub.add_parser("config")
    p_config.add_argument("--org")
    p_config.add_argument("--sections", help='JSON list: [{"name": "PXP", "match": ["PXP"]}, ...]')
    p_config.add_argument("--timezone")
    p_config.add_argument("--pat-stdin", action="store_true", help="read the PAT from stdin and save it")
    p_config.add_argument("--show", action="store_true")
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
            shown = dict(cfg, sections=cfg.get("sections", DEFAULT_SECTIONS), pat=source or "none")
            print(json.dumps(shown, indent=2))
            return 0

        client = client_from(args, cfg)
        if args.cmd == "check":
            me = whoami(client)
            print(f"OK {me['name']} <{me['email']}> {client.org}")
            return 0

        tz = timezone(cfg)
        day = parse_day(args.date, tz)
        first_run = "sections" not in cfg
        if first_run:
            cfg["sections"] = DEFAULT_SECTIONS
            save_config(cfg)
        result = collect(client, day, tz, cfg["sections"])
        result["first_run"] = first_run
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    except Fail as e:
        print(f"ERROR {e.code}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
