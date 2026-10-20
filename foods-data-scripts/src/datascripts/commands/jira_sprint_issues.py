"""JIRA sprint issues command."""

import click
from typing import Any, Optional
from rich.console import Console
from rich.table import Table

from ..services import TokenManager, JiraApiService


class JiraSprintIssuesCommand:
    """Query JIRA API for sprint issues."""
    
    # Common story points custom field names
    STORY_POINT_FIELDS = [
        'customfield_10033',  # Observed in some projects
        'customfield_10026',  # Common Atlassian default
        'customfield_10016',  # Another common default
        'customfield_10002',
        'customfield_10018',
        'customfield_10020',
        'customfield_10021',
        'customfield_10028',
        'customfield_10034',
        'story_points',
        'storypoints',
    ]
    
    def __init__(self, jira_service: JiraApiService):
        self.jira = jira_service
    
    def fetch_board_sprints(self, board_id: str, state: str, max_results: int) -> list[dict]:
        """Fetch sprints from a board using JIRA Agile API."""
        params = {
            'state': state,
            'maxResults': max_results,
        }
        
        response = self.jira.make_api_request(
            'GET',
            f'/rest/agile/1.0/board/{board_id}/sprint',
            params=params
        )
        
        return response.get('values', [])
    
    def fetch_sprint_details(self, sprint_id: str) -> dict:
        """Fetch details for a specific sprint."""
        return self.jira.make_api_request(
            'GET',
            f'/rest/agile/1.0/sprint/{sprint_id}'
        )
    
    def find_board_by_project_key(self, project_key: str, console: Console) -> Optional[str]:
        """Find a board ID by project key."""
        console.print(f"Looking for board associated with project: [yellow]{project_key}[/yellow]")
        
        # Validate project exists
        project = self.jira.make_api_request('GET', f'/rest/api/3/project/{project_key}')
        project_id = project.get('id')
        
        if not project_id:
            raise RuntimeError(f"Project {project_key} not found or has no ID")
        
        console.print(f"Found project: [green]{project.get('name', project_key)}[/green] (ID: {project_id})")
        
        boards = []
        
        # Method 1: Get all boards and filter
        try:
            all_boards_response = self.jira.make_api_request(
                'GET',
                '/rest/agile/1.0/board',
                params={'maxResults': 100}
            )
            
            for board in all_boards_response.get('values', []):
                location = board.get('location', {})
                if str(location.get('projectId')) == str(project_id):
                    boards.append(board)
                elif location.get('projectKey') == project_key:
                    boards.append(board)
        except Exception:
            console.print("[yellow]Could not fetch all boards, trying project-specific search...[/yellow]")
        
        # Method 2: Project-specific board endpoint
        if not boards:
            try:
                project_boards = self.jira.make_api_request(
                    'GET',
                    '/rest/agile/1.0/board',
                    params={'projectKeyOrId': project_key, 'maxResults': 100}
                )
                boards.extend(project_boards.get('values', []))
            except Exception:
                console.print("[yellow]Project-specific board search not available[/yellow]")
        
        if not boards:
            raise RuntimeError(f"No boards found for project {project_key}. Use --board-id directly.")
        
        # Sort boards - prefer scrum over kanban
        def board_sort_key(board):
            board_type = (board.get('type') or '').lower()
            if board_type == 'scrum':
                return (0, board.get('name', ''))
            return (1, board.get('name', ''))
        
        boards.sort(key=board_sort_key)
        
        selected = boards[0]
        console.print(
            f"Selected board: [green]{selected.get('name', 'Unknown')}[/green] "
            f"(ID: {selected.get('id')}, Type: {selected.get('type', 'Unknown')})"
        )
        
        if len(boards) > 1:
            console.print(
                f"[yellow]Found {len(boards)} boards, using the first one. "
                f"Use --board-id to specify exactly.[/yellow]"
            )
        
        return str(selected.get('id'))
    
    def find_target_sprint(
        self,
        board_id: str,
        sprint_name: Optional[str],
        state: str,
        max_sprints: int,
        console: Console
    ) -> Optional[dict]:
        """Find the target sprint based on criteria."""
        console.print("Searching for sprints...")
        
        sprints = self.fetch_board_sprints(board_id, state, max_sprints)
        
        if not sprints:
            return None
        
        # Filter by name if provided
        if sprint_name:
            name_pattern = sprint_name.lower()
            sprints = [s for s in sprints if name_pattern in (s.get('name', '') or '').lower()]
            
            if not sprints:
                raise RuntimeError(f"No sprints found matching name pattern: {sprint_name}")
        
        # Sort by date
        if state == 'closed':
            sprints.sort(key=lambda s: s.get('endDate', ''), reverse=True)
        else:
            sprints.sort(key=lambda s: s.get('startDate', ''), reverse=True)
        
        target = sprints[0]
        sprint_name = target.get('name', 'Unknown')
        sprint_id = target.get('id')
        
        console.print(f"Found sprint: [green]{sprint_name}[/green] (ID: {sprint_id})")
        
        return {
            'id': sprint_id,
            'name': sprint_name
        }
    
    def extract_story_points(self, fields: dict) -> str:
        """Extract story points from various possible custom fields."""
        for field in self.STORY_POINT_FIELDS:
            value = fields.get(field)
            
            if value is None or value == '':
                continue
            
            if isinstance(value, dict):
                if 'value' in value and isinstance(value['value'], (str, int, float)):
                    return str(value['value'])
                continue
            elif isinstance(value, list):
                scalar_values = [v for v in value if isinstance(v, (str, int, float))]
                if scalar_values:
                    return ', '.join(str(v) for v in scalar_values)
                continue
            elif isinstance(value, (str, int, float)):
                return str(value)
        
        return ''
    
    def prepare_issue_rows(self, issues: list[dict]) -> list[dict]:
        """Prepare issue data for display."""
        rows = []
        
        for issue in issues:
            fields = issue.get('fields', {})
            
            assignee = fields.get('assignee')
            if isinstance(assignee, dict):
                assignee_name = assignee.get('displayName', 'Unassigned')
            else:
                assignee_name = 'Unassigned'
            
            rows.append({
                'key': issue.get('key', ''),
                'summary': fields.get('summary', ''),
                'status': (fields.get('status') or {}).get('name', ''),
                'type': (fields.get('issuetype') or {}).get('name', ''),
                'assignee': assignee_name,
                'story_points': self.extract_story_points(fields),
                'priority': (fields.get('priority') or {}).get('name', ''),
                'url': f"{self.jira.host}/browse/{issue.get('key', '')}",
            })
        
        return rows
    
    def display_summary(
        self,
        rows: list[dict],
        group_by: list[str],
        console: Console
    ) -> None:
        """Display summary information."""
        if not group_by:
            group_by = ['status']
        
        allowed = {'status', 'type', 'assignee', 'sprint'}
        invalid = set(group_by) - allowed
        if invalid:
            console.print(f"[red]Invalid group-by: {', '.join(invalid)}. Allowed: status, type, assignee, sprint.[/red]")
            return
        
        # Calculate totals
        total_story_points = sum(
            float(r['story_points'] or 0)
            for r in rows
        )
        
        # Aggregate
        agg: dict[str, dict] = {}
        for row in rows:
            keys = [row.get(field, 'Unknown') for field in group_by]
            key = '|'.join(keys)
            
            if key not in agg:
                agg[key] = {'count': 0, 'story_points': 0.0, 'values': keys}
            
            agg[key]['count'] += 1
            agg[key]['story_points'] += float(row['story_points'] or 0)
        
        # Display table
        table = Table(title="Sprint Issues Summary")
        for field in group_by:
            table.add_column(field.title(), style="cyan")
        table.add_column("Count", justify="right")
        table.add_column("Story Points", justify="right")
        
        for data in agg.values():
            row_data = data['values'] + [str(data['count']), f"{data['story_points']:.1f}"]
            table.add_row(*row_data)
        
        console.print(table)
        console.print(f"\nTotal: {len(rows)} issues, {total_story_points:.1f} story points")


