"""GitHub metrics command - comprehensive repository metrics with flexible filtering."""

import csv as csv_module
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import click
import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

from ..services.token_manager import TokenManager


console = Console()


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
        env_vars=["GITHUB_TOKEN", "GH_TOKEN"],
        reset=reset_token,
        prompt_text=help_text,
        verbose=verbose
    )


def parse_owner_repo(repo_url: str) -> tuple:
    """Parse owner and repo from GitHub URL."""
    if not repo_url or 'github.com' not in repo_url:
        return None, None
    path = repo_url.split('github.com/')[-1].split('/tree/')[0].strip('/')
    parts = [p for p in path.split('/') if p]
    if len(parts) < 2:
        return None, None
    owner, repo = parts[0], parts[1]
    if repo.endswith('.git'):
        repo = repo[:-4]
    return owner, repo


def paged_get(url: str, headers: dict, params: dict = None, max_pages: int = 10) -> list:
    """Fetch paginated results from GitHub API with retry logic."""
    result = []
    params = params or {}
    per_page = 100
    page = 1
    while page <= max_pages:
        params.update({'per_page': per_page, 'page': page})
        
        # Retry logic for network errors
        max_retries = 3
        success = False
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                if resp.status_code != 200:
                    if resp.status_code == 403:
                        console.print(f"[red]Rate limit exceeded or forbidden: {resp.status_code}[/red]")
                    # Non-200 status, exit both loops
                    return result
                page_data = resp.json()
                if not isinstance(page_data, list) or len(page_data) == 0:
                    return result
                result.extend(page_data)
                if len(page_data) < per_page:
                    return result
                page += 1
                success = True
                time.sleep(0.2)
                break  # Success, exit retry loop
            except requests.exceptions.Timeout:
                console.print(f"[yellow]Timeout on {url} (attempt {attempt + 1}/{max_retries})[/yellow]")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                else:
                    console.print(f"[red]Failed after {max_retries} timeout attempts[/red]")
                    return result
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
                    continue
                else:
                    # Last attempt failed, return what we have
                    console.print(f"[red]Network error: {str(e)}[/red]")
                    return result
            except requests.exceptions.RequestException as e:
                # Other request errors, don't retry
                console.print(f"[red]Request error: {str(e)}[/red]")
                return result
        
        if not success:
            # Retry loop exhausted without success
            return result
    
    return result


def commits_since(owner: str, repo: str, since_iso: str, headers: dict) -> list:
    """Fetch commits since a specific date."""
    url = f'https://api.github.com/repos/{owner}/{repo}/commits'
    return paged_get(url, headers, params={'since': since_iso})


def get_repo_info(owner: str, repo: str, headers: dict) -> Optional[dict]:
    """Get repository information with retry logic."""
    url = f'https://api.github.com/repos/{owner}/{repo}'
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return None
            return resp.json()
        except requests.exceptions.Timeout:
            console.print(f"[yellow]Timeout getting repo info (attempt {attempt + 1}/{max_retries})[/yellow]")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except requests.exceptions.RequestException:
            return None
    return None


def get_prs(owner: str, repo: str, headers: dict) -> list:
    """Fetch pull requests."""
    url = f'https://api.github.com/repos/{owner}/{repo}/pulls'
    return paged_get(url, headers, params={'state': 'all'}, max_pages=5)


def get_issues(owner: str, repo: str, headers: dict) -> list:
    """Fetch issues."""
    url = f'https://api.github.com/repos/{owner}/{repo}/issues'
    return paged_get(url, headers, params={'state': 'all'}, max_pages=5)


def get_pr_details(owner: str, repo: str, pr_number: int, headers: dict) -> Optional[dict]:
    """Get detailed PR information including lines changed with retry logic."""
    url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}'
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                return None
            pr_data = resp.json()
            return {
                'number': pr_number,
                'additions': pr_data.get('additions', 0),
                'deletions': pr_data.get('deletions', 0),
                'changed_files': pr_data.get('changed_files', 0),
                'merged': pr_data.get('merged', False),
                'created_at': pr_data.get('created_at', '')[:10],
                'merged_at': pr_data.get('merged_at', '')[:10] if pr_data.get('merged_at') else ''
            }
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except requests.exceptions.RequestException:
            return None
    return None


