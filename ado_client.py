from __future__ import annotations

"""
Azure DevOps REST API client for the QA Metrics Dashboard.

Filtering strategy (confirmed from live ADO data, 2026-05-11):
  - User Stories  → filter by AssignedTo (QA member is doing the testing work)
  - Bugs          → NOT filtered by reporter/assignee (2026-07-10): bug health metrics
                    (created-vs-resolved flow, lead time, aging) care about all bugs in
                    the PI, not just ones QA happened to file
  - Both          → OR-condition: item tagged `sb_qa` is always included regardless of assignee
  - Story points  → absent on ~75 % of items in this project; count-based metrics are primary
  - TargetDate    → universally unset; sprint end date used as implicit deadline
  - CompletedWork → universally unset; test execution hours deferred to Tier 3
  - Severity/Priority → set on <2% of recent Bugs (verified 2026-07-10); not usable for
                    weighted metrics until the team adopts the field (see TIER3_CHANGES.md)
  - Tags on Bugs  → 0 of 1020 recent Bugs have any tag; no escape/regression convention
                    exists yet — do not derive that signal from tags

Test Plans live in the `sootballs` project.  The same PAT works cross-project.
Suite-level testCaseCount returns 0 via the suites API; test cases are queried
via WIQL on WorkItemType='Test Case' instead.
"""

