# Data: what `collect` returns

Shared by the EOD and weekly reports. `<eod>` is the `ado-eod-report` skill
folder.

```bash
python3 "<eod>/scripts/ado_eod.py" collect --date <date>                 # one day (EOD)
python3 "<eod>/scripts/ado_eod.py" collect --week this|last|YYYY-MM-DD   # Monday to Sunday (weekly)
```

`--days N` widens the window to N days from `--date`. The JSON has `date`,
`to` and `days` for the window; for a week, `date` is the Monday and `to` the
Sunday. "The window" below is that day or week.

It prints JSON:

- `items`: every work item the user changed in the window. It also includes
  items assigned to them that someone else moved, such as QA closing a bug.
  Each item has:
  - `section`, `type` and `title`;
  - `parent`: its `id`, `type`, `title`, `state` and `children`;
  - `children`: for an item that has child items (a story), how far they are,
    counting the ones nobody touched in the window: `total`, `done`, and
    `open` with each open child's `id`, `title` and `state`. A child whose PR
    merged counts as done; removed and cut ones aren't counted.
    `parent.children` is the same for the parent. Null when there are no
    children;
  - `state_start` and `state_end`: the state at the start and end of the
    window (`state_start` is null when the item was created in it);
  - `events`: what changed and when (`date`, `time`). `by_me` says who made
    the change. Events also carry comments, completed hours, board columns,
    linked PRs and other changed fields;
  - `prs`: linked pull requests, with status `active`, `draft`, `completed`
    or `abandoned`, and approval counts;
  - `blocked` and `url`;
  - `merged_today`: the user's PRs, linked to this item, that merged in the
    window (the name is kept from the day-only version). Such items are
    listed even when nobody touched them on the board.
- `merged_prs`: every PR the user authored that merged in the window, in any
  project, with its linked `work_items`.
- `open_assigned`: open items assigned to the user that weren't touched in
  the window, highest priority first.
- `warnings`, and `first_run`.
- `sections`: the report's sections in order, each with `name` and `style`
  (`stories`, or `plain` for one-line entries); `sections_from` says where
  they came from:
  - `"project"` (the default): no sections are configured, so each item's
    section is its own Azure DevOps project name. Nothing is built in.
  - `"config"`: the user's own list from `~/.config/ado-eod-report/config.json`.
    An item goes to the first section whose `match` words appear in its
    project, area path, iteration path or tags, as whole words. Anything else
    goes to Others.
