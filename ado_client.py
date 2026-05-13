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
from typing import Any

import requests
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
