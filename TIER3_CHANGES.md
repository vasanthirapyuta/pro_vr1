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
- **Estimation Slip Rate** (currently showing "Awaiting Approval" on Overview page)

---

## Change 2 — Establish a `ci` Tag Convention for CI/CD Work Items

**ADO Project:** AMR  
**Applies to:** User Stories and Tasks  
**Tag value:** `ci` (lowercase, consistent with existing `sb_qa` convention)

### What needs to happen
Team agreement (no ADO admin change needed) to tag all CI/CD-related User Stories and Tasks with `ci`.
This is purely a process/culture change — ask the team lead to add it to the definition-of-done for
CI-related stories.

### Why
The AMR project mixes CI/CD maintenance work alongside feature work.  Without a tag filter the dashboard
cannot isolate CI test-pass rates.  With the tag filter in place:

```
CI Stability % = CI-tagged stories closed in Completed/Resolved state / total CI-tagged stories × 100 %
```

### Dashboard KPI unlocked
- **CI Stability %** (currently showing "Awaiting Approval" on Overview page)

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
Test Plans currently live in `sootballs` which is a different ADO project from sprint work items (`AMR`).
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

| # | Change | Effort | ADO Admin Needed? | KPI Unlocked |
|---|--------|--------|-------------------|--------------|
| 1 | Make `TargetDate` required on User Stories | Low (field rule change) | Yes | Estimation Slip Rate |
| 2 | Adopt `ci` tag convention for CI work items | Low (process agreement) | No | CI Stability % |
| 3 | Log `CompletedWork` hours on QA Tasks | Low (habit change) | No | Test Execution Hours |
| 4 | Migrate Test Plans sootballs → AMR + link iterations | Medium (data migration) | Yes | Sprint test metrics |

Changes 1–3 can be approved and implemented within a single sprint.  Change 4 is a larger project and
should be scoped as a dedicated initiative with its own iteration path and acceptance criteria.
