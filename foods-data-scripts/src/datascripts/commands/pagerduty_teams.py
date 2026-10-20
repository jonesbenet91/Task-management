"""PagerDuty teams command."""

from typing import Optional, Tuple

import click
import requests
from rich.console import Console
from rich.table import Table

from .pagerduty_base import (
    PagerDutyClient,
    resolve_pagerduty_token,
    output_csv,
    output_json,
)
from ..services.csv_helper import CsvHelper


console = Console()


@click.command("teams")
@click.option("--name", help="Team name filter (sent as query to API)")
@click.option("--tag", "-t", multiple=True, help="Tag filter (can specify multiple times)")
@click.option("--limit", type=int, default=0, help="Page size / limit (0 = unlimited)")
@click.option("--all", "fetch_all", is_flag=True, help="Page through all results")
@click.option("--token", envvar="PAGERDUTY_API_TOKEN", help="PagerDuty API token")
@click.option("--reset-token", is_flag=True, help="Remove cached token")
@click.option("--skip-cache", is_flag=True, help="Skip API response cache")
@click.option("--json", "output_json_flag", is_flag=True, help="Output raw JSON")
@click.option("--csv", "output_csv_flag", is_flag=True, help="Output as CSV to stdout")
def teams_command(
    name: Optional[str],
    tag: Tuple[str, ...],
    limit: int,
    fetch_all: bool,
    token: Optional[str],
    reset_token: bool,
    skip_cache: bool,
    output_json_flag: bool,
    output_csv_flag: bool
):
    """Query PagerDuty API and list teams."""
    # Resolve token
    resolved_token = resolve_pagerduty_token(token, reset_token)
    if not resolved_token:
        console.print("[red]Error: PagerDuty API token required.[/red]")
        raise SystemExit(1)
    
    # Create client and validate token
    client = PagerDutyClient(resolved_token, skip_cache=skip_cache)
    client.clear_expired_cache()
    
    console.print("[cyan]Validating API token...[/cyan]")
    try:
        client.validate_token(console)
        console.print("[green]Token validated successfully[/green]\n")
    except Exception:
        raise SystemExit(1)
    
    # Build query
    params = {}
    if name:
        params["query"] = name
    if limit > 0:
        params["limit"] = limit
    
    # Fetch teams
    fetch = fetch_all or limit == 0
    
    try:
        teams = client.paginated_request("/teams", "teams", params, limit, fetch)
    except requests.HTTPError as e:
        console.print(f"[red]Failed to fetch teams: {e}[/red]")
        raise SystemExit(1)
    
    if not teams:
        console.print("[yellow]No teams found.[/yellow]")
        return
    
    # Apply tag filters if provided
    requested_tags = list(tag)
    if requested_tags:
        requested_lower = [t.lower() for t in requested_tags]
        filtered_teams = []
        
        console.print(f"Filtering {len(teams)} teams by tags: {', '.join(requested_tags)}")
        
        for team in teams:
            team_id = team.get("id")
            if not team_id:
                continue
            
            try:
                tag_data = client.request("GET", f"/teams/{team_id}/tags")
                tags = tag_data.get("tags", [])
                team_tags_lower = [
                    (t.get("label") or t.get("name") or str(t)).lower()
                    for t in tags
                    if isinstance(t, dict)
                ]
                
                if any(rt in team_tags_lower for rt in requested_lower):
                    team["tags"] = tags
                    filtered_teams.append(team)
            except requests.HTTPError:
                filtered_teams.append(team)
        
        teams = filtered_teams
        
        if not teams:
            console.print("[yellow]No teams matched the provided tag filters.[/yellow]")
            return
    
    # Handle JSON output
    if output_json_flag:
        output_json(teams, "teams")
        return
    
    # Build rows
    rows = []
    for team in teams:
        team_id = team.get("id", "")
        team_name = team.get("name") or team.get("summary", "")
        description = team.get("description", "")
        
        tag_labels = []
        if team.get("tags"):
            for t in team["tags"]:
                if isinstance(t, dict):
                    tag_labels.append(t.get("label") or t.get("name") or "")
                elif isinstance(t, str):
                    tag_labels.append(t)
        
        url = team.get("html_url", "")
        if not url:
            # Use extracted subdomain or fallback to app.pagerduty.com
            if client.subdomain:
                url = f"https://{client.subdomain}.pagerduty.com/teams/{team_id}"
            else:
                url = f"https://app.pagerduty.com/teams/{team_id}"
        
        rows.append([team_id, team_name, ",".join(tag_labels), description, url])
    
    headers = ["id", "name", "tags", "description", "url"]
    
    # Handle CSV output
    if output_csv_flag:
        output_csv(rows, headers)
        return
    
    # Display table
    table = Table()
    for header in headers:
        table.add_column(header)
    
    for row in rows:
        table.add_row(*row)
    
    console.print(table)
    console.print(f"Fetched {len(teams)} teams.")
