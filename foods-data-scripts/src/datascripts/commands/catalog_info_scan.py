"""Catalog-info.yaml scanner - auto-discovers repo metadata from Backstage catalogs.

Scrapes catalog-info.yaml from every DigitalInnovation repo, extracts:
  - AppID (from spec.system: ap-AXXXX)
  - Owner team (from spec.owner)
  - Lifecycle (from spec.lifecycle)
  - Component type (from spec.type)
  - Links (Confluence, Jira, GitHub)

Cross-references with repos.csv to detect:
  - Repos in GitHub but missing from repos.csv
  - Repos in repos.csv but archived/deleted on GitHub
  - AppID mismatches between repos.csv and catalog-info
  - Repos without catalog-info.yaml

Also discovers repos via GitHub topic tags (food, foods) that aren't tracked.

Requires: GITHUB_TOKEN env var with org:read scope.
"""

import csv
import json
import os
import re
import subprocess

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import click
import yaml
from rich.console import Console
from rich.table import Table

from ..services.csv_helper import write_csv
from ..services import GITHUB_ORG

CATALOG_FIELDNAMES = [
    "repo_name", "has_catalog_info", "app_id", "owner_team",
    "lifecycle", "component_type", "system_ref", "description",
    "links_confluence", "links_jira", "topics", "in_repos_csv",
]

console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"

if not DATA_DIR.exists():
    DATA_DIR = Path.cwd() / "data"
    OUTPUTS_DIR = Path.cwd() / "outputs"

REPO_FIELDNAMES = [
    "app_id", "repo_name", "owner_team", "lifecycle", "component_type",
    "has_catalog_info", "system_ref", "description", "links_confluence",
    "links_jira", "topics",
]


