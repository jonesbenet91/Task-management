"""GitHub Network Config Command - checks for *.network.config.yaml and counts network rules."""

import base64
import csv
import time

import click
import requests
import yaml
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from datascripts.services.token_manager import TokenManager
from datascripts.services import GITHUB_ORG

console = Console()

ORG = GITHUB_ORG
MAX_RETRIES = 3
RETRY_DELAY = 2


def _api_call(session: requests.Session, url: str, verbose: bool = False, retry_count: int = 0) -> Optional[requests.Response]:
    """Make GitHub API call with retry logic."""
    try:
        response = session.get(url)

        if response.status_code == 403 and 'rate limit' in response.text.lower():
            if retry_count < MAX_RETRIES:
                wait_time = RETRY_DELAY * (2 ** retry_count)
                if verbose:
                    console.print(f"[yellow]Rate limited, waiting {wait_time}s...[/yellow]")
                time.sleep(wait_time)
                return _api_call(session, url, verbose, retry_count + 1)
            return None

        if response.status_code >= 500:
            if retry_count < MAX_RETRIES:
                wait_time = RETRY_DELAY * (2 ** retry_count)
                time.sleep(wait_time)
                return _api_call(session, url, verbose, retry_count + 1)
            return None

        return response

    except requests.RequestException as e:
        if retry_count < MAX_RETRIES:
            wait_time = RETRY_DELAY * (2 ** retry_count)
            time.sleep(wait_time)
            return _api_call(session, url, verbose, retry_count + 1)
        if verbose:
            console.print(f"[red]API call failed: {e}[/red]")
        return None


def count_rules(config: dict) -> Dict[str, int]:
    """Count inbound/outbound network rules from a parsed network config.

    A 'rule' is any entry under inbound.services, inbound.internet,
    outbound.services, or outbound.internet for each service.
    """
    counts = {
        'inbound_services': 0,
        'inbound_internet': 0,
        'outbound_services': 0,
        'outbound_internet': 0,
    }

    for service in config.get('services', []):
        inbound = service.get('inbound', {}) or {}
        outbound = service.get('outbound', {}) or {}

        counts['inbound_services'] += len(inbound.get('services', []) or [])
        counts['inbound_internet'] += len(inbound.get('internet', []) or [])
        counts['outbound_services'] += len(outbound.get('services', []) or [])
        counts['outbound_internet'] += len(outbound.get('internet', []) or [])

    return counts


def analyze_repo_network(session: requests.Session, repo_name: str, verbose: bool = False) -> Dict:
    """Check a repo for *.network.config.yaml and count rules if found."""
    result = {
        'repo': repo_name,
        'has_network_config': False,
        'config_file': None,
        'services_count': 0,
        'inbound_services': 0,
        'inbound_internet': 0,
        'outbound_services': 0,
        'outbound_internet': 0,
        'total_rules': 0,
        'error': None,
    }

    # Get the default branch tree to find *.network.config.yaml at top level
    repo_url = f"https://api.github.com/repos/{ORG}/{repo_name}"
    repo_resp = _api_call(session, repo_url, verbose)

    if not repo_resp or repo_resp.status_code == 404:
        result['error'] = 'Repo not found'
        return result
    if repo_resp.status_code != 200:
        result['error'] = f'HTTP {repo_resp.status_code}'
        return result

    repo_data = repo_resp.json()
    default_branch = repo_data.get('default_branch', 'main')

    # Get top-level tree only (not recursive - we only want root files)
    tree_url = f"https://api.github.com/repos/{ORG}/{repo_name}/git/trees/{default_branch}"
    tree_resp = _api_call(session, tree_url, verbose)

    if not tree_resp or tree_resp.status_code != 200:
        result['error'] = 'Could not read repo tree'
        return result

    tree_data = tree_resp.json()
    network_files = [
        item for item in tree_data.get('tree', [])
        if item['type'] == 'blob' and item['path'].endswith('.network.config.yaml')
    ]

    if not network_files:
        return result

    # Take the first match
    config_file = network_files[0]
    result['has_network_config'] = True
    result['config_file'] = config_file['path']

    # Fetch file content via blob
    blob_url = f"https://api.github.com/repos/{ORG}/{repo_name}/git/blobs/{config_file['sha']}"
    blob_resp = _api_call(session, blob_url, verbose)

    if not blob_resp or blob_resp.status_code != 200:
        result['error'] = 'Could not fetch config file'
        return result

    blob_data = blob_resp.json()
    if blob_data.get('encoding') != 'base64':
        result['error'] = 'Unexpected encoding'
        return result

    content = base64.b64decode(blob_data['content']).decode('utf-8', errors='ignore')

    try:
        config = yaml.safe_load(content)
    except yaml.YAMLError as e:
        result['error'] = f'YAML parse error: {e}'
        return result

    if not isinstance(config, dict):
        result['error'] = 'Invalid config structure'
        return result

    result['services_count'] = len(config.get('services', []))
    counts = count_rules(config)
    result.update(counts)
    result['total_rules'] = sum(counts.values())

    return result


