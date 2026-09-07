# Auto-remediation for Apache Superset, driven by Devin

Every new GitHub issue triggers a webhook that triages it on the spot: if it carries `devin-auto`,
a Devin session opens, gets polled to completion, the outcome is classified, the issue is commented
back on if needed, and the run is recorded.


## The problem

A codebase the size of Superset accumulates bugs that never crash and never fail CI. The function
returns, the request succeeds, the dashboard renders — and half the payload was silently discarded.
Nobody files a ticket, because nobody sees an error. These defects are found by accident, months
later, by a user who notices their filter "doesn't do anything."

Here is one, live in `superset/utils/core.py:1153-1159` today:

```python
for key in [key for key in EXTRA_FORM_DATA_APPEND_KEYS if key not in filter_keys]:
    extra_value = getattr(extra_form_data, key, {})   # dict + getattr -> always {}
    form_value = getattr(form_data, key, {})          # always {}
    form_value.update(extra_value)                    # updates a throwaway dict
    if form_value:
        form_data["key"] = extra_value                # writes the literal string key "key"
```

`extra_form_data` is a plain dict, so `getattr` never finds these keys and both values are always
`{}`. The result is assigned to the literal key `"key"`. Every `custom_form_data`,
`interactive_groupby`, `interactive_drilldown` and `interactive_highlight` passed through
`extra_form_data` is thrown away — and since `extra_form_data` was already popped, it is gone.
No exception. No failing test.

The expensive part of fixing bugs like this is not the patch. It is that finding them requires
somebody to read code nobody has a reason to read, and confirm the suspicion by actually running it.
That is the work this project hands to Devin.

## What this system does

 A FastAPI service turns a new GitHub issue into a triage decision,
   and — when it qualifies — a Devin session that it supervises, verifies, and reports on.

## Architecture

```
issue opened  (every new issue enters the pipeline)
        |
        v
POST /webhook  --> 202 Accepted immediately (GitHub's timeout budget is 10s)
        |            the work continues in a BackgroundTask
        v
triage: does the issue carry `devin-auto`?
        |
        +-- no  --> runs.jsonl (skipped_by_triage) --> stop. No comment: nobody opted this in.
        |
        +-- yes --> POST /v3/organizations/{org}/sessions
        |             prompt = issue text + "decide bug vs. by-design first" + onboarding playbook
        v
        poll GET /sessions/{devin_id} every 30s, up to 60 min
        |
        v
        classify: structured_output.outcome (fallback: said "NOT A BUG"? -> opened a PR? -> neither?)
        |
        v
        append to runs.jsonl (with measured duration)
        |
        v
        GET /dashboard
```

The prompt is the part that matters. It does not say "fix this." It says: decide whether this is
even a bug before touching code, and if it is not, say `NOT A BUG`, explain why on the issue, and
stop. That single instruction is what turns a script that blindly burns an agent on every ticket
into something an engineering team would actually let near their backlog.

### Who says what on the issue

Devin speaks for itself. The orchestrator does not narrate Devin's work in the user's own voice:

| Outcome | Who comments | Why |
|---|---|---|
| PR opened | nobody — the PR body carries `Fixes #N` | GitHub cross-links it to the issue automatically, authored by `devin-ai-integration[bot]` |
| `outcome: not_a_bug` | **Devin**, from inside the session | the reasoning is Devin's; a summary written by the orchestrator would throw away the citations |
| skipped at triage | nobody, log only | nobody opted this issue in, so it does not need the noise |
| timed out / finished with no PR | the orchestrator | Devin is by definition not available to speak, and silence would hide the failure |

## Running it

Copy `.env.example` to `.env` and fill in `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_TOKEN` (repo
scope), `GITHUB_REPO`.

```bash
# local
pip install -r requirements.txt
uvicorn main:app --reload

# docker
touch runs.jsonl        # PowerShell: New-Item runs.jsonl -ItemType File
docker compose up
```

`runs.jsonl` is bind-mounted, so the run log survives container restarts.

### DRY_RUN

`DRY_RUN=1` (the default in `.env.example`) does everything except call Devin and GitHub. Instead it
prints the exact request body it would have sent — including the full, untruncated prompt — and tags
the `runs.jsonl` record with `"dry_run": true` so simulated records can never be mistaken for real
ones. Use it to inspect the prompt and confirm the triage split without spending ACUs or writing to
a real repository. Set `DRY_RUN=0` for the real thing.

### Triggering it for real

The service needs a public URL for GitHub to reach:

```bash
cloudflared tunnel --url http://localhost:8000     # no account needed; prints a https://...trycloudflare.com URL
# or: ngrok http 8000
```

