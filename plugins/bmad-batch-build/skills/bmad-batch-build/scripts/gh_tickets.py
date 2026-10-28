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


def link(path: str, pr: str, merged: set) -> None:
    if REPO is None:
        sys.exit("origin is not a GitHub repository")
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    tasks = [t for t in plan["tasks"] if t.get("id") is not None and str(t["unit"]) in merged]
    keys = {t["story"] for t in tasks}
    stories = [s["id"] for s in plan["stories"] if s["key"] in keys and s.get("id") is not None]
    ids = [str(i) for i in stories + [t["id"] for t in tasks]]
    if not ids:
        sys.exit("no merged unit has a ticket id")
    close = len(tasks) == len(plan["tasks"])
    info = json.loads(gh("pr", "view", pr, "--repo", REPO, "--json", "body,baseRefName"))
    keyword = "Closes" if close else "Refs"  # one keyword per issue: "Closes #1, #2" closes only #1
    body = with_work_items(info["body"], "Work items: " + ", ".join(f"{keyword} #{i}" for i in ids))
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
    missing = set(ids) - set(closing)
    if missing:
        sys.exit(f"not linked: {' '.join(sorted(missing))}")


if __name__ == "__main__":
    if "--link-pr" in sys.argv:
        args = sys.argv[1:]
        merged = args[args.index("--merged") + 1].split(",") if "--merged" in args else []
        link(args[0], args[args.index("--link-pr") + 1], set(merged))
    else:
        main(sys.argv[1])
