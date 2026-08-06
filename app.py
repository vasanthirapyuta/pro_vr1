from __future__ import annotations

"""
QA Metrics Dashboard — Flask API.

Exposes 8 REST endpoints consumed by the React SPA in frontend/index.html.
The ADO PAT is read exclusively from the ADO_PAT environment variable;
it is never stored in config.yaml or committed to version control.

KPI computation is done server-side so the browser receives plain JSON
and has no dependency on pandas or any heavy library.
"""

import logging
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from ado_client import ADOClient, resolve_qa_members, get_nightly_health, get_ci_stability, get_flaky_tests, get_release_automation_status

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Bootstrap ──────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent
_CFG_PATH = os.environ.get("CONFIG_PATH", str(_ROOT / "config.yaml"))

try:
    with open(_CFG_PATH) as _f:
        CFG = yaml.safe_load(_f)
except FileNotFoundError:
    logger.critical("Config file not found: %s — set CONFIG_PATH env var", _CFG_PATH)
    sys.exit(1)
except yaml.YAMLError as exc:
    logger.critical("Invalid config.yaml: %s", exc)
    sys.exit(1)

_PAT = os.environ.get("ADO_PAT", "")
if not _PAT:
    logger.warning("ADO_PAT environment variable is not set — all ADO API calls will fail")

ado = ADOClient(
    pat=_PAT,
    project=CFG["ado"]["project"],
    testplans_project=CFG["ado"]["testplans_project"],
    cache_ttl=int(os.environ.get("CACHE_TTL", "600")),
)

QA_TAG: str = CFG.get("qa_tag", "sb_qa")
# QA_MEMBERS is the flat fallback list; use _qa_members_for_sprint(sprint) for per-sprint accuracy.
QA_MEMBERS: list[str] = resolve_qa_members(CFG)   # all-time union — for non-sprint endpoints


def _qa_members_for_sprint(sprint: dict) -> list[str]:
    """Return QA members active during this sprint using the date-range roster."""
    return resolve_qa_members(CFG, sprint.get("start"), sprint.get("end"))
# Path to feature_coverage_cache.json produced by traverse_feature_coverage.py.
_COVERAGE_CACHE_FILE: str | None = (
    os.environ.get("COVERAGE_CACHE_FILE")
    or CFG.get("feature_coverage_cache_file")
)
# Path to flaky_tests_report.json produced by scan_flaky_tests.py.
_FLAKY_REPORT_FILE: str | None = (
    os.environ.get("FLAKY_REPORT_FILE")
    or CFG.get("flaky_report_file")
)
# Path to release_automation_status.yaml — the release-wise feature automation master table.
_RELEASE_AUTOMATION_FILE: str | None = (
    os.environ.get("RELEASE_AUTOMATION_FILE")
    or CFG.get("release_automation_file")
)
if _RELEASE_AUTOMATION_FILE and not os.path.isabs(_RELEASE_AUTOMATION_FILE):
    _RELEASE_AUTOMATION_FILE = str(_ROOT / _RELEASE_AUTOMATION_FILE)
# GitHub token for nightly health endpoint (reads Actions API, no OAuth needed).
# Read lazily at request time so it picks up env vars set after process start.
def _get_github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or CFG.get("github_token")
# Path to the full snapshot JSON from build_snapshot.py.
_SNAPSHOT_FILE: str | None = (
    os.environ.get("SNAPSHOT_FILE")
    or CFG.get("snapshot_file")
)

# State sets derived from live ADO data (2026-05-11)
US_DONE = {"Completed", "Closed", "Resolved"}
BUG_DONE = {"Closed", "Completed", "Resolved", "Not a Bug", "Duplicate"}

# ── App ────────────────────────────────────────────────────────────────────────

_debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
_cors_origins = CFG.get("cors_origins") or "*"

app = Flask(__name__, static_folder=str(_ROOT / "frontend"))
CORS(app, resources={r"/api/*": {"origins": _cors_origins}})


# ── Error handler ──────────────────────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_exception(exc: Exception):
    if isinstance(exc, HTTPException):
        return exc
    logger.exception("Unhandled exception on %s %s", request.method, request.path)
    body: dict = {"error": "Internal server error"}
    if _debug:
        body["detail"] = str(exc)
    return jsonify(body), 500


# ── Health ──────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "qa_members_configured": len(QA_MEMBERS),
        "project": CFG["ado"]["project"],
    })


