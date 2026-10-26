#!/usr/bin/env bash
# usage: check-test-env.sh <root>
#
# Runs one trivial Vitest test through heavy.sh, with the same limits the
# workers get, and prints "vitest ok <version>", "vitest BROKEN: <error>" or
# "vitest none". A setting heavy.sh adds once stopped every api suite before
# any test ran ("minThreads and maxThreads must not conflict"); the run only
# found out after a worker and synthesis had hit it. This finds it in seconds,
# before anything is launched. The project's own config and setup are left out
# on purpose: they need the database, and that is checked separately.
set -u
root=${1:?usage: check-test-env.sh <root>}
vt="$root/node_modules/vitest/vitest.mjs"
# A root that isn't there is a caller's mistake, not a project without Vitest:
# reporting "none" for it once let a self-test pass without testing anything.
[ -d "$root" ] || { echo "vitest BROKEN: no such project root: $root"; exit 1; }
# Not at the root: the first package folder of the project profile that has it.
if [ ! -f "$vt" ]; then
  . "$(dirname "${BASH_SOURCE[0]}")/lib-profile.sh"
  while IFS=$'\t' read -r d _; do
    [ -n "$d" ] && [ -f "$root/$d/node_modules/vitest/vitest.mjs" ] && { vt="$root/$d/node_modules/vitest/vitest.mjs"; break; }
  done <<< "$(profile_get "$root" install)"
fi
[ -f "$vt" ] || { echo "vitest none"; exit 0; }
ver=$(node -p "require(process.argv[1]).version" "$(dirname "$vt")/package.json" 2>/dev/null)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo 'import { test, expect } from "vitest"; test("env", () => expect(1).toBe(1));' > "$tmp/env.test.mjs"
echo 'export default {}' > "$tmp/vitest.config.mjs"

scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out=$(cd "$tmp" && bash "$scripts/heavy.sh" "node \"$vt\" run --root \"$tmp\"" 2>&1)
if echo "$out" | grep -q "1 passed"; then
  echo "vitest ok $ver"
else
  err=$(echo "$out" | sed 's/\x1b\[[0-9;]*m//g' | grep -m1 -E "[A-Za-z]*Error: " || echo "$out" | tail -1)
  echo "vitest BROKEN: $err"
  exit 1
fi