def unique_authors_from_commits(commits: list) -> Set[str]:
    """Extract unique author names from commits."""
    authors = set()
    for c in commits:
        author = None
        if isinstance(c.get('author'), dict):
            author = c['author'].get('login')
        if not author:
            author = c.get('commit', {}).get('author', {}).get('email') or c.get('commit', {}).get('author', {}).get('name')
        if author:
            authors.add(author)
    return authors


def filter_by_date_range(items: list, date_field: str, since_date: datetime, until_date: datetime) -> list:
    """Filter items by date range."""
    filtered = []
    for item in items:
        item_date_str = item.get(date_field, '')
        if not item_date_str:
            continue
        try:
            item_date = datetime.strptime(item_date_str[:10], '%Y-%m-%d')
            if since_date.tzinfo is not None:
                item_date = item_date.replace(tzinfo=timezone.utc)
            if since_date <= item_date <= until_date:
                filtered.append(item)
        except ValueError:
            continue
    return filtered


def collect_aggregated_metrics(row: dict, since_date: datetime, until_date: datetime, 
                               headers: dict, skip_prs: bool, skip_issues: bool, verbose: bool = False) -> Optional[dict]:
    """Collect aggregated metrics for a repository."""
    repo_name = row.get('RepoName') or row.get('Repo') or ''
    repo_url = row.get('Repository URL') or row.get('RepositoryUrl') or ''
    app_name = row.get('AppName') or ''
    product = row.get('Product Name') or row.get('Product') or ''

    owner, repo = parse_owner_repo(repo_url)
    if not owner or not repo:
        if verbose:
            console.print(f"  [yellow]- Invalid repo URL: {repo_url}[/yellow]")
        return None

    if verbose:
        console.print(f"  [cyan]- Fetching repo info for {owner}/{repo}...[/cyan]")
    
    repo_info = get_repo_info(owner, repo, headers)
    if not repo_info:
        console.print(f"  [yellow]- Skipping (not accessible): {owner}/{repo}[/yellow]")
        return None
    
    if verbose:
        console.print("  [green]- Repo info retrieved[/green]")

    days_range = (until_date - since_date).days
    since_iso = since_date.isoformat() + 'Z'
    until_iso = until_date.isoformat() + 'Z'

    if verbose:
        console.print(f"  [cyan]- Fetching commits since {since_iso[:10]}...[/cyan]")
    
    commits = commits_since(owner, repo, since_iso, headers)
    commits = [c for c in commits if c.get('commit', {}).get('author', {}).get('date', '')[:10] <= until_iso[:10]]
    authors = unique_authors_from_commits(commits)
    
    if verbose:
        console.print(f"  [green]- Found {len(commits)} commits[/green]")

    prs_opened = []
    prs_merged = []
    total_additions = 0
    total_deletions = 0
    total_changed_files = 0
    
    if not skip_prs:
        prs = get_prs(owner, repo, headers)
        prs_opened = filter_by_date_range(prs, 'created_at', since_date, until_date)
        prs_merged = [
            pr for pr in prs 
            if pr.get('merged_at') and 
            since_date <= datetime.strptime(pr['merged_at'][:10], '%Y-%m-%d').replace(tzinfo=timezone.utc) <= until_date
        ]
        
        for pr in prs_merged[:20]:
            pr_detail = get_pr_details(owner, repo, pr['number'], headers)
            if pr_detail:
                total_additions += pr_detail['additions']
                total_deletions += pr_detail['deletions']
                total_changed_files += pr_detail['changed_files']
            time.sleep(0.2)
    
    actual_issues = []
    if not skip_issues:
        issues = get_issues(owner, repo, headers)
        actual_issues = [i for i in issues if 'pull_request' not in i]
        actual_issues = filter_by_date_range(actual_issues, 'created_at', since_date, until_date)

    last_commit_date = commits[0].get('commit', {}).get('author', {}).get('date', '')[:10] if commits else ''

    return {
        'RepoName': repo_name,
        'AppName': app_name,
        'ProductName': product,
        'RepositoryURL': repo_url,
        'owner': owner,
        'repo': repo,
        'date_range_start': since_date.strftime('%Y-%m-%d'),
        'date_range_end': until_date.strftime('%Y-%m-%d'),
        'days_in_range': days_range,
        'total_commits': len(commits),
        'unique_contributors': len(authors),
        'avg_commits_per_day': round(len(commits) / max(days_range, 1), 2),
        'total_pull_requests': len(prs_opened),
        'open_pull_requests': sum(1 for p in prs_opened if p.get('state') == 'open'),
        'merged_pull_requests': len(prs_merged),
        'pr_lines_added': total_additions,
        'pr_lines_deleted': total_deletions,
        'pr_lines_changed': total_additions + total_deletions,
        'pr_files_changed': total_changed_files,
        'total_issues': len(actual_issues),
        'closed_issues': sum(1 for i in actual_issues if i.get('state') == 'closed'),
        'stars': repo_info.get('stargazers_count', 0),
        'forks': repo_info.get('forks_count', 0),
        'watchers': repo_info.get('watchers_count', 0),
        'primary_language': repo_info.get('language', ''),
        'repository_size_kb': repo_info.get('size', 0),
        'last_commit_date': last_commit_date,
        'collection_date': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    }


