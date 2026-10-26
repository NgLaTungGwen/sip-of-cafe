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

# The run dispatches bmad-batch-worker and bmad-batch-synthesis, and the workers
# hand coding to haiku-implementer; bmad-batch-enhancer drafts skill fixes after
# the run. A plugin install ships them itself (the plugin's agents/ folder,
# named bmad-batch-build:<agent>), so nothing is copied. A copied install needs
# them as project agents; a copy the project already has is left alone, and one
# that differs from the shipped version is reported. Claude Code only sees a
# newly created .claude/agents folder after a restart, and only in the session's
# own folder or its parents, which SKILL.md checks before the run starts.
if [[ -f "$skill/../../agents/bmad-batch-worker.md" && -f "$skill/../../.claude-plugin/plugin.json" ]]; then
  echo "agents: provided by the plugin (bmad-batch-build:<agent>)"
  agents=()
else
  [[ -d "$root/.claude/agents" ]] || echo "agents: created $root/.claude/agents; a session already running must restart to see it"
  mkdir -p "$root/.claude/agents"
  agents=(bmad-batch-worker bmad-batch-synthesis haiku-implementer bmad-batch-enhancer)
fi
for agent in "${agents[@]}"; do
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
