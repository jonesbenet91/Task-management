"""JIRA sprint performance command.

Builds a per-team, per-sprint performance dataset across the last N closed sprints
for each board in ``data/jira.csv``. Reuses the same JIRA connection approach as
``jira backlog-health`` (``JiraApiService`` + email/token resolution).

Sprint-scope metrics (committed vs completed, scope added/removed, carryover) come
from the Greenhopper sprint-report endpoint, with a graceful fallback to the
standard search API when that endpoint is unavailable. Feature vs KTLO is derived
from a JIRA label (default ``KTLO``, case-insensitive) on the ticket *or* on its
parent epic.
"""

import json
import os
import re
import sys
import tempfile
from datetime import timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click
import requests
from dateutil import parser as date_parser
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..services import TokenManager, JiraApiService, JiraAuthError

console = Console()

DEFAULT_HOST = "https://jira-marksandspencer-app.atlassian.net"
OUTPUT_CSV = Path("outputs/csv/sprint_performance.csv")
DEFAULT_KTLO_LABEL = "KTLO"

# Matches a well-formed Jira issue key (e.g. FSMTC-203) so we never feed garbage
# epic keys into a JQL `issuekey in (...)` clause.
ISSUE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]+-\d+$')

# Story point custom fields (same list as backlog-health / sprint-issues)
STORY_POINT_FIELDS = [
    'customfield_10033',
    'customfield_10026',
    'customfield_10016',
    'customfield_10002',
    'customfield_10018',
    'customfield_10020',
    'customfield_10021',
    'customfield_10028',
    'customfield_10034',
    'story_points',
    'storypoints',
]

CSV_HEADERS = [
    'product_group', 'team_name', 'board_key', 'board_id',
    'sprint_id', 'sprint_name', 'sprint_state', 'sprint_start', 'sprint_end',
    'ticket_key', 'issue_type', 'parent_epic_key', 'parent_epic_name',
    'status', 'status_category',
    'completed', 'added_during_sprint', 'removed', 'carryover',
    'story_points', 'is_ktlo', 'parent_epic_is_ktlo', 'category', 'url',
]


def extract_story_points(fields: dict) -> Optional[float]:
    """Extract story points from various possible custom fields."""
    for field in STORY_POINT_FIELDS:
        value = fields.get(field)
        if value is None or value == '':
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict) and 'value' in value:
            try:
                return float(value['value'])
            except (ValueError, TypeError):
                continue
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _report_issue_points(issue: dict) -> Optional[float]:
    """Extract the estimate (story points) from a Greenhopper sprint-report issue."""
    stat = issue.get('estimateStatistic') or {}
    field_value = (stat.get('statFieldValue') or {}).get('value')
    if isinstance(field_value, (int, float)):
        return float(field_value)
    return None


def extract_parent_epic(fields: dict) -> Tuple[str, str]:
    """Return ``(epic_key, epic_name)`` for an issue's parent epic, if any.

    Prefers the modern ``parent`` field (present when the immediate parent is an
    Epic, e.g. a Story under an Epic) so we can show the epic name, and falls back
    to the legacy Epic Link custom field (``customfield_10014``, key only).
    """
    parent = fields.get('parent') or {}
    if parent:
        p_fields = parent.get('fields') or {}
        p_type = ((p_fields.get('issuetype') or {}).get('name') or '').lower()
        if p_type == 'epic':
            return (parent.get('key') or ''), (p_fields.get('summary') or '')

    epic_key = fields.get('customfield_10014')
    if isinstance(epic_key, str) and epic_key.strip():
        return epic_key.strip(), ''
    return '', ''


def extract_report_epic_key(issue: dict) -> str:
    """Best-effort epic key for a Greenhopper sprint-report issue (e.g. punted).

    Report issues carry only the Epic Link *key* (no name), exposed via the
    ``epic`` field or the ``epicField`` object. Returns ``''`` when absent.
    """
    epic = issue.get('epic')
    if isinstance(epic, str) and epic.strip():
        return epic.strip()
    text = (issue.get('epicField') or {}).get('text')
    if isinstance(text, str) and text.strip():
        return text.strip()
    return ''


