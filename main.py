"""Event-driven auto-remediation: new GitHub issue -> triage on `devin-auto` -> Devin session -> PR.

Single file on purpose: this is a demo, not a platform.
"""
import html
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

DEVIN_API_KEY = os.environ["DEVIN_API_KEY"]
DEVIN_ORG_ID = os.environ["DEVIN_ORG_ID"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

DEVIN_BASE = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"
DEVIN_HEADERS = {"Authorization": f"Bearer {DEVIN_API_KEY}", "Content-Type": "application/json"}
GITHUB_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

RUNS_FILE = os.getenv("RUNS_FILE", "runs.jsonl")
DELEGATE_LABEL = "devin-auto"
DEFAULT_BASE_BRANCH = "master"

# Onboarding playbooks: what we'd hand a customer's Devin org on day one. The base
# playbook (repo conventions, no-touch areas, verification, PR format) goes into every
# session; the category playbook is picked by label and appended on top of it.
PLAYBOOKS_DIR = Path(__file__).parent / "prompts"
BASE_PLAYBOOK = (PLAYBOOKS_DIR / "base_playbook.md").read_text(encoding="utf-8")
CATEGORY_PLAYBOOKS = {
    "code-quality": "code_quality.md",
    "lint": "code_quality.md",
    "typing": "code_quality.md",
    "dependencies": "dependency_upgrade.md",
    "security": "dependency_upgrade.md",
}
POLL_INTERVAL = 30          # seconds between session polls
POLL_TIMEOUT = 60 * 60      # give up after 60 min: superset env setup + pytest is slow
MAX_ACU_LIMIT = 20          # cost ceiling per session; a session dying at the cap costs more

# v3 SessionResponse.status enum: new|claimed|running|exit|error|suspended|resuming
TERMINAL_STATUSES = {"exit", "error", "suspended"}
# A session can sit in `running` while Devin waits on a human; that is done as far as we care.
TERMINAL_STATUS_DETAILS = {"finished", "waiting_for_user", "inactivity", "error"}

# Asked of every session via `structured_output_schema` (JSON Schema Draft 7, validated by
# Devin server-side). `outcome` replaces the old "NOT A BUG" magic-phrase convention as the
# machine-readable verdict. `pr_url` is deliberately not here: `pull_requests` on the session
# is already a reliable structured source for it, and duplicating it risks the two disagreeing.
STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["pr_opened", "not_a_bug", "blocked", "no_action_needed"],
        },
        "root_cause": {"type": ["string", "null"]},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_passed": {"type": ["boolean", "null"]},
        "summary": {"type": "string"},
    },
    "required": ["outcome", "summary"],
}