# ── Sprints ─────────────────────────────────────────────────────────────────────

@app.get("/api/sprints")
def list_sprints():
    today = datetime.utcnow().date()
    result = []
    for s in CFG["sprints"]:
        start = datetime.fromisoformat(s["start"]).date()
        end = datetime.fromisoformat(s["end"]).date()
        result.append({
            **s,
            "is_current": start <= today <= end,
            "is_past": today > end,
            "is_future": today < start,
        })
    return jsonify(result)


# ── KPI summary (single sprint) ─────────────────────────────────────────────────

@app.get("/api/kpi/summary")
def kpi_summary():
    sprint = _resolve_sprint(request.args.get("sprint"))
    if sprint is None:
        return jsonify({"error": "Sprint not found"}), 404

    members = _qa_members_for_sprint(sprint)
    user_stories = ado.get_user_stories(sprint["iteration_path"], members, QA_TAG)
    bugs = ado.get_bugs(sprint["iteration_path"])
    return jsonify(_compute(user_stories, bugs, sprint))


# ── Trends (all past + current sprints) ─────────────────────────────────────────

@app.get("/api/kpi/trends")
def kpi_trends():
    today = datetime.utcnow().date()
    results = []
    for sprint in CFG["sprints"]:
        if datetime.fromisoformat(sprint["start"]).date() > today:
            continue
        members = _qa_members_for_sprint(sprint)
        user_stories = ado.get_user_stories(sprint["iteration_path"], members, QA_TAG)
        bugs = ado.get_bugs(sprint["iteration_path"])
        results.append(_compute(user_stories, bugs, sprint))
    return jsonify(results)


# ── Per-engineer breakdown ───────────────────────────────────────────────────────

@app.get("/api/engineers")
def engineers():
    sprint = _resolve_sprint(request.args.get("sprint"))
    if sprint is None:
        return jsonify({"error": "Sprint not found"}), 404

    members = _qa_members_for_sprint(sprint)
    user_stories = ado.get_user_stories(sprint["iteration_path"], members, QA_TAG)

    # qa_tag ("sb_qa") pulls in a story regardless of assignee, so a cross-team
    # contributor's item is never missed — but a *former* roster member's old
    # sb_qa-tagged story would surface here too even though they've since left
    # (roster `to` date already passed for this sprint). Drop those: anyone
    # who was ever on the roster but isn't currently active for this sprint
    # window shouldn't be attributed in the Per Engineer breakdown. A true
    # non-roster contributor (never listed at all) still counts — that's
    # exactly what the tag exists for.
    active_casefold = {m.casefold() for m in members}
    ever_roster_casefold = {
        e["name"].strip().casefold()
        for e in (CFG.get("qa_team_roster") or [])
        if e.get("name")
    }

    eng: dict[str, dict] = {}

    for us in user_stories:
        name = us.get("assignee") or "Unassigned"
        name_cf = name.casefold()
        if name_cf in ever_roster_casefold and name_cf not in active_casefold:
            continue
        e = eng.setdefault(name, _blank_eng(name))
        e["stories"] += 1
        if us["state"] in US_DONE:
            e["completed"] += 1
        e["points"] += us.get("story_points") or 0

    rows = []
    for e in eng.values():
        e["completion_rate"] = (
            round(e["completed"] / e["stories"] * 100, 1) if e["stories"] else 0
        )
        rows.append(e)

    return jsonify(sorted(rows, key=lambda x: -x["stories"]))


# ── Bug detail (single sprint) ───────────────────────────────────────────────────

