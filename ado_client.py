"""
Azure DevOps REST API client for the QA Metrics Dashboard.

Filtering strategy (confirmed from live ADO data, 2026-05-11):
  - User Stories  → filter by AssignedTo (QA member is doing the testing work)
  - Bugs          → filter by CreatedBy  (QA member is the one who found/filed it)
  - Both          → OR-condition: item tagged `sb_qa` is always included regardless of assignee
  - Story points  → absent on ~75 % of items in this project; count-based metrics are primary
  - TargetDate    → universally unset; sprint end date used as implicit deadline
  - CompletedWork → universally unset; test execution hours deferred to Tier 3

Test Plans live in the `sootballs` project.  The same PAT works cross-project.
Suite-level testCaseCount returns 0 via the suites API; test cases are queried
via WIQL on WorkItemType='Test Case' instead.
"""

import hashlib
import logging
import threading
import time
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
        status_forcelist={429, 500, 502, 503, 504},
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

        member_clause = self._member_or_tag_clause(
            field="[System.AssignedTo]", members=norm_members, tag=qa_tag
        )
        query = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{_esc(self.project)}'
              AND [System.IterationPath] = '{_esc(iteration_path)}'
              AND [System.WorkItemType] = 'User Story'
              {('AND (' + member_clause + ')') if member_clause else ''}
            ORDER BY [System.Id]
        """
        ids = self._wiql(self.project, query)
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
            result = [self._normalise(item) for item in self._batch_fetch(self.project, ids, fields)]

        self._set_cached(cache_key, result)
        return result

    def get_bugs(self, iteration_path: str, qa_members: list[str], qa_tag: str) -> list[dict]:
        """Return Bugs in the PI created by a QA member or tagged qa_tag."""
        norm_members = sorted({m.strip() for m in qa_members if m and m.strip()})
        cache_key = self._make_key("bugs", iteration_path, tuple(norm_members), qa_tag)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        member_clause = self._member_or_tag_clause(
            field="[System.CreatedBy]", members=norm_members, tag=qa_tag
        )
        # Query UNDER the PI so bugs across all sub-sprints are returned; callers filter to sprint.
        pi_path = "\\".join(iteration_path.split("\\")[:2])
        query = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{_esc(self.project)}'
              AND [System.IterationPath] UNDER '{_esc(pi_path)}'
              AND [System.WorkItemType] = 'Bug'
              {('AND (' + member_clause + ')') if member_clause else ''}
            ORDER BY [System.Id]
        """
        ids = self._wiql(self.project, query)
        if not ids:
            result = []
        else:
            fields = [
                "System.Id", "System.Title", "System.State",
                "System.WorkItemType", "System.AssignedTo", "System.CreatedBy",
                "System.CreatedDate", "System.IterationPath",
                "Microsoft.VSTS.Common.ClosedDate",
                "Microsoft.VSTS.Common.Priority",
                "System.Tags",
            ]
            result = [self._normalise(item) for item in self._batch_fetch(self.project, ids, fields)]

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
        plan_map: dict[int, dict] = {}
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
                "plan_name": plan_names.get(pid, f"Plan {pid}"),
                "confirmed_tcs": p["confirmed_tcs"],
                "automated_in_ado": p["automated_in_ado"],
                "pending_link": p["confirmed_tcs"] - p["automated_in_ado"],
                "amr_wi_ids": p["amr_wi_ids"],
            }
            for pid, p in sorted(plan_map.items())
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
        query = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{_esc(self.project)}'
              AND [System.IterationPath] = '{_esc(iteration_path)}'
              AND [System.WorkItemType] IN ('Feature', 'User Story')
              {('AND (' + member_clause + ')') if member_clause else ''}
            ORDER BY [System.Id]
        """
        ids = self._wiql(self.project, query)
        if not ids:
            self._set_cached(cache_key, [])
            return []

        fields = [
            "System.Id", "System.Title", "System.State", "System.AssignedTo",
            "System.WorkItemType",
        ]
        items = self._batch_fetch(self.project, ids, fields)
        features = [self._normalise(item) for item in items]

        # For each Feature, find linked TCs via relations
        result = []
        for feat in features:
            wi_id = feat["id"]
            total_tcs = 0
            automated_tcs = 0
            try:
                rel_url = f"{_ADO_ROOT}/{self.project}/_apis/wit/workitems/{wi_id}"
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
        resp = self._session.post(
            url,
            json={"query": query},
            params={"api-version": _API_VERSION},
            timeout=30,
        )
        resp.raise_for_status()
        ids = [w["id"] for w in resp.json().get("workItems", [])]
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
            "closed": (f.get("Microsoft.VSTS.Common.ClosedDate") or "")[:10],
            "story_points": f.get("Microsoft.VSTS.Scheduling.StoryPoints"),
            "tags": f.get("System.Tags", "") or "",
            "iteration": f.get("System.IterationPath", ""),
            "priority": f.get("Microsoft.VSTS.Common.Priority"),
        }


def _esc(value: str) -> str:
    """Escape a value for safe embedding in a single-quoted WIQL string."""
    return value.replace("'", "''")


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
    if entry.get("tc_id", 0) >= 57000:
        return True
    if entry.get("plan_id", 0) == 99999:
        return True
    if not entry.get("pytest_method"):
        return True
    return False
