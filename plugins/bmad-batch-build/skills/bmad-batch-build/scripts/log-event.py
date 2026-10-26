"""Appends one line per command that heavy.sh or bg.sh ran to
~/.claude/bmad-batch-runs/events.jsonl (BMAD_RUNS_DIR overrides the folder).

usage: log-event.py <tool> <cwd> <exit code> <seconds> <command> [<output file>]

A command that fails inside bg.sh or heavy.sh can look like a success to the
agent (a background run whose log nobody read, a workaround that hid it), so
the run journal (journal.py) reads these lines too. The error line is the last
line of the output that looks like an error. Never fails the caller.
"""
import datetime as dt
import json
import os
import re
import sys

ERR = re.compile(r"(\w*Error\b|\bERR!|\bFAIL\b|\bfailed\b|error TS\d+|No such file|not found|denied)", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def error_line(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            lines = [ANSI.sub("", s).strip() for s in fh.readlines()[-400:]]
    except OSError:
        return None
    hits = [s for s in lines if s and ERR.search(s)]
    last = hits[-1] if hits else next((s for s in reversed(lines) if s), None)
    return last[:300] if last else None


def main(args):
    tool, cwd, rc, secs, cmd = args[:5]
    out = args[5] if len(args) > 5 else None
    now = dt.datetime.now(dt.timezone.utc)
    event = {
        "end": now.isoformat(timespec="seconds"),
        "start": (now - dt.timedelta(seconds=int(secs or 0))).isoformat(timespec="seconds"),
        "tool": tool, "cwd": cwd, "rc": int(rc), "sec": int(secs or 0), "cmd": cmd[:400],
        "error": error_line(out) if out and int(rc) != 0 else None,
    }
    store = os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, "events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:  # logging must never break a build
        pass