def resolve_board(
    service: JiraApiService,
    project_or_board_key: str,
    verbose: bool = False,
) -> Optional[Tuple[str, str]]:
    """Resolve a board (id, name) for a project/board key, preferring scrum boards."""
    try:
        response = service.session.get(
            f"{service.host}/rest/agile/1.0/board",
            params={'projectKeyOrId': project_or_board_key, 'maxResults': 50},
            timeout=30,
        )
    except requests.RequestException as e:
        if verbose:
            console.print(f"[dim]  {project_or_board_key}: board lookup error: {e}[/dim]")
        return None

    if response.status_code != 200:
        if verbose:
            console.print(
                f"[dim]  {project_or_board_key}: board lookup failed "
                f"(HTTP {response.status_code})[/dim]"
            )
        return None

    boards = response.json().get('values', [])
    if not boards:
        if verbose:
            console.print(f"[dim]  {project_or_board_key}: no boards found[/dim]")
        return None

    scrum_boards = [b for b in boards if (b.get('type') or '').lower() == 'scrum']
    board = scrum_boards[0] if scrum_boards else boards[0]
    return str(board.get('id')), board.get('name', '')


def _to_epoch(raw: Optional[str]) -> Optional[float]:
    """Parse a Jira date string to a UTC epoch (float), or None if unusable.

    Jira returns ISO8601 (e.g. ``2026-06-30T15:00:00.000Z`` or with an offset).
    Naive values are assumed to be UTC so aware/naive values never collide.
    """
    if not raw:
        return None
    try:
        dt = date_parser.parse(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _sprint_recency(sprint: dict) -> Tuple[int, float, int]:
    """Sort key ranking sprints by recency (most recent = largest).

    Uses the latest of endDate / completeDate / startDate, tie-breaking on the
    numeric sprint id. Sprints with no usable date sort last (oldest) so a board
    whose sprints all lack dates never masks ones that have them.
    """
    best: Optional[float] = None
    for field in ('endDate', 'completeDate', 'startDate'):
        epoch = _to_epoch(sprint.get(field))
        if epoch is not None and (best is None or epoch > best):
            best = epoch
    try:
        sid = int(sprint.get('id') or 0)
    except (TypeError, ValueError):
        sid = 0
    return (0 if best is None else 1, best or 0.0, sid)


def fetch_closed_sprints(
    service: JiraApiService,
    board_id: str,
    limit: int,
    verbose: bool = False,
) -> List[dict]:
    """Fetch the most recent ``limit`` closed sprints for a board.

    The Jira Agile sprint endpoint is paginated (50/page) and returns closed
    sprints oldest-first, so we must page through *all* of them before choosing
    the most recent — otherwise boards with more than 50 closed sprints only ever
    expose their oldest sprints. Results are sorted by recency (latest of
    endDate / completeDate / startDate, then sprint id) and truncated to ``limit``.
    """
    collected: List[dict] = []
    start_at = 0
    page_size = 50
    max_pages = 40  # safety cap (~2000 sprints) to avoid a pathological loop
    pages = 0

    for _ in range(max_pages):
        try:
            response = service.session.get(
                f"{service.host}/rest/agile/1.0/board/{board_id}/sprint",
                params={'state': 'closed', 'startAt': start_at, 'maxResults': page_size},
                timeout=30,
            )
        except requests.RequestException as e:
            if verbose:
                console.print(f"[dim]  board {board_id}: sprint lookup error: {e}[/dim]")
            break

        if response.status_code != 200:
            if verbose:
                console.print(
                    f"[dim]  board {board_id}: sprint lookup failed "
                    f"(HTTP {response.status_code})[/dim]"
                )
            break

        data = response.json()
        values = data.get('values', [])
        collected.extend(values)
        pages += 1

        is_last = data.get('isLast')
        if is_last is None:
            # Some deployments omit isLast — fall back to a short-page heuristic.
            is_last = len(values) < page_size
        if is_last or not values:
            break
        start_at += len(values)

    collected.sort(key=_sprint_recency, reverse=True)

    if verbose:
        console.print(
            f"[dim]  board {board_id}: fetched {len(collected)} closed sprint(s) "
            f"across {pages} page(s); using most recent {min(limit, len(collected))}[/dim]"
        )

    return collected[:limit]


def fetch_sprint_report(
    service: JiraApiService,
    board_id: str,
    sprint_id: str,
    verbose: bool = False,
) -> Optional[dict]:
    """Fetch the (unofficial) Greenhopper sprint report for a board/sprint."""
    try:
        response = service.session.get(
            f"{service.host}/rest/greenhopper/1.0/rapid/charts/sprintreport",
            params={'rapidViewId': board_id, 'sprintId': sprint_id},
            timeout=30,
        )
    except requests.RequestException as e:
        if verbose:
            console.print(f"[dim]  sprint {sprint_id}: report error: {e}[/dim]")
        return None

    if response.status_code != 200:
        if verbose:
            console.print(
                f"[dim]  sprint {sprint_id}: report unavailable "
                f"(HTTP {response.status_code}) — using fallback[/dim]"
            )
        return None

    try:
        return response.json()
    except ValueError:
        if verbose:
            console.print(f"[dim]  sprint {sprint_id}: report parse error — using fallback[/dim]")
        return None


def parse_sprint_report(report: dict) -> Dict[str, set]:
    """Extract issue-key dispositions from a Greenhopper sprint report."""
    contents = (report or {}).get('contents', {}) or {}

    def keys_of(name: str) -> set:
        return {i.get('key') for i in (contents.get(name) or []) if i.get('key')}

    return {
        'completed': keys_of('completedIssues'),
        'not_completed': keys_of('issuesNotCompletedInCurrentSprint'),
        'punted': keys_of('puntedIssues'),
        'completed_elsewhere': keys_of('issuesCompletedInAnotherSprint'),
        'added': set((contents.get('issueKeysAddedDuringSprint') or {}).keys()),
    }


def fetch_ktlo_epics(
    service: JiraApiService,
    epic_keys: set,
    ktlo_lower: str,
    verbose: bool = False,
) -> set:
    """Return the subset of ``epic_keys`` whose Jira issue carries the KTLO label.

    Epic labels are not present on the child-issue search payload, so we look them
    up directly. Keys are validated and batched into ``issuekey in (...)`` queries.
    """
    valid = sorted(k for k in epic_keys if k and ISSUE_KEY_RE.match(k))
    ktlo_epics: set = set()
    for start in range(0, len(valid), 100):
        chunk = valid[start:start + 100]
        jql = f"issuekey in ({','.join(chunk)})"
        try:
            issues = service.search_issues(jql, max_results=100, fetch_all=True)
        except JiraAuthError:
            raise
        except Exception as e:
            if verbose:
                console.print(f"[dim]  epic KTLO lookup failed for a batch: {e}[/dim]")
            continue
        for issue in issues:
            labels = [str(label).lower() for label in ((issue.get('fields') or {}).get('labels') or [])]
            if ktlo_lower in labels:
                ktlo_epics.add(issue.get('key'))
    return ktlo_epics


def apply_epic_ktlo(rows: List[dict], ktlo_epics: set) -> None:
    """Mark rows whose parent epic is KTLO; KTLO epics make their tickets KTLO too."""
    for row in rows:
        if row.get('parent_epic_key') in ktlo_epics:
            row['parent_epic_is_ktlo'] = True
            if not row['is_ktlo']:
                row['is_ktlo'] = True
                row['category'] = 'KTLO'


def dedupe_subtask_story_points(rows: List[dict], parent_of: Dict[str, str]) -> int:
    """Avoid double-counting parent + sub-task estimates within a sprint.

    A sub-task's story points are only counted when its parent has **no** point
    value; if the parent carries a positive estimate the sub-task's points are
    zeroed (the parent already represents the work). ``parent_of`` maps a
    sub-task ticket key to its parent issue key. Returns the number of rows
    adjusted.
    """
    sp_by_key = {r['ticket_key']: r.get('story_points') for r in rows}
    adjusted = 0
    for row in rows:
        parent_key = parent_of.get(row['ticket_key'])
        if not parent_key:
            continue
        parent_sp = sp_by_key.get(parent_key)
        own_sp = row.get('story_points')
        if parent_sp is not None and parent_sp > 0 and own_sp is not None and own_sp > 0:
            row['story_points'] = 0.0
            adjusted += 1
    return adjusted


def build_sprint_rows(
    service: JiraApiService,
    target: dict,
    sprint: dict,
    ktlo_label: str,
    verbose: bool = False,
) -> List[dict]:
    """Build per-ticket rows for a single sprint of a board/team.

    Sub-task story points are de-duplicated against their parent (see
    ``dedupe_subtask_story_points``) so parent + sub-task estimates are never
    counted twice in the sprint totals.
    """
    sprint_id = str(sprint.get('id'))
    board_id = target['board_id']
    ktlo_lower = ktlo_label.lower()

    common = {
        'product_group': target['product_group'],
        'team_name': target['team_name'],
        'board_key': target['board_key'] or '',
        'board_id': board_id,
        'sprint_id': sprint_id,
        'sprint_name': sprint.get('name', ''),
        'sprint_state': sprint.get('state', ''),
        'sprint_start': sprint.get('startDate', ''),
        'sprint_end': sprint.get('endDate', ''),
    }

    report = fetch_sprint_report(service, board_id, sprint_id, verbose=verbose)
    dispositions = parse_sprint_report(report) if report else None

    rows: List[dict] = []
    seen_keys: set = set()
    epic_names: Dict[str, str] = {}  # epic key -> name, to backfill name-less report issues
    parent_of: Dict[str, str] = {}   # sub-task key -> parent key, for story-point de-duplication

    # Issues currently associated with the sprint (full fields incl. labels).
    try:
        issues = service.search_issues(f'sprint = {sprint_id}', max_results=100, fetch_all=True)
    except JiraAuthError:
        raise
    except Exception as e:
        if verbose:
            console.print(f"[dim]  sprint {sprint_id}: issue search error: {e}[/dim]")
        issues = []

    for issue in issues:
        fields = issue.get('fields', {})
        key = issue.get('key', '')
        seen_keys.add(key)

        status_obj = fields.get('status') or {}
        status_category = (status_obj.get('statusCategory') or {}).get('name', '')

        if dispositions is not None:
            completed = key in dispositions['completed'] or key in dispositions['completed_elsewhere']
            added = key in dispositions['added']
        else:
            completed = status_category.lower() == 'done'
            added = None  # unknown without the report

        labels = [str(label).lower() for label in (fields.get('labels') or [])]
        is_ktlo = ktlo_lower in labels
        epic_key, epic_name = extract_parent_epic(fields)
        if epic_key and epic_name:
            epic_names[epic_key] = epic_name

        issuetype = fields.get('issuetype') or {}
        if issuetype.get('subtask'):
            parent_key = (fields.get('parent') or {}).get('key') or ''
            if parent_key:
                parent_of[key] = parent_key

        rows.append({
            **common,
            'ticket_key': key,
            'issue_type': issuetype.get('name', ''),
            'parent_epic_key': epic_key,
            'parent_epic_name': epic_name,
            'status': status_obj.get('name', ''),
            'status_category': status_category,
            'completed': completed,
            'added_during_sprint': added,
            'removed': False,
            'carryover': not completed,
            'story_points': extract_story_points(fields),
            'is_ktlo': is_ktlo,
            'parent_epic_is_ktlo': False,
            'category': 'KTLO' if is_ktlo else 'Feature',
            'url': f"{service.host}/browse/{key}",
        })

    # Removed (punted) issues no longer appear in the sprint search — add from report.
    if dispositions is not None:
        contents = report.get('contents', {}) or {}
        for issue in (contents.get('puntedIssues') or []):
            key = issue.get('key')
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            punted_epic_key = extract_report_epic_key(issue)
            rows.append({
                **common,
                'ticket_key': key,
                'issue_type': issue.get('typeName', ''),
                'parent_epic_key': punted_epic_key,
                'parent_epic_name': epic_names.get(punted_epic_key, ''),
                'status': issue.get('statusName', ''),
                'status_category': '',
                'completed': False,
                'added_during_sprint': key in dispositions['added'],
                'removed': True,
                'carryover': False,
                'story_points': _report_issue_points(issue),
                'is_ktlo': False,
                'parent_epic_is_ktlo': False,
                'category': 'Feature',
                'url': f"{service.host}/browse/{key}",
            })

    dedupe_subtask_story_points(rows, parent_of)
    return rows


def export_csv_report(rows: List[dict], csv_path: str) -> None:
    """Export per-ticket sprint performance rows to CSV."""
    import csv as csv_mod

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv_mod.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in rows:
            out = {}
            for header in CSV_HEADERS:
                value = row.get(header)
                if isinstance(value, bool):
                    out[header] = 'true' if value else 'false'
                elif value is None:
                    out[header] = ''
                else:
                    out[header] = value
            writer.writerow(out)


def _sp(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def display_summary(rows: List[dict]) -> None:
    """Render a per-team, per-sprint summary table to the console."""
    groups: Dict[Tuple[str, str], dict] = {}
    for row in rows:
        gkey = (row['team_name'], row['sprint_id'])
        agg = groups.setdefault(gkey, {
            'team': row['team_name'],
            'sprint_name': row['sprint_name'],
            'sprint_end': row['sprint_end'],
            'completed_sp': 0.0,
            'committed_sp': 0.0,
            'committed_completed_sp': 0.0,
            'added_sp': 0.0,
            'removed_sp': 0.0,
            'carryover_sp': 0.0,
            'completed_count': 0,
            'ktlo_sp': 0.0,
            'feature_sp': 0.0,
        })
        sp = _sp(row['story_points'])
        if row['removed'] is True:
            agg['removed_sp'] += sp
            continue
        # Committed = work in the sprint that was not added mid-sprint.
        if row['added_during_sprint'] is True:
            agg['added_sp'] += sp
        else:
            agg['committed_sp'] += sp
            if row['completed'] is True:
                agg['committed_completed_sp'] += sp
        if row['completed'] is True:
            agg['completed_sp'] += sp
            agg['completed_count'] += 1
        else:
            agg['carryover_sp'] += sp
        if row['is_ktlo'] is True:
            agg['ktlo_sp'] += sp
        else:
            agg['feature_sp'] += sp

    ordered = sorted(groups.values(), key=lambda a: (a['team'], a['sprint_end']))

    table = Table(title="Sprint Performance Summary")
    table.add_column("Team", style="cyan")
    table.add_column("Sprint")
    table.add_column("Committed SP", justify="right")
    table.add_column("Done SP", justify="right")
    table.add_column("Say/Do", justify="right")
    table.add_column("Added", justify="right")
    table.add_column("Removed", justify="right")
    table.add_column("Carryover SP", justify="right")
    table.add_column("Ftr/KTLO SP", justify="right")

    for a in ordered:
        committed = a['committed_sp']
        say_do = f"{(a['committed_completed_sp'] / committed * 100):.0f}%" if committed > 0 else '—'
        table.add_row(
            a['team'],
            a['sprint_name'],
            f"{committed:.0f}",
            f"{a['completed_sp']:.0f}",
            say_do,
            f"{a['added_sp']:.0f}",
            f"{a['removed_sp']:.0f}",
            f"{a['carryover_sp']:.0f}",
            f"{a['feature_sp']:.0f}/{a['ktlo_sp']:.0f}",
        )

    console.print(table)


@click.command('sprint-performance')
@click.option('--projects-csv', default='data/jira.csv', help='CSV with projects -> team mapping')
@click.option('--project', multiple=True, help='Project/board key to restrict to (repeatable)')
@click.option('--board', 'boards', multiple=True, help='Explicit board ID to process directly (repeatable)')
@click.option('--host', envvar='JIRA_HOST', default=DEFAULT_HOST, help='JIRA base URL')
@click.option('--email', envvar='JIRA_EMAIL', help='JIRA user email for Basic auth')
@click.option('--token', envvar='JIRA_API_TOKEN', help='JIRA API token')
@click.option('--reset-token', is_flag=True, help='Clear cached token and email, then re-prompt')
@click.option('--sprints', default=5, show_default=True, help='Number of recent closed sprints per board')
@click.option('--ktlo-label', default=DEFAULT_KTLO_LABEL, show_default=True,
              help='JIRA label that marks KTLO work (otherwise Feature)')
@click.option('--verbose', is_flag=True, help='Show detailed diagnostics')
@click.option('--csv', 'output_csv_flag', is_flag=True, help='Export CSV to outputs/csv/')
@click.option('--csv-path', help='Custom CSV output path')
@click.option('--json', 'output_json_flag', is_flag=True, help='Output as JSON')
def sprint_performance_command(
    projects_csv: str,
    project: Tuple[str, ...],
    boards: Tuple[str, ...],
    host: str,
    email: Optional[str],
    token: Optional[str],
    reset_token: bool,
    sprints: int,
    ktlo_label: str,
    verbose: bool,
    output_csv_flag: bool,
    csv_path: Optional[str],
    output_json_flag: bool,
):
    """Measure per-team sprint performance across the last N closed sprints.

    Produces one row per ticket per sprint (velocity, committed vs completed,
    scope added/removed, carryover, ticket type, and Feature vs KTLO).

    Boards come from the --projects-csv mapping. If a row's optional
    'JIRA Board ID' column holds a numeric id it is used directly (board
    resolution is skipped); otherwise the board is resolved from
    'JIRA Board Name/Key'. Use scripts/seed_board_ids.py to bootstrap the column.
    """
    email_cache = Path(tempfile.gettempdir()) / 'jira_email.cache'
    token_manager = TokenManager('jira_token.cache')

    # Handle reset: clear both token and email caches
    if reset_token:
        for cache in (token_manager.cache_file, email_cache):
            if cache.exists():
                cache.unlink()
        console.print("[yellow]Cached credentials cleared.[/yellow]\n")

    # Resolve email (visible input — not sensitive)
    jira_email = email
    if not jira_email:
        for env_var in ('JIRA_EMAIL', 'JIRA_USER'):
            jira_email = os.environ.get(env_var)
            if jira_email:
                break
    if not jira_email and email_cache.exists():
        jira_email = email_cache.read_text().strip() or None
    if not jira_email and sys.stdin.isatty():
        jira_email = click.prompt('JIRA email address (will be cached; set JIRA_EMAIL env to override)')
        if jira_email:
            email_cache.write_text(jira_email)
            console.print("[green]Email cached for future runs.[/green]")
    if not jira_email:
        console.print("[red]Error: JIRA email required. Use --email or set JIRA_EMAIL env var.[/red]")
        raise SystemExit(1)

    # Resolve token (hidden input — sensitive)
    api_token = token_manager.get_token(
        cli_token=token,
        env_vars=['JIRA_API_TOKEN'],
        reset=False,
        prompt_text='JIRA API token (will be cached; set JIRA_API_TOKEN env to override)',
    )
    if not api_token:
        console.print("[red]Error: JIRA API token required. Use --token or set JIRA_API_TOKEN env var.[/red]")
        raise SystemExit(1)

    jira_host = host or DEFAULT_HOST

    # Build the list of targets (board key -> team) from the CSV.
    project_team_map: Dict[str, Dict[str, str]] = {}
    if projects_csv and Path(projects_csv).exists():
        import csv as csv_mod
        with open(projects_csv, newline='', encoding='utf-8-sig') as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                if not any(v.strip() for v in row.values() if v):
                    continue
                row = {k: (v.strip() if v else '') for k, v in row.items()}
                board_key = row.get('JIRA Board Name/Key', '')
                board_id_raw = row.get('JIRA Board ID', '')
                team_name = row.get('Team Name', '') or 'Unknown'
                product_group = row.get('Product Group', '') or 'Unknown'
                if board_key and board_key not in project_team_map:
                    board_id_val = None
                    if board_id_raw:
                        if board_id_raw.isdigit():
                            board_id_val = board_id_raw
                        else:
                            console.print(
                                f"[yellow]Ignoring non-numeric 'JIRA Board ID' "
                                f"'{board_id_raw}' for {board_key}; resolving by key "
                                f"instead.[/yellow]"
                            )
                    project_team_map[board_key] = {
                        'team_name': team_name,
                        'product_group': product_group,
                        'board_id': board_id_val,
                    }

    targets: List[dict] = []
    selected_keys = list(project) if project else list(project_team_map.keys())
    for board_key in dict.fromkeys(selected_keys):
        info = project_team_map.get(board_key, {})
        targets.append({
            'board_key': board_key,
            'board_id': info.get('board_id'),
            'team_name': info.get('team_name', board_key),
            'product_group': info.get('product_group', 'Unknown'),
        })
    for board_id in dict.fromkeys(boards):
        targets.append({
            'board_key': None,
            'board_id': str(board_id),
            'team_name': f'Board {board_id}',
            'product_group': 'Unknown',
        })

    if not targets:
        console.print("[red]Error: No targets. Use --projects-csv, --project, or --board.[/red]")
        raise SystemExit(1)

    console.print(f"Measuring sprint performance for [bold]{len(targets)}[/bold] board(s)\n")

    # Initialize service and validate authentication.
    service = JiraApiService(jira_host, jira_email, api_token)
    try:
        auth_resp = service.session.get(f"{service.host}/rest/api/3/myself", timeout=15)
        if auth_resp.status_code == 401:
            console.print("[red]Error: Authentication failed (HTTP 401). Check your JIRA API token.[/red]")
            console.print("[yellow]Hint: Use --reset-token to re-enter your token, or set JIRA_API_TOKEN env var.[/yellow]")
            raise SystemExit(1)
        elif auth_resp.status_code == 403:
            console.print("[red]Error: Access forbidden (HTTP 403). Your token may lack permissions.[/red]")
            raise SystemExit(1)
        elif auth_resp.status_code == 200:
            user_info = auth_resp.json()
            display_name = user_info.get('displayName', user_info.get('emailAddress', 'unknown'))
            console.print(f"Authenticated as: [green]{display_name}[/green]\n")
        else:
            console.print(f"[yellow]Warning: Could not verify authentication (HTTP {auth_resp.status_code}). Continuing.[/yellow]\n")
    except requests.RequestException as e:
        console.print(f"[red]Error: Could not connect to JIRA at {jira_host}: {e}[/red]")
        raise SystemExit(1)

    all_rows: List[dict] = []
    auth_failures = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Processing boards...", total=len(targets))

        for target in targets:
            label = target['board_key'] or f"board {target['board_id']}"
            progress.update(task, description=f"Resolving board: {label}")

            if not target['board_id']:
                resolved = resolve_board(service, target['board_key'], verbose=verbose)
                if not resolved:
                    console.print(f"[yellow]{label}: no board found, skipping.[/yellow]")
                    progress.advance(task)
                    continue
                target['board_id'], _ = resolved
            elif verbose:
                console.print(
                    f"[dim]  {label}: using explicit board id {target['board_id']} "
                    f"from CSV (skipping resolution)[/dim]"
                )

            progress.update(task, description=f"Fetching sprints: {label}")
            closed_sprints = fetch_closed_sprints(service, target['board_id'], sprints, verbose=verbose)
            if not closed_sprints:
                console.print(f"[yellow]{label}: no closed sprints found.[/yellow]")
                progress.advance(task)
                continue

            board_rows: List[dict] = []
            for sprint in closed_sprints:
                progress.update(task, description=f"{label}: {sprint.get('name', sprint.get('id'))}")
                try:
                    board_rows.extend(
                        build_sprint_rows(service, target, sprint, ktlo_label, verbose=verbose)
                    )
                except JiraAuthError as e:
                    auth_failures += 1
                    console.print(f"[red]Auth error for {label}: {e}[/red]")
                    if auth_failures >= 3:
                        console.print("[red]Multiple authentication failures. Aborting. Check your token with --reset-token.[/red]")
                        raise SystemExit(1)
                    break

            # Promote tickets to KTLO when their parent epic carries the KTLO label.
            epic_keys = {r['parent_epic_key'] for r in board_rows if r.get('parent_epic_key')}
            if epic_keys:
                progress.update(task, description=f"{label}: checking epic KTLO labels")
                try:
                    ktlo_epics = fetch_ktlo_epics(service, epic_keys, ktlo_label.lower(), verbose=verbose)
                    apply_epic_ktlo(board_rows, ktlo_epics)
                except JiraAuthError as e:
                    auth_failures += 1
                    console.print(f"[red]Auth error checking epics for {label}: {e}[/red]")
                    if auth_failures >= 3:
                        console.print("[red]Multiple authentication failures. Aborting. Check your token with --reset-token.[/red]")
                        raise SystemExit(1)

            all_rows.extend(board_rows)
            console.print(
                f"  [green]{label}[/green] ({target['team_name']}): "
                f"{len(closed_sprints)} sprints, {len(board_rows)} tickets"
            )
            progress.advance(task)

    if not all_rows:
        console.print("[yellow]No sprint performance data collected.[/yellow]")
        return

    display_summary(all_rows)

    if output_json_flag:
        click.echo(json.dumps(all_rows, indent=2, default=str))

    if output_csv_flag or csv_path:
        out_csv = csv_path or str(OUTPUT_CSV)
        export_csv_report(all_rows, out_csv)
        console.print(f"[green]CSV exported to {out_csv}[/green]")