app = FastAPI(title="Devin auto-remediation")
_runs_lock = threading.Lock()
DASHBOARD_TEMPLATE = Template(
    (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
)


def record(**fields):
    """Append one line to runs.jsonl. Past lines are never rewritten, newer truth is appended."""
    fields.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    fields["dry_run"] = DRY_RUN
    line = json.dumps(fields)
    with _runs_lock:
        with open(RUNS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(f"[run] {line}", flush=True)


def comment_on_issue(issue_number: int, body: str):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}/comments"
    if DRY_RUN:
        print(f"\n=== DRY_RUN: POST {url} ===")
        print(json.dumps({"body": body}, indent=2, ensure_ascii=False))
        print("=== end of request body ===\n", flush=True)
        return
    r = requests.post(url, headers=GITHUB_HEADERS, json={"body": body}, timeout=30)
    print(f"[github] comment on #{issue_number} -> {r.status_code}", flush=True)


def should_delegate(labels: list[str]) -> bool:
    return DELEGATE_LABEL in labels


def category_playbook(labels: list[str]) -> str | None:
    """The one category playbook whose label matches, appended after the base playbook."""
    for label in labels:
        filename = CATEGORY_PLAYBOOKS.get(label)
        if filename:
            return (PLAYBOOKS_DIR / filename).read_text(encoding="utf-8")
    return None


def build_prompt(number: int, title: str, body: str, labels: list[str]) -> str:
    base = (BASE_PLAYBOOK
            .replace("{{repo}}", GITHUB_REPO)
            .replace("{{issue_number}}", str(number))
            .replace("{{issue_title}}", title)
            .replace("{{labels}}", ", ".join(labels))
            .replace("{{issue_body}}", body or "(no description)")
            .replace("{{base_branch}}", DEFAULT_BASE_BRANCH))

    triage = f"""[Before making any code change]
First determine whether this is a genuine bug or expected/by-design behavior.
- If it is NOT a bug: do not modify any code. Post a comment on issue #{number} of
  {GITHUB_REPO} explaining, with references to the code, why the reported behavior is
  expected — you are explicitly authorized to post that comment without asking. Then set
  `outcome: "not_a_bug"` in your structured output (see Output format below).
- If it IS a bug: proceed with the playbook below.

"""
    playbook = category_playbook(labels)
    return triage + base + (f"\n\n---\n\n{playbook}" if playbook else "")


def create_session(number: int, prompt: str):
    """POST /v3/organizations/{org_id}/sessions -> SessionResponse. Returns (session_id, url)."""
    payload = {
        "prompt": prompt,
        "title": f"Auto-remediate issue #{number}",
        "tags": ["superset", "auto-remediation"],
        "max_acu_limit": MAX_ACU_LIMIT,
        "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
    }
    if DRY_RUN:
        print(f"\n=== DRY_RUN: POST {DEVIN_BASE}/sessions ===")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print("=== end of request body ===\n", flush=True)
        return None, None
    r = requests.post(f"{DEVIN_BASE}/sessions", headers=DEVIN_HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["session_id"], data["url"]


def devin_output_text(session_id: str) -> str:
    """Everything Devin said, concatenated, so we can look for the NOT A BUG verdict."""
    r = requests.get(f"{DEVIN_BASE}/sessions/{session_id}/messages",
                     headers=DEVIN_HEADERS, params={"first": 200}, timeout=30)
    r.raise_for_status()
    return "\n".join(m.get("message", "") for m in r.json().get("items", [])
                     if m.get("source") == "devin")


def poll_session(issue_number: int, session_id: str, session_url: str, started: float):
    deadline = started + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            r = requests.get(f"{DEVIN_BASE}/sessions/{session_id}", headers=DEVIN_HEADERS,
                             timeout=30)
            r.raise_for_status()
            session = r.json()
            status, detail = session.get("status"), session.get("status_detail")
            print(f"[poll] #{issue_number} {session_id} status={status} detail={detail}",
                  flush=True)
            if status not in TERMINAL_STATUSES and detail not in TERMINAL_STATUS_DETAILS:
                continue
            prs = session.get("pull_requests") or []
            structured = session.get("structured_output")
        except requests.RequestException as exc:
            # A transient TLS/network blip must not kill the run: it would leave the issue
            # stuck at "running" forever with no record of what happened.
            print(f"[poll] #{issue_number} transient error, retrying: {exc}", flush=True)
            continue
        # measured, not estimated: this is what the /metrics numbers are built from
        common = dict(issue_number=issue_number, session_id=session_id, session_url=session_url,
                      duration_sec=round(time.time() - started))
        # Devin speaks for itself on the issue: it posts the "not a bug" explanation from
        # inside the session, and a PR carries "Fixes #N". We only comment when the session
        # ended without Devin saying anything, because silence would hide the failure.
        if structured and structured.get("outcome"):
            extra = {k: structured[k] for k in ("root_cause", "files_changed", "tests_passed",
                                                 "summary") if k in structured}
            outcome = structured["outcome"]
            if outcome == "not_a_bug":
                record(**common, status="not_a_bug", **extra)
                return
            if prs:
                record(**common, status="pr_opened", pr_url=prs[0]["pr_url"], **extra)
                return
            record(**common, status="completed_no_pr", **extra)
            comment_on_issue(issue_number,
                             f"Auto-remediation: the Devin session finished without opening a PR "
                             f"(outcome `{outcome}`). Needs human review.\n\n"
                             f"Session: {session_url}")
            return
        # Fallback for sessions that didn't get a structured_output_schema (older sessions)
        # or where Devin didn't populate it: same text/PR-based classification as before.
        output = devin_output_text(session_id)
        if "NOT A BUG" in output.upper():
            record(**common, status="not_a_bug")
            return
        if prs:
            record(**common, status="pr_opened", pr_url=prs[0]["pr_url"])
            return
        record(**common, status="completed_no_pr")
        comment_on_issue(issue_number,
                         f"Auto-remediation: the Devin session finished without opening a PR "
                         f"(status `{status}` / `{detail}`). Needs human review.\n\n"
                         f"Session: {session_url}")
        return

    record(issue_number=issue_number, session_id=session_id, session_url=session_url,
           status="timeout", duration_sec=round(time.time() - started))
    comment_on_issue(issue_number,
                     f"Auto-remediation: gave up polling after {POLL_TIMEOUT // 60} minutes; "
                     f"the session may still be running.\n\nSession: {session_url}")


def handle_issue(issue: dict):
    """Runs in a background thread (FastAPI BackgroundTasks) so the webhook can answer instantly."""
    number = issue["number"]
    labels = [label["name"] for label in issue.get("labels", [])]

    if not should_delegate(labels):
        # Logged, not commented on: an issue nobody opted in does not need the noise.
        record(issue_number=number, status="skipped_by_triage")
        return

    prompt = build_prompt(number, issue.get("title", ""), issue.get("body", ""), labels)
    started = time.time()
    session_id, session_url = create_session(number, prompt)
    if DRY_RUN:
        record(issue_number=number, status="dry_run_would_create_session")
        return

    record(issue_number=number, session_id=session_id, session_url=session_url,
           status="running", created_at=datetime.now(timezone.utc).isoformat())
    poll_session(number, session_id, session_url, started)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """Every new issue is triaged immediately; devin-auto decides whether it gets delegated."""
    payload = await request.json()
    action, issue = payload.get("action"), payload.get("issue")

    if issue and action == "opened":
        background_tasks.add_task(handle_issue, issue)
        return JSONResponse(status_code=202, content={"status": "accepted"})
    return JSONResponse(status_code=202, content={"status": "ignored"})


STATUS_PILL = {
    "pr_opened": ("pill-ok", "PR opened"),
    "not_a_bug": ("pill-info", "Not a bug"),
    "skipped_by_triage": ("pill-muted", "Skipped"),
    "completed_no_pr": ("pill-warn", "No PR"),
    "timeout": ("pill-warn", "Timeout"),
    "running": ("pill-muted", "Running"),
    "dry_run_would_create_session": ("pill-muted", "Dry run"),
}


def fmt_duration(sec) -> str:
    if not sec:
        return "—"
    m, s = divmod(round(sec), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def row_link(run: dict) -> str:
    number = run["issue_number"]
    if run.get("pr_url"):
        return f'<a href="{run["pr_url"]}" target="_blank" rel="noopener">PR</a>'
    if run.get("session_url"):
        return f'<a href="{run["session_url"]}" target="_blank" rel="noopener">session</a>'
    return (f'<a href="https://github.com/{GITHUB_REPO}/issues/{number}" '
            f'target="_blank" rel="noopener">issue</a>')


def tests_pill(run: dict) -> str:
    """From structured_output.tests_passed — absent on runs classified by the text fallback."""
    tests_passed = run.get("tests_passed")
    if tests_passed is True:
        return "<span class='pill pill-ok'>Pass</span>"
    if tests_passed is False:
        return "<span class='pill pill-warn'>Fail</span>"
    return "<span class='pill pill-muted'>—</span>"


def files_changed_count(run: dict) -> str:
    files_changed = run.get("files_changed")
    return str(len(files_changed)) if files_changed else "—"


def outcome_tooltip(run: dict) -> str:
    """root_cause/summary from structured_output, shown as a hover tooltip on the outcome pill."""
    text = run.get("root_cause") or run.get("summary")
    return f' title="{html.escape(text)}"' if text else ""


def daily_series(runs: list[dict]) -> list[dict]:
    """One row per calendar day (UTC): sessions created, PRs opened, total minutes spent.

    `runs` is already one merged record per issue (see metrics()), so a single row can carry
    both `created_at` (when the session started) and `timestamp` (when it reached an outcome).
    """
    days: dict[str, dict] = {}

    def bucket(day: str) -> dict:
        return days.setdefault(day, {"date": day, "sessions": 0, "prs": 0, "minutes": 0.0})

    for r in runs:
        if r.get("created_at"):
            bucket(r["created_at"][:10])["sessions"] += 1
        if r.get("timestamp") and r.get("duration_sec"):
            row = bucket(r["timestamp"][:10])
            row["minutes"] += r["duration_sec"] / 60
            if r["status"] == "pr_opened":
                row["prs"] += 1

    return [days[d] for d in sorted(days)]


COUNT_SERIES = [
    ("sessions", "#2f4f8f", "Sessions created"),
    ("prs", "#1e6b3a", "PRs opened"),
]
MINUTES_SERIES = ("minutes", "#a03a2a", "Total minutes")
CHART_SERIES = COUNT_SERIES + [MINUTES_SERIES]
ZERO_LEAD_IN_DAYS = 3


def with_zero_lead_in(daily: list[dict], lead_days: int) -> list[dict]:
    """Prepend `lead_days` all-zero rows before the real data — genuinely zero activity (the
    pipeline hadn't run yet), not invented numbers, just enough that two real points don't
    read as a single blip."""
    first_date = datetime.strptime(daily[0]["date"], "%Y-%m-%d")
    lead_in = [
        {"date": (first_date - timedelta(days=lead_days - i)).strftime("%Y-%m-%d"),
         "sessions": 0, "prs": 0, "minutes": 0}
        for i in range(lead_days)
    ]
    return lead_in + daily


def line_chart_svg(daily: list[dict]) -> str:
    """Counts (sessions, PRs) on the left axis, minutes on the right — two different scales,
    two axes, one shared x-axis of days. The span label always reflects the real data only;
    a zero-activity lead-in is added just so a short run doesn't read as a single blip."""
    if not daily:
        return '<p class="note">No runs recorded yet.</p>'

    n_real = len(daily)
    span = (f"{n_real} day of real run data ({daily[0]['date']})" if n_real == 1 else
            f"{n_real} days of real run data ({daily[0]['date']} to {daily[-1]['date']})")
    daily = with_zero_lead_in(daily, ZERO_LEAD_IN_DAYS)

    width, height, pad_x, pad_top, pad_bottom = 640, 160, 34, 14, 22
    n = len(daily)
    step = (width - 2 * pad_x) / max(n - 1, 1)
    xs = [pad_x + i * step for i in range(n)]

    count_peak = max(max(d["sessions"] for d in daily), max(d["prs"] for d in daily)) or 1
    minutes_peak = max(d["minutes"] for d in daily) or 1

    def y_of(v: float, peak: float) -> float:
        return height - pad_bottom - (v / peak) * (height - pad_bottom - pad_top)

    def series_svg(key: str, color: str, peak: float) -> str:
        values = [d[key] for d in daily]
        ys = [y_of(v, peak) for v in values]
        points = " ".join(f"{px:.1f},{py:.1f}" for px, py in zip(xs, ys))
        dots = "".join(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{color}">'
            f'<title>{d["date"]}: {v:g}</title></circle>'
            for px, py, v, d in zip(xs, ys, values, daily)
        )
        return f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5" />{dots}'

    lines = "".join(series_svg(key, color, count_peak) for key, color, _ in COUNT_SERIES)
    lines += series_svg(MINUTES_SERIES[0], MINUTES_SERIES[1], minutes_peak)

    def axis_ticks(peak: float, x: float, anchor: str, fmt) -> str:
        return "".join(
            f'<text x="{x:.1f}" y="{y_of(v, peak) + 3:.1f}" font-size="10" fill="#767c82" '
            f'text-anchor="{anchor}">{fmt(v)}</text>'
            for v in (0, peak / 2, peak)
        )

    left_axis = axis_ticks(count_peak, pad_x - 8, "end", lambda v: f"{v:g}")
    right_axis = axis_ticks(minutes_peak, width - pad_x + 8, "start", lambda v: f"{round(v)}")
    axis_labels = (
        f'<text x="{pad_x}" y="10" font-size="9" fill="#767c82">count</text>'
        f'<text x="{width - pad_x}" y="10" font-size="9" fill="#767c82" text-anchor="end">min</text>'
    )
    x_labels = "".join(
        f'<text x="{px:.1f}" y="{height - 4}" font-size="10" fill="#767c82" text-anchor="middle">'
        f'{d["date"][5:]}</text>'
        for px, d in zip(xs, daily)
    )
    legend = "".join(
        f'<span class="legend-item"><span class="dot" style="background:{color}"></span>{label}</span>'
        for _, color, label in CHART_SERIES
    )
    svg = (f'<svg viewBox="0 0 {width} {height}" class="line-chart">'
           f'{axis_labels}{left_axis}{right_axis}{lines}{x_labels}</svg>')
    return (f'<div class="chart-wrap"><div class="chart-span">{span}</div>'
            f'{svg}<div class="legend">{legend}</div></div>')


def render_dashboard(pipeline: dict) -> str:
    rows = "".join(
        f"<tr><td>#{r['issue_number']}</td>"
        f"<td{outcome_tooltip(r)}><span class='pill {STATUS_PILL.get(r['status'], ('pill-muted', r['status']))[0]}'>"
        f"{STATUS_PILL.get(r['status'], ('pill-muted', r['status']))[1]}</span></td>"
        f"<td>{tests_pill(r)}</td>"
        f"<td class='num'>{files_changed_count(r)}</td>"
        f"<td class='num'>{fmt_duration(r.get('duration_sec'))}</td>"
        f"<td>{row_link(r)}</td></tr>"
        for r in pipeline["runs"]
    )
    return DASHBOARD_TEMPLATE.substitute(
        repo=GITHUB_REPO,
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        total_issues=pipeline["total_issues"],
        pr_opened=pipeline["pr_opened"],
        not_a_bug=pipeline["not_a_bug"],
        skipped_by_triage=pipeline["skipped_by_triage"],
        timeout_or_incomplete=pipeline["timeout_or_incomplete"],
        avg_time_to_pr=fmt_duration(pipeline["avg_time_to_pr"]["avg_sec"]),
        avg_time_to_pr_n=pipeline["avg_time_to_pr"]["n"],
        avg_time_to_outcome=fmt_duration(pipeline["avg_time_to_outcome"]["avg_sec"]),
        avg_time_to_outcome_n=pipeline["avg_time_to_outcome"]["n"],
        rows=rows or "<tr><td colspan='6'>No runs recorded yet.</td></tr>",
        daily_chart=line_chart_svg(daily_series(pipeline["runs"])),
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return render_dashboard(metrics())


@app.get("/metrics")
def metrics():
    latest: dict[int, dict] = {}
    if os.path.exists(RUNS_FILE):
        with open(RUNS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                run = json.loads(line)
                latest.setdefault(run["issue_number"], {}).update(run)
    runs = sorted(latest.values(), key=lambda r: r["issue_number"])
    counts = {"pr_opened": 0, "not_a_bug": 0, "skipped_by_triage": 0}
    for run in runs:
        if run["status"] in counts:
            counts[run["status"]] += 1

    def avg_duration(statuses: set[str]):
        # honest about sample size: an "average" of one run is just that run
        vals = [r["duration_sec"] for r in runs if r["status"] in statuses and r.get("duration_sec")]
        return {"avg_sec": round(sum(vals) / len(vals)) if vals else None, "n": len(vals)}

    return {
        "total_issues": len(runs),
        **counts,
        "timeout_or_incomplete": len(runs) - sum(counts.values()),
        "avg_time_to_pr": avg_duration({"pr_opened"}),
        "avg_time_to_outcome": avg_duration({"pr_opened", "not_a_bug", "completed_no_pr"}),
        "dry_run": DRY_RUN,
        "runs": runs,
    }