def _parse_repos_from_csv(csv_path: str) -> List[str]:
    """Parse repo names from CSV, matching the techstack command pattern."""
    repos = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        csv_reader = csv.reader(f)
        rows = list(csv_reader)

    # Skip header if present
    start_idx = 0
    if rows and len(rows[0]) >= 2:
        second_col = rows[0][1].lower()
        if any(kw in second_col for kw in ['repository', 'repo', 'reponame']):
            start_idx = 1

    for row in rows[start_idx:]:
        if len(row) >= 2 and row[1]:
            repo_name = row[1].strip().strip('"')
            if '/' in repo_name:
                repo_name = repo_name.split('/')[-1]
            if repo_name:
                repos.append(repo_name)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for r in repos:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


@click.command()
@click.option('--repos-csv',
              default='data/repos.csv',
              help='Path to repos CSV file (default: data/repos.csv)')
@click.option('--token',
              envvar='GITHUB_TOKEN',
              help='GitHub API token (reads from GITHUB_TOKEN env var)')
@click.option('--reset-token',
              is_flag=True,
              help='Clear cached token and prompt for new one')
@click.option('--csv', 'output_csv',
              is_flag=True,
              help='Output results as CSV to outputs/csv/github_network.csv')
@click.option('--verbose',
              is_flag=True,
              help='Show detailed progress information')
def network(repos_csv, token, reset_token, output_csv, verbose):
    """Check repositories for *.network.config.yaml and count network rules."""

    csv_path = Path(repos_csv)
    if not csv_path.exists():
        raise click.ClickException(f"CSV file not found: {repos_csv}")

    # Get GitHub token
    token_manager = TokenManager('github_token.cache')
    github_token = token_manager.get_token(
        cli_token=token,
        env_vars=['GITHUB_TOKEN'],
        reset=reset_token,
        prompt_text="GitHub API token (will be cached)",
        verbose=verbose
    )

    if not github_token:
        raise click.ClickException("GitHub token is required")

    repos = _parse_repos_from_csv(str(csv_path))
    if not repos:
        raise click.ClickException("No repositories found in CSV")

    if verbose:
        console.print(f"[cyan]Found {len(repos)} repositories to check[/cyan]")

    session = requests.Session()
    session.headers.update({
        'Authorization': f'token {github_token}',
        'Accept': 'application/vnd.github.v3+json',
    })

    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Checking repositories...", total=len(repos))

        for repo in repos:
            progress.update(task, description=f"Checking {repo}...")
            result = analyze_repo_network(session, repo, verbose)
            results.append(result)
            progress.advance(task)

    # Summary stats
    total = len(results)
    with_config = [r for r in results if r['has_network_config']]
    with_errors = [r for r in results if r['error']]

    # Only show detailed table for repos with configs or errors (unless verbose)
    display_results = results if verbose else [r for r in results if r['has_network_config'] or r['error']]

    if display_results:
        table = Table(show_header=True, header_style="bold cyan", show_lines=False, expand=False)
        table.add_column("Repo", style="white", no_wrap=True)
        table.add_column("Config File", no_wrap=True)
        table.add_column("Svcs", justify="right")
        table.add_column("In Svc", justify="right")
        table.add_column("In Net", justify="right")
        table.add_column("Out Svc", justify="right")
        table.add_column("Out Net", justify="right")
        table.add_column("Total", justify="right", style="bold")
        table.add_column("Error", style="red")

        for r in display_results:
            config_file = r['config_file'] or '-'
            error = r['error'] or ''
            table.add_row(
                r['repo'],
                config_file,
                str(r['services_count']) if r['has_network_config'] else '-',
                str(r['inbound_services']) if r['has_network_config'] else '-',
                str(r['inbound_internet']) if r['has_network_config'] else '-',
                str(r['outbound_services']) if r['has_network_config'] else '-',
                str(r['outbound_internet']) if r['has_network_config'] else '-',
                str(r['total_rules']) if r['has_network_config'] else '-',
                error,
            )

        console.print(table)

    console.print(f"\n[cyan]Summary: {len(with_config)}/{total} repos have network config, "
                  f"{len(with_errors)} errors[/cyan]")

    if output_csv:
        out_path = Path('outputs/csv/github_network.csv')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            'repo', 'has_network_config', 'config_file', 'services_count',
            'inbound_services', 'inbound_internet',
            'outbound_services', 'outbound_internet', 'total_rules', 'error',
        ]
        with open(out_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        console.print(f"[green]✓ CSV written to {out_path}[/green]")