Register the webhook (repo → Settings → Webhooks, or via the API):

- **Payload URL**: the tunnel URL + `/webhook`
- **Content type**: `application/json`
- **Events**: "Let me select individual events" → **Issues** only

Every newly filed issue is triaged automatically the moment it's created — the `devin-auto` label
has to be on the issue at creation time, since only the `opened` event is wired up. Nothing reacts
to a label added after the fact.

The webhook only reacts to `action: opened`, and there is no de-duplication: every accepted delivery
opens a session. GitHub's **Redeliver** button (Settings → Webhooks → Recent Deliveries) is how a
failed delivery gets retried — used for real during testing — but redelivering one that actually succeeded opens a second session and a second PR. Check
`runs.jsonl` for that issue before redelivering.

## Observability

`GET /dashboard` is an HTML page with three sections.

**This Pipeline** — summary cards at the top: issues processed, PRs opened, not a bug, skipped by triage, timeout/incomplete, avg time to PR, and avg time to any outcome. This is the number an engineering leader would check first.

**Daily Throughput** — sessions created, PRs opened, and total minutes spent per day, so a leader can see whether the system is keeping up with incoming issues.

**Issue Outcomes** — one row per issue, latest run wins:

| Column | What it shows |
|---|---|
| Issue | Issue number |
| Outcome | `PR opened` / `Not a bug` / `Skipped` / (timeout, if it happens) |
| Root cause / summary | Devin's own structured output, shown when the session provided one |
| Tests | Pass/fail, when applicable |
| Files | Number of files changed |
| Time | Session duration |
| Evidence | Link to the PR, the issue, or the session transcript |

The question this is built to answer is "how would an engineering leader know this is working?" — so it reports the two things that would make them turn it off: how many issues went in versus how many produced a mergeable PR, and how long each took. Every row links back to the PR, issue, or Devin session transcript that produced it, so any number can be traced to its source.


## Devin API notes (v3)

- Create: `POST https://api.devin.ai/v3/organizations/{org_id}/sessions` with
  `{prompt, title, tags, max_acu_limit, structured_output_schema}`.
- Read: `GET .../sessions/{devin_id}`. Terminal `status` values are `exit`, `error`, `suspended`.
  A session that finishes and waits for a human stays `running` with
  `status_detail: waiting_for_user`, which the poller also treats as done — without that, correct
  results get recorded as timeouts.
- PRs arrive structured on the session as `pull_requests: [{pr_url, pr_state}]`. `pr_url` is always
  read from here, never from structured output, so there is one source of truth for it.
- `structured_output_schema` (JSON Schema Draft 7, ≤64KB, self-contained) is sent at session
  creation; the session response's `structured_output` field is filled in by Devin to match it —
  see `STRUCTURED_OUTPUT_SCHEMA` in `main.py` and the *Output format* section of
  `prompts/base_playbook.md`. This is what `outcome`, `root_cause`, `files_changed`, and
  `tests_passed` are read from, instead of scanning the transcript for a magic phrase.
- If `structured_output` comes back `null` (a session created before this schema existed, or one
  where Devin didn't populate it), the poller falls back to the old behavior: scanning
  `GET .../sessions/{devin_id}/messages` for the literal string `NOT A BUG`, and checking whether
  `pull_requests` is non-empty.
- `POLL_TIMEOUT` is 60 minutes because installing Superset and running its unit tests inside the
  session does not fit in 20.
- `max_acu_limit` is 20. The docs give no recommended value; this is a ceiling, not an estimate.

## Known limitations

- **ACU cost per issue could not be measured.** Both `SessionResponse.acus_consumed` and
  `/consumption/daily/sessions/{id}` reported `0.0` for every session in this org, so no cost figure
  is claimed here rather than an invented one. Wall-clock duration is reported instead.
- **No webhook signature verification.** Anyone who can reach the endpoint can spend ACUs. Required
  before this is exposed anywhere real; the demo tunnel was torn down afterwards.
- **No independent CI gate.** The system verifies that a PR was opened, not that it is green.
- **No retries on a failed session.** A session that ends badly is recorded, not retried, and no
  follow-up message is sent to it. (Transient *polling* errors are retried — see below.)
- **No de-duplication.** `delivery_id` is not tracked, so a redelivered `opened` event (GitHub's own
  auto-retry, or a manual Redeliver) opens a second session and a second PR for the same issue. There
  is also no way to opt an issue into the pipeline after it was created — `devin-auto` has to be on
  it at `opened` time.
- **Polling holds a thread per issue** for up to 60 minutes. Fine for five issues, not for five
  hundred.
- **`runs.jsonl` is append-only with no rotation**, and `/metrics` re-reads the whole file per
  request.