@click.command('sprint-issues')
@click.option('--token', help='JIRA API token (or set JIRA_API_TOKEN)')
@click.option('--reset-token', is_flag=True, help='Reset cached token')
@click.option('--host', help='JIRA host URL (e.g., https://company.atlassian.net)')
@click.option('--email', help='JIRA user email')
@click.option('--board-id', help='Board ID to search for sprints')
@click.option('--board-key', help='Project key to find associated board')
@click.option('--project', multiple=True, help='Project key to limit board search')
@click.option('--sprint-id', help='Specific sprint ID to query')
@click.option('--sprint-name', help='Sprint name pattern to search for')
@click.option('--state', default='closed', type=click.Choice(['closed', 'active', 'future']), help='Sprint state to filter by')
@click.option('--max-sprints', default=20, help='Maximum number of sprints to search')
@click.option('--include-sub-tasks', is_flag=True, help='Include sub-tasks in results')
@click.option('--summary', is_flag=True, help='Show aggregated summary')
@click.option('--group-by', multiple=True, help='Group summary by: status, type, assignee, sprint')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.option('--csv', 'output_csv', is_flag=True, help='Output as CSV')
@click.option('--csv-path', help='Path for CSV output file')
def sprint_issues_command(
    token: Optional[str],
    reset_token: bool,
    host: Optional[str],
    email: Optional[str],
    board_id: Optional[str],
    board_key: Optional[str],
    project: tuple,
    sprint_id: Optional[str],
    sprint_name: Optional[str],
    state: str,
    max_sprints: int,
    include_sub_tasks: bool,
    summary: bool,
    group_by: tuple,
    output_json: bool,
    output_csv: bool,
    csv_path: Optional[str],
) -> None:
    """Query JIRA API and list issues from a specific sprint."""
    console = Console()
    
    # Get token
    token_manager = TokenManager('jira_token.cache')
    api_token = token_manager.get_token(
        token,
        ['JIRA_API_TOKEN'],
        reset_token,
        console
    )
    
    if not api_token:
        console.print("[red]Error: No API token provided[/red]")
        raise SystemExit(1)
    
    # Get host
    import os
    jira_host = host or os.environ.get('JIRA_HOST') or os.environ.get('JIRA_URL')
    if not jira_host:
        console.print("[red]Error: JIRA host required. Use --host or set JIRA_HOST[/red]")
        raise SystemExit(1)
    
    # Get email
    jira_email = email or os.environ.get('JIRA_EMAIL') or os.environ.get('JIRA_USER')
    if not jira_email:
        console.print("[red]Error: JIRA email required. Use --email or set JIRA_EMAIL[/red]")
        raise SystemExit(1)
    
    # Initialize service
    jira_service = JiraApiService(jira_host, jira_email, api_token)
    cmd = JiraSprintIssuesCommand(jira_service)
    
    # Find sprint
    sprint_display_name = None
    
    if sprint_id:
        console.print(f"Using specified sprint ID: [yellow]{sprint_id}[/yellow]")
        try:
            sprint_info = cmd.fetch_sprint_details(sprint_id)
            sprint_display_name = sprint_info.get('name', f"Sprint {sprint_id}")
        except Exception:
            sprint_display_name = f"Sprint {sprint_id}"
    else:
        # Resolve board ID
        resolved_board_id = board_id
        
        if board_key and not resolved_board_id:
            try:
                resolved_board_id = cmd.find_board_by_project_key(board_key, console)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
                raise SystemExit(1)
        
        if not resolved_board_id:
            console.print("[red]Error: Either --board-id, --board-key, or --sprint-id must be provided[/red]")
            raise SystemExit(1)
        
        try:
            sprint_info = cmd.find_target_sprint(
                resolved_board_id, sprint_name, state, max_sprints, console
            )
            
            if not sprint_info:
                console.print(f"[red]No {state} sprints found on board {resolved_board_id}[/red]")
                raise SystemExit(1)
            
            sprint_id = str(sprint_info['id'])
            sprint_display_name = sprint_info['name']
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            raise SystemExit(1)
    
    # Build JQL
    jql_parts = [f'sprint = {sprint_id}']
    
    projects = list(project)
    if projects:
        escaped = []
        for p in projects:
            if any(c in p for c in ' -\'\"'):
                escaped.append(f'"{p}"')
            else:
                escaped.append(p)
        jql_parts.append(f"project in ({','.join(escaped)})")
    
    if not include_sub_tasks:
        jql_parts.append('issuetype != Sub-task')
    
    jql = ' AND '.join(jql_parts)
    console.print(f"JQL Query: [yellow]{jql}[/yellow]")
    
    # Fetch issues
    try:
        issues = jira_service.search_issues(jql, max_results=100, fetch_all=True, console=console)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise SystemExit(1)
    
    if not issues:
        console.print("[yellow]No issues found for the specified sprint.[/yellow]")
        return
    
    # Prepare data
    rows = cmd.prepare_issue_rows(issues)
    
    # Handle summary mode
    if summary:
        cmd.display_summary(rows, list(group_by), console)
    
    # Handle JSON output
    if output_json:
        import json
        print(json.dumps(rows, indent=2))
        return
    
    # Handle CSV output
    if output_csv or csv_path:
        import csv as csv_module
        from datetime import datetime
        
        headers = ['key', 'summary', 'status', 'type', 'assignee', 'story_points', 'priority', 'url']
        output_path = csv_path or f"sprint_issues_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv_module.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([row[h] for h in headers])
        
        console.print(f"[green]CSV exported to {output_path}[/green]")
        return
    
    # Display table (when not in summary mode)
    if not summary:
        if sprint_display_name:
            console.print(f"\n[bold]Sprint: {sprint_display_name}[/bold]\n")
        
        table = Table()
        table.add_column("Key", style="cyan")
        table.add_column("Summary")
        table.add_column("Status")
        table.add_column("Type")
        table.add_column("Assignee")
        table.add_column("Story Points", justify="right")
        
        for row in rows:
            table.add_row(
                row['key'],
                row['summary'][:60] + '...' if len(row['summary']) > 60 else row['summary'],
                row['status'],
                row['type'],
                row['assignee'],
                row['story_points']
            )
        
        console.print(table)
    
    console.print(f"\nFound {len(issues)} issues in sprint {sprint_id}.")