@app.get("/api/bugs")
def bugs_detail():
    sprint = _resolve_sprint(request.args.get("sprint"))
    if sprint is None:
        return jsonify({"error": "Sprint not found"}), 404

    # PI-wide bug pool — trend/lead-time/aging need more than one 2-week sprint
    # of data to mean anything, so these analytics run over the whole PI.
    bugs = ado.get_bugs(sprint["iteration_path"])

    sprint_iter = _canon_iter(sprint["iteration_path"])
    sprint_bugs = [b for b in bugs if _canon_iter(b["iteration"]) == sprint_iter] or bugs
    mttr = _median_lead_time(sprint_bugs, "resolved")

    pi_sprints = [s for s in CFG["sprints"] if s.get("pi") == sprint.get("pi")]
    window_start = min((s["start"] for s in pi_sprints), default=sprint["start"])
    window_end = min(datetime.utcnow().date().isoformat(), max((s["end"] for s in pi_sprints), default=sprint["end"]))

    return jsonify({
        "sprint": sprint["label"],
        "pi": sprint.get("pi", ""),
        "total": len(sprint_bugs),
        "resolved": sum(1 for b in sprint_bugs if b["state"] in BUG_DONE),
        "state_distribution": dict(Counter(b["state"] for b in sprint_bugs).most_common()),
        "priority_distribution": dict(
            Counter(str(b.get("priority") or "—") for b in sprint_bugs).most_common()
        ),
        "mttr_days": mttr,
        "pi_total": len(bugs),
        "weekly_trend": _weekly_bug_trend(bugs, window_start, window_end),
        "age_buckets": _age_buckets(bugs),
        "lead_time": {
            "time_to_resolve": _lead_time_stats(bugs, "created", "resolved"),
            "time_to_verify": _lead_time_stats(bugs, "resolved", "closed"),
        },
        "oldest_open": _oldest_open_bugs(bugs, limit=10),
        "items": [
            {
                "id": b["id"],
                "title": b["title"][:80],
                "state": b["state"],
                "priority": b.get("priority"),
                "area": (b.get("area") or "").split("\\")[-1] or None,
                "created": b.get("created"),
                "resolved": b.get("resolved") or None,
                "closed": b.get("closed") or None,
            }
            for b in sorted(sprint_bugs, key=lambda x: x.get("created") or "", reverse=True)[:60]
        ],
    })


def _median_lead_time(bugs: list[dict], resolved_field: str) -> float | None:
    """Median days from created to resolved_field (falls back to closed)."""
    days = []
    for b in bugs:
        end = b.get(resolved_field) or b.get("closed")
        if b.get("created") and end:
            days.append((datetime.fromisoformat(end) - datetime.fromisoformat(b["created"])).days)
    return round(statistics.median(days), 1) if days else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f), 1)


def _lead_time_stats(bugs: list[dict], start_field: str, end_field: str) -> dict:
    """Days between two lifecycle timestamps (e.g. created→resolved, resolved→closed)."""
    days = [
        (datetime.fromisoformat(b[end_field]) - datetime.fromisoformat(b[start_field])).days
        for b in bugs if b.get(start_field) and b.get(end_field)
    ]
    return {
        "median": round(statistics.median(days), 1) if days else None,
        "p90": _percentile(days, 90),
        "count": len(days),
    }


def _week_of(date_str: str) -> str:
    d = datetime.fromisoformat(date_str).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _weekly_bug_trend(bugs: list[dict], window_start: str, window_end: str) -> list[dict]:
    """Bugs created vs. resolved per Monday-aligned week across the PI's date span."""
    created_by_week: dict[str, int] = defaultdict(int)
    resolved_by_week: dict[str, int] = defaultdict(int)
    for b in bugs:
        if b.get("created") and window_start <= b["created"] <= window_end:
            created_by_week[_week_of(b["created"])] += 1
        resolved_at = b.get("resolved") or b.get("closed")
        if resolved_at and window_start <= resolved_at <= window_end:
            resolved_by_week[_week_of(resolved_at)] += 1

    week = datetime.fromisoformat(window_start).date()
    week -= timedelta(days=week.weekday())
    end = datetime.fromisoformat(window_end).date()
    trend = []
    net = 0
    while week <= end:
        wk = week.isoformat()
        created = created_by_week.get(wk, 0)
        resolved = resolved_by_week.get(wk, 0)
        net += created - resolved
        trend.append({"week": wk, "created": created, "resolved": resolved, "net_flow": created - resolved, "cumulative_net_flow": net})
        week += timedelta(days=7)
    return trend


def _age_buckets(bugs: list[dict]) -> dict:
    today = datetime.utcnow().date()
    buckets = {"0-7d": 0, "8-14d": 0, "15-30d": 0, "31d+": 0}
    for b in bugs:
        if b["state"] in BUG_DONE or not b.get("created"):
            continue
        age = (today - datetime.fromisoformat(b["created"]).date()).days
        key = "0-7d" if age <= 7 else "8-14d" if age <= 14 else "15-30d" if age <= 30 else "31d+"
        buckets[key] += 1
    return buckets