def collect_daily_metrics(row: dict, since_date: datetime, until_date: datetime,
                          headers: dict, skip_prs: bool, skip_issues: bool) -> List[dict]:
    """Collect daily metrics for a repository."""
    repo_name = row.get('RepoName') or row.get('Repo') or ''
    repo_url = row.get('Repository URL') or row.get('RepositoryUrl') or ''
    app_name = row.get('AppName') or ''
    product = row.get('Product Name') or row.get('Product') or ''

    owner, repo = parse_owner_repo(repo_url)
    if not owner or not repo:
        return []

    repo_info = get_repo_info(owner, repo, headers)
    if not repo_info:
        console.print(f"  [yellow]- Skipping (not accessible): {owner}/{repo}[/yellow]")
        return []

    since_iso = since_date.isoformat() + 'Z'
    until_iso = until_date.isoformat() + 'Z'
    
    all_commits = commits_since(owner, repo, since_iso, headers)
    all_commits = [c for c in all_commits if c.get('commit', {}).get('author', {}).get('date', '')[:10] <= until_iso[:10]]
    
    all_prs = []
    all_issues = []
    
    if not skip_prs:
        all_prs = get_prs(owner, repo, headers)
    
    if not skip_issues:
        issues = get_issues(owner, repo, headers)
        all_issues = [i for i in issues if 'pull_request' not in i]
        all_issues = filter_by_date_range(all_issues, 'created_at', since_date, until_date)
    
    daily_metrics = []
    current_date = since_date
    
    while current_date <= until_date:
        date_str = current_date.strftime('%Y-%m-%d')
        
        day_commits = [c for c in all_commits if c.get('commit', {}).get('author', {}).get('date', '')[:10] == date_str]
        day_authors = unique_authors_from_commits(day_commits)
        day_prs_opened = [p for p in all_prs if p.get('created_at', '')[:10] == date_str] if not skip_prs else []
        day_prs_merged = [p for p in all_prs if p.get('merged_at') and p.get('merged_at', '')[:10] == date_str] if not skip_prs else []
        day_issues = [i for i in all_issues if i.get('created_at', '')[:10] == date_str] if not skip_issues else []
        
        total_additions = 0
        total_deletions = 0
        total_changed_files = 0
        
        if day_prs_merged:
            for pr in day_prs_merged[:10]:
                pr_detail = get_pr_details(owner, repo, pr['number'], headers)
                if pr_detail:
                    total_additions += pr_detail['additions']
                    total_deletions += pr_detail['deletions']
                    total_changed_files += pr_detail['changed_files']
                time.sleep(0.2)
        
        if day_commits or day_prs_opened or day_prs_merged or day_issues:
            daily_metrics.append({
                'date': date_str,
                'RepoName': repo_name,
                'AppName': app_name,
                'ProductName': product,
                'RepositoryURL': repo_url,
                'owner': owner,
                'repo': repo,
                'total_commits': len(day_commits),
                'unique_contributors': len(day_authors),
                'total_pull_requests': len(day_prs_opened),
                'open_pull_requests': sum(1 for p in day_prs_opened if p.get('state') == 'open'),
                'merged_pull_requests': len(day_prs_merged),
                'pr_lines_added': total_additions,
                'pr_lines_deleted': total_deletions,
                'pr_lines_changed': total_additions + total_deletions,
                'pr_files_changed': total_changed_files,
                'total_issues': len(day_issues),
                'closed_issues': sum(1 for i in day_issues if i.get('state') == 'closed'),
                'stars': repo_info.get('stargazers_count', 0),
                'forks': repo_info.get('forks_count', 0),
                'watchers': repo_info.get('watchers_count', 0),
                'primary_language': repo_info.get('language', ''),
                'repository_size_kb': repo_info.get('size', 0),
                'collection_date': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            })
        
        current_date += timedelta(days=1)
    
    time.sleep(0.3)
    return daily_metrics


