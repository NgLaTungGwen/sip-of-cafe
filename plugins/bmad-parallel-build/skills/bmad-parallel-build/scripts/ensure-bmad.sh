#!/usr/bin/env bash
# Make sure the project has the BMAD install this skill drives, and install it
# if not.
#
# usage: ensure-bmad.sh <main-root>
#
# Workers run bmad-build-auto through _bmad/scripts/render_skill.py, and
# prepare-worktree.sh copies <main-root>/_bmad and <main-root>/.claude/skills
# into every worktree, so BMAD has to live in the project, not in ~/.claude.
# An existing install is left alone. BMAD_VERSION overrides the pinned release,
# which is the one this skill was verified against.
set -euo pipefail

root="${1:?usage: ensure-bmad.sh <main-root>}"
version="${BMAD_VERSION:-6.12.0}"
skill="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The run dispatches bmad-pb-worker and bmad-pb-synthesis, and the workers
# hand coding to haiku-implementer. They have to be project agents so
# prepare-worktree.sh can copy them into every worktree. A copy the project
# already has is left alone; one that differs from the shipped version is
# reported, so an older copy can be updated by hand.
mkdir -p "$root/.claude/agents"
for agent in bmad-pb-worker bmad-pb-synthesis haiku-implementer; do
  if [[ ! -f "$root/.claude/agents/$agent.md" ]]; then
    cp "$skill/agents/$agent.md" "$root/.claude/agents/"
    echo "agents: installed $agent into $root/.claude/agents"
  elif ! cmp -s "$skill/agents/$agent.md" "$root/.claude/agents/$agent.md"; then
    echo "agents: kept the project's $agent.md, which differs from $skill/agents/$agent.md"
  fi
done

if [[ -f "$root/_bmad/scripts/render_skill.py" && -f "$root/.claude/skills/bmad-build-auto/SKILL.md" ]]; then
  echo "bmad: already installed in $root"
  exit 0
fi

echo "bmad: installing bmad-method@$version into $root"
npx -y "bmad-method@$version" install \
  --directory "$root" --modules bmm --tools claude-code --yes

if [[ ! -f "$root/_bmad/scripts/render_skill.py" || ! -f "$root/.claude/skills/bmad-build-auto/SKILL.md" ]]; then
  echo "bmad: install finished but bmad-build-auto or render_skill.py is missing" >&2
  exit 1
fi
echo "bmad: installed"