def _oldest_open_bugs(bugs: list[dict], limit: int = 10) -> list[dict]:
    today = datetime.utcnow().date()
    open_bugs = [b for b in bugs if b["state"] not in BUG_DONE and b.get("created")]
    open_bugs.sort(key=lambda b: b["created"])
    return [
        {
            "id": b["id"],
            "title": b["title"][:80],
            "state": b["state"],
            "area": (b.get("area") or "").split("\\")[-1] or None,
            "created": b["created"],
            "age_days": (today - datetime.fromisoformat(b["created"]).date()).days,
        }
        for b in open_bugs[:limit]
    ]


# ── Test Plans (Tier 2 — sootballs project) ──────────────────────────────────────

@app.get("/api/testplans")
def testplans():
    plans = ado.get_test_plans()
    return jsonify({
        "plans": [
            {
                "id": p["id"],
                "name": p["name"],
                "iteration": p.get("iteration", ""),
                "state": p.get("state", ""),
            }
            for p in plans
        ],
    })


# ── Release Automation (Feature -> Suite ID -> Backend PR -> Test PR -> Status) ──

@app.get("/api/release_automation")
def release_automation():
    """Return the release-wise feature automation master table (3.4 through 3.7).

    Each release's real feature content was reconstructed from git tag-range
    commit analysis across rr_sootballs / sootballs_wms_interface / rr_lbc,
    cross-referenced against ADO Test Plans/Suites, with the backend product
    PR and test-automation PR looked up and real (non-skipped) test coverage
    verified against actual file content. Slack search resolved any gaps
    git/ADO evidence left open.

    Data comes from release_automation_status.yaml.
    Set release_automation_file in config.yaml or RELEASE_AUTOMATION_FILE env var.

    Response:
      status      ok | no_file_configured | file_not_found | parse_error: ...
      releases    dict {release: {tag_range, total, counted, automated,
                   written_not_merged, yet_to_automate, manually_verified,
                   not_automatable, excluded, unverified, automation_pct,
                   features: [...]}}
      totals      same status counts summed across all releases
    """
    return jsonify(get_release_automation_status(_RELEASE_AUTOMATION_FILE))


# ── Feature Coverage v2 (cache-driven — from traverse_feature_coverage.py) ───

@app.get("/api/feature_coverage_v2")
def feature_coverage_v2():
    """Return Feature automation coverage from the pre-built traversal cache.

    The cache is produced by:
      ADO_PAT=<pat> python3 qa_tooling/traverse_feature_coverage.py --apply

    Query params:
      sprint=<label>  Filter to a single sprint (e.g. "PI2 Sprint 4").
                      Omit to return all Features across all sprints.

    Response includes:
      cache_status    "ok" | "no_cache_configured" | "cache_file_not_found" | "parse_error"
      generated_at    ISO timestamp of when the cache was last built
      data_quality    Coverage and confidence breakdown (formal vs parsed PR links)
      summary         Headline numbers: total_features, coverage %, etc.
      features        List of Feature records with tc_ids, coverage %, engineers
      unresolved      Features where no PR link was found (need manual follow-up)
    """
    sprint = request.args.get("sprint")  # optional filter
    data = ado.get_feature_coverage_v2(_COVERAGE_CACHE_FILE, sprint_filter=sprint or None)
    return jsonify(data)


# ── Nightly CI Health (GitHub Actions API) ───────────────────────────────────────

@app.get("/api/nightly_health")
def nightly_health():
    """Return CI nightly run trends from GitHub Actions API.

    No Allure server OAuth needed — reads GitHub Actions workflow run history.
    Covers run_nightly_integration.yml and run_nightly_e2e.yml.

    Query params:
      n_runs  number of recent runs to fetch per workflow (default 30)

    Response per workflow:
      workflow_file   filename in .github/workflows/
      trend           pass_rate %, success/failure/cancelled counts
      runs            list of {run_id, date, conclusion, url, passed, failed, ...}
    """
    n_runs = int(request.args.get("n_runs", 30))
    data = get_nightly_health(_get_github_token(), n_runs=n_runs)
    return jsonify(data)


@app.get("/api/ci_stability")
def ci_stability():
    """Return CI Stability % for the nightly integration and e2e suites,
    computed from each run's Allure summary.json — the same artifact
    send_slack_report.yml reads to build the Slack nightly notification.

    Falls back to the most recent prior run with a usable summary if
    today's run hasn't produced one yet (crashed early, or hasn't run) —
    flagged per-workflow via "is_stale" + "date".

    Response:
      workflows.integration / workflows.e2e
        status        "ok" | "no_summary_found" | "no_completed_runs" | "error: ..."
        date          date of the run this data actually came from
        is_stale      true if that date isn't the most recent completed run
        passed/failed/broken/skipped/missing/considered/pass_pct
      combined_pass_pct   passed / considered, weighted across both suites
    """
    data = get_ci_stability(_get_github_token())
    return jsonify(data)


