#!/usr/bin/env bash
# usage: self-test.sh [<project root> ...]
#
# Checks the skill itself: every shell script parses, every Python script
# compiles, the unit tests in tests/ pass, and, for each project root given,
# one trivial Vitest test runs through heavy.sh (check-test-env.sh). The
# enhancer runs it before committing a change and ship-enhancement.sh before
# publishing one. 0.9.0 shipped a heavy.sh that stopped every api suite
# because nothing like this ran.
#
# Prints one line per check and "self-test: ok" or "self-test: FAILED", and
# exits 0 or 1.
set -uo pipefail
skill="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py=$(command -v python || command -v python3)
fail=0

for f in "$skill"/scripts/*.sh; do
  bash -n "$f" || { echo "FAIL syntax: $f"; fail=1; }
done
for f in "$skill"/scripts/*.py "$skill"/tests/*.py; do
  "$py" -m py_compile "$f" 2>&1 || { echo "FAIL compile: $f"; fail=1; }
done
find "$skill" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null

out=$(SELF_TEST_BASH="$BASH" "$py" -W ignore "$skill/tests/test_scripts.py" 2>&1)
if [ $? -eq 0 ]; then
  echo "unit tests: $(echo "$out" | grep -m1 '^Ran ')"
else
  echo "$out" | grep -A8 -E '^(FAIL|ERROR):'
  echo "FAIL unit tests"
  fail=1
fi
find "$skill" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null

for root in "$@"; do
  out=$(bash "$skill/scripts/check-test-env.sh" "$root")
  echo "$root: $out"
  case $out in *BROKEN*) fail=1 ;; esac
done

if [ "$fail" -eq 0 ]; then echo "self-test: ok"; else echo "self-test: FAILED"; fi
exit "$fail"
