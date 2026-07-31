"""
Azure DevOps REST API client for the QA Metrics Dashboard.

Filtering strategy (confirmed from live ADO data, 2026-05-11):
  - User Stories  → filter by AssignedTo (QA member is doing the testing work)
  - Bugs          → NOT filtered by reporter/assignee: bug health metrics
                    (created-vs-resolved flow, lead time, aging) care about all of this
                    team's bugs in the PI, not just ones a QA member happened to file.
                    Filtered instead by the `sb-bug` tag (added 2026-07-30) to scope out
                    other teams' bugs sharing the same ADO project — confirmed live that
                    date: 80 of 145 PI bugs carry it, the rest carry unrelated
                    release/customer tags (e.g. `SB_3.5_bugs`) or none at all
  - Both          → OR-condition: item tagged `sb_qa` is always included regardless of assignee
  - Story points  → absent on ~75 % of items in this project; count-based metrics are primary
  - TargetDate    → universally unset; sprint end date used as implicit deadline
  - CompletedWork → universally unset; test execution hours not currently derivable
  - Severity/Priority → set on <2% of recent Bugs (verified 2026-07-10); not usable for
                    weighted metrics until the team adopts the field

Test Plans live in the `sootballs` project.  The same PAT works cross-project.
Suite-level testCaseCount returns 0 via the suites API; test cases are queried
via WIQL on WorkItemType='Test Case' instead.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date as _date
from typing import Any

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_ADO_ROOT = "https://dev.azure.com/rapyuta-robotics"
_BATCH_SIZE = 200   # ADO hard limit for workitemsbatch
_API_VERSION = "7.0"


def _build_session(pat: str) -> requests.Session:
    session = requests.Session()
    session.auth = ("", pat)
    session.headers.update({"Content-Type": "application/json"})
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        # Do NOT retry 500 — ADO returns HTML 500 "looping logins" on PAT auth issues
        # and retrying only worsens the loop. Only retry transient gateway errors.
        status_forcelist={429, 502, 503, 504},
        allowed_methods={"GET", "POST"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


class ADOClient:
    def __init__(self, pat: str, project: str, testplans_project: str, cache_ttl: int = 600):
        self.project = project
        self.testplans_project = testplans_project
        self._session = _build_session(pat)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_user_stories(self, iteration_path: str, qa_members: list[str], qa_tag: str) -> list[dict]:
        """Return User Stories in the sprint assigned to a QA member or tagged qa_tag."""
        norm_members = sorted({m.strip() for m in qa_members if m and m.strip()})
        cache_key = self._make_key("user_stories", iteration_path, tuple(norm_members), qa_tag)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # Route to the correct ADO project based on the iteration path prefix.
        # sootballs\... paths live in the sootballs project; everything else in AMR.
        project = "sootballs" if iteration_path.lower().startswith("sootballs") else self.project
        member_clause = self._member_or_tag_clause(
            field="[System.AssignedTo]", members=norm_members, tag=qa_tag
        )
        query = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{_esc(project)}'
              AND [System.IterationPath] = '{_esc(iteration_path)}'
              AND [System.WorkItemType] = 'User Story'
              {('AND (' + member_clause + ')') if member_clause else ''}
            ORDER BY [System.Id]
        """
        ids = self._wiql(project, query)
        if not ids:
            result = []
        else:
            fields = [
                "System.Id", "System.Title", "System.State",
                "System.WorkItemType", "System.AssignedTo",
                "System.CreatedDate", "System.IterationPath", "System.Tags",
                "Microsoft.VSTS.Scheduling.StoryPoints",
                "Microsoft.VSTS.Common.ClosedDate",
            ]
            result = [self._normalise(item) for item in self._batch_fetch(project, ids, fields)]

        self._set_cached(cache_key, result)
        return result

    def get_bugs(self, iteration_path: str) -> list[dict]:
        """Return all Bugs in the PI containing this iteration tagged 'sb-bug',
        regardless of who filed or is assigned to them — bug health is a
        team-wide signal, not a per-reporter one. The sb-bug tag scopes this
        team's bugs out of the shared ADO project (confirmed live 2026-07-30:
        80 of 145 PI bugs carry it; the rest carry unrelated release/customer
        tags like 'SB_3.5_bugs' or none at all)."""
        cache_key = self._make_key("bugs", iteration_path)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # All bugs live in the AMR project now (migrated from sootballs) —
        # unlike get_user_stories, no sootballs-project routing branch applies here.
        project = self.project
        pi_path = "\\".join(iteration_path.split("\\")[:2])
        query = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{_esc(project)}'
              AND [System.IterationPath] UNDER '{_esc(pi_path)}'
              AND [System.WorkItemType] = 'Bug'
              AND [System.Tags] CONTAINS 'sb-bug'
            ORDER BY [System.Id]
        """
        ids = self._wiql(project, query)
        if not ids:
            result = []
        else:
            fields = [
                "System.Id", "System.Title", "System.State",
                "System.WorkItemType", "System.AssignedTo", "System.CreatedBy",
                "System.CreatedDate", "System.IterationPath",
                "Microsoft.VSTS.Common.ResolvedDate",
                "Microsoft.VSTS.Common.ClosedDate",
                "Microsoft.VSTS.Common.Priority",
                "System.AreaPath",
                "System.Tags",
            ]
            result = [self._normalise(item) for item in self._batch_fetch(project, ids, fields)]

        self._set_cached(cache_key, result)
        return result

    def get_test_plans(self) -> list[dict]:
        url = f"{_ADO_ROOT}/{self.testplans_project}/_apis/testplan/plans"
        return self._cached_get(url, {"api-version": _API_VERSION}).get("value", [])

    def get_feature_coverage_v2(
        self,
        cache_file: str | None,
        sprint_filter: str | None = None,
    ) -> dict:
        """
        Serve Feature coverage data from the pre-built cache file produced by
        qa_tooling/traverse_feature_coverage.py.

        No ADO API calls are made at query time — this is purely cache-driven.
        The cache is refreshed by running traverse_feature_coverage.py --apply
        (nightly CI job or manual run).

        Args:
            cache_file:    Path to feature_coverage_cache.json.  None → return empty result.
            sprint_filter: If given, filter features list to those whose 'sprint' field
                           matches this label exactly.  None → return all features.

        Returns a dict with keys:
          generated_at, data_quality, features (filtered), unresolved,
          cache_status, sprint_filter_applied
        """
        import json as _json
        from pathlib import Path as _Path

        _empty = {
            "generated_at": None,
            "data_quality": {},
            "features": [],
            "unresolved": [],
            "cache_status": "no_cache_configured",
            "sprint_filter_applied": sprint_filter,
        }

        if not cache_file:
            return {**_empty, "cache_status": "no_cache_configured"}

        path = _Path(cache_file)
        if not path.is_file():
            return {**_empty, "cache_status": "cache_file_not_found",
                    "cache_file": str(path)}

        try:
            data = _json.loads(path.read_text())
        except Exception as exc:
            logger.warning("Failed to parse coverage cache %s: %s", path, exc)
            return {**_empty, "cache_status": "parse_error"}

        features = data.get("features", [])
        unresolved = data.get("unresolved", [])

        if sprint_filter:
            features   = [f for f in features   if f.get("sprint") == sprint_filter]
            unresolved = [u for u in unresolved if u.get("sprint") == sprint_filter]

        # Recompute summary over filtered set
        total_features   = len(features)
        fully_covered    = sum(1 for f in features if f.get("coverage_pct", 0) == 100
                               and f.get("total_tcs", 0) > 0)
        partially_covered = sum(1 for f in features if 0 < f.get("coverage_pct", 0) < 100)
        no_tcs_linked    = sum(1 for f in features if f.get("total_tcs", 0) == 0)
        total_tcs        = sum(f.get("total_tcs", 0) for f in features)
        automated_tcs    = sum(f.get("automated_count", 0) for f in features)
        formal_links     = sum(1 for f in features if f.get("pr_confidence") == "formal")
        parsed_links     = sum(1 for f in features if f.get("pr_confidence") == "parsed")

        return {
            "generated_at":         data.get("generated_at"),
            "query":                data.get("query", {}),
            "data_quality":         data.get("data_quality", {}),
            "cache_status":         "ok",
            "sprint_filter_applied": sprint_filter,
            "summary": {
                "total_features":     total_features,
                "fully_covered":      fully_covered,
                "partially_covered":  partially_covered,
                "no_tcs_linked":      no_tcs_linked,
                "total_tcs":          total_tcs,
                "automated_tcs":      automated_tcs,
                "overall_coverage_pct": (
                    round(automated_tcs / total_tcs * 100, 1) if total_tcs else 0
                ),
                "formal_pr_links":    formal_links,
                "parsed_pr_links":    parsed_links,
                "unresolved_features": len(unresolved),
            },
            "features":   features,
            "unresolved": unresolved,
        }

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache.clear()
        logger.info("ADO cache cleared")

    # ── Cache helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _make_key(*parts: Any) -> str:
        raw = repr(parts)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Any | None:
        with self._lock:
            entry = self._cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts < self._cache_ttl:
            return data
        return None

    def _set_cached(self, key: str, data: Any) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic(), data)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _wiql(self, project: str, query: str) -> list[int]:
        url = f"{_ADO_ROOT}/{project}/_apis/wit/wiql"
        try:
            resp = self._session.post(
                url,
                json={"query": query},
                params={"api-version": _API_VERSION},
                timeout=30,
            )
            if not resp.ok:
                logger.warning("WIQL %s status %s: %s", project, resp.status_code, resp.text[:120])
                return []
            ids = [w["id"] for w in resp.json().get("workItems", [])]
        except Exception as exc:
            logger.warning("WIQL %s failed (%s) — returning empty", project, exc)
            return []
        logger.debug("WIQL returned %d ids from %s", len(ids), project)
        return ids

    def _batch_fetch(self, project: str, ids: list[int], fields: list[str]) -> list[dict]:
        results = []
        for i in range(0, len(ids), _BATCH_SIZE):
            chunk = ids[i:i + _BATCH_SIZE]
            url = f"{_ADO_ROOT}/{project}/_apis/wit/workitemsbatch"
            resp = self._session.post(
                url,
                json={"ids": chunk, "fields": fields},
                params={"api-version": _API_VERSION},
                timeout=30,
            )
            resp.raise_for_status()
            results.extend(resp.json().get("value", []))
        return results

    def _cached_get(self, url: str, params: dict | None = None) -> Any:
        key = self._make_key("GET", url, params)
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self._set_cached(key, data)
        return data

    @staticmethod
    def _member_or_tag_clause(field: str, members: list[str], tag: str) -> str:
        parts = [f"{field} = '{_esc(m)}'" for m in members if m.strip()]
        if tag:
            parts.append(f"[System.Tags] CONTAINS '{_esc(tag)}'")
        return " OR ".join(parts)

    @staticmethod
    def _normalise(item: dict) -> dict:
        f = item["fields"]
        assignee = f.get("System.AssignedTo") or {}
        created_by = f.get("System.CreatedBy") or {}
        return {
            "id": f.get("System.Id"),
            "title": f.get("System.Title", ""),
            "type": f.get("System.WorkItemType", ""),
            "state": _canonical_state(f.get("System.State", "")),
            "assignee": (
                assignee.get("displayName", "") if isinstance(assignee, dict)
                else str(assignee or "")
            ),
            "created_by": (
                created_by.get("displayName", "") if isinstance(created_by, dict)
                else str(created_by or "")
            ),
            "created": (f.get("System.CreatedDate") or "")[:10],
            "resolved": (f.get("Microsoft.VSTS.Common.ResolvedDate") or "")[:10],
            "closed": (f.get("Microsoft.VSTS.Common.ClosedDate") or "")[:10],
            "story_points": f.get("Microsoft.VSTS.Scheduling.StoryPoints"),
            "tags": f.get("System.Tags", "") or "",
            "iteration": f.get("System.IterationPath", ""),
            "area": f.get("System.AreaPath", "") or "",
            "priority": f.get("Microsoft.VSTS.Common.Priority"),
        }


def _esc(value: str) -> str:
    """Escape a value for safe embedding in a single-quoted WIQL string."""
    return value.replace("'", "''")


# ADO lets users/automation free-type a work item's state box in some views,
# so the same logical state ("Ready To Test") shows up under multiple
# casings in raw System.State data. Every consumer of "state" downstream
# (state_distribution, BUG_DONE/US_DONE membership) compares by exact string,
# so an uncanonicalized casing variant silently fragments a KPI bucket
# instead of erroring — canonicalize once, here, at ingestion.
_CANONICAL_STATES = [
    "Reported", "Assigned", "In Progress", "In Review", "Ready To Test",
    "Postponed", "Waiting for Release", "Resolved", "Closed", "Completed",
    "Duplicate", "Not a Bug", "Blocked",
]
_STATE_BY_CASEFOLD = {s.casefold(): s for s in _CANONICAL_STATES}


def _canonical_state(raw: str) -> str:
    """Fold known-state casing variants to one canonical spelling.
    An unrecognized state passes through unchanged, so a genuinely new ADO
    workflow state is never hidden or dropped, only left uncanonicalized."""
    raw = (raw or "").strip()
    return _STATE_BY_CASEFOLD.get(raw.casefold(), raw)


# ── GitHub nightly health helper ──────────────────────────────────────────────


def get_nightly_health(github_token: str | None, n_runs: int = 30) -> dict:
    """
    Fetch nightly CI health data from GitHub Actions API.

    Returns pass/fail trends for the nightly integration and e2e workflows
    without requiring Allure server OAuth.

    Workflow files targeted:
      run_nightly_integration.yml  → integration test suite (1,200 tests)
      run_nightly_e2e.yml          → E2E browser test suite

    For each of the last n_runs:
      - run_id, date, status (success/failure/cancelled)
      - test counts if available from the workflow run annotation/artifact
    """
    import time as _time

    _GH_API  = "https://api.github.com"
    _GH_REPO = "rapyuta-robotics/sootballs_tests"

    headers: dict = {"Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    import requests as _req
    sess = _req.Session()
    sess.headers.update(headers)

    _WORKFLOWS = {
        "integration": "run_nightly_integration.yml",
        "e2e":         "run_nightly_e2e.yml",
    }

    result: dict = {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
        "workflows":    {},
    }

    for label, wf_file in _WORKFLOWS.items():
        runs_data: list[dict] = []
        try:
            url = f"{_GH_API}/repos/{_GH_REPO}/actions/workflows/{wf_file}/runs"
            r = sess.get(url, params={"per_page": n_runs, "status": "completed"}, timeout=20)
            if not r.ok:
                result["workflows"][label] = {"error": f"GitHub {r.status_code}"}
                continue

            for run in r.json().get("workflow_runs", []):
                entry: dict = {
                    "run_id":     run["id"],
                    "date":       (run.get("created_at") or "")[:10],
                    "conclusion": run.get("conclusion", ""),   # success / failure / cancelled
                    "status":     run.get("status", ""),
                    "url":        run.get("html_url", ""),
                    "branch":     run.get("head_branch", ""),
                    # Test counts populated below if artifact available
                    "passed":  None,
                    "failed":  None,
                    "skipped": None,
                    "total":   None,
                }
                runs_data.append(entry)
                _time.sleep(0.05)

        except Exception as exc:
            result["workflows"][label] = {"error": str(exc)}
            continue

        # Try to enrich the most recent run with test counts from artifacts
        if runs_data:
            latest = runs_data[0]
            try:
                art_url = f"{_GH_API}/repos/{_GH_REPO}/actions/runs/{latest['run_id']}/artifacts"
                ar = sess.get(art_url, timeout=15)
                if ar.ok:
                    artifacts = ar.json().get("artifacts", [])
                    allure_art = next(
                        (a for a in artifacts
                         if "allure" in (a.get("name") or "").lower()),
                        None,
                    )
                    if allure_art:
                        latest["allure_artifact_id"] = allure_art["id"]
                        latest["allure_artifact_size_mb"] = round(
                            allure_art.get("size_in_bytes", 0) / 1_048_576, 1
                        )
            except Exception:
                pass

        # Build pass/fail trend summary
        total_runs     = len(runs_data)
        success_runs   = sum(1 for r in runs_data if r["conclusion"] == "success")
        failure_runs   = sum(1 for r in runs_data if r["conclusion"] == "failure")
        cancelled_runs = sum(1 for r in runs_data if r["conclusion"] == "cancelled")

        result["workflows"][label] = {
            "workflow_file": wf_file,
            "runs_fetched":  total_runs,
            "trend": {
                "success":   success_runs,
                "failure":   failure_runs,
                "cancelled": cancelled_runs,
                "pass_rate": round(100 * success_runs / total_runs, 1) if total_runs else 0,
            },
            "runs": runs_data,
        }

    return result


class _ArtifactNoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib handler that captures a redirect URL instead of following it —
    GitHub's artifact-zip endpoint 302/303s to a time-limited Azure Blob SAS
    URL that must be fetched WITHOUT the GitHub Authorization header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_artifact_cdn_url(repo: str, artifact_id: int, token: str | None) -> str | None:
    api_url = f"https://api.github.com/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    opener = urllib.request.build_opener(_ArtifactNoRedirect)
    req_headers = {"Accept": "application/vnd.github+json"}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(api_url, headers=req_headers)
    try:
        with opener.open(req):
            return None  # 200 means no redirect — unexpected
    except urllib.error.HTTPError as exc:
        if exc.code in (302, 303):
            return exc.headers.get("Location")
        return None
    except urllib.error.URLError:
        return None


def _list_allure_summary_artifacts(sess: requests.Session, repo: str, run_id: int) -> list[dict]:
    """Return non-expired allure-summary-* artifacts for one run, paginating
    through the full artifact list. A single nightly run can carry 150-250+
    artifacts (per-shard results, per-shard logs, videos) once the summary
    file is just one entry among them — a single per_page=100 page is NOT
    enough (confirmed against live runs: 181 artifacts on a recent e2e run,
    253 on a recent integration run), so relying on page 1 alone risks
    silently missing the summary and falling back to a stale day for no
    real reason."""
    matches: list[dict] = []
    page = 1
    while page <= 5:  # 500-artifact backstop against runaway pagination
        try:
            resp = sess.get(
                f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts",
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            if not resp.ok:
                break
            batch = resp.json().get("artifacts", [])
        except Exception:
            break
        if not batch:
            break
        matches.extend(
            a for a in batch
            if not a.get("expired") and (a.get("name") or "").startswith("allure-summary")
        )
        if len(batch) < 100:
            break
        page += 1
    return matches


def _download_allure_summary(repo: str, artifact_id: int, token: str | None) -> dict | None:
    """Download one allure-summary-* artifact ZIP and return its parsed
    widgets/summary.json, or None if unavailable/unparseable."""
    cdn_url = _get_artifact_cdn_url(repo, artifact_id, token)
    if not cdn_url:
        return None
    try:
        # No Authorization header on the CDN request — the SAS token in the
        # URL rejects requests carrying an extra auth header.
        with urllib.request.urlopen(urllib.request.Request(cdn_url), timeout=60) as resp:
            zip_bytes = resp.read()
    except (urllib.error.URLError, OSError):
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            name = next((n for n in zf.namelist() if n.endswith("summary.json")), None)
            if not name:
                return None
            return json.loads(zf.read(name))
    except Exception:
        # Covers BadZipFile, JSONDecodeError, and corrupt-entry errors
        # (e.g. zlib.error) that zipfile can raise on a truncated download —
        # any of these just means "this artifact isn't usable," not a crash.
        return None


_CI_STABILITY_CACHE_TTL = 1800  # 30 min — nightly data only changes once a day; this just
                                # avoids repeating a multi-request GitHub walk + artifact
                                # download on every Overview page load.
_ci_stability_cache: dict[str, tuple[float, dict]] = {}
_ci_stability_lock = threading.Lock()


def _get_ci_stability_cached() -> dict | None:
    with _ci_stability_lock:
        entry = _ci_stability_cache.get("data")
    if entry is None:
        return None
    ts, data = entry
    return data if time.monotonic() - ts < _CI_STABILITY_CACHE_TTL else None


def _set_ci_stability_cache(data: dict) -> None:
    with _ci_stability_lock:
        _ci_stability_cache["data"] = (time.monotonic(), data)


def get_ci_stability(github_token: str | None, max_lookback: int = 10) -> dict:
    """Return CI Stability % for the nightly integration and e2e suites,
    computed from each run's Allure summary.json (the same artifact
    send_slack_report.yml reads to build the Slack nightly notification —
    using it here means this number always matches what's already posted
    in Slack, by construction).

    For each workflow, walks backward from the most recent completed run
    until it finds one with a usable, correctly-type-stamped
    allure-summary-* artifact (mirrors the has_summary / SUMMARY_TYPE
    validation in .github/workflows/send_slack_report.yml). If the most
    recent run has no usable summary (crashed before producing one, or
    hasn't run yet today), falls back to the next most recent and marks
    the result as stale with the actual date used.

    Stability % = passed / (passed + failed + broken + missing) * 100.
    Skipped tests are excluded from the denominator (not a stability
    signal); missing tests (the summary's "unknown" bucket — crashed/
    cancelled/timed-out, already crash-padded upstream) count against
    stability, since a crashed run is not a neutral outcome.

    Cached for _CI_STABILITY_CACHE_TTL: this run walks completed-run
    history and downloads an artifact ZIP per workflow, so a cache-free
    version would repeat that full round-trip on every Overview page load
    even though nightly data only actually changes once a day.
    """
    cached = _get_ci_stability_cached()
    if cached is not None:
        return cached

    _GH_REPO = "rapyuta-robotics/sootballs_tests"
    _WORKFLOWS = {
        "integration": ("run_nightly_integration.yml", "Integration"),
        "e2e":         ("run_nightly_e2e.yml", "E2E"),
    }

    headers: dict = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    sess = requests.Session()
    sess.headers.update(headers)

    def _suite_stats(stat: dict) -> dict:
        passed  = stat.get("passed", 0) or 0
        failed  = stat.get("failed", 0) or 0
        broken  = stat.get("broken", 0) or 0
        skipped = stat.get("skipped", 0) or 0
        missing = stat.get("unknown", 0) or 0
        considered = passed + failed + broken + missing
        return {
            "passed": passed, "failed": failed, "broken": broken,
            "skipped": skipped, "missing": missing, "considered": considered,
            "pass_pct": round(passed / considered * 100, 1) if considered else None,
        }

    result: dict = {"generated_at": __import__("datetime").datetime.utcnow().isoformat(), "workflows": {}}

    for label, (wf_file, type_stamp) in _WORKFLOWS.items():
        try:
            runs_resp = sess.get(
                f"https://api.github.com/repos/{_GH_REPO}/actions/workflows/{wf_file}/runs",
                params={"per_page": max_lookback, "status": "completed"},
                timeout=20,
            )
            runs = runs_resp.json().get("workflow_runs", []) if runs_resp.ok else []
        except Exception as exc:
            result["workflows"][label] = {"status": f"error: {exc}"}
            continue

        if not runs:
            result["workflows"][label] = {"status": "no_completed_runs"}
            continue

        newest_date = (runs[0].get("created_at") or "")[:10]
        found = None
        found_date = None
        for run in runs:
            candidates = _list_allure_summary_artifacts(sess, _GH_REPO, run["id"])
            for a in candidates:
                try:
                    summary = _download_allure_summary(_GH_REPO, a["id"], github_token)
                except Exception:
                    # A single flaky download shouldn't sink the whole endpoint —
                    # treat it the same as "no usable summary" and keep looking.
                    summary = None
                if not summary:
                    continue
                stamp = summary.get("nightly_test_type") or ""
                if stamp and stamp != type_stamp:
                    continue  # scoped to the other suite (e.g. a shared combined-run summary)
                found = summary
                found_date = (run.get("created_at") or "")[:10]
                break
            if found:
                break

        if not found:
            result["workflows"][label] = {"status": "no_summary_found", "checked_runs": len(runs)}
            continue

        stats = _suite_stats(found.get("statistic", {}))
        result["workflows"][label] = {
            "status": "ok",
            "date": found_date,
            "is_stale": found_date != newest_date,
            **stats,
        }

    ok_suites = [w for w in result["workflows"].values() if w.get("status") == "ok"]
    total_passed = sum(w["passed"] for w in ok_suites)
    total_considered = sum(w["considered"] for w in ok_suites)
    result["combined_pass_pct"] = (
        round(total_passed / total_considered * 100, 1) if total_considered else None
    )
    _set_ci_stability_cache(result)
    return result


def get_flaky_tests(flaky_report_path: str | None) -> dict:
    """Read the pre-built flaky_tests_report.json from scan_flaky_tests.py."""
    import json as _json

    if not flaky_report_path:
        return {"status": "no_report_configured", "total_flaky": 0, "entries": []}

    from pathlib import Path as _Path
    p = _Path(flaky_report_path)
    if not p.is_file():
        return {"status": "report_not_found", "path": str(p), "total_flaky": 0, "entries": []}

    try:
        data = _json.loads(p.read_text())
        return {"status": "ok", **data}
    except Exception as exc:
        return {"status": f"parse_error: {exc}", "total_flaky": 0, "entries": []}


_RELEASE_AUTOMATION_STATUSES = (
    "automated", "written_not_merged", "yet_to_automate",
    "manually_verified", "not_automatable", "excluded", "unverified",
)


def get_release_automation_status(release_automation_file: str | None) -> dict:
    """Read release_automation_status.yaml — the release-wise feature automation
    master table (Feature -> ADO Suite ID -> Backend PR -> Test PR -> Status),
    built by reconstructing each release's real feature content from git
    tag-range commit analysis across rr_sootballs / sootballs_wms_interface /
    rr_lbc, cross-referencing ADO Test Plans/Suites, and verifying real
    (non-skipped) test coverage — with Slack used to resolve any gaps git/ADO
    left open.

    "excluded" features (not actually a new distinct feature for that release)
    are shown in the table but excluded from automation_pct's denominator so
    they don't understate real coverage.
    """
    from pathlib import Path as _Path

    if not release_automation_file:
        return {"status": "no_file_configured", "releases": {}}

    p = _Path(release_automation_file)
    if not p.is_file():
        return {"status": "file_not_found", "path": str(p), "releases": {}}

    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception as exc:
        return {"status": f"parse_error: {exc}", "releases": {}}

    releases = {}
    total_counts = {s: 0 for s in _RELEASE_AUTOMATION_STATUSES}
    for release, rel_data in (data.get("releases") or {}).items():
        features = rel_data.get("features") or []
        counts = {s: 0 for s in _RELEASE_AUTOMATION_STATUSES}
        for f in features:
            status = f.get("status", "unverified")
            counts[status] = counts.get(status, 0) + 1
            total_counts[status] = total_counts.get(status, 0) + 1

        total = len(features)
        counted = total - counts["excluded"]
        releases[release] = {
            "tag_range": rel_data.get("tag_range", ""),
            "total": total,
            "counted": counted,
            **counts,
            "automation_pct": round(counts["automated"] / counted * 100, 1) if counted else 0,
            "features": features,
        }

    total = sum(r["total"] for r in releases.values())
    counted = total - total_counts["excluded"]
    return {
        "status": "ok",
        "releases": releases,
        "totals": {
            "total": total,
            "counted": counted,
            **total_counts,
            "automation_pct": round(total_counts["automated"] / counted * 100, 1) if counted else 0,
        },
    }


def resolve_qa_members(cfg: dict, sprint_start: str | None = None, sprint_end: str | None = None) -> list[str]:
    """Return QA team members active during the given sprint date range.

    Uses qa_team_roster (date-range entries) when present; falls back to
    the flat qa_team_members list.

    Args:
        cfg:          Parsed config.yaml dict.
        sprint_start: ISO date string (YYYY-MM-DD) — first day of the sprint.
                      Pass None to get all members ever on the team (union).
        sprint_end:   ISO date string (YYYY-MM-DD) — last day of the sprint.
                      Pass None to use sprint_start as both bounds.

    A roster entry is included when its tenure overlaps with [sprint_start, sprint_end]:
        entry.from <= sprint_end  AND  (entry.to is null OR entry.to >= sprint_start)
    """
    roster = cfg.get("qa_team_roster") or []
    if not roster:
        return [m for m in cfg.get("qa_team_members", []) if m and m.strip()]

    if sprint_start is None:
        # All-time: return every member ever listed
        return [e["name"] for e in roster if e.get("name")]

    try:
        s_start = _date.fromisoformat(sprint_start)
        s_end   = _date.fromisoformat(sprint_end) if sprint_end else s_start
    except (ValueError, TypeError) as exc:
        logger.warning("resolve_qa_members: invalid sprint date %r/%r — returning all (%s)",
                       sprint_start, sprint_end, exc)
        return [e["name"] for e in roster if e.get("name", "").strip()]

    active: list[str] = []
    for entry in roster:
        name = entry.get("name", "").strip()
        if not name:
            continue
        try:
            e_from = _date.fromisoformat(entry["from"])
        except (KeyError, ValueError, TypeError):
            logger.warning("resolve_qa_members: roster entry %r missing valid 'from' date — "
                           "defaulting to 2000-01-01", name)
            e_from = _date(2000, 1, 1)
        to_val = entry.get("to")
        e_to = _date.fromisoformat(to_val) if to_val else _date(9999, 12, 31)

        # Overlap: entry active during any part of the sprint
        if e_from <= s_end and e_to >= s_start:
            active.append(name)

    # Fall back to flat list if roster produced nothing (e.g. all entries have future from-dates)
    return active or [m for m in cfg.get("qa_team_members", []) if m and m.strip()]

