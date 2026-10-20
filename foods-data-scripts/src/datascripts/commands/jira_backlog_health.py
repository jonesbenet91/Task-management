"""JIRA backlog health assessment command."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click
import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..services import TokenManager, JiraApiService, JiraAuthError
from ..services.csv_helper import CsvHelper

console = Console()

DEFAULT_HOST = "https://jira-marksandspencer-app.atlassian.net"
OUTPUT_CSV = Path("outputs/csv/backlog_health.csv")

# Story point custom fields (same as sprint_issues)
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

# ── Ticket quality assessment rules ────────────────────────────────────────

QUALITY_RULES = {
    'has_description': {
        'name': 'Has Description',
        'description': 'Ticket must have a description',
        'weight': 2.0,
    },
    'description_length': {
        'name': 'Adequate Description',
        'description': 'Description should be at least 50 characters to provide enough context',
        'weight': 2.0,
    },
    'has_acceptance_criteria': {
        'name': 'Has Acceptance Criteria',
        'description': 'Description should contain acceptance criteria or definition of done',
        'weight': 3.0,
    },
    'has_summary': {
        'name': 'Has Meaningful Summary',
        'description': 'Summary should be at least 10 characters and descriptive',
        'weight': 1.0,
    },
    'is_sized': {
        'name': 'Is Sized',
        'description': 'Ticket should have story points or an estimate',
        'weight': 2.0,
    },
    'has_priority': {
        'name': 'Has Priority',
        'description': 'Ticket should have a priority set',
        'weight': 1.0,
    },
    'has_assignee': {
        'name': 'Has Assignee',
        'description': 'Ticket should be assigned to someone',
        'weight': 1.0,
    },
    'not_stale': {
        'name': 'Not Stale',
        'description': 'Ticket should have been updated within the last 90 days',
        'weight': 1.0,
    },
    'has_labels_or_components': {
        'name': 'Has Labels or Components',
        'description': 'Ticket should have labels or components for categorisation',
        'weight': 1.0,
    },
}

ACCEPTANCE_CRITERIA_PATTERNS = [
    r'acceptance\s*criteria',
    r'definition\s*of\s*done',
    r'given\s+.*\s+when\s+.*\s+then',
    r'expected\s*result',
    r'expected\s*outcome',
    r'ac\s*:',
    r'ac\s*\d',
    r'criteria\s*:',
    r'\b(dod|DoD)\b',
]


def extract_description_text(description) -> str:
    """Extract plain text from JIRA's Atlassian Document Format (ADF) or plain string."""
    if not description:
        return ""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        # ADF format
        return _extract_adf_text(description)
    return str(description)


def _extract_adf_text(node) -> str:
    """Recursively extract text from an ADF node."""
    if not isinstance(node, dict):
        return ""
    text_parts = []
    if node.get('type') == 'text':
        text_parts.append(node.get('text', ''))
    for child in node.get('content', []):
        text_parts.append(_extract_adf_text(child))
    return ' '.join(text_parts)


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


