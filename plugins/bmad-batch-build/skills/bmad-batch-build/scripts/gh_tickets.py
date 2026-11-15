"""Create the run's GitHub issues: a story issue and one sub-issue per unit.

usage: python gh_tickets.py <plan.json>

plan.json is the same file ado_tickets.py takes (written by the orchestrator,
updated in place with the issue numbers), with Markdown descriptions:

    {
      "stories": [
        {"key": "s1", "id": null, "title": "...", "description": "<markdown>"}
      ],
      "tasks": [
        {"unit": 1, "story": "s1", "id": null, "title": "...", "description": "<markdown>"}
      ]
    }

An entry whose "id" is already set is an existing issue number. It is reused
and never edited or re-parented. Only entries with "id": null are created. Each
new task issue becomes a sub-issue of the story its "story" key names, and its
body ends with "Part of #<story>". Everything created is assigned to the
signed-in gh account and labelled bmad-parallel-build (the skill's former
name, the same tag the ADO tickets carry); the label is created when the
repository has none. The repository comes from the origin remote.

Issues are created one at a time: a gh call takes about a second, and bursts
of creates trip GitHub's secondary rate limit. The plan file is rewritten even
when something fails, so the numbers that were created are kept, and a re-run
creates only what is still missing.

usage: python gh_tickets.py <plan.json> --link-pr <pr number or url> --merged <unit,unit,...>

Writes the PR description's "Work items:" line with the stories of the merged
units and their tasks, never a left-out unit's task. When every task in the
plan was merged each one gets "Closes #n", so GitHub closes them when the PR
merges (only into the default branch); otherwise "Refs #n", which links
without closing. Prints the linked numbers and whether close-on-complete is on.
A story issue that still has open sub-issues outside this PR (a reused story
with other work under it) gets "Refs #story" even then, because "Closes" would
close it with that work open; so does one whose sub-issues could not be read.
The script prints which.

usage: python gh_tickets.py <plan.json> --link-pr <pr> --merged <unit> --unit-pr

One PR per unit (bmad-batch-build-split): "Closes #task" for the unit's task
and "Refs #story" for its story, so each PR closes only its own task.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

GH = shutil.which("gh") or "gh"
TAG = "bmad-parallel-build"  # former skill name, kept for board queries


def gh_remote():
    """owner/name from the origin remote of the current directory, so gh never
    guesses between remotes; None when origin is not GitHub."""
    url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    m = re.match(r"(?:https://(?:[^@/]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


REPO = gh_remote()


def gh(*args: str, stdin: str = None) -> str:
    result = subprocess.run([GH, *args], input=stdin, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def create(title: str, body: str, login: str):
    """(number, database id) of a new issue; sub-issues are added by id."""
    data = json.loads(gh("api", "-X", "POST", f"repos/{REPO}/issues", "--input", "-",
                         stdin=json.dumps({"title": title, "body": body, "assignees": [login], "labels": [TAG]})))
    return data["number"], data["id"]


def main(path: str) -> None:
    if REPO is None:
        sys.exit("origin is not a GitHub repository")
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    errors: list = []
    login = gh("api", "user", "--jq", ".login")
    gh("label", "create", TAG, "--repo", REPO, "--force", "--color", "0e8a16",
       "--description", "Created by bmad-batch-build")

    for story in plan["stories"]:
        if story.get("id") is not None:
            print(f"reusing issue #{story['id']}")
            continue
        try:
            story["id"], _ = create(story["title"], story["description"], login)
            print(f"created story #{story['id']}: {story['title']}")
        except RuntimeError as exc:  # report every failure, keep the numbers that worked
            errors.append(str(exc))
    story_ids = {s["key"]: s.get("id") for s in plan["stories"]}

    for task in plan["tasks"]:
        if task.get("id") is not None:
            print(f"reusing issue #{task['id']} for unit {task['unit']}")
            continue
        parent = story_ids.get(task["story"])
        if parent is None:
            errors.append(f"unit {task['unit']}: story {task['story']!r} has no id")
            continue
        try:
            task["id"], db_id = create(task["title"], f"{task['description']}\n\nPart of #{parent}", login)
            gh("api", "-X", "POST", f"repos/{REPO}/issues/{parent}/sub_issues", "--input", "-",
               stdin=json.dumps({"sub_issue_id": db_id}))
            print(f"created task #{task['id']} under #{parent} for unit {task['unit']}: {task['title']}")
        except RuntimeError as exc:
            errors.append(f"unit {task['unit']}: {exc}")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)

    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)


WORK_ITEMS = re.compile(r"^Work items:[^\r\n]*", re.M)  # a CRLF body keeps its \r


def with_work_items(body: str, line: str) -> str:
    """The PR body with its Work items: line replaced by `line`, or `line`
    added above the Claude Code footer (or at the end) when it has none."""
    if WORK_ITEMS.search(body):
        return WORK_ITEMS.sub(lambda _: line, body, count=1)
    footer = body.find("🤖 Generated with")
    if footer == -1:
        return body.rstrip() + "\n\n" + line + "\n"
    return body[:footer] + line + "\n\n" + body[footer:]


def open_sub_issues(story) -> set:
    """Numbers of the story issue's sub-issues that are still open."""
    out = gh("api", "--paginate", f"repos/{REPO}/issues/{int(story)}/sub_issues",
             "--jq", '.[] | select(.state == "open") | .number')
    return {int(n) for n in out.split()}


