"""GitHub commits command."""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import click
import requests
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn
from rich.table import Table

from ..services.token_manager import TokenManager
from ..services.csv_helper import CsvHelper


console = Console()

REPO_COL = "Repository URL"
TEAM_COL = "Product Name"


def resolve_github_token(
    token: Optional[str] = None,
    reset_token: bool = False,
    verbose: bool = False
) -> Optional[str]:
    """Resolve GitHub API token."""
    manager = TokenManager("github_token.cache")
    
    help_text = """Go to https://github.com/settings/tokens
Click "Generate new token" (classic token)
Give it a descriptive name and select required scopes (repo)
Click "Generate token"
Copy the token and paste it below
Click "Configure SSO > Authorize token" for DigitalInnovation org"""
    
    return manager.get_token(
        cli_token=token,
        env_vars=["GITHUB_TOKEN", "GITHUB"],
        reset=reset_token,
        prompt_text=help_text,
        verbose=verbose
    )


def fetch_commits(
    repo: str,
    token: str,
    limit: int = 30,
    authors: Optional[List[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None
) -> List[Dict]:
    """Fetch commits from a GitHub repository.
    
    Args:
        repo: Repository in format "owner/repo".
        token: GitHub API token.
        limit: Maximum commits to fetch (0 = unlimited).
        authors: Filter by author login names.
        since: Only commits after this date.
        until: Only commits before this date.
    
    Returns:
        List of commit dictionaries.
    """
    base_url = f"https://api.github.com/repos/{repo}/commits"
    
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Data-Scripts-GitHub-Fetcher",
        "Authorization": f"Bearer {token}",
    }
    
    params = {
        "per_page": min(limit, 100) if limit > 0 else 100,
    }
    
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    
    # If single author, use API filter
    if authors and len(authors) == 1:
        params["author"] = authors[0]
    
    matched_commits = []
    page = 1
    
    def matches_author(commit: Dict, filter_authors: List[str]) -> bool:
        """Check if commit matches author filter."""
        login = commit.get("author", {}).get("login") if commit.get("author") else None
        name = commit.get("commit", {}).get("author", {}).get("name")
        return login in filter_authors or name in filter_authors
    
    while True:
        query = {**params, "page": page}
        
        try:
            response = requests.get(base_url, headers=headers, params=query, timeout=30)
            response.raise_for_status()
            commits = response.json()
        except requests.RequestException:
            break
        
        if not commits:
            break
        
        # Apply author filter locally if multiple authors
        if authors and len(authors) > 1:
            page_matches = [c for c in commits if matches_author(c, authors)]
        else:
            page_matches = commits
        
        matched_commits.extend(page_matches)
        
        if limit > 0 and len(matched_commits) >= limit:
            matched_commits = matched_commits[:limit]
            break
        
        if len(commits) < 100:
            break
        
        page += 1
        time.sleep(0.3)
    
    return matched_commits


def normalize_date(date: Optional[str]) -> Optional[str]:
    """Normalize date to ISO8601 format.
    
    Args:
        date: Date string in YYYY-MM-DD or ISO8601 format.
    
    Returns:
        ISO8601 formatted date string.
    """
    if not date:
        return None
    
    if len(date) == 10 and date[4] == "-" and date[7] == "-":
        return f"{date}T00:00:00Z"
    
    return date


@click.command("commits")
@click.argument("repo_list", type=click.Path(exists=True))
@click.option("--summary", is_flag=True, help="Show summarized output by user")
@click.option("--aggregate", is_flag=True, help="Aggregate across all repositories")
@click.option("--aggregate-team", is_flag=True, help="Aggregate by team")
@click.option("--output", "output_format", default="default", help="Output format: default|csv|json")
@click.option("--limit", type=int, default=30, help="Commits per repo (0 for all)")
@click.option("--since", help="Only commits after this date (YYYY-MM-DD or ISO8601)")
@click.option("--until", help="Only commits before this date (YYYY-MM-DD or ISO8601)")
@click.option("--author", multiple=True, help="Filter by GitHub username(s)")
@click.option("--team", help="Team to filter repositories by")
@click.option("--active-only", is_flag=True, help="Exclude authors with no commits in last 3 months")
@click.option("--detail", is_flag=True, help="Show per-repository output")
@click.option("--token", envvar="GITHUB_TOKEN", help="GitHub API token")
@click.option("--reset-token", is_flag=True, help="Remove cached GitHub token")
def commits_command(
    repo_list: str,
    summary: bool,
    aggregate: bool,
    aggregate_team: bool,
    output_format: str,
    limit: int,
    since: Optional[str],
    until: Optional[str],
    author: Tuple[str, ...],
    team: Optional[str],
    active_only: bool,
    detail: bool,
    token: Optional[str],
    reset_token: bool
):
    """Fetch commit history for a list of GitHub repositories."""
    # Parse repository CSV
    repos = CsvHelper.parse_repos_csv(repo_list, REPO_COL, TEAM_COL)
    
    if not repos:
        console.print(f"[red]No repositories found in '{repo_list}'.[/red]")
        raise SystemExit(1)
    
    # Get unique teams
    teams = sorted(set(r.get(TEAM_COL, "Unknown") for r in repos))
    
    # Filter by team if specified
    author_filter = list(author) if author else None
    
    if not author_filter and team is None:
        # Interactive team selection
        choices = ["All"] + teams
        console.print("Select team:")
        for i, choice in enumerate(choices):
            console.print(f"  {i}. {choice}")
        
        try:
            selection = click.prompt("Enter number", type=int, default=0)
            if 0 <= selection < len(choices):
                selected_team = choices[selection]
            else:
                selected_team = "All"
        except (ValueError, click.Abort):
            selected_team = "All"
    else:
        selected_team = team or "All"
    
    if selected_team and selected_team != "All":
        repos = [r for r in repos if r.get(TEAM_COL, "Unknown") == selected_team]
        if not repos:
            console.print(f"[yellow]No repositories found for team '{selected_team}'.[/yellow]")
            return
    
    # Resolve token
    resolved_token = resolve_github_token(token, reset_token)
    if not resolved_token:
        console.print("[red]A GitHub token is required to access the API.[/red]")
        raise SystemExit(1)
    
    # Normalize dates
    since = normalize_date(since)
    until = normalize_date(until)
    
    # Calculate cutoff for active-only
    cutoff_dt = datetime.utcnow() - timedelta(days=90)
    cutoff_ts = cutoff_dt.timestamp()
    
    # Default to detail if neither aggregate specified
    if not aggregate and not aggregate_team and not detail:
        detail = True
    
    console.print("[bold]Fetching commit histories from GitHub[/bold]\n")
    
    all_commits = []
    global_author_stats: Dict[str, Dict[str, Any]] = {}
    team_author_stats: Dict[str, Dict[str, Dict[str, Any]]] = {}
    active_authors: set = set()
    
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Processing repositories", total=len(repos))
        
        for repo_row in repos:
            repo_url = repo_row.get(REPO_COL, "")
            repo = repo_url.replace("https://github.com/", "")
            team_name = repo_row.get(TEAM_COL, "Unknown")
            
            commits = fetch_commits(repo, resolved_token, limit, author_filter, since, until)
            
            if detail and output_format == "default" and commits:
                console.print(f"\n[bold]Repository: {repo} (Team: {team_name})[/bold]")
            
            if not commits:
                progress.advance(task)
                continue
            
            author_counts: Dict[str, int] = {}
            
            for commit in commits:
                author_name = commit.get("commit", {}).get("author", {}).get("name", "Unknown")
                login = commit.get("author", {}).get("login", "unknown") if commit.get("author") else "unknown"
                date = commit.get("commit", {}).get("author", {}).get("date", "N/A")
                message = commit.get("commit", {}).get("message", "")
                sha = commit.get("sha", "")
                link = f"https://github.com/{repo}/commit/{sha}"
                
                # Check active author
                if date != "N/A":
                    try:
                        ts = datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp()
                        if ts >= cutoff_ts:
                            active_authors.add(author_name)
                    except ValueError:
                        pass
                
                author_counts[author_name] = author_counts.get(author_name, 0) + 1
                
                # Update global stats
                if author_name not in global_author_stats:
                    global_author_stats[author_name] = {"count": 0, "latest": None}
                global_author_stats[author_name]["count"] += 1
                
                if date != "N/A":
                    prev_latest = global_author_stats[author_name]["latest"]
                    if prev_latest is None or date > prev_latest:
                        global_author_stats[author_name]["latest"] = date
                
                # Update team stats
                if team_name not in team_author_stats:
                    team_author_stats[team_name] = {}
                if author_name not in team_author_stats[team_name]:
                    team_author_stats[team_name][author_name] = {"count": 0, "latest": None}
                team_author_stats[team_name][author_name]["count"] += 1
                
                if date != "N/A":
                    prev_latest = team_author_stats[team_name][author_name]["latest"]
                    if prev_latest is None or date > prev_latest:
                        team_author_stats[team_name][author_name]["latest"] = date
                
                all_commits.append({
                    "repository": repo,
                    "team": team_name,
                    "author_name": author_name,
                    "author_login": login,
                    "date": date,
                    "message": message,
                    "url": link,
                })
                
                if detail and not summary and output_format == "default":
                    short_msg = message[:50] + "..." if len(message) > 50 else message
                    console.print(f"  👤 {author_name[:20]:<20} (@{login[:15]:<15}) 📅 {date[:25]:<25} ✏️  {short_msg}")
            
            if detail and summary and not aggregate and output_format == "default":
                console.print(f"\n📊 Commit Summary for {repo} (Team: {team_name}):")
                sorted_counts = sorted(author_counts.items(), key=lambda x: x[1], reverse=True)
                for auth, count in sorted_counts:
                    if active_only and auth not in active_authors:
                        continue
                    console.print(f"  👤 {auth:<25} 🧾 {count:>3} commits")
            
            progress.advance(task)
    
    console.print()
    
    # Filter if active-only
    if active_only:
        global_author_stats = {k: v for k, v in global_author_stats.items() if k in active_authors}
        for team_name in list(team_author_stats.keys()):
            team_author_stats[team_name] = {
                k: v for k, v in team_author_stats[team_name].items() if k in active_authors
            }
            if not team_author_stats[team_name]:
                del team_author_stats[team_name]
        all_commits = [c for c in all_commits if c["author_name"] in active_authors]
    
    # Display aggregates
    if aggregate_team:
        console.print("[bold]📈 Aggregate Commit Summary (By Team)[/bold]")
        for team_name, stats in team_author_stats.items():
            console.print(f"\nTeam: [cyan]{team_name}[/cyan]")
            sorted_stats = sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True)
            for auth, s in sorted_stats:
                latest = s["latest"] or "N/A"
                console.print(f"  👤 {auth:<25} 🧾 {s['count']:>3} commits   ⏱️ {latest}")
        console.print()
    elif aggregate and output_format == "default":
        console.print("[bold]📈 Aggregate Commit Summary (All Repositories)[/bold]")
        sorted_stats = sorted(global_author_stats.items(), key=lambda x: x[1]["count"], reverse=True)
        for auth, s in sorted_stats:
            latest = s["latest"] or "N/A"
            console.print(f"  👤 {auth:<25} 🧾 {s['count']:>3} commits   ⏱️ {latest}")
        console.print()
    
    # Handle file outputs
    if output_format in ["csv", "json"]:
        filename = f"commits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{output_format}"
        
        if output_format == "json":
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(all_commits, f, indent=2)
        else:
            import csv
            if all_commits:
                with open(filename, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=all_commits[0].keys())
                    writer.writeheader()
                    writer.writerows(all_commits)
        
        console.print(f"[green]Data saved to {filename}[/green]")
    
    console.print("[green]Done.[/green]")
