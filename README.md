# QA Metrics Dashboard

A Flask + React single-page application that pulls **all data live** — from Azure DevOps,
GitHub Actions, and a curated release-sanity export. No CSV uploads. No spreadsheets.
No manual data entry.

---

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│  Browser  →  frontend/index.html  (React 18 + CDN)        │
│                       ↕  fetch /api/*                     │
│            app.py  (Flask 3, port 5050)                    │
│                       ↕  REST                              │
│            ado_client.py  (ADO API v7.0 + GitHub Actions)  │
│                       ↕                                    │
│   ADO AMR project (Work Items)                             │
│   ADO sootballs project (Test Plans / Test Cases)          │
│   GitHub Actions (rapyuta-robotics/sootballs_tests)         │
│   Local generated files (qa_tooling/*.json, .yaml)          │
└───────────────────────────────────────────────────────────┘
```

Data sources:

| Data | Source | API / mechanism |
|------|--------|------------------|
| User Stories, Bugs, sprint KPIs | ADO `AMR` | WIQL + workitemsbatch |
| Test Plans, Test Cases, automation status | ADO `sootballs`, AreaPath `sootballs\qa` | testplan/plans + WIQL |
| Automation Work Items (`sb_qa` tag) | ADO `AMR` | WIQL + workitemsbatch |
| CI Stability % (nightly integration + e2e) | GitHub Actions | `allure-summary-*` artifact (`widgets/summary.json`) per workflow run |
| Nightly CI health / run history | GitHub Actions | workflow runs API |
| sb_qa PR coverage | GitHub Actions | `qa_snapshot_*.json` snapshot file |
| Release-scoped sanity coverage (3.4–3.7) | Local file | `qa_tooling/amr_master_sanity_status.json` (see below) |
| Mapping file enrichment (pytest ↔ ADO TC ↔ AMR WI) | Local file | `ado_test_mapping.yaml` |
| Flaky test tracking | Local file | `flaky_tests_report.json` |

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.9+ | `flask==3.1.0` in requirements.txt needs it. On Python 3.8, use a venv with an unpinned/compatible Flask instead (resolves to 3.0.3 — works fine, nothing depends on 3.1-only features) |
| ADO Personal Access Token (`ADO_PAT`) | Scopes: **Work Items** Read, **Test Management** Read |
| ADO projects | `AMR` (work items) + `sootballs` (test plans, AreaPath `sootballs\qa`) under `rapyuta-robotics` org |
| GitHub token (`GITHUB_TOKEN`) | Needed for CI Stability, nightly health, and sb_qa PR coverage — read access to `rapyuta-robotics/sootballs_tests` Actions API. `gh auth token` works locally if you have the CLI authenticated |

---

## Configuration

Edit `config.yaml`:

```yaml
ado:
  organization: rapyuta-robotics
  project: AMR
  testplans_project: sootballs

qa_team_roster:            # preferred — date-range membership, see comments in config.yaml
  - name: "First Last"
    from: "2026-01-01"
    to:                     # blank/omitted = still active
qa_team_members:            # fallback flat list, used only if qa_team_roster is empty
  - "First Last"

qa_tag: "sb_qa"     # WIs with this tag are always included regardless of assignee

# Local generated-file paths — all optional, each unlocks one dashboard section
# if configured, degrades gracefully with a status message if not.
sootballs_mapping_file: null       # qa_tooling/ado_test_mapping.yaml — Automation tab
feature_coverage_cache_file: null  # qa_tooling/feature_coverage_cache.json — Feature Coverage tab (v2)
snapshot_file: null                # qa_tooling/qa_snapshot_*.json — ci_fix_coverage, test_area_breakdown, sb_qa_coverage
flaky_report_file: null            # qa_tooling/flaky_tests_report.json — Flaky tests
amr_master_sanity_file: null       # qa_tooling/amr_master_sanity_status.json — Release Sanity Coverage (3.4-3.7)

sprints:
  - label: "PI3 Sprint 2"
    iteration_path: "AMR\\26PI3\\PI3 Sprint 2"
    start: "2026-07-20"
    end:   "2026-07-31"
    pi:    "26PI3"
```

Each dashboard sprint entry needs adding as new PIs/sprints roll out — verify the `iteration_path`
against ADO's classification-nodes API before adding (`curl -s -u ":$ADO_PAT"
"https://dev.azure.com/rapyuta-robotics/AMR/_apis/wit/classificationnodes/iterations?\$depth=10&api-version=7.0"`),
don't guess it.

---

## Running Locally

```bash
# 1. Install dependencies (use a venv if system Python is 3.8 — see Prerequisites)
pip install -r requirements.txt

# 2. Set required tokens
export ADO_PAT=<your-personal-access-token>
export GITHUB_TOKEN=<your-github-token>          # or rely on `gh auth token` locally

# 3. Optional: point to the generated local files listed in Configuration above
export MAPPING_FILE=/path/to/sootballs_tests/qa_tooling/ado_test_mapping.yaml
export SANITY_FILE=/path/to/sootballs_tests/qa_tooling/amr_master_sanity_status.json

# 4. Start the server
python app.py
# → http://localhost:5050
```

---

## Running with Docker

```bash
docker build -t qa-dashboard .

docker run \
  -e ADO_PAT=<your-pat> \
  -e MAPPING_FILE=/app/ado_test_mapping.yaml \
  -v /path/to/sootballs_tests/qa_tooling/ado_test_mapping.yaml:/app/ado_test_mapping.yaml:ro \
  -p 5050:5050 \
  qa-dashboard
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Server health + ADO connectivity check |
| GET | `/api/sprints` | All configured sprints from config.yaml |
| GET | `/api/kpi/summary?sprint=<label>` | Sprint KPI cards (velocity, bug rate, carry-over…) |
| GET | `/api/kpi/trends` | KPI trends across all configured sprints |
| GET | `/api/engineers?sprint=<label>` | Per-engineer story + bug breakdown |
| GET | `/api/bugs?sprint=<label>` | PI-wide bug health — flow, lead time, aging, by-area (not just QA-filed) |
| GET | `/api/testplans` | Test plan list; Test Case overview (raw TC count + de-duplicated distinct-scenario count, both with their own automation rate) |
| GET | `/api/automation_coverage` | Mapping YAML + ADO TCs (AreaPath `sootballs\qa`) + AMR WIs, same raw/distinct-scenario split as `/api/testplans` |
| GET | `/api/feature_coverage?sprint=<label>` | Sprint-scoped Feature → Test Case → Automated traceability |
| GET | `/api/feature_coverage_v2?sprint=<label>` | Same, cache-driven from `traverse_feature_coverage.py`'s pre-built export |
| GET | `/api/engineer_automation` | Per-engineer automation attribution, from `ado_test_mapping.yaml`'s `github_pr` field |
| GET | `/api/nightly_health?n_runs=<n>` | Nightly integration/e2e run history and pass-rate trend from GitHub Actions |
| GET | `/api/ci_stability` | CI Stability % — real Allure pass rate per nightly suite (see below), 30-min cached |
| GET | `/api/flaky_tests` | Tests marked `@pytest.mark.flaky`, from `flaky_tests_report.json` |
| GET | `/api/release_feature_coverage` | Release Sanity Coverage (3.4–3.7) — see below |
| GET | `/api/ci_fix_coverage` | `fix/*` PR → ADO work-item link rate, from the snapshot file |
| GET | `/api/sb_qa_coverage` | `sb_qa`-labeled GitHub PR → ADO/mapping-file link rate, from the snapshot file |
| GET | `/api/test_area_breakdown` | User Stories/Tasks classified by test-area keyword, from the snapshot file |
| POST | `/api/refresh` | Invalidate ADO cache and force re-fetch |

All responses are JSON. ADO responses cache for **10 minutes** (`CACHE_TTL` env var); CI Stability
caches separately for **30 minutes** (it walks GitHub run history and downloads an artifact per
workflow, ~7–10s cold — not worth repeating on every page load for data that changes once a day).

### CI Stability % — how it actually works

Computed from each nightly workflow's (`run_nightly_integration.yml`, `run_nightly_e2e.yml`) most
recent `allure-summary-*` GitHub Actions artifact (`widgets/summary.json`) — the same artifact
`.github/workflows/send_slack_report.yml` in `sootballs_tests` reads to build the Slack nightly
notification, so this number always matches Slack by construction. Formula:
`passed / (passed + failed + broken + missing) × 100` — skipped tests excluded from the denominator,
crashed/cancelled ("missing") tests count against stability. If the most recent run has no usable
summary, falls back to the next most recent and flags the result stale with the actual date used
(e.g. "E2E 90.1% (Jul 26)"). This deliberately does **not** use an ADO tag-based convention that was
originally considered for it.

### Release Sanity Coverage (3.4–3.7) — how it actually works

Reads `qa_tooling/amr_master_sanity_status.json` in `sootballs_tests` — 51 TC-SAN sanity cases
spanning the 3.4-3.7 releases, each with a real, previously-verified automation status
(Automated/Partial/Gap/Doc Mismatch). This file is exported by a step added to
`gen_amr_master_sanity_suite_v2.py` (the docx generator that owns this data, currently in
`~/Downloads`, not yet in a repo) — re-run that script to refresh it. Deliberately **not** built
from ADO's own "Feature" work item type: only 39 Features exist project-wide, and only 5 carry any
release tag at all — confirmed too sparse to represent release-level coverage before this was built.

---

## Dashboard Tabs

| Tab | What it shows |
|-----|--------------|
| 🏠 Overview | Sprint KPI cards (incl. CI Stability %), health score, carry-over, bug rate |
| 📈 Trends | KPI trends across sprints (velocity, closure rate, automation %) |
| 🐛 Bug Analytics | PI-wide bug flow (created vs. resolved), lead time, aging, by-area breakdown |
| 👤 Per Engineer | Individual story completion per QA team member |
| 🧪 Test Plans | ADO Test Plans; Test Case overview with raw + distinct-scenario automation rates |
| 🤖 Automation | Mapping file coverage, per-plan progress, AMR WI cross-links, sb_qa PR coverage |
| 🎯 Feature Coverage | Release Sanity Coverage (3.4–3.7) + sprint-scoped Feature → TC traceability |

---

## Automation Tracking Integration

The **🤖 Automation** tab connects three systems:

```
ADO sootballs TCs ←→ ado_test_mapping.yaml ←→ ADO AMR Work Items (sb_qa)
                              ↕
              pytest functions in rapyuta-robotics/sootballs_tests
```

The mapping file lives in the `sootballs_tests` repo:
`qa_tooling/ado_test_mapping.yaml`

A GitHub Action (`link_ado_on_merge.yml`) automatically marks TCs as Automated
and cross-links AMR Work Items whenever the mapping file changes on `devel`.

See [QA Automation Tracking System Guide](QA_Automation_Tracking_System_Guide.docx)
for the complete setup and workflow guide.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ADO_PAT` | — | **Required.** Azure DevOps Personal Access Token |
| `GITHUB_TOKEN` | from config.yaml `github_token`, or unset | Needed for CI Stability, nightly health, sb_qa PR coverage. `gh auth token` works locally if unset and `gh` is authenticated |
| `CONFIG_PATH` | `config.yaml` | Path to config file |
| `MAPPING_FILE` | from config.yaml | Path to `ado_test_mapping.yaml` |
| `COVERAGE_CACHE_FILE` | from config.yaml | Path to `feature_coverage_cache.json` |
| `SNAPSHOT_FILE` | from config.yaml | Path to `qa_snapshot_*.json` |
| `FLAKY_REPORT_FILE` | from config.yaml | Path to `flaky_tests_report.json` |
| `SANITY_FILE` | from config.yaml | Path to `amr_master_sanity_status.json` |
| `CACHE_TTL` | `600` | ADO response cache TTL in seconds (CI Stability caches separately, fixed at 30 min) |
| `PORT` | `5050` | HTTP port |
| `FLASK_DEBUG` | `false` | Enable Flask debug mode |

---

## KPI Reference

`KPI_CALCULATIONS.md` in this repo is **stale — do not use it.** It documents an older CSV/Streamlit
architecture (`compute_kpis.py`, `streamlit_dashboard.py`) that no longer exists anywhere in this
codebase; confirmed orphaned with zero references from `app.py` or `frontend/index.html`. The
current KPI formulas live as docstrings/comments directly in `ado_client.py` next to each
calculation (`_compute()`, `get_ci_stability()`, `get_release_feature_coverage()`, etc.) — that's
the actual source of truth, kept next to the code it describes rather than in a separate doc that
can drift out of sync with it.