def work_items_line(plan: dict, merged: set, unit_pr: bool = False, children=None, skipped=None):
    """(the Work items: line, the ids that should close or link, close-on-complete).
    One PR for the run closes everything only when no unit was left out; a
    unit's own PR (unit_pr) closes its task and only refers to its story.
    With close-on-complete on and `children` (story number -> open sub-issue
    numbers) given, a story with open sub-issues outside this PR gets "Refs",
    since "Closes" would close it with work still open; so does a story whose
    sub-issues could not be read. It goes to `skipped` as (number, others)."""
    tasks = [t for t in plan["tasks"] if t.get("id") is not None and str(t["unit"]) in merged]
    keys = {t["story"] for t in tasks}
    stories = [s["id"] for s in plan["stories"] if s["key"] in keys and s.get("id") is not None]
    # one keyword per issue: "Closes #1, #2" closes only #1
    if unit_pr:
        parts = [f"Closes #{t['id']}" for t in tasks] + [f"Refs #{i}" for i in stories]
        return "Work items: " + ", ".join(parts), [str(t["id"]) for t in tasks], bool(tasks)
    ids = [str(i) for i in stories + [t["id"] for t in tasks]]
    close = len(tasks) == len(plan["tasks"])
    refs = set() if close else set(ids)
    if close and children is not None:
        task_ids = {int(t["id"]) for t in tasks}
        for sid in stories:
            try:
                others = sorted(set(children(sid)) - task_ids)
            except Exception as exc:  # unknown sub-issues: never risk closing the story
                others = [f"unknown ({exc})"]
            if others:
                refs.add(str(sid))
                if skipped is not None:
                    skipped.append((sid, others))
    parts = [f"{'Refs' if i in refs else 'Closes'} #{i}" for i in ids]
    return "Work items: " + ", ".join(parts), ids, close


def link(path: str, pr: str, merged: set, unit_pr: bool = False) -> None:
    if REPO is None:
        sys.exit("origin is not a GitHub repository")
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    skipped: list = []
    line, ids, close = work_items_line(plan, merged, unit_pr, open_sub_issues, skipped)
    for sid, others in skipped:
        names = " ".join(o if isinstance(o, str) else f"#{o}" for o in others)
        print(f"story #{sid} not closed: open sub-issues outside this PR ({names}), "
              "so it gets Refs instead of Closes")
    if not ids:
        sys.exit("no merged unit has a ticket id")
    info = json.loads(gh("pr", "view", pr, "--repo", REPO, "--json", "body,baseRefName"))
    body = with_work_items(info["body"], line)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8", newline="") as f:
        f.write(body)
    try:
        gh("pr", "edit", pr, "--repo", REPO, "--body-file", f.name)
    finally:
        os.unlink(f.name)
    print(f"linked: {' '.join('#' + i for i in ids)}")
    if not close:
        print("close-on-complete: off (a unit was left out)")
        return
    default = gh("repo", "view", REPO, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name")
    if info["baseRefName"] != default:
        print(f"close-on-complete: off (the PR targets {info['baseRefName']}; GitHub closes issues "
              f"only on merges into {default})")
        return
    print("close-on-complete: on")
    closing = gh("pr", "view", pr, "--repo", REPO, "--json", "closingIssuesReferences",
                 "--jq", ".closingIssuesReferences[].number").split()
    missing = set(ids) - {str(s) for s, _ in skipped} - set(closing)
    if missing:
        sys.exit(f"not linked: {' '.join(sorted(missing))}")


if __name__ == "__main__":
    # A Windows console is cp1252, which has no '≈' or '≤' (UnicodeEncodeError).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if "--link-pr" in sys.argv:
        args = sys.argv[1:]
        merged = args[args.index("--merged") + 1].split(",") if "--merged" in args else []
        link(args[0], args[args.index("--link-pr") + 1], set(merged), "--unit-pr" in args)
    else:
        main(sys.argv[1])
