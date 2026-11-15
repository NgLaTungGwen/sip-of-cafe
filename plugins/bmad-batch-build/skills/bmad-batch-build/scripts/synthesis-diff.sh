#!/usr/bin/env bash
# The code synthesis wrote itself, for its review: the conflict resolutions
# inside its merge commits and its own commits, without the units' commits,
# which their workers' reviews already covered.
#
# usage: synthesis-diff.sh <worktree> <since> [<unit branch>...]
#
# <since> is HEAD when this synthesis started, or the head the last review
# covered, to review only the fixes made after it. The unit branches merged
# since then are left out, and so is what a merge of origin/<base> brought
# in: only HEAD's first-parent line counts. A resolution is what `git show --remerge-diff`
# shows: the merge as committed against git's own automatic merge, so a
# clean merge adds nothing. The diff goes to <git dir>/bmad/synthesis.diff,
# out of the working tree, which must stay clean. Prints one line:
#   SYNTH-DIFF none
#   SYNTH-DIFF <c> commits, <m> resolved merges, <n> lines: <out file>
set -uo pipefail

wt=${1:?usage: synthesis-diff.sh <worktree> <since> [<unit branch>...]} since=${2:?since}
shift 2
gd=$(git -C "$wt" rev-parse --absolute-git-dir) || exit 2
mkdir -p "$gd/bmad"
out="$gd/bmad/synthesis.diff"
: > "$out"
c=0 m=0 units=()
[ $# -gt 0 ] && units=(--not "$@")
# Only HEAD's own line (--first-parent): a merge of origin/<base> brings in
# the base's commits and merges, which are neither synthesis's work nor its
# resolutions (db-pool-error-handler counted them with 0 resolved merges).
# The base ref is left out too, in case that merge was a fast-forward; a
# plain local base is not, since synthesis may be working on it.
. "$(dirname "${BASH_SOURCE[0]}")/lib-profile.sh"
base=$(base_ref "$wt") not_base=()
# ^<ref>, ahead of the units' --not, which would flip it back.
case $base in origin/*) not_base=("^$base") ;; esac
for rev in $(git -C "$wt" rev-list --reverse --first-parent --no-merges "$since..HEAD" "${not_base[@]}" "${units[@]}"); do
  git -C "$wt" show --format='=== commit %h %s' "$rev" >> "$out"
  c=$((c + 1))
done
for rev in $(git -C "$wt" rev-list --reverse --first-parent --merges "$since..HEAD" "${not_base[@]}"); do
  d=$(git -C "$wt" show --remerge-diff --format= "$rev")
  [ -n "$d" ] || continue
  { git -C "$wt" log -1 --format='=== resolved merge %h %s' "$rev"; printf '%s\n' "$d"; } >> "$out"
  m=$((m + 1))
done
if [ $((c + m)) -eq 0 ]; then
  echo "SYNTH-DIFF none"
else
  echo "SYNTH-DIFF $c commits, $m resolved merges, $(wc -l < "$out" | tr -d ' ') lines: $out"
fi
