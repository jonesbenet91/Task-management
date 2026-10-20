"""Azure Resource Scan Command - auto-discovers Azure resources via Resource Graph.

Queries Azure Resource Graph for all resources tagged with EA_Application_ID,
cross-references with team_app_mapping.csv for enrichment, and reconciles
against the asset mappings baseline to detect drift.

This replaces the manual process of querying each subscription individually
and hand-editing the baseline CSV. One command, all subscriptions, all AppIDs.

Requires:
  - az CLI authenticated (user session or OIDC in CI)
  - resource-graph extension installed (az extension add --name resource-graph)
"""

import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import click
from rich.console import Console
from rich.table import Table

console = Console()

# Subdomain -> product_group mapping (derived from existing baseline data)
SUBDOMAIN_TO_PRODUCT_GROUP = {
    "Plan": "Food Plan",
    "Move": "Food Move",
    "Design, Buy, Make": "Design, Buy, Make",
}

BASELINE_FIELDNAMES = [
    "product_group", "team", "app_id", "app_name",
    "asset_type", "asset_id", "asset_name", "mapping_type", "confidence",
]


class AzureResourceScanner:
    """Scans Azure resources using Resource Graph and reconciles with asset mappings."""

    PAGE_SIZE = 1000
    MAX_SKIP_TOKEN_LENGTH = 8192
    SKIP_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=%-]+$")

    def __init__(self, team_mapping_path: str, verbose: bool = False):
        self.verbose = verbose
        self.team_mapping = self._load_team_mapping(team_mapping_path)

    def _load_team_mapping(self, path: str) -> Dict[str, dict]:
        """Load AppID -> team/product_group/app_name mapping from team_app_mapping.csv."""
        mapping = {}
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                app_id = row.get("AppID", "").strip()
                if not app_id:
                    continue

                subdomain = row.get("Subdomain", "").strip()
                product_group = SUBDOMAIN_TO_PRODUCT_GROUP.get(
                    subdomain, f"Food {subdomain}" if subdomain else "Unknown"
                )

                mapping[app_id] = {
                    "product_group": product_group,
                    "team": row.get("Team", "").strip(),
                    "app_name": row.get("Application", "").strip(),
                    "subdomain": subdomain,
                }
        return mapping

    def _check_az_cli(self) -> bool:
        """Verify az CLI is authenticated and resource-graph extension is available."""
        try:
            result = subprocess.run(
                ["az", "account", "show", "--query", "name", "-o", "tsv"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                console.print("[red]Not logged in to Azure CLI. Run 'az login' first.[/red]")
                return False

            if self.verbose:
                console.print(f"[dim]Azure account: {result.stdout.strip()}[/dim]")

            ext_check = subprocess.run(
                ["az", "extension", "show", "--name", "resource-graph", "-o", "json"],
                capture_output=True, text=True, timeout=10,
            )
            if ext_check.returncode != 0:
                console.print("[yellow]Installing resource-graph extension...[/yellow]")
                subprocess.run(
                    ["az", "extension", "add", "--name", "resource-graph", "--yes"],
                    capture_output=True, text=True, timeout=60,
                )
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            console.print("[red]Azure CLI not found or timed out.[/red]")
            return False

    def _validate_skip_token(self, skip_token: object) -> Optional[str]:
        """Return a safe Azure Resource Graph pagination token, or fail closed."""
        if skip_token in (None, ""):
            return None

        if not isinstance(skip_token, str):
            raise click.ClickException("Azure Resource Graph returned an invalid skip token.")

        if len(skip_token) > self.MAX_SKIP_TOKEN_LENGTH:
            raise click.ClickException("Azure Resource Graph returned an oversized skip token.")

        if not self.SKIP_TOKEN_PATTERN.fullmatch(skip_token):
            raise click.ClickException("Azure Resource Graph returned an unsafe skip token.")

        return skip_token

    def _query_resource_graph(
        self, app_ids: Optional[List[str]] = None
    ) -> List[dict]:
        """Query Azure Resource Graph for all tagged resources.

        Uses pagination to handle large result sets. Excludes
        NetworkWatcherRG resources (auto-managed, not app-owned).
        """
        if app_ids:
            id_list = ", ".join(f"'{a}'" for a in app_ids)
            query = f"""
Resources
| where isnotempty(tags['EA_Application_ID'])
| where tostring(tags['EA_Application_ID']) in ({id_list})
| where resourceGroup !contains 'NetworkWatcherRG'
| project id, name, type, resourceGroup, subscriptionId,
          appId=tostring(tags['EA_Application_ID']),
          location
| order by appId asc, name asc
"""
        else:
            query = """
Resources
| where isnotempty(tags['EA_Application_ID'])
| where resourceGroup !contains 'NetworkWatcherRG'
| project id, name, type, resourceGroup, subscriptionId,
          appId=tostring(tags['EA_Application_ID']),
          location
| order by appId asc, name asc
"""

        all_resources: List[dict] = []
        skip_token = None
        page = 0

        while True:
            page += 1
            cmd = [
                "az", "graph", "query",
                "-q", query.strip(),
                "--first", str(self.PAGE_SIZE),
                "-o", "json",
            ]
            if skip_token:
                cmd.extend(["--skip-token", skip_token])

            if self.verbose:
                console.print(f"[dim]  Resource Graph page {page}...[/dim]")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                console.print(f"[red]Resource Graph query failed:[/red] {result.stderr.strip()}")
                break

            data = json.loads(result.stdout)
            rows = data.get("data", [])
            all_resources.extend(rows)

            skip_token = self._validate_skip_token(data.get("skip_token"))
            total = data.get("total_records", len(all_resources))

            if self.verbose:
                console.print(f"[dim]  Fetched {len(all_resources)}/{total}[/dim]")

            if not skip_token or len(all_resources) >= total:
                break

        return all_resources

    def scan(self, app_ids: Optional[List[str]] = None) -> List[dict]:
        """Run full scan: query Resource Graph, enrich with team mapping, return CSV-ready rows."""
        if not self._check_az_cli():
            return []

        console.print("[bold]Querying Azure Resource Graph...[/bold]")
        raw_resources = self._query_resource_graph(app_ids)

        if not raw_resources:
            console.print("[yellow]No resources found.[/yellow]")
            return []

        console.print(f"[green]Found {len(raw_resources)} tagged resources[/green]")

        enriched = []
        unmapped_app_ids: Set[str] = set()

        for r in raw_resources:
            app_id = r.get("appId", "")
            mapping = self.team_mapping.get(app_id)

            if mapping:
                product_group = mapping["product_group"]
                team = mapping["team"]
                app_name = mapping["app_name"]
            else:
                unmapped_app_ids.add(app_id)
                product_group = "Unknown"
                team = "Unknown"
                app_name = app_id

            enriched.append({
                "product_group": product_group,
                "team": team,
                "app_id": app_id,
                "app_name": app_name,
                "asset_type": "AzureResource",
                "asset_id": r["id"],
                "asset_name": r["name"],
                "mapping_type": "resource_graph",
                "confidence": 1,
            })

        if unmapped_app_ids:
            console.print(
                f"\n[yellow]Warning: {len(unmapped_app_ids)} AppIDs not in "
                f"team_app_mapping.csv:[/yellow]"
            )
            for aid in sorted(unmapped_app_ids):
                count = sum(1 for e in enriched if e["app_id"] == aid)
                console.print(f"  {aid}: {count} resources")

        return enriched

    def generate_csv(self, resources: List[dict], output_path: str) -> None:
        """Write scanned resources to a standalone CSV."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=BASELINE_FIELDNAMES, lineterminator="\r\n"
            )
            writer.writeheader()
            for row in resources:
                writer.writerow(row)

        console.print(f"[green]Wrote {len(resources)} rows to {output_path}[/green]")

    def reconcile(self, resources: List[dict], baseline_path: str) -> dict:
        """Compare scanned resources against existing baseline.

        Returns a dict with added, removed, unchanged sets and summary counts.
        """
        existing_azure_ids: Set[str] = set()
        with open(baseline_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("asset_type") == "AzureResource":
                    existing_azure_ids.add(row.get("asset_id", ""))

        scanned_ids = {r["asset_id"] for r in resources}

        added = scanned_ids - existing_azure_ids
        removed = existing_azure_ids - scanned_ids

        return {
            "added": added,
            "removed": removed,
            "unchanged": scanned_ids & existing_azure_ids,
            "total_scanned": len(scanned_ids),
            "total_baseline": len(existing_azure_ids),
        }

    def update_baseline(self, resources: List[dict], baseline_path: str) -> None:
        """Replace all AzureResource rows in baseline with fresh scan data.

        Preserves all non-Azure rows (repos, Databricks jobs, Dynatrace entities)
        and replaces the Azure section with the latest Resource Graph results.
        """
        non_azure_rows: List[dict] = []
        with open(baseline_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("asset_type") != "AzureResource":
                    non_azure_rows.append(row)

        with open(baseline_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=BASELINE_FIELDNAMES, lineterminator="\r\n"
            )
            writer.writeheader()

            for row in non_azure_rows:
                writer.writerow({fn: row.get(fn, "") for fn in BASELINE_FIELDNAMES})

            for row in resources:
                writer.writerow(row)

        total = len(non_azure_rows) + len(resources)
        console.print(
            f"[green]Updated baseline: {len(non_azure_rows)} non-Azure + "
            f"{len(resources)} Azure = {total} total rows[/green]"
        )

    def print_summary(self, resources: List[dict], drift: Optional[dict] = None) -> None:
        """Print a summary table of scanned resources by AppID."""
        by_app: Dict[str, int] = {}
        for r in resources:
            key = f"{r['app_id']} ({r['app_name']})"
            by_app[key] = by_app.get(key, 0) + 1

        table = Table(title="Azure Resources by AppID", show_lines=True)
        table.add_column("AppID (Name)", style="cyan")
        table.add_column("Resources", justify="right", style="green")

        for key in sorted(by_app.keys()):
            table.add_row(key, str(by_app[key]))

        table.add_row("[bold]Total[/bold]", f"[bold]{len(resources)}[/bold]")
        console.print(table)

        if drift:
            console.print()
            drift_table = Table(title="Drift Report", show_lines=True)
            drift_table.add_column("Metric", style="cyan")
            drift_table.add_column("Count", justify="right")

            drift_table.add_row("Baseline Azure resources", str(drift["total_baseline"]))
            drift_table.add_row("Scanned Azure resources", str(drift["total_scanned"]))
            drift_table.add_row(
                "New (not in baseline)",
                f"[green]+{len(drift['added'])}[/green]" if drift["added"] else "0",
            )
            drift_table.add_row(
                "Removed (in baseline, not in Azure)",
                f"[red]-{len(drift['removed'])}[/red]" if drift["removed"] else "0",
            )
            drift_table.add_row("Unchanged", str(len(drift["unchanged"])))
            console.print(drift_table)

            if drift["added"] and len(drift["added"]) <= 20:
                console.print("\n[green]New resources:[/green]")
                for rid in sorted(drift["added"]):
                    name = rid.split("/")[-1]
                    console.print(f"  + {name}")

            if drift["removed"] and len(drift["removed"]) <= 20:
                console.print("\n[red]Removed resources:[/red]")
                for rid in sorted(drift["removed"]):
                    name = rid.split("/")[-1]
                    console.print(f"  - {name}")


@click.command("scan")
@click.option(
    "--team-mapping",
    default="data/team_app_mapping.csv",
    show_default=True,
    help="Path to team/app mapping CSV",
)
@click.option(
    "--baseline",
    default="data/asset_mappings_baseline.csv",
    show_default=True,
    help="Path to existing baseline CSV for drift detection",
)
@click.option(
    "--output",
    default="outputs/csv/azure_resources.csv",
    show_default=True,
    help="Output path for standalone Azure resources CSV",
)
@click.option(
    "--app-ids",
    default=None,
    help="Comma-separated AppIDs to scan (default: all from team mapping)",
)
@click.option(
    "--update-baseline",
    is_flag=True,
    help="Replace Azure rows in baseline CSV with fresh scan results",
)
@click.option(
    "--update-csv",
    is_flag=True,
    help="Alias for --update-baseline (consistency with techstack command)",
)
@click.option("--verbose", is_flag=True, help="Verbose output")
def scan_command(team_mapping, baseline, output, app_ids, update_baseline, update_csv, verbose):
    """Scan Azure resources via Resource Graph and reconcile with asset mappings.

    Uses Azure Resource Graph to discover all resources tagged with
    EA_Application_ID across every accessible subscription in a single query.
    Cross-references with team_app_mapping.csv for product_group/team enrichment.

    \b
    Modes:
      Default       Scan and write to --output CSV, show drift report
      --update-baseline  Also replace Azure rows in the baseline CSV
      --app-ids A2412,A2238  Scan only specific AppIDs

    \b
    Examples:
        # Scan all AppIDs, show drift, write standalone CSV
        data-scripts azure scan --verbose

        # Scan and update the baseline (for daily refresh)
        data-scripts azure scan --update-baseline

        # Scan only PAP team apps
        data-scripts azure scan --app-ids A2412,A2238,A2680,A3040

    \b
    Requirements:
        az CLI authenticated (az login / OIDC in CI)
        resource-graph extension (auto-installed if missing)
    """
    should_update = update_baseline or update_csv

    filter_ids = [a.strip() for a in app_ids.split(",")] if app_ids else None

    scanner = AzureResourceScanner(team_mapping, verbose=verbose)
    resources = scanner.scan(app_ids=filter_ids)

    if not resources:
        console.print("[red]No resources found. Check Azure CLI auth and filters.[/red]")
        raise SystemExit(1)

    # Always write standalone output
    scanner.generate_csv(resources, output)

    # Drift report if baseline exists
    drift = None
    baseline_path = Path(baseline)
    if baseline_path.exists():
        drift = scanner.reconcile(resources, str(baseline_path))

    scanner.print_summary(resources, drift)

    if should_update and baseline_path.exists():
        console.print("\n[bold]Updating baseline CSV...[/bold]")
        scanner.update_baseline(resources, str(baseline_path))

        # Also update the outputs mirror copy
        outputs_copy = Path("outputs/csv") / baseline_path.name
        if outputs_copy.exists() or outputs_copy.parent.exists():
            scanner.update_baseline(resources, str(outputs_copy))
            console.print(f"[green]Also updated {outputs_copy}[/green]")

    console.print("\n[bold green]Done.[/bold green]")