# ── Flaky Tests ───────────────────────────────────────────────────────────────────

@app.get("/api/flaky_tests")
def flaky_tests():
    """Return all tests decorated with @pytest.mark.flaky from sootballs_tests.

    Data comes from flaky_tests_report.json built by:
      python3 qa_tooling/scan_flaky_tests.py

    Set flaky_report_file in config.yaml or FLAKY_REPORT_FILE env var.

    Response:
      status          ok | no_report_configured | report_not_found
      total_flaky     count of @pytest.mark.flaky occurrences
      by_area         dict {test_area: count}
      entries         list of {file, line, class, function, reruns, added_date, test_area}
    """
    return jsonify(get_flaky_tests(_FLAKY_REPORT_FILE))


# ── CI Fix PR Coverage ────────────────────────────────────────────────────────────

@app.get("/api/ci_fix_coverage")
def ci_fix_coverage():
    """Return CI fix PR coverage from the snapshot — which fix PRs have AB# ADO links
    and which are unlinked (invisible to sprint tracking).

    Reads from the snapshot file built by build_snapshot.py.
    Set snapshot_file in config.yaml or SNAPSHOT_FILE env var.

    Response:
      total_fix_prs          total fix/* PRs in snapshot
      linked_count           PRs with AB# ADO links
      unlinked_count         PRs without any AB# link
      post_feb_linked        post-2026-02-01 fix PRs with AB# (enforcement era)
      post_feb_unlinked      post-2026-02-01 fix PRs WITHOUT AB# (enforcement gap)
      linked_prs             list of linked PRs with {number, title, merged_at, ado_wi_ids}
      unlinked_prs           list of unlinked PRs (need retroactive AB# or ADO task)
      wi_ids_to_tag          unique ADO WI IDs that should receive ci_fix tag
    """
    import json as _json
    from pathlib import Path as _Path

    if not _SNAPSHOT_FILE:
        return jsonify({"status": "no_snapshot_configured"}), 200

    snap_path = _Path(_SNAPSHOT_FILE)
    if not snap_path.is_file():
        return jsonify({"status": "snapshot_not_found", "path": str(snap_path)}), 200

    try:
        snap = _json.loads(snap_path.read_text())
    except Exception as exc:
        return jsonify({"status": f"parse_error: {exc}"}), 200

    prs = snap.get("github", {}).get("prs", [])
    fix_prs = [p for p in prs if p.get("title", "").lower().startswith("fix")]
    post_feb = [p for p in fix_prs if (p.get("merged_at") or "") >= "2026-02-01"]
    pre_feb  = [p for p in fix_prs if (p.get("merged_at") or "") <  "2026-02-01"]

    def _pr_summary(p: dict) -> dict:
        return {
            "number":     p["number"],
            "title":      p["title"],
            "merged_at":  p.get("merged_at", ""),
            "ado_wi_ids": p.get("ado_wi_ids", []),
            "url":        f"https://github.com/rapyuta-robotics/sootballs_tests/pull/{p['number']}",
        }

    linked   = [p for p in fix_prs if p.get("ado_wi_ids")]
    unlinked = [p for p in fix_prs if not p.get("ado_wi_ids")]
    post_linked   = [p for p in post_feb if p.get("ado_wi_ids")]
    post_unlinked = [p for p in post_feb if not p.get("ado_wi_ids")]

    all_wi_ids = sorted({wi for p in linked for wi in p.get("ado_wi_ids", [])})

    return jsonify({
        "status":             "ok",
        "snapshot_date":      snap.get("cutoff_date", ""),
        "total_fix_prs":      len(fix_prs),
        "linked_count":       len(linked),
        "unlinked_count":     len(unlinked),
        "link_rate_pct":      round(100 * len(linked) / len(fix_prs), 1) if fix_prs else 0,
        "post_feb_total":     len(post_feb),
        "post_feb_linked":    len(post_linked),
        "post_feb_unlinked":  len(post_unlinked),
        "pre_feb_total":      len(pre_feb),
        "wi_ids_to_tag":      all_wi_ids,
        "linked_prs":         [_pr_summary(p) for p in sorted(linked, key=lambda x: -x["number"])],
        "unlinked_prs":       [_pr_summary(p) for p in sorted(post_unlinked, key=lambda x: -x["number"])],
    })