import hashlib
import logging
import threading
import time
from datetime import date as _date
from pathlib import Path
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
        """Return ALL Bugs in the PI containing this iteration, regardless of who
        filed or is assigned to them — bug health is a team-wide signal, not a
        per-reporter one."""
        cache_key = self._make_key("bugs", iteration_path)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # Route to correct project; query UNDER the PI for all sub-sprint bugs.
        project = "sootballs" if iteration_path.lower().startswith("sootballs") else self.project
        pi_path = "\\".join(iteration_path.split("\\")[:2])
        query = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{_esc(project)}'
              AND [System.IterationPath] UNDER '{_esc(pi_path)}'
              AND [System.WorkItemType] = 'Bug'
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

    def get_test_case_summary(self) -> dict:
        """
        Count test cases in sootballs by AutomationStatus via WIQL.
        Suite-level API returns 0 because cases are linked at plan level;
        WIQL on WorkItemType='Test Case' is the reliable path.
        """
        cache_key = self._make_key("tc_summary", self.testplans_project)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            url = f"{_ADO_ROOT}/{self.testplans_project}/_apis/wit/wiql"
            resp = self._session.post(
                url,
                json={"query": (
                    "SELECT [System.Id],[Microsoft.VSTS.TCM.AutomationStatus] "
                    "FROM WorkItems WHERE [System.WorkItemType] = 'Test Case' "
                    "ORDER BY [System.Id]"
                )},
                params={"api-version": _API_VERSION},
                timeout=30,
            )
            if not resp.ok:
                logger.warning("test case WIQL failed: %s", resp.status_code)
                return {"total": 0, "automated": 0, "not_automated": 0, "error": resp.status_code}

            ids = [w["id"] for w in resp.json().get("workItems", [])]
            if not ids:
                return {"total": 0, "automated": 0, "not_automated": 0}

            automated = not_automated = 0
            # Cap at 1000 for speed; adequate for trend metrics
            for chunk_start in range(0, min(len(ids), 1000), _BATCH_SIZE):
                chunk = ids[chunk_start:chunk_start + _BATCH_SIZE]
                batch_url = f"{_ADO_ROOT}/{self.testplans_project}/_apis/wit/workitemsbatch"
                batch_resp = self._session.post(
                    batch_url,
                    json={"ids": chunk, "fields": ["Microsoft.VSTS.TCM.AutomationStatus"]},
                    params={"api-version": _API_VERSION},
                    timeout=30,
                )
                if batch_resp.ok:
                    for item in batch_resp.json().get("value", []):
                        status = item["fields"].get("Microsoft.VSTS.TCM.AutomationStatus", "")
                        if status == "Automated":
                            automated += 1
                        else:
                            not_automated += 1

            total = len(ids)
            result = {
                "total": total,
                "automated": automated,
                "not_automated": not_automated,
                "automation_rate": round(automated / total * 100, 1) if total else 0,
                "capped_at": min(total, 1000),
            }
            self._set_cached(cache_key, result)
            return result
        except Exception:
            logger.exception("Failed to fetch test case summary")
            return {"total": 0, "automated": 0, "not_automated": 0, "error": "fetch failed"}

    def get_amr_automation_wis(self, qa_tag: str = "sb_qa") -> list[dict]:
        """Return all AMR Work Items tagged with qa_tag (default: sb_qa).

        These represent automation tasks — the demand side of the coverage gap.
        """
        cache_key = self._make_key("amr_automation_wis", qa_tag)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        query = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{_esc(self.project)}'
              AND [System.Tags] CONTAINS '{_esc(qa_tag)}'
            ORDER BY [System.ChangedDate] DESC
        """
        try:
            ids = self._wiql(self.project, query)
        except Exception:
            logger.exception("Failed WIQL for AMR automation WIs")
            return []

        if not ids:
            self._set_cached(cache_key, [])
            return []

        fields = [
            "System.Id", "System.Title", "System.State",
            "System.WorkItemType", "System.AssignedTo", "System.CreatedDate",
            "System.ChangedDate", "System.Tags", "System.IterationPath",
        ]
        items = self._batch_fetch(self.project, ids, fields)
        result = [self._normalise(item) for item in items]
        self._set_cached(cache_key, result)
        return result

    def get_automation_coverage(self, mapping_file: str | None = None) -> dict:
        """Combine ado_test_mapping.yaml + live ADO TC status + AMR WIs (sb_qa tag).

        Returns a coverage report structured for the dashboard Automation tab:
          summary      — headline numbers (% automated, mapping stats, WI counts)
          by_plan      — per test-plan breakdown with progress metrics
          amr_wis      — AMR work items tagged sb_qa with linked TC counts
        """
        cache_key = self._make_key("automation_coverage", mapping_file or "")
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # 1. Load mapping YAML — None means file present but malformed
        mapping_result = _load_mapping(mapping_file)
        if not mapping_file:
            mappings, mapping_loaded, mapping_error = [], False, False
        elif mapping_result is None:
            mappings, mapping_loaded, mapping_error = [], False, True
        else:
            mappings, mapping_loaded, mapping_error = mapping_result, True, False
        confirmed = [m for m in mappings if not _is_mapping_placeholder(m)]

        # 2. Live TC automation status for confirmed tc_ids
        tc_ids = list({m["tc_id"] for m in confirmed})
        tc_details: dict[int, dict] = {}
        if tc_ids:
            try:
                url = f"{_ADO_ROOT}/{self.testplans_project}/_apis/wit/workitemsbatch"
                resp = self._session.post(
                    url,
                    json={
                        "ids": tc_ids,
                        "fields": [
                            "System.Id", "System.Title",
                            "Microsoft.VSTS.TCM.AutomationStatus",
                            "Microsoft.VSTS.TCM.AutomatedTestName",
                        ],
                    },
                    params={"api-version": _API_VERSION},
                    timeout=30,
                )
                if resp.ok:
                    for item in resp.json().get("value", []):
                        f = item["fields"]
                        tc_details[f["System.Id"]] = {
                            "title":       f.get("System.Title", ""),
                            "status":      f.get("Microsoft.VSTS.TCM.AutomationStatus", "Not Automated"),
                            "linked_test": f.get("Microsoft.VSTS.TCM.AutomatedTestName", ""),
                        }
            except Exception:
                logger.exception("Failed to fetch TC details for automation coverage")

        # 3. Overall TC summary (all TCs in sootballs, capped at 1 000 for speed)
        tc_summary = self.get_test_case_summary()

        # 4. AMR WIs with sb_qa tag
        amr_wis = self.get_amr_automation_wis()

        # 5. Group confirmed mappings by plan
        plan_map: dict[int | None, dict] = {}
        for m in confirmed:
            pid = m["plan_id"]
            if pid not in plan_map:
                plan_map[pid] = {
                    "plan_id": pid, "confirmed_tcs": 0,
                    "automated_in_ado": 0, "tc_ids": [], "amr_wi_ids": [],
                }
            p = plan_map[pid]
            p["confirmed_tcs"] += 1
            p["tc_ids"].append(m["tc_id"])
            if m.get("amr_wi_id"):
                p["amr_wi_ids"].append(m["amr_wi_id"])
            if tc_details.get(m["tc_id"], {}).get("status") == "Automated":
                p["automated_in_ado"] += 1

        # 6. Resolve plan names from test plans API
        try:
            plan_names = {p["id"]: p["name"] for p in self.get_test_plans()}
        except Exception:
            plan_names = {}

        by_plan = [
            {
                "plan_id": pid,
                "plan_name": "Unmapped (no Test Plan linked)" if pid is None else plan_names.get(pid, f"Plan {pid}"),
                "confirmed_tcs": p["confirmed_tcs"],
                "automated_in_ado": p["automated_in_ado"],
                "pending_link": p["confirmed_tcs"] - p["automated_in_ado"],
                "amr_wi_ids": p["amr_wi_ids"],
            }
            # Some confirmed mappings have plan_id: null (tc_id/pytest_method/amr_wi_id
            # filled in but never linked to a Test Plan) — sort them last instead of
            # crashing on None-vs-int comparison.
            for pid, p in sorted(plan_map.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        ]

        # 7. Enrich AMR WIs with linked TC info
        wi_to_tcs: dict[int, list[int]] = {}
        for m in confirmed:
            wi_id = m.get("amr_wi_id")
            if wi_id:
                wi_to_tcs.setdefault(wi_id, []).append(m["tc_id"])

        amr_wi_rows = []
        for w in amr_wis:
            linked = wi_to_tcs.get(w["id"], [])
            amr_wi_rows.append({
                **w,
                "linked_tc_ids": linked,
                "linked_count": len(linked),
                "all_automated": all(
                    tc_details.get(tc, {}).get("status") == "Automated"
                    for tc in linked
                ) if linked else False,
            })

        # 8. Summary stats
        automated_confirmed = sum(
            1 for m in confirmed
            if tc_details.get(m["tc_id"], {}).get("status") == "Automated"
        )
        from collections import Counter
        wi_states = dict(Counter(w["state"] for w in amr_wis).most_common())

        result = {
            "mapping_loaded": mapping_loaded,
            "mapping_error":  mapping_error,
            "summary": {
                "total_tcs_in_ado":        tc_summary["total"],
                "automated_in_ado":        tc_summary["automated"],
                "pct_automated_in_ado":    tc_summary.get("automation_rate", 0),
                "confirmed_in_mapping":    len(confirmed),
                "todo_in_mapping":         len(mappings) - len(confirmed),
                "pending_link_to_ado":     len(confirmed) - automated_confirmed,
                "amr_wis_total":           len(amr_wis),
                "amr_wis_with_linked_tcs": sum(1 for w in amr_wi_rows if w["linked_count"] > 0),
                "amr_wi_state_breakdown":  wi_states,
            },
            "by_plan": by_plan,
            "amr_wis": amr_wi_rows,
        }
        self._set_cached(cache_key, result)
        return result

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

    def get_feature_coverage(
        self, iteration_path: str, qa_members: list[str], qa_tag: str
    ) -> list[dict]:
        """Return Feature-level TC automation coverage for a sprint.

        For each Feature in the sprint, counts total TCs linked via 'Tested By'
        and how many are Automated.  Returns a list of dicts:
          { id, title, assignee, state, total_tcs, automated_tcs, coverage_pct }
        """
        norm_members = sorted({m.strip() for m in qa_members if m and m.strip()})
        cache_key = self._make_key("feature_coverage", iteration_path, tuple(norm_members), qa_tag)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        member_clause = self._member_or_tag_clause(
            field="[System.AssignedTo]", members=norm_members, tag=qa_tag
        )
        project = "sootballs" if iteration_path.lower().startswith("sootballs") else self.project
        query = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{_esc(project)}'
              AND [System.IterationPath] = '{_esc(iteration_path)}'
              AND [System.WorkItemType] IN ('Feature', 'User Story')
              {('AND (' + member_clause + ')') if member_clause else ''}
            ORDER BY [System.Id]
        """
        ids = self._wiql(project, query)
        if not ids:
            self._set_cached(cache_key, [])
            return []

        fields = [
            "System.Id", "System.Title", "System.State", "System.AssignedTo",
            "System.WorkItemType",
        ]
        items = self._batch_fetch(project, ids, fields)
        features = [self._normalise(item) for item in items]

        # For each Feature, find linked TCs via relations
        result = []
        for feat in features:
            wi_id = feat["id"]
            total_tcs = 0
            automated_tcs = 0
            try:
                rel_url = f"{_ADO_ROOT}/{project}/_apis/wit/workitems/{wi_id}"
                rel_resp = self._session.get(
                    rel_url,
                    params={"api-version": _API_VERSION, "$expand": "relations"},
                    timeout=15,
                )
                if rel_resp.ok:
                    relations = rel_resp.json().get("relations", []) or []
                    tc_ids = [
                        int(r["url"].rstrip("/").split("/")[-1])
                        for r in relations
                        if r.get("rel") == "Microsoft.VSTS.Common.TestedBy-Reverse"
                    ]
                    if tc_ids:
                        total_tcs = len(tc_ids)
                        # Batch-fetch TC automation status
                        batch_url = f"{_ADO_ROOT}/{self.testplans_project}/_apis/wit/workitemsbatch"
                        b = self._session.post(
                            batch_url,
                            json={
                                "ids": tc_ids[:200],
                                "fields": ["Microsoft.VSTS.TCM.AutomationStatus"],
                            },
                            params={"api-version": _API_VERSION},
                            timeout=15,
                        )
                        if b.ok:
                            automated_tcs = sum(
                                1 for item in b.json().get("value", [])
                                if item["fields"].get(
                                    "Microsoft.VSTS.TCM.AutomationStatus"
                                ) == "Automated"
                            )
            except Exception:
                pass

            result.append({
                "id": wi_id,
                "title": feat.get("title", ""),
                "assignee": feat.get("assignee", ""),
                "state": feat.get("state", ""),
                "type": feat.get("work_item_type", ""),
                "total_tcs": total_tcs,
                "automated_tcs": automated_tcs,
                "coverage_pct": round(automated_tcs / total_tcs * 100, 1) if total_tcs else 0,
            })

        result.sort(key=lambda x: x["coverage_pct"])
        self._set_cached(cache_key, result)
        return result

    def get_engineer_automation(self, mapping_file: str | None = None) -> list[dict]:
        """Return per-engineer automation counts derived from ado_test_mapping.yaml.

        Uses the github_pr field in YAML entries to attribute TCs to the engineer
        who wrote the automation (by PR author).  Returns:
          { engineer, tcs_automated, prs, tc_ids[] }
        """
        cache_key = self._make_key("engineer_automation", mapping_file or "")
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        mapping_result = _load_mapping(mapping_file)
        if not mapping_result:
            return []

        mappings = [m for m in mapping_result if not _is_mapping_placeholder(m)]

        # Group TC IDs by github_pr number
        by_pr: dict[int, list[int]] = {}
        for m in mappings:
            pr = m.get("github_pr")
            if pr and isinstance(pr, int):
                by_pr.setdefault(pr, []).append(m["tc_id"])

        if not by_pr:
            self._set_cached(cache_key, [])
            return []

        # Fetch PR author from GitHub API for each PR (no auth required for public repos)
        by_engineer: dict[str, dict] = {}
        import time as _time
        for pr_num, tc_ids in sorted(by_pr.items()):
            author = f"PR #{pr_num}"  # fallback if GitHub API unavailable
            try:
                gh_resp = self._session.get(
                    f"https://api.github.com/repos/rapyuta-robotics/sootballs_tests/pulls/{pr_num}",
                    timeout=10,
                )
                if gh_resp.ok:
                    author = gh_resp.json().get("user", {}).get("login", author)
            except Exception:
                pass
            eng = by_engineer.setdefault(author, {
                "engineer": author, "tcs_automated": 0, "prs": [], "tc_ids": [],
            })
            eng["tcs_automated"] += len(tc_ids)
            eng["tc_ids"].extend(tc_ids)
            if pr_num not in eng["prs"]:
                eng["prs"].append(pr_num)
            _time.sleep(0.05)

        result = sorted(by_engineer.values(), key=lambda x: -x["tcs_automated"])
        self._set_cached(cache_key, result)
        return result

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
            "state": f.get("System.State", ""),
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


