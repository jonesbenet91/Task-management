"""GitHub repo health command - scans repos for PR/branch hygiene and config compliance."""

import csv as csv_module
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click
import requests
from dateutil import parser as dateparser
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..services.token_manager import TokenManager


console = Console()

GITHUB_API = "https://api.github.com"

DEFAULT_THRESHOLDS = {
    "stale_pr_days": 7,
    "stale_branch_days": 90,
}

DEFAULT_STANDARDS = {
    "allowed_default_branches": ["main", "develop"],
    "required_merge_strategies": {"allow_merge_commit": True},
}


BRANCHES_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    refs(refPrefix:"refs/heads/", first:100, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target {
          ... on Commit { committedDate }
        }
      }
    }
  }
}
"""


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
        verbose=verbose,
    )


def parse_owner_repo(repo_url: str) -> Tuple[Optional[str], Optional[str]]:
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
    # Some entries have trailing junk after the repo (e.g. ": repository created from ...")
    repo = repo.split(':')[0].split(' ')[0].strip()
    return owner, repo


def gh_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Data-Scripts-Repo-Health",
    })
    return s


def gh_get(session: requests.Session, url: str,
           params: Optional[dict] = None) -> requests.Response:
    """GET with rate-limit aware retries."""
    r = None
    for attempt in range(5):
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            reset = int(r.headers.get("X-RateLimit-Reset", "0"))
            wait = max(reset - int(time.time()), 5)
            console.print(f"[yellow]Rate limited. Sleeping {wait}s...[/yellow]")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return r
    return r


def paginate(session: requests.Session, url: str,
             params: Optional[dict] = None):
    params = dict(params or {})
    params.setdefault("per_page", 100)
    while url:
        r = gh_get(session, url, params=params)
        if r is None or r.status_code != 200:
            return
        for item in r.json():
            yield item
        url = r.links.get("next", {}).get("url")
        params = None


def fetch_stale_branches(session: requests.Session, owner: str, name: str,
                         default_branch: str, stale_days: int,
                         now: datetime) -> List[dict]:
    """Return branches older than stale_days, using GraphQL (1 query per 100 branches)."""
    stale: List[dict] = []
    cursor = None
    for _ in range(50):  # hard safety cap: 5000 branches per repo
        resp = session.post(
            f"{GITHUB_API}/graphql",
            json={"query": BRANCHES_QUERY,
                  "variables": {"owner": owner, "name": name, "cursor": cursor}},
            timeout=30,
        )
        if resp.status_code != 200:
            return stale
        body = resp.json()
        if "errors" in body:
            return stale
        refs = (body.get("data") or {}).get("repository", {}).get("refs") or {}
        for node in refs.get("nodes") or []:
            if node["name"] == default_branch:
                continue
            target = node.get("target") or {}
            committed = target.get("committedDate")
            if not committed:
                continue
            commit_date = dateparser.isoparse(committed)
            age_days = (now - commit_date).days
            if age_days > stale_days:
                stale.append({
                    "name": node["name"],
                    "last_commit": commit_date.isoformat(),
                    "age_days": age_days,
                })
        page_info = refs.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return stale


def scan_repo(session: requests.Session, owner: str, name: str,
              cfg: dict) -> dict:
    """Scan a single repo for health signals."""
    result: Dict[str, Any] = {
        "owner": owner,
        "name": name,
        "url": f"https://github.com/{owner}/{name}",
        "issues": [],
    }
    r = gh_get(session, f"{GITHUB_API}/repos/{owner}/{name}")
    if r is None:
        result["error"] = "no_response"
        result["issues"].append("No API response")
        return result
    if r.status_code == 404:
        result["error"] = "not_found"
        result["issues"].append("Repo not found or no access")
        return result
    if r.status_code != 200:
        result["error"] = f"http_{r.status_code}"
        result["issues"].append(f"API error: {r.status_code}")
        return result

    meta = r.json()
    result.update({
        "default_branch": meta.get("default_branch"),
        "archived": meta.get("archived", False),
        "pushed_at": meta.get("pushed_at"),
        "allow_merge_commit": meta.get("allow_merge_commit"),
        "allow_squash_merge": meta.get("allow_squash_merge"),
        "allow_rebase_merge": meta.get("allow_rebase_merge"),
        "visibility": meta.get("visibility"),
    })

    if meta.get("archived"):
        return result

    allowed = cfg["standards"]["allowed_default_branches"]
    if result["default_branch"] not in allowed:
        result["issues"].append(
            f"Default branch '{result['default_branch']}' not in {allowed}"
        )

    if cfg["standards"]["required_merge_strategies"].get("allow_merge_commit"):
        if result["allow_merge_commit"] is False:
            result["issues"].append("Merge commit strategy is disabled")

    now = datetime.now(timezone.utc)
    stale_pr_days = cfg["thresholds"]["stale_pr_days"]
    prs: List[dict] = []
    for pr in paginate(session, f"{GITHUB_API}/repos/{owner}/{name}/pulls",
                       {"state": "open"}):
        created = dateparser.isoparse(pr["created_at"])
        age_days = (now - created).days
        prs.append({
            "number": pr["number"],
            "title": pr["title"],
            "user": (pr.get("user") or {}).get("login"),
            "created_at": pr["created_at"],
            "age_days": age_days,
            "url": pr["html_url"],
            "draft": pr.get("draft", False),
            "stale": age_days > stale_pr_days,
        })
    result["open_prs"] = prs
    result["open_pr_count"] = len(prs)
    result["stale_pr_count"] = sum(1 for p in prs if p["stale"])
    if result["stale_pr_count"]:
        result["issues"].append(
            f"{result['stale_pr_count']} PR(s) open >{stale_pr_days} days"
        )

    stale_branch_days = cfg["thresholds"]["stale_branch_days"]
    stale_branches = fetch_stale_branches(
        session, owner, name, result["default_branch"], stale_branch_days, now
    )
    stale_branches.sort(key=lambda b: b["age_days"], reverse=True)
    result["stale_branches"] = stale_branches
    result["stale_branch_count"] = len(stale_branches)
    if stale_branches:
        result["issues"].append(
            f"{len(stale_branches)} branch(es) stale >{stale_branch_days} days"
        )

    return result


def summarize(results: List[dict]) -> dict:
    """Summarise scan results across repos. Pure function, used by tests."""
    active = [r for r in results if not r.get("archived") and not r.get("error")]
    return {
        "total": len(results),
        "active": len(active),
        "archived": sum(1 for r in results if r.get("archived")),
        "errored": sum(1 for r in results if r.get("error")),
        "non_standard_default_branch": sum(
            1 for r in active
            if "Default branch" in " ".join(r.get("issues", []))
        ),
        "merge_commit_disabled": sum(
            1 for r in active if r.get("allow_merge_commit") is False
        ),
        "repos_with_stale_prs": sum(
            1 for r in active if r.get("stale_pr_count", 0) > 0
        ),
        "total_open_prs": sum(r.get("open_pr_count", 0) for r in active),
        "total_stale_prs": sum(r.get("stale_pr_count", 0) for r in active),
        "repos_with_stale_branches": sum(
            1 for r in active if r.get("stale_branch_count", 0) > 0
        ),
        "total_stale_branches": sum(
            r.get("stale_branch_count", 0) for r in active
        ),
        "healthy": sum(1 for r in active if not r.get("issues")),
    }


CSV_FIELDS = [
    "AppID", "AppName", "ProductGroup", "ProductName",
    "Owner", "RepoName", "Url", "Visibility", "Archived",
    "DefaultBranch", "AllowMergeCommit", "AllowSquashMerge", "AllowRebaseMerge",
    "OpenPRCount", "StalePRCount", "StaleBranchCount",
    "Issues", "Error", "ScannedAt",
]


def _row_for_csv(repo: dict, scanned_at: str) -> dict:
    return {
        "AppID": repo.get("app_id", ""),
        "AppName": repo.get("app_name", ""),
        "ProductGroup": repo.get("product_group", ""),
        "ProductName": repo.get("product_name", ""),
        "Owner": repo.get("owner", ""),
        "RepoName": repo.get("name", ""),
        "Url": repo.get("url", ""),
        "Visibility": repo.get("visibility", ""),
        "Archived": repo.get("archived", ""),
        "DefaultBranch": repo.get("default_branch", ""),
        "AllowMergeCommit": repo.get("allow_merge_commit", ""),
        "AllowSquashMerge": repo.get("allow_squash_merge", ""),
        "AllowRebaseMerge": repo.get("allow_rebase_merge", ""),
        "OpenPRCount": repo.get("open_pr_count", 0),
        "StalePRCount": repo.get("stale_pr_count", 0),
        "StaleBranchCount": repo.get("stale_branch_count", 0),
        "Issues": "; ".join(repo.get("issues", []) or []),
        "Error": repo.get("error", ""),
        "ScannedAt": scanned_at,
    }


def _update_index(json_dir: Path) -> None:
    snapshots = []
    for p in json_dir.glob("*.json"):
        if p.name in ("latest.json", "index.json"):
            continue
        snapshots.append(p.stem)
    snapshots.sort(reverse=True)
    (json_dir / "index.json").write_text(
        json.dumps({"snapshots": snapshots}, indent=2)
    )


@click.command()
@click.argument('repos_csv', type=click.Path(exists=True), required=False)
@click.option('--limit', type=int, default=0, help='Limit number of repos to process (0 = all)')
@click.option('--repo', help='Filter by repository name (partial match, case-insensitive)')
@click.option('--csv-path', default='outputs/csv/repo_health.csv', help='Output CSV file path')
@click.option('--json-dir', default='outputs/json/repo_health', help='Directory for JSON snapshots')
@click.option('--stale-pr-days', type=int, default=DEFAULT_THRESHOLDS["stale_pr_days"],
              help='PRs open longer than this are flagged stale')
@click.option('--stale-branch-days', type=int, default=DEFAULT_THRESHOLDS["stale_branch_days"],
              help='Branches with no commits in this many days are flagged stale')
@click.option('--token', help='GitHub Personal Access Token')
@click.option('--reset-token', is_flag=True, help='Reset cached GitHub token')
@click.option('--verbose', is_flag=True, help='Show verbose output')
def repo_health_command(repos_csv, limit, repo, csv_path, json_dir,
                        stale_pr_days, stale_branch_days,
                        token, reset_token, verbose):
    """
    Scan GitHub repositories for PR & branch health and config compliance.

    Checks default branch, merge-commit policy, stale PRs (>7d) and stale
    branches (>90d). Uses GraphQL for branch listing so repos with hundreds
    of branches stay fast.

    Examples:

        # Scan all repos in the default catalog
        data-scripts github repo-health data/repos.csv

        # One specific repo
        data-scripts github repo-health data/repos.csv --repo mtp_aso_UI

        # Custom thresholds
        data-scripts github repo-health data/repos.csv --stale-pr-days 14
    """
    if repos_csv is None:
        repos_csv = 'data/repos.csv'
        if not Path(repos_csv).exists():
            raise click.ClickException(f"File not found: {repos_csv}")

    github_token = resolve_github_token(token, reset_token, verbose)
    if not github_token:
        raise click.ClickException("GitHub token required. Set GITHUB_TOKEN or use --token")

    cfg = {
        "thresholds": {
            "stale_pr_days": stale_pr_days,
            "stale_branch_days": stale_branch_days,
        },
        "standards": DEFAULT_STANDARDS,
    }

    rows: List[dict] = []
    with open(repos_csv, newline='', encoding='utf-8') as fh:
        reader = csv_module.DictReader(fh)
        for r in reader:
            if r and (r.get('RepoName') or r.get('Repository URL')):
                rows.append(r)

    if repo:
        needle = repo.lower()
        rows = [r for r in rows if needle in (r.get('RepoName') or '').lower()]

    if limit and limit > 0:
        rows = rows[:limit]

    console.print(f"[green]Found {len(rows)} repositories to scan[/green]\n")

    session = gh_session(github_token)
    results: List[dict] = []
    scanned_at = datetime.now(timezone.utc).isoformat()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning repos...", total=len(rows))

        for row in rows:
            repo_url = row.get('Repository URL') or ''
            owner, name = parse_owner_repo(repo_url)
            if not owner or not name:
                if verbose:
                    console.print(
                        f"[yellow]Skipping row with unparseable URL: "
                        f"{row.get('RepoName')} -> {repo_url!r}[/yellow]"
                    )
                progress.advance(task)
                continue

            progress.update(task, description=f"Scanning {owner}/{name}...")
            try:
                data = scan_repo(session, owner, name, cfg)
            except Exception as exc:  # noqa: BLE001
                data = {
                    "owner": owner,
                    "name": name,
                    "url": f"https://github.com/{owner}/{name}",
                    "error": "exception",
                    "issues": [f"Scan failed: {exc}"],
                }

            data["app_id"] = row.get('AppID') or ''
            data["app_name"] = row.get('AppName') or ''
            data["product_group"] = row.get('Product Group') or ''
            data["product_name"] = row.get('Product Name') or ''
            results.append(data)
            progress.advance(task)

    summary = summarize(results)

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, 'w', newline='', encoding='utf-8') as out:
        writer = csv_module.DictWriter(out, fieldnames=CSV_FIELDS,
                                       quoting=csv_module.QUOTE_MINIMAL)
        writer.writeheader()
        for r in results:
            writer.writerow(_row_for_csv(r, scanned_at))
    console.print(f"\n[green]✓ Wrote {len(results)} rows to {csv_path}[/green]")

    json_dir_path = Path(json_dir)
    json_dir_path.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "generated_at": scanned_at,
        "thresholds": cfg["thresholds"],
        "standards": cfg["standards"],
        "summary": summary,
        "repos": results,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_path = json_dir_path / f"{stamp}.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2))
    latest_path = json_dir_path / "latest.json"
    shutil.copyfile(snapshot_path, latest_path)
    _update_index(json_dir_path)
    console.print(f"[green]✓ Wrote snapshot to {snapshot_path}[/green]")
    console.print(f"[green]✓ Updated {latest_path} and index.json[/green]")

    table = Table(title="Repo Health Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", justify="right")
    for key in [
        "total", "active", "archived", "errored",
        "non_standard_default_branch", "merge_commit_disabled",
        "repos_with_stale_prs", "total_stale_prs",
        "repos_with_stale_branches", "total_stale_branches",
        "healthy",
    ]:
        table.add_row(key, str(summary.get(key, 0)))
    console.print(table)
