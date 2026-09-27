# Getting suggestions, and answering them

## Local by default

Nothing is applied automatically. Out of the box it is all local, with no accounts and no
network:

1. **`runtune notify`** derives and reviews, then writes one digest to `.runtune/inbox/`:
   `latest.md` and a card image styled like a weekly status report. It also pops a desktop
   notification (macOS Notification Center, or `notify-send` on Linux).
2. **`runtune show --open`** prints the digest and opens the card.
3. **`runtune reply 1,3`** approves items 1 and 3. `all`, `all except 2` and `none` work too.
   Anything else ("all but number three", "the first two") gets a question back, never a guess.
   Declined items are snoozed for 28 days.
4. **`runtune schedule --install`** makes it run by itself: a launchd job on macOS, or it prints
   cron lines on Linux. The digest goes out Monday 09:00, and replies from remote channels are
   checked every hour.

Some items can't be approved by reply on any channel:

- A widening (a looser rule, a broader grant, a model admitted to a mode) is applied from a
  terminal with `--reason`, and with `--eval` for routes.
- A constraint whose own replay shows it would mostly fire on working calls is listed as
  needing a person to narrow it.
- A proposal already covered by a rule in force is not offered.
- A code fix (no artifact to write) is recorded as acknowledged.

### Channels are extensions

Add any number; the digest goes to all of them. The first valid answer from any channel is
acted on, and the other channels are told it was handled, so nothing can be applied twice.

```bash
runtune channels add slack  --opt user=U0123 --opt token_env=RUNTUNE_SLACK_TOKEN   # DM + card; reply in thread
runtune channels add email  --opt to=me@example.com       # Gmail API thread (reply loop) or SMTP (send only)
runtune channels add plugin --opt module=mypkg.teams:TeamsChannel                  # your own
runtune channels test                                     # sends a test through each
runtune channels                                          # list
```

The config (`.runtune/channels.json`) stores environment-variable names, never credentials,
and `channels add` refuses a value that looks like a token. A plugin is any class with
`send(digest, text, png)`, `replies(delivery)` and `answer(delivery, text)`.
`RUNTUNE_DRY_RUN=1` refuses every remote send.

| channel | needs |
|---|---|
| local | nothing |
| slack | a bot token with chat:write, im:write, im:history, files:write |
| email | `RUNTUNE_GMAIL_TOKEN` (an authorized-user token file) for the reply loop, or `RUNTUNE_SMTP_*` to send only |

## Running on a VM (optional)

Not needed: `runtune schedule` runs the same loop on your own machine. When you want it to
run while the laptop is closed, `deploy/fly/` runs it unattended on one small Fly machine:

- **Weekly:** derive + review + digest.
- **Hourly:** inbox.
- **Daily:** an OpenRouter bill snapshot.

On a VM the targets live in a git repo, so `inbox --git` applies approvals into a clone,
commits them on a branch and opens a pull request. Merging is the final approval, and
`git revert` is the undo.

An approved change is not treated as live until it is merged. Review reports it as `pending`
("pull request not merged yet"), and measurement starts on the day the change first appears in
the target's pulled main branch. That way a change nobody merged can't be credited or blamed
for what happened after it was approved.

Protect the target branch, so that RunTune's token can push branches and open pull requests
but can't merge them. That makes "Git remains the deployment boundary" a setting, not a
convention.

Secrets are named one by one (`deploy/fly/set-secrets.py`), never a whole `.env`. Give it a
SELECT-only database role: RunTune opens read-only sessions, but a read-only session is a
setting, not a permission.
