"""PreToolUse hook: send heavy commands in batch-build worktrees through heavy.sh.

Agents are told to run full suites, typechecks, installs and builds through
heavy.sh, the machine-wide queue. This hook makes that hold even when an agent
forgets: it rewrites a Bash command of the exact shape

    [cd <worktree under .claude/worktrees/> &&] [ENV=val ...] <heavy command> [2>&1] [| tail -n N]

into `bash <skill>/scripts/heavy.sh '<the same command>'`, and approves only
that rewritten call. Anything else, including a heavy command chained with
other commands, a command outside the worktrees, or one already queued, is
left untouched and goes through the normal permission flow. Targeted test runs
(vitest or bun test with file arguments) stay direct: they are short.

Prints nothing when it leaves a command alone; never fails the tool call.
"""
import json
import os
import re
import sys

HEAVY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heavy.sh").replace("\\", "/")

SAFE_ARGS = r"(?:\s+[\w@./:=,+-]+)*"
CMD = re.compile(
    r"""^(?P<env>(?:[A-Z_][A-Z0-9_]*=\S+\s+)*)
    (?P<tool>
        npm\s+(?:ci|test|install)
      | npm\s+(?:--workspace(?:=|\s+)\S+\s+)?run\s+(?:build|typecheck|test)(?::[\w-]+)?
      | npx\s+tsc
      | npx\s+vitest(?:\s+run)?
      | bun\s+(?:install|test)
      | bun\s+run\s+(?:typecheck|build|test)
    )(?P<args>""" + SAFE_ARGS + r""")
    (?P<tail>\s+2>&1)?(?:\s*\|\s*tail\s+-n?\s*-?\d+)?\s*$""",
    re.X,
)
CD = re.compile(r"""^\s*cd\s+(?P<q>["']?)(?P<dir>[^"'&;|]*?/\.claude/worktrees/[^"'&;|]+?)(?P=q)\s*&&\s*(?P<rest>.+)$""")


def targeted(tool, args):
    """A vitest or bun test run with file or directory arguments is short."""
    if not re.search(r"vitest|bun\s+test", tool):
        return False
    return any(not a.startswith("-") for a in args.split())


def rewrite(command):
    if "heavy.sh" in command or "\n" in command:
        return None
    m = CD.match(command.replace("\\", "/"))
    if not m or ".." in m.group("dir"):
        return None
    c = CMD.match(m.group("rest").strip())
    if not c or targeted(c.group("tool"), c.group("args")):
        return None
    quoted = command.replace("'", "'\\''")
    return f"bash \"{HEAVY}\" '{quoted}'"


def main():
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return
        tool_input = event.get("tool_input") or {}
        new = rewrite(tool_input.get("command", ""))
        if not new:
            return
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "heavy command in a batch-build worktree, queued through heavy.sh",
            "updatedInput": {**tool_input, "command": new},
        }}))
    except Exception:  # a hook bug must never block the agent's command
        return


if __name__ == "__main__":
    main()
