"""GitHub Actions Health Scanner - CI health metrics across all Foods repos.

Queries the GitHub Actions API for every repo in repos.csv, calculates
workflow success rates, average duration, failure counts, and last run
status over the past 30 days.

Gives every Foods team visibility into their CI pipeline health, and feeds
the estate dashboard with a CI health dimension.

Requires: GITHUB_TOKEN env var (the existing PAT_GITHUB_TOKEN in CI).
"""

import csv
import json
import os
import re
import subprocess

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.table import Table

from ..services.csv_helper import write_csv
from ..services import GITHUB_ORG

console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"

if not DATA_DIR.exists():
    DATA_DIR = Path.cwd() / "data"
    OUTPUTS_DIR = Path.cwd() / "outputs"

FIELDNAMES = [
    "repo_name", "app_id", "app_name", "team",
    "workflow_count", "total_runs", "success_count", "failure_count",
    "success_rate_pct", "avg_duration_seconds", "last_run_status",
    "last_run_at", "longest_workflow", "most_failing_workflow",
]


class ActionsHealthScanner:
    """Scans GitHub Actions health metrics across all repos."""

    ORG = GITHUB_ORG
    MAX_GH_ENDPOINT_LENGTH = 2048
    GH_REPO_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
    GH_WORKFLOW_RUNS_ENDPOINT_PATTERN = re.compile(
        r"^repos/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/actions/runs"
        r"\?per_page=100&created=>=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
    )

    def __init__(self, repos_csv: str, days: int = 30, verbose: bool = False):
        self.verbose = verbose
        self.days = days
        self.repos = self._load_repos(repos_csv)
        self.cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def _load_repos(self, path: str) -> List[dict]:
        """Load repos from repos.csv."""
        repos = []
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("RepoName", "").strip()
                status = row.get("RepoStatus", "").strip()
                if name and status != "Archived":
                    repos.append(row)
        if self.verbose:
            console.print(f"[dim]Loaded {len(repos)} active repos from repos.csv[/dim]")
        return repos

    def _validate_repo_segment(self, value: object, label: str) -> str:
        """Validate GitHub organization/repository path segments."""
        if not isinstance(value, str) or not value:
            raise click.ClickException(f"GitHub {label} must be a non-empty string.")

        if not self.GH_REPO_SEGMENT_PATTERN.fullmatch(value):
            raise click.ClickException(f"GitHub {label} contains unsafe characters.")

        return value

    def _validate_since_timestamp(self, since: object) -> str:
        """Validate the generated GitHub Actions created filter timestamp."""
        if not isinstance(since, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", since
        ):
            raise click.ClickException("GitHub Actions timestamp filter is invalid.")

        return since

    def _build_workflow_runs_endpoint(self, repo_name: str, since: str) -> str:
        """Build the only GitHub API endpoint used for Actions health scanning."""
        safe_org = self._validate_repo_segment(self.ORG, "organization")
        safe_repo_name = self._validate_repo_segment(repo_name, "repository name")
        safe_since = self._validate_since_timestamp(since)
        endpoint = (
            f"repos/{safe_org}/{safe_repo_name}/actions/runs"
            f"?per_page=100&created=>={safe_since}"
        )

        if len(endpoint) > self.MAX_GH_ENDPOINT_LENGTH:
            raise click.ClickException("GitHub API endpoint is too long.")

        if not self.GH_WORKFLOW_RUNS_ENDPOINT_PATTERN.fullmatch(endpoint):
            raise click.ClickException("GitHub API endpoint is not allowed for Actions health scanning.")

        return endpoint

    def _gh_workflow_runs(self, repo_name: str, since: str) -> Optional[Any]:
        """Call the GitHub Actions workflow runs API via gh CLI."""
        safe_endpoint = self._build_workflow_runs_endpoint(repo_name, since)
        cmd = ["gh", "api", safe_endpoint, "--paginate"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                if self.verbose and "404" not in result.stderr:
                    console.print(f"[dim]API error: {result.stderr.strip()[:100]}[/dim]")
                return None

            return self._parse_gh_paginated_json(result.stdout)
        except subprocess.TimeoutExpired:
            if self.verbose:
                console.print("[dim]API call timed out[/dim]")
            return None

    def _parse_gh_paginated_json(self, output: str) -> Optional[Any]:
        """Parse single or newline-concatenated JSON output from gh --paginate."""
        text = output.strip()
        if not text:
            return None

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            parts = []
            for chunk in text.split("\n"):
                parsed = self._parse_json_chunk(chunk)
                if isinstance(parsed, list):
                    parts.extend(parsed)
                elif parsed is not None:
                    parts.append(parsed)
            return parts if parts else None

    def _parse_json_chunk(self, chunk: str) -> Optional[Any]:
        """Parse one JSON chunk, returning None for blank or invalid chunks."""
        text = chunk.strip()
        if not text:
            return None

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _fetch_workflow_runs(self, repo_name: str) -> List[dict]:
        """Fetch recent workflow runs for a repo."""
        # Use created filter to limit to recent runs
        since = self.cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._gh_workflow_runs(repo_name, since)
        if data is None:
            return []

        # Handle both paginated (list) and single response (dict with workflow_runs)
        if isinstance(data, dict):
            return data.get("workflow_runs", [])
        elif isinstance(data, list):
            # Paginated results may be raw run objects or dicts with workflow_runs
            runs = []
            for item in data:
                if isinstance(item, dict) and "workflow_runs" in item:
                    runs.extend(item["workflow_runs"])
                elif isinstance(item, dict) and "id" in item:
                    runs.append(item)
            return runs
        return []

    def scan_repo(self, repo: dict) -> dict:
        """Scan a single repo's Actions health."""
        repo_name = repo.get("RepoName", "").strip()
        app_id = repo.get("AppID", "").strip()
        app_name = repo.get("AppName", "").strip()
        team = repo.get("Owner", "").strip()

        result = {
            "repo_name": repo_name,
            "app_id": app_id,
            "app_name": app_name,
            "team": team,
            "workflow_count": 0,
            "total_runs": 0,
            "success_count": 0,
            "failure_count": 0,
            "success_rate_pct": 0.0,
            "avg_duration_seconds": 0,
            "last_run_status": "",
            "last_run_at": "",
            "longest_workflow": "",
            "most_failing_workflow": "",
        }

        runs = self._fetch_workflow_runs(repo_name)
        if not runs:
            return result

        # Filter to completed runs within the time window
        valid_runs = []
        for run in runs:
            created = run.get("created_at", "")
            if not created:
                continue
            try:
                run_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if run_dt >= self.cutoff:
                    valid_runs.append(run)
            except (ValueError, TypeError):
                continue

        if not valid_runs:
            return result

        # Calculate metrics
        workflows = set()
        successes = 0
        failures = 0
        durations = []
        workflow_failures: Dict[str, int] = {}
        workflow_durations: Dict[str, List[float]] = {}

        for run in valid_runs:
            wf_name = run.get("name", "unknown")
            workflows.add(wf_name)
            conclusion = run.get("conclusion", "")

            if conclusion == "success":
                successes += 1
            elif conclusion in ("failure", "timed_out"):
                failures += 1
                workflow_failures[wf_name] = workflow_failures.get(wf_name, 0) + 1

            # Calculate duration if both timestamps exist
            created = run.get("created_at", "")
            updated = run.get("updated_at", "")
            if created and updated and conclusion:
                try:
                    start = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    end = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    dur = (end - start).total_seconds()
                    if 0 < dur < 86400:  # Sanity: under 24 hours
                        durations.append(dur)
                        if wf_name not in workflow_durations:
                            workflow_durations[wf_name] = []
                        workflow_durations[wf_name].append(dur)
                except (ValueError, TypeError):
                    pass

        total = len(valid_runs)
        result["workflow_count"] = len(workflows)
        result["total_runs"] = total
        result["success_count"] = successes
        result["failure_count"] = failures
        result["success_rate_pct"] = round(successes / total * 100, 1) if total else 0.0
        result["avg_duration_seconds"] = round(sum(durations) / len(durations)) if durations else 0

        # Last run
        sorted_runs = sorted(valid_runs, key=lambda r: r.get("created_at", ""), reverse=True)
        if sorted_runs:
            last = sorted_runs[0]
            result["last_run_status"] = last.get("conclusion", last.get("status", ""))
            result["last_run_at"] = last.get("created_at", "")

        # Longest average workflow
        if workflow_durations:
            longest = max(workflow_durations.items(), key=lambda x: sum(x[1]) / len(x[1]))
            result["longest_workflow"] = longest[0]

        # Most failing workflow
        if workflow_failures:
            most_failing = max(workflow_failures.items(), key=lambda x: x[1])
            result["most_failing_workflow"] = most_failing[0]

        return result

    def scan(self) -> List[dict]:
        """Scan all repos for Actions health."""
        console.print(f"[bold]Scanning GitHub Actions health for {len(self.repos)} repos "
                      f"(last {self.days} days)...[/bold]")

        results = []
        for i, repo in enumerate(self.repos, 1):
            if i % 25 == 0:
                console.print(f"  Scanned {i}/{len(self.repos)}...")
            result = self.scan_repo(repo)
            results.append(result)

        console.print(f"[green]Scanned {len(results)} repos[/green]")
        return results

    def generate_csv(self, results: List[dict], output_path: str) -> None:
        """Write results to CSV."""
        write_csv(results, FIELDNAMES, output_path, label="repos")

    def print_summary(self, results: List[dict]) -> None:
        """Print a summary table."""
        active = [r for r in results if r["total_runs"] > 0]
        total_runs = sum(r["total_runs"] for r in active)
        total_success = sum(r["success_count"] for r in active)
        total_failures = sum(r["failure_count"] for r in active)
        overall_rate = round(total_success / total_runs * 100, 1) if total_runs else 0

        table = Table(title=f"GitHub Actions Health Summary (last {self.days} days)", show_lines=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right", style="green")

        table.add_row("Total repos scanned", str(len(results)))
        table.add_row("Repos with CI activity", str(len(active)))
        table.add_row("Repos with no CI runs", str(len(results) - len(active)))
        table.add_row("Total workflow runs", f"{total_runs:,}")
        table.add_row("Successful runs", f"{total_success:,}")
        table.add_row("Failed runs", f"[red]{total_failures:,}[/red]" if total_failures else "0")
        table.add_row("Overall success rate", f"{overall_rate}%")
        console.print(table)

        # Show worst repos
        failing = sorted(
            [r for r in active if r["failure_count"] > 0],
            key=lambda x: x["failure_count"],
            reverse=True,
        )[:10]

        if failing:
            console.print("\n[bold]Top 10 repos by failure count:[/bold]")
            fail_table = Table(show_lines=True)
            fail_table.add_column("Repo", style="cyan")
            fail_table.add_column("Failures", justify="right", style="red")
            fail_table.add_column("Success %", justify="right")
            fail_table.add_column("Most Failing Workflow")
            for r in failing:
                fail_table.add_row(
                    r["repo_name"],
                    str(r["failure_count"]),
                    f"{r['success_rate_pct']}%",
                    r["most_failing_workflow"],
                )
            console.print(fail_table)


@click.command("actions-health")
@click.option(
    "--repos-csv",
    default=str(DATA_DIR / "repos.csv"),
    show_default=True,
    help="Path to repos.csv registry file.",
)
@click.option(
    "--output",
    default=str(OUTPUTS_DIR / "csv" / "actions_health.csv"),
    show_default=True,
    help="Output CSV path for Actions health data.",
)
@click.option(
    "--days",
    default=30,
    show_default=True,
    help="Number of days to look back for workflow runs.",
)
@click.option("--verbose", is_flag=True, help="Show detailed progress.")
def actions_health_command(repos_csv: str, output: str, days: int, verbose: bool):
    """Scan GitHub Actions health across all Foods repos.

    Queries the GitHub Actions API for every repo in repos.csv,
    calculating CI health metrics over the specified time window.

    \\b
    Metrics per repo:
      - Workflow count, total runs, success/failure counts
      - Success rate (%), average run duration
      - Last run status and timestamp
      - Most failing and longest-running workflows

    \\b
    Examples:
        # Scan all repos (last 30 days)
        data-scripts github actions-health --verbose

        # Last 7 days only
        data-scripts github actions-health --days 7

    \\b
    Requirements:
        GITHUB_TOKEN env var (uses existing PAT_GITHUB_TOKEN in CI)
    """
    scanner = ActionsHealthScanner(repos_csv, days=days, verbose=verbose)
    results = scanner.scan()

    if not results:
        console.print("[red]No repos found. Check repos.csv path.[/red]")
        raise SystemExit(1)

    scanner.generate_csv(results, output)
    scanner.print_summary(results)
    console.print("\n[bold green]Done.[/bold green]")