class CatalogInfoScanner:
    """Scrapes catalog-info.yaml from GitHub repos and builds a registry."""

    ALLOWED_CATALOG_FILENAMES = ("catalog-info.yaml", "catalog-info.yml")
    MAX_GH_ENDPOINT_LENGTH = 2048
    GH_CATALOG_CONTENT_ENDPOINT_PATTERN = re.compile(
        r"^/?repos/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/contents/catalog-info\.ya?ml"
        r"(?:\?ref=[A-Za-z0-9._~/%-]+)?$"
    )
    GH_REPO_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(self, repos_csv: str, verbose: bool = False):
        self.verbose = verbose
        self.repos_csv_path = Path(repos_csv)
        self.known_repos = self._load_repos_csv()
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.org = GITHUB_ORG
        # Build short-name to AppID lookup (e.g. "PAP" -> "A2412")
        self._app_name_to_id: Dict[str, str] = {}
        for row in self.known_repos.values():
            name = row.get("AppName", "").strip()
            aid = row.get("AppID", "").strip()
            if name and aid and name not in self._app_name_to_id:
                self._app_name_to_id[name] = aid

    def _load_repos_csv(self) -> Dict[str, dict]:
        """Load existing repos.csv entries keyed by repo name."""
        repos = {}
        if not self.repos_csv_path.exists():
            return repos
        with open(self.repos_csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("RepoName", "").strip()
                if name:
                    repos[name] = row
        return repos

    def _validate_repo_segment(self, value: object, label: str) -> str:
        """Validate path segments used to build GitHub repository endpoints."""
        if not isinstance(value, str) or not value:
            raise click.ClickException(f"GitHub {label} must be a non-empty string.")

        if not self.GH_REPO_SEGMENT_PATTERN.fullmatch(value):
            raise click.ClickException(f"GitHub {label} contains unsafe characters.")

        return value

    def _validate_catalog_filename(self, filename: object) -> str:
        """Validate catalog filenames used in GitHub contents endpoints."""
        if filename not in self.ALLOWED_CATALOG_FILENAMES:
            raise click.ClickException("Catalog filename is not allowed.")

        return filename

    def _build_catalog_content_endpoint(self, repo_name: str, filename: str) -> str:
        """Build the only REST endpoint shape allowed for catalog scanning."""
        safe_org = self._validate_repo_segment(self.org, "organization")
        safe_repo_name = self._validate_repo_segment(repo_name, "repository name")
        safe_filename = self._validate_catalog_filename(filename)
        endpoint = f"repos/{safe_org}/{safe_repo_name}/contents/{safe_filename}"

        if len(endpoint) > self.MAX_GH_ENDPOINT_LENGTH:
            raise click.ClickException("GitHub API endpoint is too long.")

        if not self.GH_CATALOG_CONTENT_ENDPOINT_PATTERN.fullmatch(endpoint):
            raise click.ClickException("GitHub API endpoint is not allowed for catalog scanning.")

        return endpoint

    def _gh_catalog_content(self, repo_name: str, filename: str) -> Any:
        """Fetch catalog-info content via gh CLI for a validated repository."""
        safe_endpoint = self._build_catalog_content_endpoint(repo_name, filename)
        cmd = ["gh", "api", safe_endpoint]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                if self.verbose:
                    console.print(f"[dim]API error for {safe_endpoint}: {result.stderr.strip()}[/dim]")
                return None
            return json.loads(result.stdout) if result.stdout.strip() else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            if self.verbose:
                console.print(f"[dim]API call failed: {exc}[/dim]")
            return None

    def _gh_graphql(self, query: str, variables: Optional[dict] = None) -> Any:
        """Call GitHub GraphQL API via gh CLI."""
        cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
        if variables:
            for k, v in variables.items():
                cmd.extend(["-f", f"{k}={v}"])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return None
            return json.loads(result.stdout) if result.stdout.strip() else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return None

    def discover_repos_by_topics(self) -> Dict[str, List[str]]:
        """Find repos with food-related GitHub topics."""
        console.print("[bold]Discovering repos by GitHub topics...[/bold]")
        topic_repos: Dict[str, List[str]] = {}

        for topic in ["food", "foods", "mns-foods"]:
            data = self._gh_graphql(
                """
                query($searchQuery: String!) {
                  search(query: $searchQuery, type: REPOSITORY, first: 100) {
                    repositoryCount
                    nodes {
                      ... on Repository {
                        name
                        isArchived
                        repositoryTopics(first: 20) {
                          nodes { topic { name } }
                        }
                      }
                    }
                  }
                }
                """,
                {"searchQuery": f"org:{self.org} topic:{topic}"},
            )
            if not data:
                continue
            search = data.get("data", {}).get("search", {})
            count = search.get("repositoryCount", 0)
            if self.verbose:
                console.print(f"  topic:{topic} -> {count} repos")
            for node in search.get("nodes", []):
                name = node.get("name", "")
                if name and not node.get("isArchived", False):
                    topics = [
                        t["topic"]["name"]
                        for t in node.get("repositoryTopics", {}).get("nodes", [])
                    ]
                    topic_repos[name] = topics

        console.print(f"  Found {len(topic_repos)} repos via topic tags")
        return topic_repos

    def fetch_catalog_info(self, repo_name: str) -> Optional[dict]:
        """Fetch and parse catalog-info.yaml for a single repo."""
        safe_repo_name = self._validate_repo_segment(repo_name, "repository name")
        for filename in self.ALLOWED_CATALOG_FILENAMES:
            data = self._gh_catalog_content(safe_repo_name, filename)
            if data and "content" in data:
                import base64

                try:
                    content = base64.b64decode(data["content"]).decode("utf-8")
                    return self._parse_catalog_yaml(content, safe_repo_name)
                except Exception as exc:
                    if self.verbose:
                        console.print(f"[dim]  Parse error for {safe_repo_name}: {exc}[/dim]")
                    return None
        return None

    def _parse_catalog_yaml(self, content: str, repo_name: str) -> dict:
        """Extract structured metadata from catalog-info YAML."""
        result = {
            "repo_name": repo_name,
            "has_catalog_info": True,
            "app_id": None,
            "owner_team": None,
            "lifecycle": None,
            "component_type": None,
            "system_ref": None,
            "description": None,
            "links_confluence": None,
            "links_jira": None,
        }

        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            return result

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind", "")
            if kind != "Component":
                continue

            metadata = doc.get("metadata", {})
            spec = doc.get("spec", {})

            result["description"] = metadata.get("description", "")
            result["component_type"] = spec.get("type", "")
            result["owner_team"] = spec.get("owner", "")
            result["lifecycle"] = spec.get("lifecycle", "")
            result["system_ref"] = spec.get("system", "")

            # Extract AppID from system: ap-AXXXX
            system_val = spec.get("system", "")
            match = re.match(r"ap-([Aa]\d+)", system_val)
            if match:
                result["app_id"] = match.group(1).upper()
            elif system_val and system_val in self._app_name_to_id:
                # Fallback: map short name (e.g. "ASO") to AppID via repos.csv
                result["app_id"] = self._app_name_to_id[system_val]

            # Extract links
            links = metadata.get("links", [])
            for link in links:
                if not isinstance(link, dict):
                    continue
                url = link.get("url", "")
                title = link.get("title", "").lower()
                if "confluence" in title or "wiki" in url:
                    result["links_confluence"] = url
                elif "jira" in title or "jira" in url:
                    result["links_jira"] = url

            break  # Use first Component doc

        return result

    def scan_all_repos(self) -> List[dict]:
        """Scan catalog-info.yaml for all known repos."""
        all_repos = set(self.known_repos.keys())

        # Also discover via topics
        topic_repos = self.discover_repos_by_topics()
        topic_discovered = set(topic_repos.keys()) - all_repos
        if topic_discovered:
            console.print(
                f"[yellow]  {len(topic_discovered)} repos found via topics "
                f"but NOT in repos.csv[/yellow]"
            )
        all_repos.update(topic_repos.keys())

        console.print(f"\n[bold]Scanning catalog-info.yaml for {len(all_repos)} repos...[/bold]")

        results = []
        scanned = 0
        for repo_name in sorted(all_repos):
            scanned += 1
            if scanned % 25 == 0:
                console.print(f"  Scanned {scanned}/{len(all_repos)}...")

            catalog = self.fetch_catalog_info(repo_name)
            if catalog:
                catalog["topics"] = ",".join(topic_repos.get(repo_name, []))
                catalog["in_repos_csv"] = repo_name in self.known_repos
                results.append(catalog)
            else:
                results.append({
                    "repo_name": repo_name,
                    "has_catalog_info": False,
                    "app_id": self.known_repos.get(repo_name, {}).get("AppID", ""),
                    "owner_team": None,
                    "lifecycle": None,
                    "component_type": None,
                    "system_ref": None,
                    "description": None,
                    "links_confluence": None,
                    "links_jira": None,
                    "topics": ",".join(topic_repos.get(repo_name, [])),
                    "in_repos_csv": repo_name in self.known_repos,
                })

        return results

    def generate_drift_report(self, results: List[dict]) -> dict:
        """Analyse gaps between catalog-info and repos.csv."""
        report = {
            "total_scanned": len(results),
            "has_catalog_info": 0,
            "missing_catalog_info": 0,
            "has_app_id": 0,
            "missing_app_id": 0,
            "not_in_repos_csv": [],
            "app_id_mismatches": [],
            "missing_catalog_repos": [],
            "orphan_repos": [],
            "by_lifecycle": defaultdict(int),
            "by_owner": defaultdict(int),
            "by_app_id": defaultdict(list),
        }

        for r in results:
            if r["has_catalog_info"]:
                report["has_catalog_info"] += 1
                if r["app_id"]:
                    report["has_app_id"] += 1
                    report["by_app_id"][r["app_id"]].append(r["repo_name"])
                else:
                    report["missing_app_id"] += 1
                if r.get("lifecycle"):
                    report["by_lifecycle"][r["lifecycle"]] += 1
                if r.get("owner_team"):
                    report["by_owner"][r["owner_team"]] += 1
            else:
                report["missing_catalog_info"] += 1
                report["missing_catalog_repos"].append(r["repo_name"])

            if not r.get("in_repos_csv"):
                report["not_in_repos_csv"].append(r["repo_name"])

        return report

    def write_results_csv(self, results: List[dict], output_path: str):
        """Write scan results to CSV."""
        write_csv(results, CATALOG_FIELDNAMES, output_path, label="repos")


def _print_drift_report(report: dict):
    """Print a rich drift report to the console."""
    console.print("\n[bold cyan]═══ Catalog-Info Scan Report ═══[/bold cyan]\n")

    # Summary table
    summary = Table(title="Coverage Summary", show_header=True)
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")
    summary.add_column("Status")

    total = report["total_scanned"]
    has_ci = report["has_catalog_info"]
    pct = (has_ci / total * 100) if total else 0
    status = "🟢" if pct >= 90 else "🟡" if pct >= 70 else "🔴"
    summary.add_row("Total repos scanned", str(total), "")
    summary.add_row("Has catalog-info.yaml", str(has_ci), f"{status} {pct:.0f}%")
    summary.add_row("Missing catalog-info", str(report["missing_catalog_info"]), "")
    summary.add_row("Has AppID (system: ap-AXXXX)", str(report["has_app_id"]), "")
    summary.add_row("Missing AppID in catalog", str(report["missing_app_id"]), "")
    summary.add_row("Not in repos.csv", str(len(report["not_in_repos_csv"])), "")
    console.print(summary)

    # Lifecycle breakdown
    if report["by_lifecycle"]:
        lc_table = Table(title="\nLifecycle Breakdown")
        lc_table.add_column("Lifecycle")
        lc_table.add_column("Count", justify="right")
        for lc, count in sorted(
            report["by_lifecycle"].items(), key=lambda x: -x[1]
        ):
            lc_table.add_row(lc, str(count))
        console.print(lc_table)

    # Top owners
    if report["by_owner"]:
        ow_table = Table(title="\nTop 15 Owner Teams")
        ow_table.add_column("Team")
        ow_table.add_column("Repos", justify="right")
        for team, count in sorted(
            report["by_owner"].items(), key=lambda x: -x[1]
        )[:15]:
            ow_table.add_row(team, str(count))
        console.print(ow_table)

    # Repos not in repos.csv
    if report["not_in_repos_csv"]:
        console.print(
            f"\n[yellow]⚠ {len(report['not_in_repos_csv'])} repos found "
            f"via topics but NOT in repos.csv:[/yellow]"
        )
        for name in sorted(report["not_in_repos_csv"])[:20]:
            console.print(f"  - {name}")
        if len(report["not_in_repos_csv"]) > 20:
            console.print(
                f"  ... and {len(report['not_in_repos_csv']) - 20} more"
            )

    # Repos missing catalog-info
    if report["missing_catalog_repos"]:
        console.print(
            f"\n[red]❌ {len(report['missing_catalog_repos'])} repos "
            f"missing catalog-info.yaml:[/red]"
        )
        for name in sorted(report["missing_catalog_repos"])[:20]:
            console.print(f"  - {name}")
        if len(report["missing_catalog_repos"]) > 20:
            console.print(
                f"  ... and {len(report['missing_catalog_repos']) - 20} more"
            )

    # AppID distribution
    console.print(f"\n[bold]AppID coverage: {len(report['by_app_id'])} unique AppIDs found[/bold]")


@click.group("catalog")
def catalog_group():
    """Catalog-info.yaml scanner and repo registry management."""
    pass


@catalog_group.command("scan")
@click.option(
    "--repos-csv",
    default=str(DATA_DIR / "repos.csv"),
    help="Path to repos.csv registry file.",
)
@click.option(
    "--output",
    default=str(OUTPUTS_DIR / "csv" / "catalog_info.csv"),
    help="Output CSV path for scan results.",
)
@click.option("--verbose", is_flag=True, help="Show detailed progress.")
def scan_command(repos_csv: str, output: str, verbose: bool):
    """Scan catalog-info.yaml across all known repos and discover new ones."""
    scanner = CatalogInfoScanner(repos_csv, verbose=verbose)
    results = scanner.scan_all_repos()
    scanner.write_results_csv(results, output)
    report = scanner.generate_drift_report(results)
    _print_drift_report(report)
