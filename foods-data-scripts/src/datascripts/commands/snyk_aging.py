"""Snyk vulnerability aging analysis.

Fetches all Snyk projects and their vulnerabilities via the Snyk REST API,
then builds an aging view (open and closed/resolved vulnerabilities) at the
team level, evaluating each issue against severity-based SLA targets.

Workflow: list orgs -> list projects per org -> list issues per org ->
enrich with team/repo -> compute age + SLA status -> export CSV.

SLA targets (days to remediate from introduction):
    Critical : 7
    High     : 14
    Medium   : 30
    Low      : 30

Requires:
    SNYK_TOKEN env var (Snyk API token). Passed as an environment variable.

Output:
    outputs/csv/snyk_vulnerability_aging.csv
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import click
import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..services.csv_helper import write_csv
from ..services.token_manager import TokenManager

console = Console()

OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"

if not OUTPUTS_DIR.exists():
    OUTPUTS_DIR = Path.cwd() / "outputs"

DEFAULT_OUTPUT = OUTPUTS_DIR / "csv" / "snyk_vulnerability_aging.csv"

# Snyk REST API version (date-based). Update as needed.
API_VERSION = "2024-10-15"
DEFAULT_BASE_URL = "https://api.snyk.io"

# Severity -> SLA target in days.
SLA_DAYS = {
    "critical": 7,
    "high": 14,
    "medium": 30,
    "low": 30,
}

# Aging buckets (upper bound in days, label).
AGING_BUCKETS = [
    (7, "0-7 days"),
    (14, "8-14 days"),
    (30, "15-30 days"),
    (60, "31-60 days"),
    (90, "61-90 days"),
    (float("inf"), "90+ days"),
]

FIELDNAMES = [
    "org_id", "team", "project_id", "project_name", "repo",
    "issue_id", "issue_type",
    "severity", "status", "created_at", "resolved_at",
    "age_days", "sla_days", "sla_status", "days_over_sla", "aging_bucket",
    "is_fixable", "issue_url",
]


class SnykClient:
    """Thin wrapper around the Snyk REST API with pagination support."""

    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL,
                 verify_ssl: bool = True, verbose: bool = False):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.api+json",
        })

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET a REST endpoint. ``path`` may be a full path or relative link."""
        if path.startswith("http"):
            url = path
            params = None  # next-links already carry query params
        else:
            url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(
                url, params=params, timeout=60, verify=self.verify_ssl
            )
            if resp.status_code != 200:
                if self.verbose:
                    console.print(
                        f"[dim]Snyk API {resp.status_code}: {resp.text[:200]}[/dim]"
                    )
                return None
            return resp.json()
        except requests.RequestException as exc:
            if self.verbose:
                console.print(f"[dim]Snyk API error: {exc}[/dim]")
            return None

    def _paginate(self, path: str, params: dict) -> List[dict]:
        """Follow REST ``links.next`` pagination and collect all ``data`` items."""
        items: List[dict] = []
        page = self._get(path, params)
        while page:
            items.extend(page.get("data", []))
            next_link = (page.get("links") or {}).get("next")
            if not next_link:
                break
            page = self._get(next_link)
        return items

    def list_orgs(self) -> List[dict]:
        """List all organisations the token can access."""
        return self._paginate(
            "/rest/orgs", {"version": API_VERSION, "limit": 100}
        )

    def list_projects(self, org_id: str) -> List[dict]:
        """List all projects in an organisation."""
        return self._paginate(
            f"/rest/orgs/{org_id}/projects",
            {"version": API_VERSION, "limit": 100},
        )

    def list_issues(self, org_id: str) -> List[dict]:
        """List all issues in an organisation (open and resolved)."""
        return self._paginate(
            f"/rest/orgs/{org_id}/issues",
            {"version": API_VERSION, "limit": 100},
        )

    def list_project_issues(self, org_id: str, project_id: str) -> List[dict]:
        """List all issues (open and resolved) for a single project.

        Filters the org issues endpoint by ``scan_item`` so we can iterate
        project by project.
        """
        return self._paginate(
            f"/rest/orgs/{org_id}/issues",
            {
                "version": API_VERSION,
                "limit": 100,
                "scan_item.id": project_id,
                "scan_item.type": "project",
            },
        )


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO8601 timestamp (with optional trailing Z) to aware datetime."""
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _aging_bucket(age_days: int) -> str:
    for upper, label in AGING_BUCKETS:
        if age_days <= upper:
            return label
    return AGING_BUCKETS[-1][1]


def _extract_repo(project_name: str) -> str:
    """Derive a repo name from a Snyk project name.

    Snyk project names commonly look like ``owner/repo:path/to/manifest`` or
    ``owner/repo(branch)``. Strip the manifest path and branch, keep repo.
    """
    if not project_name:
        return ""
    name = project_name.split(":", 1)[0]
    name = name.split("(", 1)[0]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.strip()


def _is_fixable(attrs: dict) -> str:
    """Return "Yes"/"No" indicating whether the first coordinate is fixable.

    Only the fixability flag is read - no package name/version or CVE/CWE data
    is extracted, to avoid persisting vulnerability details to disk.
    """
    coords = attrs.get("coordinates") or []
    if not coords:
        return ""
    first = coords[0]
    fixable = (
        first.get("is_fixable_snyk")
        or first.get("is_fixable_upstream")
        or first.get("is_upgradeable")
    )
    return "Yes" if fixable else "No"


def _extract_resolved_at(attrs: dict) -> str:
    """Return the resolution timestamp for a resolved issue.

    Snyk exposes this in two places:
    1. ``attributes.resolution.resolved_at`` - the authoritative issue-level
       resolution date (present even when coordinates still read ``open``).
    2. ``coordinates[].last_resolved_at`` - per-project resolution timestamps.

    Prefer the issue-level ``resolution.resolved_at``; fall back to the latest
    coordinate timestamp.
    """
    resolution = attrs.get("resolution") or {}
    if resolution.get("resolved_at"):
        return resolution["resolved_at"]
    resolved_dates = [
        c.get("last_resolved_at")
        for c in (attrs.get("coordinates") or [])
        if c.get("last_resolved_at")
    ]
    return max(resolved_dates) if resolved_dates else ""


def _compute_age(attrs: dict, status: str, resolved_at: str, now: datetime) -> int:
    """Age in days: open -> now; resolved -> resolution time."""
    created = _parse_dt(attrs.get("created_at"))
    if not created:
        return 0
    resolved = _parse_dt(resolved_at)
    end = resolved if (status == "resolved" and resolved) else now
    return max((end - created).days, 0)


def _build_row(
    issue: dict,
    org_id: str,
    org_name: str,
    org_slug: str,
    project: dict,
    now: datetime,
) -> dict:
    """Build a single aging row from one Snyk issue within a project.

    Deliberately excludes vulnerability details (title, package, CVE/CWE) from
    the output. Instead it stores a deep link to the issue in the Snyk UI, so
    sensitive security data is never persisted to CSV/Git.
    """
    attrs = issue.get("attributes") or {}

    project_id = project.get("id", "")
    project_name = (project.get("attributes") or {}).get("name", "")

    repo = _extract_repo(project_name)
    # Group by the Snyk organisation (team), straight from Snyk data.
    team = org_name

    severity = (attrs.get("effective_severity_level") or "").lower()
    coords = attrs.get("coordinates") or []
    coord_state = (coords[0].get("state") if coords else "") or ""
    status = (attrs.get("status") or coord_state).lower()  # open | resolved
    resolved_at = _extract_resolved_at(attrs)
    age_days = _compute_age(attrs, status, resolved_at, now)

    sla_days = SLA_DAYS.get(severity, 30)
    days_over = age_days - sla_days
    sla_status = "Breached" if days_over > 0 else "Within SLA"

    is_fixable = _is_fixable(attrs)

    # Deep link to the specific issue on the project page in the Snyk UI.
    slug = org_slug or org_id
    issue_key = attrs.get("key", "")
    issue_url = f"https://app.snyk.io/org/{slug}/project/{project_id}"
    if issue_key:
        issue_url += f"#issue-{issue_key}"

    return {
        "org_id": org_id,
        "team": team,
        "project_id": project_id,
        "project_name": project_name,
        "repo": repo,
        "issue_id": issue.get("id", ""),
        "issue_type": attrs.get("type", ""),
        "severity": severity,
        "status": status,
        "created_at": attrs.get("created_at", ""),
        "resolved_at": resolved_at,
        "age_days": age_days,
        "sla_days": sla_days,
        "sla_status": sla_status,
        "days_over_sla": days_over if days_over > 0 else 0,
        "aging_bucket": _aging_bucket(age_days),
        "is_fixable": is_fixable,
        "issue_url": issue_url,
    }


def _build_project_rows(
    org_id: str,
    org_name: str,
    org_slug: str,
    project: dict,
    issues: List[dict],
    now: datetime,
) -> List[dict]:
    """Build aging rows for all issues belonging to a single project."""
    return [
        _build_row(issue, org_id, org_name, org_slug, project, now)
        for issue in issues
    ]


def _print_summary(rows: List[dict]) -> None:
    """Print a team-level breach summary table to the console."""
    if not rows:
        console.print("[yellow]No vulnerabilities found.[/yellow]")
        return

    table = Table(title="Snyk Vulnerability Aging - Team Summary")
    table.add_column("Team", style="cyan", no_wrap=False)
    table.add_column("Open", justify="right")
    table.add_column("Resolved", justify="right")
    table.add_column("Breached (open)", justify="right", style="red")
    table.add_column("Critical", justify="right")
    table.add_column("High", justify="right")

    teams: Dict[str, Dict[str, int]] = {}
    for r in rows:
        t = teams.setdefault(r["team"], {
            "open": 0, "resolved": 0, "breached": 0, "critical": 0, "high": 0,
        })
        if r["status"] == "open":
            t["open"] += 1
            if r["sla_status"] == "Breached":
                t["breached"] += 1
        elif r["status"] == "resolved":
            t["resolved"] += 1
        if r["severity"] == "critical":
            t["critical"] += 1
        elif r["severity"] == "high":
            t["high"] += 1

    for team in sorted(teams, key=lambda k: teams[k]["breached"], reverse=True):
        s = teams[team]
        table.add_row(
            team, str(s["open"]), str(s["resolved"]),
            str(s["breached"]), str(s["critical"]), str(s["high"]),
        )
    console.print(table)


@click.command()
@click.option("--token", help="Snyk API token (prefer SNYK_TOKEN env var).")
@click.option("--reset-token", is_flag=True, help="Clear cached Snyk token.")
@click.option("--org", "org_filter", multiple=True,
              help="Limit to specific Snyk org ID(s). Repeat for multiple.")
@click.option("--base-url", default=DEFAULT_BASE_URL, show_default=True,
              envvar="SNYK_API_URL", help="Snyk API base URL.")
@click.option("--output", "output_path", default=str(DEFAULT_OUTPUT),
              show_default=True, help="Output CSV path.")
@click.option("--no-verify-ssl", is_flag=True,
              help="Disable SSL verification (for corporate proxies).")
@click.option("--verbose", is_flag=True, help="Show detailed progress.")
def snyk_aging_command(token, reset_token, org_filter, base_url,
                       output_path, no_verify_ssl, verbose):
    """Build a Snyk vulnerability aging view at team level.

    Fetches all Snyk projects and vulnerabilities (open and resolved), computes
    each issue's age against severity-based SLA targets, and exports the result
    to outputs/csv/snyk_vulnerability_aging.csv for the dashboard.

    \b
    SLA targets: Critical=7d, High=14d, Medium=30d, Low=30d.

    \b
    Environment Variables:
        SNYK_TOKEN    Snyk API token (recommended).
        SNYK_API_URL  Optional Snyk API base URL override.

    \b
    Examples:
        # All orgs
        data-scripts snyk aging

        # Single org
        data-scripts snyk aging --org 4a18d42f-...
    """
    if no_verify_ssl:
        import warnings
        import urllib3
        warnings.filterwarnings(
            "ignore", category=urllib3.exceptions.InsecureRequestWarning
        )

    token_manager = TokenManager("snyk_token.cache")
    resolved = token_manager.get_token(
        cli_token=token,
        env_vars=["SNYK_TOKEN", "SNYK_API_TOKEN"],
        reset=reset_token,
        prompt_text="Snyk API token (will be cached)",
        verbose=verbose,
    )
    if not resolved:
        raise click.ClickException(
            "No Snyk token found. Set the SNYK_TOKEN environment variable."
        )

    client = SnykClient(
        resolved, base_url=base_url,
        verify_ssl=not no_verify_ssl, verbose=verbose,
    )
    now = datetime.now(timezone.utc)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Fetching Snyk organisations...", total=None)
        orgs = client.list_orgs()
        if org_filter:
            wanted = set(org_filter)
            orgs = [o for o in orgs if o.get("id") in wanted]
        if not orgs:
            raise click.ClickException(
                "No Snyk organisations accessible with this token."
            )
        progress.update(task, description=f"Found {len(orgs)} org(s).")

        all_rows: List[dict] = []
        for org in orgs:
            org_id = org.get("id", "")
            org_attrs = org.get("attributes") or {}
            org_name = org_attrs.get("name") or org_id
            org_slug = org_attrs.get("slug") or ""
            progress.update(task, description=f"[{org_name}] fetching projects...")
            projects = client.list_projects(org_id)

            # Iterate the Snyk projects one by one, pulling each project's
            # issues and building its aging rows.
            for idx, project in enumerate(projects, start=1):
                project_id = project.get("id", "")
                project_name = (project.get("attributes") or {}).get("name", "")
                progress.update(
                    task,
                    description=(
                        f"[{org_name}] project {idx}/{len(projects)}: "
                        f"{project_name}"
                    ),
                )
                issues = client.list_project_issues(org_id, project_id)
                rows = _build_project_rows(
                    org_id, org_name, org_slug, project, issues, now
                )
                all_rows.extend(rows)
                if verbose:
                    console.print(
                        f"[dim]  {project_name}: {len(issues)} issues[/dim]"
                    )
            progress.advance(task)

    write_csv(all_rows, FIELDNAMES, output_path, label="vulnerabilities")
    _print_summary(all_rows)
    console.print("\n[bold green]Done.[/bold green]")
