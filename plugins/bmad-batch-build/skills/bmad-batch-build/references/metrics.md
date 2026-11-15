# Metrics format (schema 1)

`scripts/metrics.py` writes this format. A dashboard reads it, never
`runs.jsonl` or `events.jsonl` directly: those are the skill's internal files
and change with it. A field change that would break a reader raises `SCHEMA`.

## Where it is

- **One machine's bundle:** `metrics.py export` writes
  `metrics-<id>-<date>.json`, an object with `schema`, `installation_id`,
  `exported`, `includes_text`, `runs` and `events`.
- **Every machine:** `metrics.py import <bundle>...` merges bundles, plus this
  machine's own runs, into `~/.claude/bmad-batch-runs/metrics/runs.jsonl` and
  `events.jsonl`, one JSON object per line. Run it again to refresh this
  machine's rows. Rows are unique per `(installation_id, session, slug)` for
  runs (one session can hold several runs, each its own row) and
  `(installation_id, end, tool, sec)` for events. A bundle replaces every row
  of the sessions it carries, so an older row that merged a session's runs
  into one goes; a row without a slug is kept as it is.

## Refresh before reading

Nothing updates the dataset on its own. Before reading it, run, in order:

```bash
python "<skill>/scripts/cost-report.py" --collect
python "<skill>/scripts/metrics.py" import
```

`--collect` records runs that ended since the last `--record`. It reads
every session log on the machine and can take several minutes, so run it in
the background (`run_in_background`) and wait for it to finish. `import`
with no bundles refreshes only this machine's rows. Heavy-command history
survives cost-report's 45-day prune of `events.jsonl` only if `import` runs
at least that often.

## runs: one row per batch-build run

| Field | Meaning |
|---|---|
| `installation_id` | Random id of the machine's skill store; not the host name |
| `session` | Claude Code session id of the run |
| `start` | Start of the run, ISO 8601 UTC |
| `project` | Project name on the maintainer's own rows; `p-<hash>` from other machines |
| `slug` | Run name on the maintainer's own rows; `r-<hash>` from other machines. Part of the key |
| `skill_version`, `version_exact` | Version that ran; `version_exact` false means guessed from dates (`before-X` = older than X) |
| `mode` | `lane`, `parallel`, `waves`, or null for runs before modes were recorded |
| `usd`, `usd_by_role` | Cost in USD, total and per role (`main`, `worker`, `haiku`, `implementer`, `reviewer`, `synthesis`, `explore`, `other`); `implementer` is a worker's general-purpose coder, counted under `reviewer` before 0.12.13 |
| `usd_idle_max` | Upper bound on the cost of cache re-writes after the cache expired in an idle gap, summed over all agents |
| `mtok_in`, `ktok_out`, `mtok_w1h_subagents` | Input Mtok, output ktok, subagent 1-hour cache writes in Mtok |
| `min_wall` | Wall time of the run, minutes: from its own start (its skill invocation, or its run file's creation) to its own last synthesis result |
| `min_user_wait` | Minutes spent waiting for the user: `min_ask` + `min_typed` (each rounded on its own) |
| `min_ask` | Minutes waiting on `AskUserQuestion` answers and on permission prompts the user answered in the main session |
| `min_typed` | Minutes before the user's other typed messages (an approval asked in plain text, a nudge) |
| `questions` | Questions asked with `AskUserQuestion` (each question of a call counts) |
| `min_slowest_worker`, `min_synthesis`, `min_rest` | Slowest worker, synthesis, and everything else, minutes |
| `workers` | Workers dispatched |
| `units` | One `{size, min}` per unit: S/M/L/XL and its worker minutes |
| `units_reviewed`, `units_haiku`, `units_with_high` | Units with a review, implemented by Haiku, with a high finding |
| `first_review_high_medium` | Mean high+medium findings per unit at first review |
| `fix_merge_commits` | `fix(merge)` commits synthesis needed |
| `intake` | Whether the run had an intake (`<slug>-intake.md`); null on rows recorded before 0.17.0 |
| `intake_min` | Minutes from `INTAKE start` to `INTAKE approved` in the run file, the question round included; null without an intake or an approval |
| `questions_at_gate`, `questions_after_gate` | `AskUserQuestion` questions in the main session answered at or before the intake approval (the approval call itself counts at the gate), and after it. Split at the `INTAKE approved` time; when that time is off (outside the run's span of the log, or before `INTAKE start`), at the line that ran `intake.py freeze`. Null without an intake |
| `wait_min_gate`, `wait_min_after_gate` | `min_ask` + `min_typed` split the same way, minutes (each rounded on its own). Null without an intake |
| `layer_findings` | Triage rows per review layer tag over the run's unit specs: `{layer: {total, rejected}}`, layers `blind-hunter`, `edge-case`, `verification-gap`, `intent-alignment`. Only tagged rows count (`[intent-alignment]`, with or without backticks); rejected means the action was `reject`, `dismissed` or `false`, not `defer`. Null when no spec had a tagged row |
| `intent_auditor_reject_rate` | Share of `[intent-alignment]` findings that were rejected, 0-1; null with no such finding |
| `scenario_pass_rate`, `assert_edits`, `escape_count` | From `<slug>-scenarios.json` (synthesis's holdout scenarios): `pass_rate`, `assert_edits`, and the number of `escapes`. Null without that file |
| `max_turns`, `turn_budget_hit` | Most turns per role; roles that hit their turn budget |
| `by_role` | Per role: `agents`, `turns`, `ktok_ctx_per_turn`, `read`, `grep_glob`, `bash_other` |
| `system_map` | `ok`, `stale`, `none` or null: the system-map check for the split |
| `hardware` | `os`, `cpus`, `ram_gb` |
| `journal_counts` | Problems per kind: `tool_error`, `skill_issue`, `gate_failed`, `missing_reply`, `resume`, `synthesis`, ... |
| `skill_issue_count` | Problems the agents reported about the skill |
| `pr_opened` | Whether synthesis opened a PR |
| `skill_issues`, `journal_items` | Text, only when the bundle `includes_text` (always on the maintainer's own rows), with paths, branches and project names replaced |

Missing numbers are null: older runs predate some fields.

## events: one row per heavy command

Commands run through `heavy.sh` and `bg.sh`.

| Field | Meaning |
|---|---|
| `installation_id` | As above |
| `end` | When it finished, ISO 8601 UTC |
| `tool` | `heavy`, or `bg:<label>` for a background command (for example `bg:suite`, `bg:install`) |
| `kind` | `install`, `verify`, `typecheck`, `lint`, `test`, `build` or `other`, classified from the command line, which is not kept |
| `rc` | Exit code; 124 or 137 means it was stopped after the time limit |
| `sec` | Duration in seconds |
