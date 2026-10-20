"""JIRA tickets command."""

import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import click
import requests
from rich.console import Console
from rich.table import Table

from ..services.token_manager import TokenManager
from ..services.csv_helper import CsvHelper
from ..services.jira_api import JiraApiService


console = Console()

DEFAULT_HOST = "https://jira-marksandspencer-app.atlassian.net"


def resolve_jira_token(
    token: Optional[str] = None,
    reset_token: bool = False,
    verbose: bool = False
) -> Optional[str]:
    """Resolve JIRA API token."""
    manager = TokenManager("jira_token.cache")
    return manager.get_token(
        cli_token=token,
        env_vars=["JIRA_API_TOKEN"],
        reset=reset_token,
        prompt_text="JIRA API token (will be cached; set JIRA_API_TOKEN env to override)",
        verbose=verbose
    )


def output_json(data: List[Dict], key: str) -> None:
    """Output data as JSON."""
    click.echo(json.dumps({key: data}, indent=2))


def export_csv(rows: List[Dict], headers: List[str], filename: str) -> None:
    """Export data to CSV file."""
    import csv
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    console.print(f"CSV exported to: {filename}")


@click.command("tickets")
@click.option("--projects-csv", help="CSV with projects -> team mapping")
@click.option("--project", multiple=True, help="Project key (repeatable)")
@click.option("--status", "statuses", multiple=True, help="Status filter (repeatable)")
@click.option("--jql", "custom_jql", help="Additional JQL to combine with filters")
@click.option("--since", default=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
              help="Created since (YYYY-MM-DD)")
@click.option("--until", default=datetime.now().strftime("%Y-%m-%d"),
              help="Created until (YYYY-MM-DD)")
@click.option("--max", "max_results", type=int, default=50, help="Max results per page")
@click.option("--all", "fetch_all", is_flag=True, help="Page through all results")
@click.option("--summary", is_flag=True, help="Show aggregated summary")
@click.option("--group-by", multiple=True, help="Group summary by: status, type, team")
@click.option("--host", envvar="JIRA_HOST", help="JIRA base URL")
@click.option("--user", default="apitoken", help="Username for Basic auth")
@click.option("--token", envvar="JIRA_API_TOKEN", help="JIRA API token")
@click.option("--reset-token", is_flag=True, help="Remove cached token")
@click.option("--json", "output_json_flag", is_flag=True, help="Output raw JSON")
@click.option("--csv", "output_csv_flag", is_flag=True, help="Export CSV")
@click.option("--csv-path", help="CSV output path")
def tickets_command(
    projects_csv: Optional[str],
    project: Tuple[str, ...],
    statuses: Tuple[str, ...],
    custom_jql: Optional[str],
    since: str,
    until: str,
    max_results: int,
    fetch_all: bool,
    summary: bool,
    group_by: Tuple[str, ...],
    host: Optional[str],
    user: str,
    token: Optional[str],
    reset_token: bool,
    output_json_flag: bool,
    output_csv_flag: bool,
    csv_path: Optional[str]
):
    """Query JIRA API and list tickets."""
    # Resolve host
    jira_host = host or DEFAULT_HOST
    
    # Resolve token
    resolved_token = resolve_jira_token(token, reset_token)
    if not resolved_token:
        console.print("[red]Error: JIRA API token required.[/red]")
        raise SystemExit(1)
    
    # Parse projects
    projects = list(project)
    project_team_map: Dict[str, str] = {}
    
    if projects_csv:
        project_team_map = CsvHelper.parse_projects_csv(projects_csv)
        if not projects:
            projects = list(project_team_map.keys())
    
    if not projects:
        console.print("[yellow]No projects provided. Query may be broad.[/yellow]")
    
    # Create service
    service = JiraApiService(jira_host, user, resolved_token)
    
    # Validate custom JQL
    status_list = [s for s in statuses if s]
    custom_jql = (custom_jql or "").strip()
    
    if custom_jql:
        warnings = service.validate_custom_jql(custom_jql, projects, status_list, since, until)
        for warning in warnings:
            console.print(f"[yellow]Warning: {warning}[/yellow]")
    
    # Build JQL
    jql = service.build_jql(projects, status_list, since, until, custom_jql)
    
    if jql:
        console.print(f"JQL Query: [dim]{jql}[/dim]")
    else:
        console.print("JQL Query: [dim](empty - will return all issues)[/dim]")
    
    # Fetch issues
    try:
        issues = service.search_issues(jql, max_results, fetch_all)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise SystemExit(1)
    
    if not issues:
        console.print("[yellow]No tickets found for the provided filters.[/yellow]")
        return
    
    # Prepare rows
    rows = []
    for issue in issues:
        fields = issue.get("fields", {})
        key = issue.get("key", "")
        proj_key = key.split("-")[0] if key else (fields.get("project", {}).get("key", ""))
        team = project_team_map.get(proj_key, "Unknown")
        
        rows.append({
            "key": key,
            "summary": fields.get("summary", ""),
            "status": fields.get("status", {}).get("name", ""),
            "type": fields.get("issuetype", {}).get("name", ""),
            "created": fields.get("created", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", ""),
            "team": team,
            "url": f"{jira_host}/browse/{key}",
        })
    
    # Handle summary
    group_by_list = [g.lower().strip() for g in group_by if g]
    if summary and not group_by_list:
        group_by_list = ["status"]
    
    allowed_groups = ["status", "type", "team"]
    invalid = [g for g in group_by_list if g not in allowed_groups]
    if invalid:
        console.print(f"[red]Invalid group-by: {', '.join(invalid)}. Allowed: status, type, team.[/red]")
        raise SystemExit(1)
    
    if summary:
        agg: Dict[str, Dict] = {}
        for r in rows:
            keys = [r.get(g, "Unknown") for g in group_by_list]
            k = "|".join(keys)
            if k not in agg:
                agg[k] = {"count": 0, "values": keys}
            agg[k]["count"] += 1
        
        console.print("\n[bold]Ticket summary[/bold]")
        table = Table()
        for g in group_by_list:
            table.add_column(g)
        table.add_column("count", justify="right")
        
        for v in agg.values():
            table.add_row(*v["values"], str(v["count"]))
        
        console.print(table)
    
    # Handle JSON output
    if output_json_flag:
        output_json(rows, "issues")
        return
    
    # Handle CSV export
    if output_csv_flag or csv_path:
        filename = csv_path or f"jira_issues_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        export_csv(rows, ["key", "summary", "status", "type", "created", "assignee", "team", "url"], filename)
    
    # Display table (when not summarizing)
    if not summary:
        table = Table()
        for col in ["key", "summary", "status", "type", "created", "team"]:
            table.add_column(col)
        
        for r in rows:
            table.add_row(r["key"], r["summary"], r["status"], r["type"], r["created"], r["team"])
        
        console.print(table)
    
    console.print(f"Fetched {len(rows)} tickets.")
