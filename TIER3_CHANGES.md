# Tier 3 — ADO Configuration Changes (Pending Manager Approval)

These four changes require coordination with the ADO project administrators and/or team process agreements.
None of them modify existing data; all are additive.  Once approved, the dashboard will automatically
pick up the new data and replace the grey "Awaiting Approval" placeholder cards with live metrics.

---

## Change 1 — Make `TargetDate` a Required Field on User Story Work Items

**ADO Project:** AMR  
**Work Item Type:** User Story  
**Field:** `Microsoft.VSTS.Scheduling.TargetDate`

### What needs to happen
An ADO project administrator adds a **Required** validation rule to the `TargetDate` field on the
User Story work item type so that every new story must have an explicit due date before it can be saved.

### Why
Today every User Story in the AMR project has `TargetDate = null`.  This means the dashboard cannot
measure **Estimation Slip Rate** (the gap between the planned delivery date and the actual closed date).
With target dates populated the KPI becomes:

```
Estimation Slip Rate = stories closed after TargetDate / total stories × 100 %
```

### Dashboard KPI unlocked
- **Estimation Slip Rate** (the "Awaiting Approval" placeholder for this metric was removed from the Overview page pending this change; it will be re-added there once `TargetDate` is populated and the KPI is live)

---

## Change 2 — CI Stability % — DONE (superseded the `ci` Tag Convention below)

**Status: implemented 2026-07-29, live on the Overview page.** No ADO change was needed after all —
see "What we built instead" below. The original `ci`-tag proposal is kept underneath for context
but is no longer planned.

### What we built instead
CI Stability % is computed directly from GitHub Actions, not from ADO work items. For each of the
nightly `run_nightly_integration.yml` and `run_nightly_e2e.yml` workflows, the dashboard reads the
`allure-summary-*` artifact (`widgets/summary.json`) from the most recent completed run — the same
artifact `.github/workflows/send_slack_report.yml` already reads to build the Slack nightly
notification, so this number always matches what's posted in Slack by construction. If the most
recent run has no usable summary (crashed before producing one), it falls back to the next most
recent run and flags the result as stale with the actual date used.

```
CI Stability % = passed / (passed + failed + broken + missing) × 100
```

Skipped tests are excluded from the denominator (not a stability signal); "missing" (crashed/
cancelled/timed-out, already crash-padded in the summary's "unknown" bucket) counts against
stability, since a crashed run isn't a neutral outcome. Integration and e2e are shown individually
alongside a combined, test-count-weighted headline number.

This is a materially better metric than the original proposal below: it measures actual test-pass
health per run, not how diligently the team tags ADO work items.

### Dashboard KPI unlocked
- **CI Stability %** — live on the Overview page (`/api/ci_stability`, `ado_client.py:get_ci_stability`)

---

### (Superseded) Original proposal — Establish a `ci` Tag Convention for CI/CD Work Items

**ADO Project:** AMR
**Applies to:** User Stories and Tasks
**Tag value:** `ci` (lowercase, consistent with existing `sb_qa` convention)

This would have required tagging all CI/CD-related User Stories and Tasks with `ci` and computing:

```
CI Stability % = CI-tagged stories closed in Completed/Resolved state / total CI-tagged stories × 100 %
```

Not pursued — the GitHub Actions-based approach above needed no ADO process change and measures the
actual thing this KPI is meant to represent.

---

## Change 3 — Log `CompletedWork` on QA Tasks

**ADO Project:** AMR  
**Work Item Type:** Task (child tasks under QA User Stories)  
**Field:** `Microsoft.VSTS.Scheduling.CompletedWork` (unit: hours)

### What needs to happen
Team process agreement for QA engineers to fill in **Completed Work** (hours) on their Task work items
when they close or resolve them.  No ADO admin change is required — the field already exists; it just
needs to be used.

A lightweight reminder in the team's sprint-close checklist is sufficient.

### Why
Currently `CompletedWork` is 0 on every Task in the project.  Actual hours data enables:

```
Test Execution Hours = Σ CompletedWork across QA Tasks in sprint
QA Effort vs Story Points ratio = Test Execution Hours / Total Story Points
```

These metrics help justify QA headcount and surface sprint-over-sprint trends in testing effort.

### Dashboard KPI unlocked
- **Test Execution Hours** (currently showing "Awaiting Approval" on Overview page)

---

## Change 4 — Migrate Test Plans from `sootballs` to `AMR` Project and Link to Sprint Iterations

**ADO Projects involved:** `sootballs` (source) → `AMR` (destination)  
**Applies to:** Test Plans, Test Suites, Test Cases

### What needs to happen
1. **Migrate** all Test Plans and their associated Test Cases from the `sootballs` ADO project to the
   `AMR` project.  (ADO does not provide a native migrate button; the recommended path is the
   [Azure DevOps Migration Tools](https://github.com/nkdAgility/azure-devops-migration-tools) OSS project
   or the ADO REST API export/import scripts.)
2. After migration, **link each Test Plan to the matching sprint iteration** in `AMR` using
   `iteration` field on the plan (e.g. `AMR\26PI2\PI2 Sprint 4`).

### Why
Test Plans currently live in `sootballs`, which is a different ADO project from sprint work items (`AMR`).
The dashboard can already read test case counts from `sootballs` (Tier 2), but sprint-level drill-down
(e.g. "how many test cases were executed this sprint?", pass/fail rates per sprint) is impossible
without the plans being in the same project and bound to the same iteration tree.

Once migrated and linked, the Test Plans tab will show per-sprint test execution summaries alongside
the User Story completion data.

### Dashboard KPIs unlocked
- **Sprint-level test execution metrics** (pass rate, fail rate, blocked %)
- **Test Plan → Sprint linkage** in the Test Plans tab (currently showing migration note)

---

## Summary Table

| # | Change | Effort | ADO Admin Needed? | KPI Unlocked | Status |
|---|--------|--------|-------------------|--------------|--------|
| 1 | Make `TargetDate` required on User Stories | Low (field rule change) | Yes | Estimation Slip Rate | Pending |
| 2 | ~~Adopt `ci` tag convention~~ — built via GitHub Actions instead | — | No | CI Stability % | **Done** (2026-07-29) |
| 3 | Log `CompletedWork` hours on QA Tasks | Low (habit change) | No | Test Execution Hours | Pending |
| 4 | Migrate Test Plans sootballs → AMR + link iterations | Medium (data migration) | Yes | Sprint test metrics | Pending |

Changes 1 and 3 can be approved and implemented within a single sprint. Change 4 is a larger project
and should be scoped as a dedicated initiative with its own iteration path and acceptance criteria.
Change 2 is done — see its section above for what shipped instead of the original proposal.