def _load_mapping(mapping_file: str | None) -> list[dict] | None:
    """Load mappings from ado_test_mapping.yaml.

    Returns:
        list[dict]  — valid mappings (may be empty if file has no entries).
        []          — mapping_file is None/empty (feature disabled, not an error).
        None        — file exists but could not be read or fails schema checks;
                      callers should surface this as a configuration error.
    """
    if not mapping_file:
        return []

    try:
        with open(mapping_file) as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("Mapping file not found: %s", mapping_file)
        return None
    except yaml.YAMLError as exc:
        logger.warning("Could not parse mapping file %s as YAML: %s", mapping_file, exc)
        return None
    except Exception as exc:
        logger.warning("Unexpected error reading mapping file %s: %s", mapping_file, exc)
        return None

    if not isinstance(raw, dict):
        logger.warning(
            "Mapping file %s: expected a dict root, got %s",
            mapping_file, type(raw).__name__,
        )
        return None

    mappings = raw.get("mappings")
    if mappings is None:
        logger.warning(
            "Mapping file %s missing 'mappings' key. Found keys: %s",
            mapping_file, list(raw.keys()),
        )
        return None

    if not isinstance(mappings, list):
        logger.warning(
            "Mapping file %s: 'mappings' must be a list, got %s",
            mapping_file, type(mappings).__name__,
        )
        return None

    return mappings


def _is_mapping_placeholder(entry: dict) -> bool:
    """Mirror of link_ado_tests._is_placeholder — must stay in sync."""
    tc_id = entry.get("tc_id")
    if tc_id is not None and tc_id >= 57000:
        return True
    if entry.get("plan_id", 0) == 99999:
        return True
    if not entry.get("pytest_method"):
        return True
    return False