def assess_ticket_quality(issue: dict, jira_host: str) -> dict:
    """Assess a single ticket against quality rules and return scored result."""
    fields = issue.get('fields', {})
    key = issue.get('key', '')

    description_raw = fields.get('description')
    description_text = extract_description_text(description_raw)
    summary = fields.get('summary', '') or ''
    story_points = extract_story_points(fields)
    priority = (fields.get('priority') or {}).get('name', '')
    assignee = (fields.get('assignee') or {}).get('displayName', '')
    status = (fields.get('status') or {}).get('name', '')
    issue_type = (fields.get('issuetype') or {}).get('name', '')
    created = fields.get('created', '')
    updated = fields.get('updated', '')
    labels = fields.get('labels', []) or []
    components = [c.get('name', '') for c in (fields.get('components') or [])]
    epic_key = fields.get('customfield_10014') or fields.get('epic', {}).get('key', '') if isinstance(fields.get('epic'), dict) else fields.get('customfield_10014', '')

    # Check each quality rule
    compliance = {}
    total_weight = 0
    earned = 0

    for rule_key, rule in QUALITY_RULES.items():
        passed = False

        if rule_key == 'has_description':
            passed = bool(description_text.strip())
        elif rule_key == 'description_length':
            passed = len(description_text.strip()) >= 50
        elif rule_key == 'has_acceptance_criteria':
            desc_lower = description_text.lower()
            passed = any(re.search(p, desc_lower) for p in ACCEPTANCE_CRITERIA_PATTERNS)
        elif rule_key == 'has_summary':
            passed = len(summary.strip()) >= 10
        elif rule_key == 'is_sized':
            passed = story_points is not None and story_points > 0
        elif rule_key == 'has_priority':
            passed = bool(priority) and priority.lower() not in ('none', '')
        elif rule_key == 'has_assignee':
            passed = bool(assignee)
        elif rule_key == 'not_stale':
            if updated:
                try:
                    updated_dt = datetime.fromisoformat(updated.replace('Z', '+00:00'))
                    days_since = (datetime.now(timezone.utc) - updated_dt).days
                    passed = days_since <= 90
                except (ValueError, TypeError):
                    passed = False
        elif rule_key == 'has_labels_or_components':
            passed = bool(labels) or bool(components)

        compliance[rule_key] = {
            'passed': passed,
            'name': rule['name'],
            'weight': rule['weight'],
        }
        total_weight += rule['weight']
        if passed:
            earned += rule['weight']

    quality_score = round((earned / total_weight * 100) if total_weight > 0 else 0, 1)

    # Determine ticket category
    issue_type_lower = issue_type.lower()
    if issue_type_lower in ('bug', 'incident', 'production incident', 'defect'):
        category = 'Bugs & Incidents'
    elif issue_type_lower in ('technical task', 'tech task', 'technical debt', 'spike', 'sub-task'):
        category = 'Tech Work'
    elif issue_type_lower in ('story', 'user story'):
        if epic_key:
            category = 'Projects'
        else:
            category = 'Change'
    elif issue_type_lower == 'task':
        if epic_key:
            category = 'Projects'
        else:
            category = 'Change'
    elif issue_type_lower == 'epic':
        category = 'Projects'
    else:
        category = 'Change'

    # Parse dates
    created_date = None
    if created:
        try:
            created_date = datetime.fromisoformat(created.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            pass

    days_old = None
    if created_date:
        days_old = (datetime.now(timezone.utc) - created_date).days

    return {
        'key': key,
        'summary': summary,
        'status': status,
        'type': issue_type,
        'category': category,
        'priority': priority,
        'assignee': assignee,
        'story_points': story_points,
        'created': created,
        'updated': updated,
        'days_old': days_old,
        'labels': labels,
        'components': components,
        'epic_key': epic_key or '',
        'quality_score': quality_score,
        'compliance': compliance,
        'description_length': len(description_text.strip()),
        'url': f"{jira_host}/browse/{key}",
    }


def compute_team_metrics(tickets: List[dict], velocity: Optional[float] = None) -> dict:
    """Compute aggregated backlog health metrics for a team."""
    if not tickets:
        return {
            'total_tickets': 0,
            'sized_tickets': 0,
            'unsized_tickets': 0,
            'avg_quality_score': 0,
            'oldest_ticket_days': 0,
            'oldest_ticket_key': '',
            'avg_ticket_age_days': 0,
            'total_story_points': 0,
            'estimated_total_sp': 0,
            'sprints_of_work': None,
            'categories': {},
            'quality_distribution': {'good': 0, 'fair': 0, 'poor': 0},
        }

    total = len(tickets)
    sized = [t for t in tickets if t['story_points'] is not None and t['story_points'] > 0]
    unsized = total - len(sized)
    total_sp = sum(t['story_points'] for t in sized)
    avg_quality = round(sum(t['quality_score'] for t in tickets) / total, 1)

    # Oldest ticket and average age
    oldest_days = 0
    oldest_key = ''
    ages = [t['days_old'] for t in tickets if t['days_old'] is not None]
    avg_age = round(sum(ages) / len(ages)) if ages else 0
    for t in tickets:
        if t['days_old'] is not None and t['days_old'] > oldest_days:
            oldest_days = t['days_old']
            oldest_key = t['key']

    # Category breakdown
    categories = {}
    for t in tickets:
        cat = t['category']
        if cat not in categories:
            categories[cat] = {'count': 0, 'story_points': 0, 'sized': 0}
        categories[cat]['count'] += 1
        if t['story_points'] is not None and t['story_points'] > 0:
            categories[cat]['story_points'] += t['story_points']
            categories[cat]['sized'] += 1

    # Quality distribution
    good = sum(1 for t in tickets if t['quality_score'] >= 70)
    fair = sum(1 for t in tickets if 40 <= t['quality_score'] < 70)
    poor = sum(1 for t in tickets if t['quality_score'] < 40)

    # Sprints of work — estimate total effort including unsized tickets
    sprints_of_work = None
    estimated_total_sp = total_sp
    if velocity and velocity > 0:
        # Use avg SP of sized tickets to estimate unsized ticket effort
        if len(sized) > 0 and unsized > 0:
            avg_sp_per_ticket = total_sp / len(sized)
            estimated_total_sp = total_sp + round(avg_sp_per_ticket * unsized, 1)
        if estimated_total_sp > 0:
            sprints_of_work = round(estimated_total_sp / velocity, 1)

    return {
        'total_tickets': total,
        'sized_tickets': len(sized),
        'unsized_tickets': unsized,
        'avg_quality_score': avg_quality,
        'oldest_ticket_days': oldest_days,
        'oldest_ticket_key': oldest_key,
        'avg_ticket_age_days': avg_age,
        'total_story_points': total_sp,
        'estimated_total_sp': estimated_total_sp,
        'sprints_of_work': sprints_of_work,
        'categories': categories,
        'quality_distribution': {'good': good, 'fair': fair, 'poor': poor},
    }


def fetch_completed_sprint_velocity(jira_service: JiraApiService, project_key: str, verbose: bool = False) -> Optional[float]:
    """Estimate team velocity from the last 3 completed sprints.

    Uses the Agile board associated with the project to find closed sprints,
    then averages story points completed across the last 3.
    """
    try:
        # Find board for project
        response = jira_service.session.get(
            f"{jira_service.host}/rest/agile/1.0/board",
            params={'projectKeyOrId': project_key, 'maxResults': 10},
            timeout=30
        )
        if response.status_code != 200:
            if verbose:
                console.print(f"[dim]  {project_key}: Board lookup failed (HTTP {response.status_code})[/dim]")
            return None

        boards = response.json().get('values', [])
        if not boards:
            if verbose:
                console.print(f"[dim]  {project_key}: No boards found[/dim]")
            return None

        # Prefer scrum boards
        scrum_boards = [b for b in boards if (b.get('type') or '').lower() == 'scrum']
        board = scrum_boards[0] if scrum_boards else boards[0]
        board_id = board.get('id')

        # Get last 3 closed sprints
        sprint_resp = jira_service.session.get(
            f"{jira_service.host}/rest/agile/1.0/board/{board_id}/sprint",
            params={'state': 'closed', 'maxResults': 10},
            timeout=30
        )
        if sprint_resp.status_code != 200:
            if verbose:
                console.print(f"[dim]  {project_key}: Sprint lookup failed (HTTP {sprint_resp.status_code})[/dim]")
            return None

        sprints = sprint_resp.json().get('values', [])
        # Sort by end date descending and take last 3
        sprints.sort(key=lambda s: s.get('endDate', ''), reverse=True)
        recent_sprints = sprints[:3]

        if not recent_sprints:
            if verbose:
                console.print(f"[dim]  {project_key}: No closed sprints found[/dim]")
            return None

        # Sum story points per sprint
        sprint_points = []
        for sprint in recent_sprints:
            sid = sprint.get('id')
            jql = f'sprint = {sid} AND statusCategory = Done AND issuetype != Sub-task'
            try:
                issues = jira_service.search_issues(jql, max_results=100, fetch_all=True)
                sp_total = 0.0
                for issue in issues:
                    pts = extract_story_points(issue.get('fields', {}))
                    if pts:
                        sp_total += pts
                sprint_points.append(sp_total)
            except Exception:
                continue

        if not sprint_points:
            if verbose:
                console.print(f"[dim]  {project_key}: No story points found in closed sprints[/dim]")
            return None

        avg = round(sum(sprint_points) / len(sprint_points), 1)
        if verbose:
            console.print(f"[dim]  {project_key}: Velocity = {avg} SP/sprint (from {len(sprint_points)} sprints)[/dim]")
        return avg

    except Exception as e:
        if verbose:
            console.print(f"[dim]  {project_key}: Velocity estimation error: {e}[/dim]")
        return None





def export_csv_report(team_data: List[dict], csv_path: str) -> None:
    """Export backlog health data as CSV."""
    import csv
    rows = []
    for team in team_data:
        for ticket in team.get('tickets', []):
            rows.append({
                'team': team['team_name'],
                'product_group': team['product_group'],
                'project_key': team['project_key'],
                'key': ticket['key'],
                'summary': ticket['summary'],
                'type': ticket['type'],
                'category': ticket['category'],
                'status': ticket['status'],
                'priority': ticket['priority'],
                'assignee': ticket['assignee'],
                'story_points': ticket['story_points'] if ticket['story_points'] else '',
                'days_old': ticket['days_old'],
                'quality_score': ticket['quality_score'],
                'epic_key': ticket['epic_key'],
                'url': ticket['url'],
            })

    if not rows:
        return

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    headers = list(rows[0].keys())
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


@click.command('backlog-health')
@click.option('--projects-csv', default='data/jira.csv', help='CSV with projects -> team mapping')
@click.option('--project', multiple=True, help='Project key (repeatable)')
@click.option('--host', envvar='JIRA_HOST', default=DEFAULT_HOST, help='JIRA base URL')
@click.option('--email', envvar='JIRA_EMAIL', help='JIRA user email for Basic auth')
@click.option('--token', envvar='JIRA_API_TOKEN', help='JIRA API token')
@click.option('--reset-token', is_flag=True, help='Clear cached token and email, then re-prompt')
@click.option('--velocity', type=float, help='Override team velocity (SP/sprint) for all teams')
@click.option('--estimate-velocity/--no-estimate-velocity', default=True, help='Estimate velocity from last 3 completed sprints (default: on)')
@click.option('--verbose', is_flag=True, help='Show detailed velocity estimation diagnostics')
@click.option('--csv', 'output_csv_flag', is_flag=True, help='Export CSV to outputs/csv/')
@click.option('--csv-path', help='Custom CSV output path')
@click.option('--json', 'output_json_flag', is_flag=True, help='Output as JSON')
def backlog_health_command(
    projects_csv: str,
    project: Tuple[str, ...],
    host: str,
    email: Optional[str],
    token: Optional[str],
    reset_token: bool,
    velocity: Optional[float],
    estimate_velocity: bool,
    verbose: bool,
    output_csv_flag: bool,
    csv_path: Optional[str],
    output_json_flag: bool,
):
    """Assess JIRA backlog health per team: ticket quality, sizing, age, and categorisation."""
    import sys
    import tempfile

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

    # Parse projects from CSV
    projects = list(project)
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
                proj_name = row.get('JIRA Project Name', '')
                team_name = row.get('Team Name', '') or 'Unknown'
                product_group = row.get('Product Group', '') or 'Unknown'

                if board_key and board_key not in project_team_map:
                    project_team_map[board_key] = {
                        'team_name': team_name,
                        'product_group': product_group,
                        'project_name': proj_name,
                    }
        if not projects:
            projects = list(project_team_map.keys())

    if not projects:
        console.print("[red]Error: No projects specified. Use --projects-csv or --project.[/red]")
        raise SystemExit(1)

    # Deduplicate projects
    projects = list(dict.fromkeys(projects))

    console.print(f"Assessing backlog health for [bold]{len(projects)}[/bold] projects\n")

    # Initialize service
    service = JiraApiService(jira_host, jira_email, api_token)

    # Validate authentication before processing
    try:
        auth_resp = service.session.get(f"{service.host}/rest/api/3/myself", timeout=15)
        if auth_resp.status_code == 401:
            console.print("[red]Error: Authentication failed (HTTP 401). Check your JIRA API token.[/red]")
            console.print("[yellow]Hint: Use --reset-token to re-enter your token, or set JIRA_API_TOKEN env var.[/yellow]")
            raise SystemExit(1)
        elif auth_resp.status_code == 403:
            console.print("[red]Error: Access forbidden (HTTP 403). Your token may lack permissions.[/red]")
            console.print("[yellow]Hint: Ensure your API token has read access to the target projects.[/yellow]")
            raise SystemExit(1)
        elif auth_resp.status_code == 200:
            user_info = auth_resp.json()
            display_name = user_info.get('displayName', user_info.get('emailAddress', 'unknown'))
            console.print(f"Authenticated as: [green]{display_name}[/green]\n")
        else:
            console.print(f"[yellow]Warning: Could not verify authentication (HTTP {auth_resp.status_code}). Continuing anyway.[/yellow]\n")
    except requests.RequestException as e:
        console.print(f"[red]Error: Could not connect to JIRA at {jira_host}: {e}[/red]")
        raise SystemExit(1)

    team_data = []
    auth_failures = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Processing projects...", total=len(projects))

        for proj_key in projects:
            progress.update(task, description=f"Fetching backlog: {proj_key}")

            team_info = project_team_map.get(proj_key, {})
            team_name = team_info.get('team_name', proj_key)
            product_group = team_info.get('product_group', 'Unknown')

            # Query backlog: open/in-progress tickets
            jql = (
                f'project = "{proj_key}" '
                f'AND statusCategory != Done '
                f'AND issuetype != Sub-task '
                f'AND issuetype != Epic '
                f'ORDER BY created ASC'
            )

            try:
                issues = service.search_issues(jql, max_results=100, fetch_all=True)
            except JiraAuthError as e:
                auth_failures += 1
                console.print(f"[red]Auth error for {proj_key}: {e}[/red]")
                if auth_failures >= 3:
                    console.print("[red]Multiple authentication failures. Aborting. Check your token with --reset-token.[/red]")
                    raise SystemExit(1)
                progress.advance(task)
                continue
            except RuntimeError as e:
                console.print(f"[red]Error fetching {proj_key}: {e}[/red]")
                progress.advance(task)
                continue

            if not issues:
                console.print(f"[yellow]{proj_key}: No backlog tickets found.[/yellow]")
                progress.advance(task)
                continue

            # Assess each ticket
            tickets = [assess_ticket_quality(issue, jira_host) for issue in issues]

            # Estimate velocity if requested
            team_velocity = velocity
            if estimate_velocity and not velocity:
                progress.update(task, description=f"Estimating velocity: {proj_key}")
                team_velocity = fetch_completed_sprint_velocity(service, proj_key, verbose=verbose)

            metrics = compute_team_metrics(tickets, team_velocity)

            team_data.append({
                'team_name': team_name,
                'product_group': product_group,
                'project_key': proj_key,
                'metrics': metrics,
                'tickets': tickets,
                'velocity': team_velocity,
            })

            console.print(
                f"  [green]{proj_key}[/green] ({team_name}): "
                f"{metrics['total_tickets']} tickets, "
                f"avg quality {metrics['avg_quality_score']}%, "
                f"{metrics['sized_tickets']} sized"
            )

            progress.advance(task)

    if not team_data:
        console.print("[yellow]No backlog data collected.[/yellow]")
        return

    # Sort teams by avg quality ascending (worst first)
    team_data.sort(key=lambda t: t['metrics']['avg_quality_score'])

    # Display summary table
    table = Table(title="Backlog Health Summary")
    table.add_column("Team", style="cyan")
    table.add_column("Project")
    table.add_column("Tickets", justify="right")
    table.add_column("Sized", justify="right")
    table.add_column("SP", justify="right")
    table.add_column("Avg Age", justify="right")
    table.add_column("Oldest", justify="right")
    table.add_column("Sprints", justify="right")
    table.add_column("Quality", justify="right")

    for t in team_data:
        m = t['metrics']
        sprints_str = str(m['sprints_of_work']) if m['sprints_of_work'] is not None else '—'
        quality_color = 'green' if m['avg_quality_score'] >= 70 else ('yellow' if m['avg_quality_score'] >= 40 else 'red')
        table.add_row(
            t['team_name'],
            t['project_key'],
            str(m['total_tickets']),
            f"{m['sized_tickets']}/{m['total_tickets']}",
            str(m['total_story_points']),
            f"{m['avg_ticket_age_days']}d",
            f"{m['oldest_ticket_days']}d",
            sprints_str,
            f"[{quality_color}]{m['avg_quality_score']}%[/{quality_color}]",
        )

    console.print(table)

    # JSON output
    if output_json_flag:
        click.echo(json.dumps(team_data, indent=2, default=str))

    # CSV output
    if output_csv_flag or csv_path:
        out_csv = csv_path or str(OUTPUT_CSV)
        export_csv_report(team_data, out_csv)
        console.print(f"[green]CSV exported to {out_csv}[/green]")