# ── Test Area Breakdown ────────────────────────────────────────────────────────────

@app.get("/api/test_area_breakdown")
def test_area_breakdown():
    """Return breakdown of QA User Stories and Tasks by test area keyword.

    Maps ADO work item titles to test areas using keyword matching:
      picking, induction, pgs, replenishment, charge, zone, group,
      agent, tote, navigation, barcode, print, stats, order

    Reads from the snapshot file.
    """
    import json as _json
    from pathlib import Path as _Path

    _AREA_KEYWORDS = {
        "picking":       ["picking", "pick"],
        "induction":     ["induction", "induct"],
        "pgs":           ["pgs", "guiding", "picker guid"],
        "replenishment": ["replenishment", "replen"],
        "charge":        ["charge", "charging", "autodock"],
        "zone_picking":  ["zone picking", "zone_picking"],
        "group_picking": ["group picking", "group_picking", "improved group"],
        "navigation":    ["navigation", "waiting spot", "dynamic picking"],
        "tote":          ["tote", "scan", "barcode"],
        "order":         ["order", "priority", "csv", "print", "label"],
        "agent":         ["agent mode", "agent_mode", "fleet", "capacity"],
        "stats":         ["stats", "lpmh", "analytics"],
        "ci_infra":      ["ci", "nightly", "workflow", "allure", "runner", "playwright"],
    }

    if not _SNAPSHOT_FILE:
        return jsonify({"status": "no_snapshot_configured"}), 200

    snap_path = _Path(_SNAPSHOT_FILE)
    if not snap_path.is_file():
        return jsonify({"status": "snapshot_not_found"}), 200

    try:
        snap = _json.loads(snap_path.read_text())
    except Exception as exc:
        return jsonify({"status": f"parse_error: {exc}"}), 200

    us_list   = snap.get("ado", {}).get("user_stories", [])
    task_list = snap.get("ado", {}).get("tasks", [])

    def _classify(title: str) -> str:
        title_lower = (title or "").lower()
        for area, keywords in _AREA_KEYWORDS.items():
            if any(kw in title_lower for kw in keywords):
                return area
        return "other"

    us_by_area:   dict[str, int] = {}
    task_by_area: dict[str, int] = {}

    for us in us_list:
        area = _classify(us.get("title", ""))
        us_by_area[area] = us_by_area.get(area, 0) + 1
    for t in task_list:
        area = _classify(t.get("title", ""))
        task_by_area[area] = task_by_area.get(area, 0) + 1

    all_areas = sorted(set(list(us_by_area) + list(task_by_area)))
    breakdown = []
    for area in all_areas:
        us_cnt   = us_by_area.get(area, 0)
        task_cnt = task_by_area.get(area, 0)
        breakdown.append({
            "area":    area,
            "stories": us_cnt,
            "tasks":   task_cnt,
            "total":   us_cnt + task_cnt,
        })
    breakdown.sort(key=lambda x: -x["total"])

    return jsonify({
        "status":    "ok",
        "breakdown": breakdown,
        "total_us":  len(us_list),
        "total_tasks": len(task_list),
    })


# ── Cache invalidation ────────────────────────────────────────────────────────────

@app.post("/api/refresh")
def refresh_cache():
    ado.invalidate_cache()
    return jsonify({"status": "cache cleared"})


# ── SPA fallback ──────────────────────────────────────────────────────────────────

@app.get("/", defaults={"path": ""})
@app.get("/<path:path>")
def serve_spa(path):
    return send_from_directory(app.static_folder, "index.html")


# ── KPI helpers ───────────────────────────────────────────────────────────────────

def _canon_iter(path: str) -> str:
    """Normalise iteration path: collapse any double-backslash and lowercase."""
    return path.replace("\\\\", "\\").lower()


def _resolve_sprint(label: str | None):
    sprints = CFG.get("sprints") or []
    if not sprints:
        return None
    today = datetime.utcnow().date()
    if not label:
        for s in sprints:
            start = datetime.fromisoformat(s["start"]).date()
            end   = datetime.fromisoformat(s["end"]).date()
            if start <= today <= end:
                return s
        past = [s for s in sprints if datetime.fromisoformat(s["end"]).date() < today]
        return past[-1] if past else sprints[0]
    return next((s for s in sprints if s["label"] == label), None)


