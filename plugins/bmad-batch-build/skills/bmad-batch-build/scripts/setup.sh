#!/usr/bin/env bash
# Step 0 of every run: resolve what this machine and project need, fix what can
# be fixed without the user, and report the rest. The run starts unless a line
# says BLOCKED.
#
# usage: setup.sh <main-root>
#
# Prints one line per item, then SETUP ok|blocked:
#   PYTHON <command>          the Python 3 to type for <skill>/scripts/*.py
#   UV ok|installed           uv, which workers need; installed when missing
#   BMAD ...                  ensure-bmad.sh: BMAD present or installed
#   AGENTS plugin|project|copied
#                             copied = written to <root>/.claude/agents just now;
#                             this session can't see them until a restart, so
#                             the run uses general-purpose agents instead
#   IGNORE ok|added           .claude/worktrees kept out of git status
#   BASE <branch>             the default branch (when there is no profile)
#   REMOTE ado|github|other|none
#   TICKETS ado|gh|off (<why>)
#                             Azure DevOps tickets with an ADO remote and az;
#                             GitHub issues with a GitHub remote and gh
#   PR az|gh|none (<why>)     the CLI synthesis opens the PR with
# A BLOCKED line says what the user must install; nothing else stops a run.
set -uo pipefail

root="${1:?usage: setup.sh <main-root>}"
root=${root//\\//}
skill="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
blocked=0

# Python: macOS and many Linux machines have only python3.
py=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    py=$c; break
  fi
done
if [ -n "$py" ]; then echo "PYTHON $py"; else echo "PYTHON BLOCKED: install Python 3.9 or later"; blocked=1; fi

# uv: workers render bmad-build-auto with `uv run`, which also fetches the
# Python 3.11 that BMAD's scripts declare. Installed from PyPI when missing,
# else with Astral's installer; then made reachable on PATH without touching
# shell profiles, by copying it into a user folder that is already on PATH.
if command -v uv >/dev/null 2>&1; then
  echo "UV ok"
elif [ -n "$py" ]; then
  "$py" -m pip install --user --quiet --disable-pip-version-check uv >/dev/null 2>&1 || true
  sdir=$("$py" -c 'import sysconfig
try:
    s = sysconfig.get_preferred_scheme("user")
except AttributeError:
    import os; s = os.name + "_user"
print(sysconfig.get_path("scripts", s))' 2>/dev/null | tr -d '\r')
  if [ -n "$sdir" ] && [ ! -e "$sdir/uv" ] && [ ! -e "$sdir/uv.exe" ]; then
    # PyPI refused (an externally managed Python): Astral's own installer.
    case "$(uname -s)" in
      MINGW*|MSYS*|CYGWIN*) powershell.exe -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" >/dev/null 2>&1 ;;
      *) curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 ;;
    esac
    sdir="$HOME/.local/bin"
  fi
  if ! command -v uv >/dev/null 2>&1; then
    sdir=$(cygpath -u "$sdir" 2>/dev/null || printf '%s' "$sdir")
    IFS=: read -ra dirs <<< "$PATH"
    for d in "${dirs[@]}"; do
      case $d in "$HOME"/*) ;; *) continue ;; esac
      if [ -d "$d" ] && [ -w "$d" ]; then
        for f in "$sdir"/uv "$sdir"/uv.exe "$sdir"/uvx "$sdir"/uvx.exe; do [ -e "$f" ] && cp "$f" "$d/"; done
        break
      fi
    done
  fi
  hash -r
  if command -v uv >/dev/null 2>&1; then
    echo "UV installed ($(command -v uv))"
  else
    echo "UV BLOCKED: could not install uv; install it from https://docs.astral.sh/uv/ and run the skill again"
    blocked=1
  fi
fi

if ! git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
  echo "GIT BLOCKED: $root is not a git repository"
  echo "SETUP blocked"
  exit 1
fi

# BMAD and the agents. ensure-bmad.sh installs BMAD with npx, so it needs Node.
agents_before=0
[ -f "$root/.claude/agents/bmad-batch-worker.md" ] && agents_before=1
if [ ! -f "$root/_bmad/scripts/render_skill.py" ] && ! command -v npx >/dev/null 2>&1; then
  echo "BMAD BLOCKED: BMAD is not installed here and installing it needs Node.js (npx); install Node.js 20 or later"
  blocked=1
else
  out=$(bash "$skill/scripts/ensure-bmad.sh" "$root" 2>&1); rc=$?
  # A project copy of an agent that differs from the shipped one is kept; say so.
  printf '%s\n' "$out" | sed -n -e 's/^bmad: /BMAD /p' -e '/^agents: kept/s/^agents: /AGENTS note: /p'
  if [ $rc -ne 0 ]; then
    echo "BMAD BLOCKED: the install failed; its last lines:"
    printf '%s\n' "$out" | tail -5 | sed 's/^/  /'
    blocked=1
  fi
fi
if [ -f "$skill/../../agents/bmad-batch-worker.md" ] && [ -f "$skill/../../.claude-plugin/plugin.json" ]; then
  echo "AGENTS plugin"
elif [ "$agents_before" = 1 ]; then
  echo "AGENTS project"
else
  echo "AGENTS copied (loaded at the next session start; this run uses general-purpose agents)"
fi

# Worktrees live under .claude/worktrees; keep them out of git status even
# when the project's .gitignore doesn't mention them (local only, not committed).
if git -C "$root" check-ignore -q .claude/worktrees/x 2>/dev/null; then
  echo "IGNORE ok"
else
  ex="$(git -C "$root" rev-parse --git-path info/exclude)"
  case $ex in /*|?:*) ;; *) ex="$root/$ex" ;; esac
  mkdir -p "$(dirname "$ex")" && printf '\n# bmad-batch-build worktrees\n.claude/worktrees/\n' >> "$ex"
  echo "IGNORE added .claude/worktrees/ to .git/info/exclude"
fi

# The default branch, for projects without a profile.
base=$(git -C "$root" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
if [ -z "$base" ]; then
  for b in main master; do
    if git -C "$root" rev-parse --verify --quiet "refs/remotes/origin/$b" >/dev/null || \
       git -C "$root" rev-parse --verify --quiet "refs/heads/$b" >/dev/null; then base=$b; break; fi
  done
fi
# Neither: the branch the main checkout is on (a repo whose trunk has another name).
[ -n "$base" ] || base=$(git -C "$root" symbolic-ref --quiet --short HEAD 2>/dev/null)
echo "BASE ${base:-main}"

# Where tickets and the PR go.
url=$(git -C "$root" remote get-url origin 2>/dev/null || true)
case $url in
  *dev.azure.com*|*visualstudio.com*) remote=ado ;;
  *github.com*) remote=github ;;
  "") remote=none ;;
  *) remote=other ;;
esac
echo "REMOTE $remote"
az_ok=0
if command -v az >/dev/null 2>&1 || command -v az.cmd >/dev/null 2>&1; then
  az account show --only-show-errors >/dev/null 2>&1 && az_ok=1
fi
if [ "$remote" = ado ] && [ "$az_ok" = 1 ]; then
  echo "TICKETS ado"
  echo "PR az"
elif [ "$remote" = ado ]; then
  echo "TICKETS off (Azure DevOps remote, but the az CLI is missing or not signed in: az login)"
  echo "PR none (push the branch and open the PR in the browser)"
elif [ "$remote" = github ] && command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  echo "TICKETS gh"
  echo "PR gh"
elif [ "$remote" = github ]; then
  echo "TICKETS off (GitHub remote, but the gh CLI is missing or not signed in: gh auth login)"
  echo "PR none (push the branch and open the PR in the browser)"
else
  echo "TICKETS off ($remote remote)"
  if [ "$remote" = none ]; then
    echo "PR none (no origin remote: the run ends with a local branch)"
  else
    echo "PR none (push the branch and open the PR in the browser)"
  fi
fi

if [ "$blocked" = 1 ]; then echo "SETUP blocked"; exit 1; fi
echo "SETUP ok"
