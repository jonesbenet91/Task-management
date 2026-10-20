"""PagerDuty services command."""

from typing import List, Optional, Tuple

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


def filter_services_by_teams(services: List[dict], team_ids: List[str]) -> List[dict]:
    """Filter services by team IDs.
    
    Args:
        services: List of service dictionaries.
        team_ids: List of team IDs to filter by.
    
    Returns:
        Filtered list of services.
    """
    team_ids_lower = [t.lower() for t in team_ids]
    filtered = []
    
    for service in services:
        svc_team_ids = []
        
        teams = service.get("teams", [])
        if teams and isinstance(teams, list):
            for t in teams:
                if isinstance(t, dict) and t.get("id"):
                    svc_team_ids.append(str(t["id"]))
        
        team = service.get("team")
        if not svc_team_ids and isinstance(team, dict) and team.get("id"):
            svc_team_ids.append(str(team["id"]))
        
        svc_team_ids_lower = [s.lower() for s in svc_team_ids]
        
        if any(tid in svc_team_ids_lower for tid in team_ids_lower):
            filtered.append(service)
    
    return filtered


@click.command("services")
@click.option("--teams-csv", help="CSV of teams to filter services")
@click.option("--team", multiple=True, help="Team ID to filter services (repeatable)")
@click.option("--name", help="Service name filter (sent as query to API)")
@click.option("--limit", type=int, default=0, help="Page size / limit (0 = unlimited)")
@click.option("--all", "fetch_all", is_flag=True, help="Page through all results")
@click.option("--token", envvar="PAGERDUTY_API_TOKEN", help="PagerDuty API token")
@click.option("--reset-token", is_flag=True, help="Remove cached token")
@click.option("--skip-cache", is_flag=True, help="Skip API response cache")
@click.option("--json", "output_json_flag", is_flag=True, help="Output raw JSON")
@click.option("--csv", "output_csv_flag", is_flag=True, help="Output as CSV to stdout")
def services_command(
    teams_csv: Optional[str],
    team: Tuple[str, ...],
    name: Optional[str],
    limit: int,
    fetch_all: bool,
    token: Optional[str],
    reset_token: bool,
    skip_cache: bool,
    output_json_flag: bool,
    output_csv_flag: bool
):
    """Query PagerDuty API and list services."""
    # Resolve token
    resolved_token = resolve_pagerduty_token(token, reset_token)
    if not resolved_token:
        console.print("[red]Error: PagerDuty API token required.[/red]")
        raise SystemExit(1)
    
    # Collect team IDs
    team_ids = list(team)
    if teams_csv:
        csv_teams = CsvHelper.parse_teams_csv(teams_csv)
        for t in csv_teams:
            if t not in team_ids:
                team_ids.append(t)
    
    # Create client and validate token
    client = PagerDutyClient(resolved_token, skip_cache=skip_cache)
    client.clear_expired_cache()
    
    console.print("[cyan]Validating API token...[/cyan]")
    try:
        client.validate_token(console)
        console.print("[green]Token validated successfully[/green]\n")
    except Exception:
        raise SystemExit(1)
    
    # Build query - use server-side team filtering if team IDs provided
    params = {}
    if team_ids:
        params['team_ids[]'] = team_ids
        params['include[]'] = 'teams'
    if name:
        params["query"] = name
    if limit > 0:
        params["limit"] = limit
    
    # Fetch services
    fetch = fetch_all or limit == 0
    
    try:
        services = client.paginated_request("/services", "services", params, limit, fetch)
    except requests.HTTPError as e:
        console.print(f"[red]Failed to fetch services: {e}[/red]")
        raise SystemExit(1)
    
    # No need for client-side filtering when using API's team_ids[] parameter
    if not services:
        console.print("[yellow]No services found.[/yellow]")
        return
    
    # Handle JSON output
    if output_json_flag:
        output_json(services, "services")
        return
    
    # Build rows
    rows = []
    for s in services:
        svc_id = s.get("id", "")
        svc_name = s.get("name") or s.get("summary", "")
        description = s.get("description", "")
        
        team_names = []
        teams = s.get("teams", [])
        if teams and isinstance(teams, list):
            for t in teams:
                if isinstance(t, dict):
                    team_names.append(t.get("summary") or t.get("name") or t.get("id", ""))
        team_obj = s.get("team")
        if not team_names and isinstance(team_obj, dict):
            team_names.append(team_obj.get("summary") or team_obj.get("name") or team_obj.get("id", ""))
        
        url = s.get("html_url", "")
        if not url:
            # Use extracted subdomain or fallback to app.pagerduty.com
            if client.subdomain:
                url = f"https://{client.subdomain}.pagerduty.com/services/{svc_id}"
            else:
                url = f"https://app.pagerduty.com/services/{svc_id}"
        
        rows.append([svc_id, svc_name, ",".join(team_names), description, url])
    
    headers = ["id", "name", "teams", "description", "url"]
    
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
    console.print(f"Fetched {len(services)} services.")