def _compute(user_stories: list[dict], bugs: list[dict], sprint: dict) -> dict:
    sprint_start = sprint["start"]
    sprint_end = sprint["end"]
    today = datetime.utcnow().date().isoformat()

    # ── User Story KPIs ──
    total_us = len(user_stories)
    completed_us = sum(1 for us in user_stories if us["state"] in US_DONE)
    completion_rate = round(completed_us / total_us * 100, 1) if total_us else 0

    total_pts = sum(us.get("story_points") or 0 for us in user_stories)
    done_pts = sum(
        us.get("story_points") or 0
        for us in user_stories if us["state"] in US_DONE
    )

    # Unplanned: created after sprint start
    unplanned = sum(
        1 for us in user_stories if us.get("created") and us["created"] > sprint_start
    )
    unplanned_rate = round(unplanned / total_us * 100, 1) if total_us else 0

    # Carry-over: created >14 days before sprint start → lived in a previous sprint
    carry_cutoff = (datetime.fromisoformat(sprint_start) - timedelta(days=14)).date().isoformat()
    carry_over = sum(
        1 for us in user_stories if us.get("created") and us["created"] < carry_cutoff
    )
    carry_over_rate = round(carry_over / total_us * 100, 1) if total_us else 0

    if sprint_end < today:
        slipped = sum(
            1 for us in user_stories if us.get("closed") and us["closed"] > sprint_end
        )
        still_open = sum(1 for us in user_stories if not us.get("closed"))
        slip_rate = round((slipped + still_open) / total_us * 100, 1) if total_us else 0
    else:
        slipped = sum(
            1 for us in user_stories if us.get("closed") and us["closed"] > sprint_end
        )
        slip_rate = round(slipped / total_us * 100, 1) if total_us else 0

    wip = sum(
        1 for us in user_stories
        if us["state"] in {"In Progress", "In Review", "QA Testing", "Active"}
    )
    wip_rate = round(wip / total_us * 100, 1) if total_us else 0
    state_dist = dict(Counter(us["state"] for us in user_stories).most_common())

    # ── Bug KPIs (exact match to this sprint's iteration) ──
    sprint_iter = _canon_iter(sprint["iteration_path"])
    sprint_bugs = [b for b in bugs if _canon_iter(b["iteration"]) == sprint_iter] or bugs
    total_bugs = len(sprint_bugs)
    resolved_bugs = sum(1 for b in sprint_bugs if b["state"] in BUG_DONE)
    bug_res_rate = round(resolved_bugs / total_bugs * 100, 1) if total_bugs else 0
    defect_density = round(total_bugs / total_us, 2) if total_us else 0

    # Lead time = created → resolved (falls back to closed if no ResolvedDate).
    # Median, not mean: a handful of multi-hundred-day legacy bugs otherwise
    # dominate the average and hide the real distribution.
    resolved_with_dates = [
        b for b in sprint_bugs
        if b.get("created") and (b.get("resolved") or b.get("closed"))
    ]
    mttr = None
    if resolved_with_dates:
        days = [
            (datetime.fromisoformat(b.get("resolved") or b["closed"]) - datetime.fromisoformat(b["created"])).days
            for b in resolved_with_dates
        ]
        mttr = round(statistics.median(days), 1)

    return {
        "sprint": sprint["label"],
        "pi": sprint.get("pi", ""),
        "start": sprint_start,
        "end": sprint_end,
        "total_user_stories": total_us,
        "completed_user_stories": completed_us,
        "completion_rate": completion_rate,
        "total_story_points": round(total_pts, 1),
        "completed_story_points": round(done_pts, 1),
        "unplanned_count": unplanned,
        "unplanned_rate": unplanned_rate,
        "carry_over_count": carry_over,
        "carry_over_rate": carry_over_rate,
        "slip_rate": slip_rate,
        "wip_count": wip,
        "wip_rate": wip_rate,
        "state_breakdown": state_dist,
        "bug_count": total_bugs,
        "resolved_bugs": resolved_bugs,
        "bug_resolution_rate": bug_res_rate,
        "defect_density": defect_density,
        "mttr_days": mttr,
    }


def _blank_eng(name: str) -> dict:
    return {"name": name, "stories": 0, "completed": 0, "points": 0.0}


if __name__ == "__main__":
    app.run(debug=_debug, port=5050, host="0.0.0.0")
