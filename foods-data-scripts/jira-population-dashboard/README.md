# Jira Hygiene Dashboard

A **fully self-contained** tool that reconciles two competing "completion"
signals for every completed JIRA ticket and flags the ones that disagree. It is
the only feature in this folder — a single script plus its own dashboard.

## Why it exists

Delivery-efficiency metrics (lead time, cycle time, throughput) all depend on a
single, trustworthy "this ticket is done" event. In practice two different dates
are commonly used and they do **not** always agree:

| Signal         | Source                                                  | Meaning                                         |
| -------------- | ------------------------------------------------------- | ----------------------------------------------- |
| `completed_at` | The ticket's `resolutiondate` field                     | The value most metric tools treat as completion |
| `first_done`   | First changelog transition into a Done-category status  | The first time work genuinely reached "Done"    |

When these land in **different months** the ticket is flagged
`COMPLETED AND FIRST DONE DIFFER`.

## What it does

1. Reads team → project mapping from [data/jira.csv](data/jira.csv) (the
   `JIRA Board Name/Key` column is the project key, joined to `Team Name` /
   `Product Group`, with `TPM` used as the card owner).
2. Loads the JIRA status catalogue once to map each status to its category
   (`new` / `indeterminate` / `done`).
3. For each project, fetches `statusCategory = Done` tickets resolved within the
   reporting window (plus a ±45-day buffer to catch edge cases).
4. For each ticket, reads `resolutiondate` (`completed_at`) and walks the issue
   changelog to find the earliest transition into a Done-category status
   (`first_done`).
5. Classifies each ticket's **population** and writes one row per relevant
   ticket to `outputs/csv/jirapopulation.csv`, then renders
   `outputs/html/jirapopulation.html`.

## Population labels

| Label                             | Condition                                                        |
| --------------------------------- | ---------------------------------------------------------------- |
| `COMPLETED AND FIRST DONE DIFFER` | Both dates present, but in different months                      |
| `ALIGNED`                         | Both dates present and in the same month                         |
| `NO FIRST DONE`                   | `completed_at` present but no Done transition found in changelog |
| `FIRST DONE ONLY`                 | A Done transition exists but no `resolutiondate`                 |

## Setup

```bash
cd jira-population-dashboard
python -m venv venv
venv\Scripts\activate        # Windows  (source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
```

## Authentication

Priority: env vars → CLI options → cached values → interactive prompt.

```bash
set JIRA_HOST=https://your-org.atlassian.net
set JIRA_EMAIL=you@company.com
set JIRA_API_TOKEN=<api-token>
```

## Usage

```bash
# Current month, all projects — build the dashboard and open the HTML file in a browser
python jira_population.py --html --serve

# A specific reporting month, write CSV + HTML
python jira_population.py --month 2026-07 --csv --html

# An explicit window, restricted to two projects
python jira_population.py --since 2026-06-01 --until 2026-06-30 \
    --project FSCPC --project FSMTC --csv --html

# Specific tickets only (period filter is ignored)
python jira_population.py --ticket FSCPC-1022 --ticket FSMTC-905 --csv

# JSON to stdout (no file), with diagnostics
python jira_population.py --month 2026-07 --json --verbose
```

### Options

| Option           | Description                                                         |
| ---------------- | ----------------------------------------------------------------- |
| `--projects-csv` | Team/project mapping CSV (default `data/jira.csv`)                 |
| `--project`      | Restrict to one project key (repeatable)                          |
| `--ticket`       | Analyze specific issue keys (repeatable); ignores the period filter |
| `--month`        | Reporting month `YYYY-MM` (defaults to the current month)         |
| `--since` / `--until` | Explicit date window `YYYY-MM-DD` (overrides `--month`)       |
| `--host`         | JIRA base URL (env `JIRA_HOST`)                                    |
| `--email`        | JIRA email for Basic auth (env `JIRA_EMAIL`, cached)              |
| `--token`        | JIRA API token (env `JIRA_API_TOKEN`, cached)                     |
| `--reset-token`  | Clear cached token and email, then re-prompt                      |
| `--csv`          | Export to `outputs/csv/jirapopulation.csv`                        |
| `--csv-path`     | Custom CSV output path                                             |
| `--html`         | Build `outputs/html/jirapopulation.html`                          |
| `--json`         | Emit raw rows as JSON to stdout                                    |
| `--serve [PORT]` | Open the generated HTML dashboard directly; implies `--html` |
| `--verbose`      | Detailed per-ticket / API diagnostics                             |

## Output columns (`outputs/csv/jirapopulation.csv`)

`product_group`, `team_name`, `board_key`, `project_key`, `ticket_key`,
`issue_type`, `summary`, `status`, `status_category`, `completed_at`,
`first_done`, `completed_month`, `first_done_month`, `completed_in_period`,
`first_done_in_period`, `population`, `url`.

## Dashboard

Each team is shown as a **signal card** — Signal / What this means / Owner /
Next action — with an expandable **"N ticket differences"** panel listing each
differing ticket (Ticket, Summary, Population, Completed_at, First Done). Filter
by search text, quality status (has differences / fully aligned), or product
group.

`--serve` opens the generated HTML file directly in your browser. To open it
manually instead:

```bash
start outputs/html/jirapopulation.html
```
