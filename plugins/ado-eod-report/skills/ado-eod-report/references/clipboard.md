# Clipboard: printing the report for Teams

Shared by the EOD and weekly reports. `<eod>` is the `ado-eod-report` skill
folder. Each skill's SKILL.md lists its own headings for step 1.

The user pastes the report into Teams, which shows markdown as raw `#` and
`**`. So also put it on the clipboard as rich text:

1. Write the same report as an HTML fragment to a file in the scratchpad:
   - The title as `<p><b>... Report – <date></b></p>`.
   - Each top heading as `<p><b><Heading>:</b></p>`, followed by its content:
     a `<p>` for prose, a `<ul>` for bullets.
   - A sectioned heading (the accomplished or delivered work) gets one `<ul>`
     whose items are the sections in bold, each holding a nested `<ul>` of
     the story lines, and each story a further `<ul>` with its Tasks and PRs
     lines.
   - Bold the `[Type #id] name:` prefix and every status word. Escape `&`,
     `<`, `>` and write `->` as `-&gt;`.
2. On Windows, copy it:
   `powershell -NoProfile -STA -File "<eod>/scripts/copy_html.ps1" <file.html>`.
   Elsewhere, skip this step.

Then print the report in one fenced `text` block too, as a fallback:

````
```text
# ... Report – ...
...
```
````

Below the block, say first that the report is on the clipboard and pastes
into Teams with Ctrl+V (on Windows). Then write at most three short lines:
- how many work items were read, how many story lines they rolled up into,
  and anything left out on purpose (planning noise, items only someone else
  touched);
- any `warnings`, such as PR status being unknown;
- on `first_run` only: that the work is grouped by Azure DevOps project (name
  the projects from `sections`), and that they can set their own sections
  instead with
  `config --sections '[{"name":"<Section>","match":["<project, area or tag word>"]}, ...]'`;
  a section with an empty `match` collects everything else, and
  `config --sections '[]'` goes back to one section per project.

Don't write to ADO. The report is only printed; the user copies it.
