# QA Metrics Dashboard

A Flask + React single-page application that pulls **all data live from Azure DevOps**.  
No CSV uploads. No spreadsheets. No manual data entry.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Browser  →  frontend/index.html  (React 18 + CDN)  │
│                       ↕  fetch /api/*               │
│            app.py  (Flask 3, port 5050)              │
│                       ↕  REST                       │
│            ado_client.py  (ADO API v7.0)             │
│                       ↕                             │
│   ADO AMR project (Work Items)                       │
│   ADO sootballs project (Test Plans / Test Cases)    │
└─────────────────────────────────────────────────────┘
```

Data sources — **ADO only**:

| Data | ADO project | ADO API |
|------|-------------|---------|
| User Stories, Bugs, sprint KPIs | AMR | WIQL + workitemsbatch |
| Test Plans, Test Cases, automation status | sootballs | testplan/plans + WIQL |
| Automation Work Items (sb_qa tag) | AMR | WIQL + workitemsbatch |
| Mapping file enrichment (optional) | n/a | local YAML file |

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | |
| ADO Personal Access Token | Scopes: **Work Items** Read, **Test Management** Read |
| ADO projects | `AMR` (work items) + `sootballs` (test plans) under `rapyuta-robotics` org |

---

## Configuration

Edit `config.yaml`:

```yaml
ado:
  organization: rapyuta-robotics
  project: AMR
  testplans_project: sootballs

qa_team_members:
  - "First Last"    # display name exactly as shown in ADO Settings → Users

qa_tag: "sb_qa"     # WIs with this tag are always included regardless of assignee

# Optional: path to sootballs_tests/qa_tooling/ado_test_mapping.yaml
# Set to null (default) or override via MAPPING_FILE env var.
sootballs_mapping_file: null

sprints:
  - label: "PI2 Sprint 4"
    iteration_path: "AMR\\26PI2\\PI2 Sprint 4"
    start: "2026-05-11"
    end:   "2026-05-22"
    pi:    "26PI2"
```

---

## Running Locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set ADO PAT (required)
export ADO_PAT=<your-personal-access-token>

# 3. Optional: point to the automation mapping file
export MAPPING_FILE=/path/to/sootballs_tests/qa_tooling/ado_test_mapping.yaml

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
| GET | `/api/bugs?sprint=<label>` | Bug detail list with state, priority, assignee |
| GET | `/api/testplans` | Test plan list with TC counts and automation rate |
| GET | `/api/automation_coverage` | Three-way join: mapping YAML + ADO TCs + AMR WIs |
| POST | `/api/refresh` | Invalidate cache and force re-fetch from ADO |

All responses are JSON. The server caches ADO responses for **10 minutes** (configurable via `CACHE_TTL` env var).

---

## Dashboard Tabs

| Tab | What it shows |
|-----|--------------|
| 🏠 Overview | Sprint KPI cards, health score, carry-over, bug rate |
| 📈 Trends | KPI trends across sprints (velocity, closure rate, automation %) |
| 🐛 Bug Analytics | Bug volume, state breakdown, priority distribution |
| 👤 Per Engineer | Individual story/bug metrics per QA team member |
| 🧪 Test Plans | ADO Test Plans with TC counts and automation status |
| 🤖 Automation | Mapping file coverage, per-plan progress, AMR WI cross-links |

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
| `CONFIG_PATH` | `config.yaml` | Path to config file |
| `MAPPING_FILE` | from config.yaml | Path to `ado_test_mapping.yaml` |
| `CACHE_TTL` | `600` | Cache TTL in seconds |
| `PORT` | `5050` | HTTP port |
| `FLASK_DEBUG` | `false` | Enable Flask debug mode |

---

## ADO Configuration Roadmap

Four ADO project configuration changes are required to unlock additional KPIs
(Estimation Slip Rate, CI Stability %, Test Execution Hours, sprint-level test metrics).

See [TIER3_CHANGES.md](TIER3_CHANGES.md) for the full specification and approval checklist.

---

## KPI Reference

All KPI formulas, field mappings, and calculation logic are documented in
[KPI_CALCULATIONS.md](KPI_CALCULATIONS.md).
