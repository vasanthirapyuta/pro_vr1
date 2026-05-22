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
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from ado_client import ADOClient

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

QA_MEMBERS: list[str] = CFG.get("qa_team_members", [])
QA_TAG: str = CFG.get("qa_tag", "sb_qa")
# Path to qa_tooling/ado_test_mapping.yaml from the sootballs_tests repo.
# Override via MAPPING_FILE env var or set sootballs_mapping_file in config.yaml.
_MAPPING_FILE: str | None = (
    os.environ.get("MAPPING_FILE")
    or CFG.get("sootballs_mapping_file")
)
# Path to feature_coverage_cache.json produced by traverse_feature_coverage.py.
_COVERAGE_CACHE_FILE: str | None = (
    os.environ.get("COVERAGE_CACHE_FILE")
    or CFG.get("feature_coverage_cache_file")
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

    user_stories = ado.get_user_stories(sprint["iteration_path"], QA_MEMBERS, QA_TAG)
    bugs = ado.get_bugs(sprint["iteration_path"], QA_MEMBERS, QA_TAG)
    return jsonify(_compute(user_stories, bugs, sprint))


# ── Trends (all past + current sprints) ─────────────────────────────────────────

@app.get("/api/kpi/trends")
def kpi_trends():
    today = datetime.utcnow().date()
    results = []
    for sprint in CFG["sprints"]:
        if datetime.fromisoformat(sprint["start"]).date() > today:
            continue
        user_stories = ado.get_user_stories(sprint["iteration_path"], QA_MEMBERS, QA_TAG)
        bugs = ado.get_bugs(sprint["iteration_path"], QA_MEMBERS, QA_TAG)
        results.append(_compute(user_stories, bugs, sprint))
    return jsonify(results)


# ── Per-engineer breakdown ───────────────────────────────────────────────────────

@app.get("/api/engineers")
def engineers():
    sprint = _resolve_sprint(request.args.get("sprint"))
    if sprint is None:
        return jsonify({"error": "Sprint not found"}), 404

    user_stories = ado.get_user_stories(sprint["iteration_path"], QA_MEMBERS, QA_TAG)
    bugs = ado.get_bugs(sprint["iteration_path"], QA_MEMBERS, QA_TAG)

    eng: dict[str, dict] = {}

    for us in user_stories:
        name = us.get("assignee") or "Unassigned"
        e = eng.setdefault(name, _blank_eng(name))
        e["stories"] += 1
        if us["state"] in US_DONE:
            e["completed"] += 1
        e["points"] += us.get("story_points") or 0

    sprint_iter = _canon_iter(sprint["iteration_path"])
    sprint_bugs = [b for b in bugs if _canon_iter(b["iteration"]) == sprint_iter] or bugs

    for bug in sprint_bugs:
        name = bug.get("created_by") or bug.get("assignee") or "Unassigned"
        e = eng.setdefault(name, _blank_eng(name))
        e["bugs_reported"] += 1
        if bug["state"] in BUG_DONE:
            e["bugs_resolved"] += 1

    rows = []
    for e in eng.values():
        e["completion_rate"] = (
            round(e["completed"] / e["stories"] * 100, 1) if e["stories"] else 0
        )
        e["bug_resolution_rate"] = (
            round(e["bugs_resolved"] / e["bugs_reported"] * 100, 1) if e["bugs_reported"] else 0
        )
        rows.append(e)

    return jsonify(sorted(rows, key=lambda x: -x["stories"]))


# ── Bug detail (single sprint) ───────────────────────────────────────────────────

@app.get("/api/bugs")
def bugs_detail():
    sprint = _resolve_sprint(request.args.get("sprint"))
    if sprint is None:
        return jsonify({"error": "Sprint not found"}), 404

    bugs = ado.get_bugs(sprint["iteration_path"], QA_MEMBERS, QA_TAG)

    sprint_iter = _canon_iter(sprint["iteration_path"])
    sprint_bugs = [b for b in bugs if _canon_iter(b["iteration"]) == sprint_iter] or bugs

    closed = [
        b for b in sprint_bugs
        if b["state"] in BUG_DONE and b.get("closed") and b.get("created")
    ]
    mttr = None
    if closed:
        days = [
            (datetime.fromisoformat(b["closed"]) - datetime.fromisoformat(b["created"])).days
            for b in closed
        ]
        mttr = round(sum(days) / len(days), 1)

    return jsonify({
        "sprint": sprint["label"],
        "total": len(sprint_bugs),
        "resolved": sum(1 for b in sprint_bugs if b["state"] in BUG_DONE),
        "state_distribution": dict(Counter(b["state"] for b in sprint_bugs).most_common()),
        "priority_distribution": dict(
            Counter(str(b.get("priority") or "—") for b in sprint_bugs).most_common()
        ),
        "by_reporter": dict(
            Counter(
                b.get("created_by") or b.get("assignee") or "Unknown"
                for b in sprint_bugs
            ).most_common()
        ),
        "mttr_days": mttr,
        "items": [
            {
                "id": b["id"],
                "title": b["title"][:80],
                "state": b["state"],
                "priority": b.get("priority"),
                "reporter": b.get("created_by") or b.get("assignee"),
                "created": b.get("created"),
                "closed": b.get("closed"),
            }
            for b in sorted(sprint_bugs, key=lambda x: x.get("created") or "", reverse=True)[:60]
        ],
    })


# ── Test Plans (Tier 2 — sootballs project) ──────────────────────────────────────

@app.get("/api/testplans")
def testplans():
    plans = ado.get_test_plans()
    tc_summary = ado.get_test_case_summary()
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
        "test_case_summary": tc_summary,
        "source_project": CFG["ado"]["testplans_project"],
        "migration_pending": True,
        "migration_note": (
            "Test Plans currently reside in the 'sootballs' ADO project. "
            "Migration to the 'AMR' project is a tracked initiative (see TIER3_CHANGES.md). "
            "Once migrated, sprint-level automation metrics will be linked directly to sprint iterations."
        ),
    })


