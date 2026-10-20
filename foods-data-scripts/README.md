📊 **[Dashboard](https://upgraded-adventure-wr1zw3e.pages.github.io/outputs/html/parent.html)** — Live metrics and visualisations

# Data Scripts — Python CLI

A Python CLI toolkit for data extraction from **GitHub**, **PagerDuty**, **JIRA**, and **BMC Remedy ITSM** APIs. It exports data to CSV / HTML dashboards and runs automatically via GitHub Actions.

---

## Project Structure

```
foods-data-scripts/
├── data/                       # Input CSV files (teams, repos, projects)
│   ├── itsm_bmc_groups.csv
│   ├── jira.csv
│   ├── pagerduty.csv
│   └── repos.csv
├── docs/                       # Detailed usage guides per command
├── outputs/
│   ├── csv/                    # Generated CSV reports
│   ├── html/                   # Generated HTML dashboards
│   └── json/                   # Snapshot JSON (repo health)
├── src/datascripts/
│   ├── cli.py                  # Main CLI entry point
│   ├── commands/               # One file per command group
│   └── services/               # Shared utilities (token manager, CSV helper)
├── .github/workflows/          # GitHub Actions pipelines
├── Dockerfile
├── requirements.txt
└── setup.py
```

### Command groups

| Group | Commands | Purpose |
| ------------ | ----------------------------------------------- | ------------------------------------------------ |
| `github`     | `metrics`, `techstack`, `repo-health`, `commits`, `network`, `catalog-scan`, `actions-health` | Repository metrics, tech-stack, branch hygiene, CI health |
| `pagerduty`  | `incidents`, `teams`, `services`, `standards`   | On-call & service health compliance             |
| `jira`       | `tickets`, `sprint-issues`, `backlog-health`, `sprint-performance` | Backlog quality, sprint data & per-team performance dashboard |
| `itsm`       | `incidents`, `change-requests`                  | BMC Remedy incident & change extraction         |
| `azure`      | `scan`, `kv-expiry`                             | Azure resource inventory, Key Vault secret expiry tracking |
| `dynatrace`  | `scan`                                          | Service discovery and alert config audit        |
| `estate`     | `report`                                        | Unified Foods portfolio dashboard (HTML)        |

---

## Automated Workflows

All scheduled data refresh is handled by a **single consolidated pipeline** — no fragmented cron jobs.

### Daily Refresh Pipeline — `daily_refresh.yml`

**Schedule**: Every day at **06:00 AM UTC**
**Manual trigger**: `workflow_dispatch` (with optional `run_repo_health` boolean input)
**Commit strategy**: All stage outputs are collected and pushed in **one GPG-signed commit per day**

Stages run sequentially — each depends on the previous one passing:

| # | Stage | Frequency | Key outputs |
| -- | ----- | --------- | ----------- |
| 1 | **Semgrep** security scan | Daily | *(no data files)* |
| 2 | **PagerDuty** standards | Daily | `outputs/csv/pagerduty_standard.csv` |
| 3 | **Jira** backlog health + sprint performance | Daily | `outputs/csv/backlog_health.csv`, `outputs/csv/sprint_performance.csv` |
| 4 | **ITSM** incidents + change requests | Daily | `outputs/csv/incident_dump.csv`, `outputs/csv/change_requests_dump.csv` |
| 5 | **GitHub** metrics + techstack | Daily | `outputs/csv/github_metrics.csv`, `data/repos.csv` |
| 6 | **Repo Health** scan | **Mondays only** | `outputs/csv/repo_health.csv`, `outputs/json/repo_health/` |
| 7 | **Azure Resource Graph** scan | Daily | `outputs/csv/azure_resources.csv` |
| 8 | **Catalog-info.yaml** scan | Daily | `outputs/csv/catalog_info.csv` |
| 9 | **GitHub Actions Health** | Daily | `outputs/csv/actions_health.csv` |
| 10 | **Estate Report** (unified dashboard) | Daily | `outputs/html/estate_report.html` |

> To force Stage 6 on a non-Monday, trigger the workflow manually with `run_repo_health = true`.

The individual workflow files (`pagerduty_standard_refresh.yml`, `itsm_incident_refresh.yml`, etc.) are kept for **targeted manual runs** and `push`-triggered runs only — their scheduled crons have been removed.

---

## Local Setup

### Prerequisites

- Python 3.12+

### Install

```bash
# 1. Clone the repository
git clone <repo-url>
cd foods-data-scripts

# 2. Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install in development mode
pip install -e .

# 3a. (Optional) Install dev tools (ruff, pytest)
pip install -e ".[dev]"

# 4. Verify
data-scripts --help
```

### Environment variables

```bash
# GitHub
export GITHUB_TOKEN=<pat>

# PagerDuty
export PAGERDUTY_API_TOKEN=<token>

# Jira
export JIRA_HOST=https://jira-marksandspencer-app.atlassian.net
export JIRA_EMAIL=firstname.lastname@mnscorp.net
export JIRA_API_TOKEN=<token>

# BMC Remedy ITSM
export ITSM_BASE_URL=https://your-bmc-instance
export ITSM_USERNAME=<user>
export ITSM_PASSWORD=<password>

# Azure (for resource scan and Key Vault expiry, OIDC in CI)
export AZURE_SUBSCRIPTION_ID=<subscription-id>

# Dynatrace (for service and alert discovery)
export DYNATRACE_ENV_URL=https://<env-id>.live.dynatrace.com
export DYNATRACE_API_TOKEN=<token>
```

Tokens are cached locally after first use. Pass `--reset-token` to any command to clear the cache.

### Quick-start examples

```bash
# GitHub — daily metrics for last 14 days
data-scripts github metrics data/repos.csv --daily --days 14

# GitHub — tech stack analysis (updates repos.csv)
data-scripts github techstack --repos-csv data/repos.csv --update-csv

# GitHub — weekly repo / branch health
data-scripts github repo-health data/repos.csv

# PagerDuty — service standards compliance
data-scripts pagerduty standards --teams-csv data/pagerduty.csv --all --csv

# Jira — backlog health report
data-scripts jira backlog-health --projects-csv data/jira.csv --estimate-velocity

# Jira — per-team sprint performance (CSV powers the static HTML dashboard)
# Feature vs KTLO is derived from a "KTLO" label (case-insensitive) on the
# ticket or its parent epic; velocity is normalised for 3-week sprints.
data-scripts jira sprint-performance --projects-csv data/jira.csv --sprints 6 --csv

# ITSM — last 60 days of incidents
data-scripts itsm incidents --groups-csv data/itsm_bmc_groups.csv --days 60 --csv

# ITSM — last 60 days of change requests
data-scripts itsm change-requests --days 60 --cr-product-tier1 "Food" --csv

# GitHub — CI health across all repos
data-scripts github actions-health --repos-csv data/repos.csv

# GitHub — catalog-info.yaml coverage scan
data-scripts github catalog-scan --repos-csv data/repos.csv

# Azure — resource inventory via Resource Graph
data-scripts azure scan --subscription <subscription-id>

# Azure — Key Vault secret expiry tracker
data-scripts azure kv-expiry --resources-csv outputs/csv/azure_resources.csv

# Dynatrace — service and alert discovery
data-scripts dynatrace scan

# Estate — unified portfolio dashboard
data-scripts estate report
```

### Jira board mapping (sprint-performance)

`data/jira.csv` maps each team to a Jira board. By default the board is resolved
from the `JIRA Board Name/Key` column, but a project can expose several boards
(old + active) and the wrong one may be picked. To pin the exact board, fill the
optional **`JIRA Board ID`** column with the numeric id from the board URL
(e.g. `.../boards/1234` → `1234`). When present it is used verbatim and board
resolution is skipped; when blank the command falls back to key resolution, so
already-correct teams need no change. Non-numeric values are ignored with a
warning.

To seed the column in one pass (fills only blank cells, so hand-made corrections
are never overwritten and re-runs are safe), run the one-off bootstrap script,
then hand-correct any known-wrong rows and commit:

```bash
JIRA_EMAIL=you@example.com JIRA_API_TOKEN=... \
  python scripts/seed_board_ids.py            # add --dry-run to preview
```

### Docker

```bash
docker build -t data-scripts .

docker run -it --rm \
  -v "$(pwd)":/app \
  -e GITHUB_TOKEN="$GITHUB_TOKEN" \
  data-scripts github metrics data/repos.csv --daily --days 14
```

---

## Further Reading

- [GitHub Metrics Guide](docs/GITHUB_METRICS_GUIDE.md)
- [Tech Stack Analysis Guide](docs/TECHSTACK_ANALYSIS_GUIDE.md)
- [PagerDuty Standards Guide](docs/PAGERDUTY_STANDARDS_GUIDE.md)
- [ITSM Usage Guide](docs/ITSM_USAGE_GUIDE.md)