@click.command()
@click.argument('repos_csv', type=click.Path(exists=True))
@click.option('--since', help='Start date (YYYY-MM-DD). Default: 30 days ago')
@click.option('--until', help='End date (YYYY-MM-DD). Default: today')
@click.option('--days', type=int, help='Number of days back from today (overrides --since)')
@click.option('--repo', help='Filter by repository name (partial match, case-insensitive)')
@click.option('--exact-repo', help='Filter by exact repository name (case-sensitive)')
@click.option('--team', help='Filter by team/product name (supports partial match)')
@click.option('--app', help='Filter by app name (supports partial match)')
@click.option('--limit', type=int, default=0, help='Limit number of repos to process (0 = all)')
@click.option('--all', 'process_all', is_flag=True, help='Process all repositories (same as --limit 0)')
@click.option('--csv-path', default='outputs/csv/github_metrics.csv', help='Output CSV file path')
@click.option('--json', 'output_json', is_flag=True, help='Also output results as JSON')
@click.option('--summary', is_flag=True, help='Show summary statistics only')
@click.option('--skip-prs', is_flag=True, help='Skip PR collection (faster)')
@click.option('--skip-issues', is_flag=True, help='Skip issue collection (faster)')
@click.option('--daily', is_flag=True, help='Generate daily metrics (one row per repo per day)')
@click.option('--token', help='GitHub Personal Access Token')
@click.option('--reset-token', is_flag=True, help='Reset cached GitHub token')
@click.option('--verbose', is_flag=True, help='Show verbose output')
def metrics_command(repos_csv, since, until, days, repo, exact_repo, team, app,
                   limit, process_all, csv_path, output_json, summary, skip_prs, skip_issues,
                   daily, token, reset_token, verbose):
    """
    Collect comprehensive GitHub repository metrics with flexible filtering.
    
    Examples:
    
        # Default: last 30 days, all repos
        data-scripts github metrics data/repos.csv
        
        # Specific date range
        data-scripts github metrics data/repos.csv --since 2024-01-01 --until 2024-01-31
        
        # Filter by team
        data-scripts github metrics data/repos.csv --team "Product Flow"
        
        # Daily metrics mode
        data-scripts github metrics data/repos.csv --daily --days 14
    """
    github_token = resolve_github_token(token, reset_token, verbose)
    if not github_token:
        raise click.ClickException("GitHub token required. Set GITHUB_TOKEN or use --token")
    
    headers = {
        'Authorization': f'token {github_token}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'Data-Scripts-GitHub-Fetcher',
    }
    
    # Calculate date range
    if days:
        since_date = datetime.now(timezone.utc) - timedelta(days=days)
        until_date = datetime.now(timezone.utc)
    else:
        if since:
            since_date = datetime.strptime(since, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        else:
            since_date = datetime.now(timezone.utc) - timedelta(days=30)
        
        if until:
            until_date = datetime.strptime(until, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        else:
            until_date = datetime.now(timezone.utc)
    
    console.print(f"\n[cyan]Date Range:[/cyan] {since_date.strftime('%Y-%m-%d')} to {until_date.strftime('%Y-%m-%d')}")
    console.print(f"[cyan]Days:[/cyan] {(until_date - since_date).days}\n")
    
    # Read repositories
    if not Path(repos_csv).exists():
        raise click.ClickException(f"File not found: {repos_csv}")
    
    rows = []
    with open(repos_csv, newline='', encoding='utf-8') as fh:
        reader = csv_module.DictReader(fh)
        for r in reader:
            if r and (r.get('RepoName') or r.get('Repository URL')):
                rows.append(r)
    
    # Exclude archived repositories
    rows = [r for r in rows if (r.get('RepoStatus') or 'Active').lower() != 'archived']
    
    # Apply filters
    if exact_repo:
        rows = [r for r in rows if (r.get('RepoName') or '') == exact_repo]
    elif repo:
        rows = [r for r in rows if repo.lower() in (r.get('RepoName') or '').lower()]
    
    if team:
        rows = [r for r in rows if team.lower() in (r.get('Product Name') or '').lower()]
    
    if app:
        rows = [r for r in rows if app.lower() in (r.get('AppName') or '').lower()]
    
    # Apply limit
    if limit > 0 and not process_all:
        rows = rows[:limit]
    
    console.print(f"[green]Found {len(rows)} repositories to analyze[/green]\n")
    
    # Collect metrics
    results = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.completed}/{task.total}"),
        console=console
    ) as progress:
        task = progress.add_task("Collecting metrics...", total=len(rows))
        
        for row in rows:
            repo_name = row.get('RepoName', '')
            progress.update(task, description=f"Processing {repo_name}...")
            
            try:
                if daily:
                    daily_records = collect_daily_metrics(row, since_date, until_date, headers, skip_prs, skip_issues)
                    if daily_records:
                        results.extend(daily_records)
                else:
                    metrics = collect_aggregated_metrics(row, since_date, until_date, headers, skip_prs, skip_issues, verbose)
                    if metrics:
                        results.append(metrics)
            except Exception as e:
                console.print(f"[red]Error collecting metrics for {repo_name}: {str(e).replace('[', '').replace(']', '')}[/red]")
            
            progress.advance(task)
    
    if not results:
        console.print("[yellow]No metrics collected[/yellow]")
        return
    
    # Show summary if requested
    if summary:
        table = Table(title="Summary Statistics")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        
        if not daily:
            total_commits = sum(r['total_commits'] for r in results)
            total_prs = sum(r['total_pull_requests'] for r in results)
            total_issues = sum(r['total_issues'] for r in results)
            
            table.add_row("Repositories", str(len(results)))
            table.add_row("Total Commits", f"{total_commits:,}")
            table.add_row("Total Pull Requests", f"{total_prs:,}")
            table.add_row("Total Issues", f"{total_issues:,}")
        
        console.print(table)
    
    # Define field order
    if daily:
        fieldnames = [
            'date', 'RepoName', 'AppName', 'ProductName', 'RepositoryURL', 'owner', 'repo',
            'total_commits', 'unique_contributors',
            'total_pull_requests', 'open_pull_requests', 'merged_pull_requests',
            'pr_lines_added', 'pr_lines_deleted', 'pr_lines_changed', 'pr_files_changed',
            'total_issues', 'closed_issues',
            'stars', 'forks', 'watchers',
            'primary_language', 'repository_size_kb', 'collection_date'
        ]
    else:
        fieldnames = [
            'RepoName', 'AppName', 'ProductName', 'RepositoryURL', 'owner', 'repo',
            'date_range_start', 'date_range_end', 'days_in_range',
            'total_commits', 'unique_contributors', 'avg_commits_per_day',
            'total_pull_requests', 'open_pull_requests', 'merged_pull_requests',
            'pr_lines_added', 'pr_lines_deleted', 'pr_lines_changed', 'pr_files_changed',
            'total_issues', 'closed_issues',
            'stars', 'forks', 'watchers',
            'primary_language', 'repository_size_kb', 'last_commit_date', 'collection_date'
        ]
    
    # Ensure output directory exists
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    
    # Write CSV
    with open(csv_path, 'w', newline='', encoding='utf-8') as out:
        writer = csv_module.DictWriter(out, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, '') for k in fieldnames})
    
    console.print(f"\n[green]✓ Wrote {len(results)} records to {csv_path}[/green]")
    
    # Write JSON if requested
    if output_json:
        json_path = csv_path.replace('.csv', '.json')
        with open(json_path, 'w', encoding='utf-8') as jf:
            json.dump(results, jf, indent=2)
        console.print(f"[green]✓ Wrote JSON to {json_path}[/green]")
