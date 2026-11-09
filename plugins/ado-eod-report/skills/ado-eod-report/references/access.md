# Access: `check` and its errors

Shared by the EOD and weekly reports. `<eod>` is the `ado-eod-report` skill
folder.

```bash
python3 "<eod>/scripts/ado_eod.py" check
```

`OK <name> <email> <org> (via PAT)` or `(via az login)` means you can go
on, after the `PROFILE` line below. If you get an error line, handle it and
run `check` again:

- **`ERROR NO_AUTH`**: no PAT, and no Azure CLI sign-in that works for this
  organization. Offer the two ways, Azure CLI first when the message names
  an `az login` command:
  - **Azure CLI:** they run the `az login ...` command from the error
    themselves. It opens a browser, so suggest typing it as
    `! az login --tenant ... --scope ...` in the prompt. The `--tenant` part
    matters: the organization can sit in another tenant than their default
    `az` account.
  - **PAT:**
  1. Open `https://dev.azure.com/<org>/_usersSettings/tokens` and click
     **New Token**.
  2. Select the organization, and the scopes **Work Items: Read & write**
     and **Code: Read** (Code: Read is only used for PR status).
  3. Save the token in one of these ways, in their own terminal, so it never
     appears in the chat:
     - `export AZURE_DEVOPS_EXT_PAT=<token>` in their shell profile, then
       restart Claude Code; or
     - `python3 "<eod>/scripts/ado_eod.py" config --pat-stdin`, paste the
       token, then press Enter and Ctrl-D. It's saved to
       `~/.config/ado-eod-report/pat` with owner-only access.

  Don't ask for the token in the chat. If the user pastes it anyway, save it
  with `printf '%s' '<token>' | python3 "<eod>/scripts/ado_eod.py" config --pat-stdin`
  and tell them in one line to revoke it later, since it is now in the
  transcript.
- **`ERROR NO_ORG`**: no ADO remote here and none saved. Ask for the
  organization URL or name, then run `config --org <it>`.
- **`ERROR AUTH`**: the PAT expired, was revoked or belongs to another
  organization, and `az login` didn't work either. When the message names an
  `az login --tenant ...` command, offer that first; otherwise, a new PAT (as
  above).
- **`ERROR FORBIDDEN`**: the PAT lacks the Work Items scope, or the `az`
  account has no access to the project. Ask the user to fix the PAT's scopes,
  or to get access to the project.
- **`ERROR NETWORK`**: say that Azure DevOps can't be reached, and stop.

## Who the report is for

The second line of `check` says whose report this is:

- **`PROFILE <name> | <role>`** (the role may be missing): go on. The report
  header uses this name and role.
- **`PROFILE NONE`**: nobody has said yet. Before collecting, ask the user
  with one question (AskUserQuestion when you have it):
  1. Is the signed-in account, `<name> <email>` from the `OK` line, theirs?
     The report covers only that account's work. If it isn't theirs (a
     shared, service or teammate's account), stop and have them sign in as
     themselves (`access` steps above), then run `check` again.
  2. Their name as it should appear on the report (offer the signed-in name),
     and their role or team, such as "Backend Engineer, Payments" (optional).

  Save the answer, then run `check` again:
  `python3 "<eod>/scripts/ado_eod.py" config --name "<name>" --role "<role>"`.
  It is asked once; `config --name/--role` changes it later.
