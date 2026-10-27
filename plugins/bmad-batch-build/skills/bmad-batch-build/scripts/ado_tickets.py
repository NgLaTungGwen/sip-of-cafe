"""Create the run's Azure DevOps tickets: User Stories and one Task per unit.

usage: python ado_tickets.py <plan.json>

plan.json (written by the orchestrator, updated in place with the IDs):

    {
      "stories": [
        {"key": "s1", "id": null, "title": "...", "description": "<html>"}
      ],
      "tasks": [
        {"unit": 1, "story": "s1", "id": null, "title": "...", "description": "<html>"}
      ]
    }

An entry whose "id" is already set is an existing work item. It is reused and
never edited or re-parented. Only entries with "id": null are created. Each new
Task becomes a child of the story its "story" key names. Everything created is
assigned to the signed-in az account and tagged bmad-parallel-build (the skill's
former name, kept so board queries on the tag keep working). The
organization and project come from the origin remote (or, when it is not
Azure DevOps, from the `az devops configure` defaults).

Tasks are created in parallel because each az call takes 30-60 s here. The plan
file is rewritten even when something fails, so the IDs that were created are
kept, and a re-run creates only what is still missing.

usage: python ado_tickets.py <plan.json> --link-pr <pr id> --merged <unit,unit,...>

Links the PR to the stories of the merged units and to their tasks, never to a
left-out unit's task. When every task in the plan was merged it also turns on
close-on-complete. Prints the IDs ADO then reports as linked, and whether
close-on-complete is on, in one call instead of three slow ones.
"""
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

AZ = shutil.which("az") or shutil.which("az.cmd") or "az"
TAG = "bmad-parallel-build"  # former skill name, kept for board queries


def ado_remote():
    """(organization URL, project) from the origin remote of the current
    directory, so a machine without `az devops configure` defaults still
    works; (None, None) when origin is not Azure DevOps."""
    url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    for pattern, org in ((r"https://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/", "https://dev.azure.com/{}"),
                         (r"(?:git@)?ssh\.dev\.azure\.com:v3/([^/]+)/([^/]+)/", "https://dev.azure.com/{}"),
                         (r"https://(?:[^@/]+@)?([^./]+)\.visualstudio\.com/(?:DefaultCollection/)?([^/]+)/_git/",
                          "https://{}.visualstudio.com")):
        m = re.match(pattern, url)
        if m:
            return org.format(m.group(1)), urllib.parse.unquote(m.group(2))
    return None, None


ORG, PROJECT = ado_remote()


def az(*args: str) -> str:
    extra = []
    if ORG and args[:1] in (("boards",), ("repos",)):
        extra = ["--org", ORG]
        if args[:3] == ("boards", "work-item", "create"):
            extra += ["--project", PROJECT]
    result = subprocess.run([AZ, *args, *extra, "--only-show-errors"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def create(kind: str, title: str, description: str, assignee: str) -> int:
    out = az(
        "boards", "work-item", "create",
        "--type", kind,
        "--title", title,
        "--description", description,
        "--assigned-to", assignee,
        "--fields", f"System.Tags={TAG}",
        "--query", "id", "-o", "tsv",
    )
    return int(out)


def run_all(jobs, errors: list) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(job) for job in jobs]:
            try:
                future.result()
            except Exception as exc:  # report every failure, keep the IDs that worked
                errors.append(str(exc))


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    errors: list = []
    assignee = az("account", "show", "--query", "user.name", "-o", "tsv")

    def make_story(story: dict):
        def job() -> None:
            if story.get("id") is not None:
                print(f"reusing User Story {story['id']}")
                return
            story["id"] = create("User Story", story["title"], story["description"], assignee)
            print(f"created User Story {story['id']}: {story['title']}")
        return job

    run_all([make_story(s) for s in plan["stories"]], errors)
    story_ids = {s["key"]: s.get("id") for s in plan["stories"]}

    def make_task(task: dict):
        def job() -> None:
            if task.get("id") is not None:
                print(f"reusing work item {task['id']} for unit {task['unit']}")
                return
            parent = story_ids.get(task["story"])
            if parent is None:
                raise RuntimeError(f"unit {task['unit']}: story {task['story']!r} has no id")
            task["id"] = create("Task", task["title"], task["description"], assignee)
            az("boards", "work-item", "relation", "add",
               "--id", str(task["id"]), "--relation-type", "parent",
               "--target-id", str(parent), "-o", "none")
            print(f"created Task {task['id']} under {parent} for unit {task['unit']}: {task['title']}")
        return job

    run_all([make_task(t) for t in plan["tasks"]], errors)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)

    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)


def link(path: str, pr: str, merged: set) -> None:
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    tasks = [t for t in plan["tasks"] if t.get("id") is not None and str(t["unit"]) in merged]
    keys = {t["story"] for t in tasks}
    stories = [s["id"] for s in plan["stories"] if s["key"] in keys and s.get("id") is not None]
    ids = [str(i) for i in stories + [t["id"] for t in tasks]]
    if not ids:
        sys.exit("no merged unit has a ticket id")
    az("repos", "pr", "work-item", "add", "--id", pr, "--work-items", *ids, "-o", "none")
    close = len(tasks) == len(plan["tasks"])
    if close:
        az("repos", "pr", "update", "--id", pr, "--transition-work-items", "true", "-o", "none")
    linked = az("repos", "pr", "work-item", "list", "--id", pr, "--query", "[].id", "-o", "tsv").split()
    print(f"linked: {' '.join('#' + i for i in linked)}")
    print(f"close-on-complete: {'on' if close else 'off (a unit was left out)'}")
    missing = set(ids) - set(linked)
    if missing:
        sys.exit(f"not linked: {' '.join(sorted(missing))}")


if __name__ == "__main__":
    if "--link-pr" in sys.argv:
        args = sys.argv[1:]
        merged = args[args.index("--merged") + 1].split(",") if "--merged" in args else []
        link(args[0], args[args.index("--link-pr") + 1], set(merged))
    else:
        main(sys.argv[1])