# ── Automation Coverage (Tier 2 — sootballs TCs + AMR WIs) ───────────────────────

@app.get("/api/automation_coverage")
def automation_coverage():
    """Return test automation coverage combining three data sources:
      - ado_test_mapping.yaml  (pytest ↔ TC linkage, confirmed + TODO counts)
      - ADO sootballs          (live AutomationStatus per TC)
      - ADO AMR (sb_qa tagged) (Work Items driving the automation backlog)
    """
    data = ado.get_automation_coverage(_MAPPING_FILE)
    return jsonify({
        **data,
        "mapping_file": _MAPPING_FILE,
    })


# ── Feature Coverage (Feature/Story → TC Automation) ─────────────────────────

@app.get("/api/feature_coverage")
def feature_coverage():
    """Return Feature-level TC automation coverage for a sprint.

    Each Feature/User Story in the sprint shows:
      - total TCs linked via 'Tested By' relation
      - how many are Automated
      - coverage percentage
    """
    sprint = _resolve_sprint(request.args.get("sprint"))
    if sprint is None:
        return jsonify({"error": "Sprint not found"}), 404

    data = ado.get_feature_coverage(sprint["iteration_path"], QA_MEMBERS, QA_TAG)
    total_features = len(data)
    fully_covered  = sum(1 for f in data if f["coverage_pct"] == 100 and f["total_tcs"] > 0)
    no_tcs         = sum(1 for f in data if f["total_tcs"] == 0)
    total_tcs      = sum(f["total_tcs"] for f in data)
    automated_tcs  = sum(f["automated_tcs"] for f in data)

    return jsonify({
        "sprint": sprint["label"],
        "summary": {
            "total_features": total_features,
            "fully_covered": fully_covered,
            "partially_covered": total_features - fully_covered - no_tcs,
            "no_tcs_linked": no_tcs,
            "total_tcs": total_tcs,
            "automated_tcs": automated_tcs,
            "overall_coverage_pct": round(automated_tcs / total_tcs * 100, 1) if total_tcs else 0,
        },
        "items": data,
    })


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


# ── Engineer Automation (who automated what) ──────────────────────────────────

@app.get("/api/engineer_automation")
def engineer_automation():
    """Return per-engineer automation counts from ado_test_mapping.yaml github_pr field."""
    data = ado.get_engineer_automation(_MAPPING_FILE)
    total_tcs = sum(e["tcs_automated"] for e in data)
    return jsonify({
        "total_tcs_attributed": total_tcs,
        "engineers": data,
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
    """Normalise iteration path to single backslashes for exact comparison."""
    return path.replace("\\\\", "\\")


def _resolve_sprint(label: str | None):
    today = datetime.utcnow().date()
    if not label:
        for s in CFG["sprints"]:
            start = datetime.fromisoformat(s["start"]).date()
            end = datetime.fromisoformat(s["end"]).date()
            if start <= today <= end:
                return s
        past = [s for s in CFG["sprints"] if datetime.fromisoformat(s["end"]).date() < today]
        return past[-1] if past else CFG["sprints"][0]
    return next((s for s in CFG["sprints"] if s["label"] == label), None)


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

    closed_bugs = [
        b for b in sprint_bugs
        if b["state"] in BUG_DONE and b.get("closed") and b.get("created")
    ]
    mttr = None
    if closed_bugs:
        days = [
            (datetime.fromisoformat(b["closed"]) - datetime.fromisoformat(b["created"])).days
            for b in closed_bugs
        ]
        mttr = round(sum(days) / len(days), 1)

    bug_state_dist = dict(Counter(b["state"] for b in sprint_bugs).most_common())
    health = _health_score(completion_rate, bug_res_rate, unplanned_rate, carry_over_rate)

    return {
        "sprint": sprint["label"],
        "pi": sprint.get("pi", ""),
        "start": sprint_start,
        "end": sprint_end,
        "health_score": health,
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
        "bug_state_breakdown": bug_state_dist,
    }


def _health_score(completion_rate, bug_res_rate, unplanned_rate, carry_over_rate) -> int:
    def score(val, thresholds, invert=False):
        hi, med, lo = thresholds
        if invert:
            return 100 if val <= lo else (75 if val <= med else (50 if val <= hi else 25))
        return 100 if val >= hi else (75 if val >= med else (50 if val >= lo else 25))

    c = score(completion_rate,  (80, 60, 40))
    b = score(bug_res_rate,     (70, 40, 20))
    u = score(unplanned_rate,   (50, 30, 15), invert=True)
    k = score(carry_over_rate,  (40, 25, 10), invert=True)
    return int(0.35 * c + 0.25 * b + 0.20 * u + 0.20 * k)


def _blank_eng(name: str) -> dict:
    return {
        "name": name, "stories": 0, "completed": 0, "points": 0.0,
        "bugs_reported": 0, "bugs_resolved": 0,
    }


if __name__ == "__main__":
    app.run(debug=_debug, port=5050, host="0.0.0.0")
