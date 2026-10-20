"""Dynatrace Live Scanner - replaces manual baseline with live data from the API.

Discovers all Foods services and alert configurations via the Dynatrace
API v2, maps them to AppIDs via host group or entity tags, and generates
CSVs that replace the 898 manual baseline rows.

Gives every Foods team live observability coverage data, and addresses
the Yuktha cost query issue by listing all alert configs.

Requires:
  - DYNATRACE_API_TOKEN env var with entities.read and settings.read scopes
  - DYNATRACE_ENV_URL env var (e.g. https://{env-id}.live.dynatrace.com)
"""

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import click
import requests
from rich.console import Console
from rich.table import Table

from ..services.csv_helper import write_csv

console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"

if not DATA_DIR.exists():
    DATA_DIR = Path.cwd() / "data"
    OUTPUTS_DIR = Path.cwd() / "outputs"

SERVICE_FIELDNAMES = [
    "entity_id", "display_name", "entity_type", "app_id", "app_name",
    "team", "host_group", "management_zones", "technology_type",
    "discovered_name", "tags",
]

ALERT_FIELDNAMES = [
    "object_id", "schema_id", "alert_name", "alert_type", "severity",
    "enabled", "scope", "scope_entity", "description",
]


class DynatraceScanner:
    """Scans Dynatrace API v2 for Foods services and alert configurations."""

    def __init__(
        self,
        env_url: str,
        api_token: str,
        team_mapping_path: str,
        verbose: bool = False,
        verify_ssl: bool = True,
    ):
        self.verbose = verbose
        self.env_url = env_url.rstrip("/")
        self.api_token = api_token
        self.verify_ssl = verify_ssl
        self.headers = {
            "Authorization": f"Api-Token {api_token}",
            "Accept": "application/json",
        }
        self.team_mapping = self._load_team_mapping(team_mapping_path)

    def _load_team_mapping(self, path: str) -> Dict[str, dict]:
        """Load AppID -> team/app_name mapping."""
        mapping = {}
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    app_id = row.get("AppID", "").strip()
                    if app_id:
                        mapping[app_id] = {
                            "team": row.get("Team", "").strip(),
                            "app_name": row.get("Application", "").strip(),
                        }
        except FileNotFoundError:
            pass
        return mapping

    def _api_get(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        """Make a GET request to the Dynatrace API v2."""
        url = f"{self.env_url}/api/v2/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        try:
            resp = requests.get(url, headers=self.headers, timeout=30, verify=self.verify_ssl)
            if resp.status_code != 200:
                if self.verbose:
                    console.print(f"[dim]API {resp.status_code}: {resp.text[:100]}[/dim]")
                return None
            return resp.json()
        except requests.RequestException as exc:
            if self.verbose:
                console.print(f"[dim]API error: {exc}[/dim]")
            return None

    def _api_get_paginated(self, path: str, params: Optional[dict] = None) -> List[dict]:
        """Paginate through a Dynatrace API v2 endpoint."""
        all_items = []
        next_key = None

        while True:
            if next_key:
                # Dynatrace v2: when using nextPageKey, only pass that param
                req_params = {"nextPageKey": next_key}
            else:
                req_params = dict(params or {})

            data = self._api_get(path, req_params)
            if data is None:
                break

            entities = data.get("entities", data.get("items", []))
            all_items.extend(entities)

            next_key = data.get("nextPageKey")
            if not next_key:
                break

            if self.verbose:
                console.print(f"[dim]  Fetched {len(all_items)} so far...[/dim]")

        return all_items

    def _extract_app_id_from_tags(self, tags: List[dict]) -> Optional[str]:
        """Try to extract AppID from Dynatrace entity tags."""
        for tag in tags:
            key = tag.get("key", "")
            value = tag.get("value", "")
            # Common tag patterns: EA_Application_ID, AppID, app_id
            if key.lower() in ("ea_application_id", "appid", "app_id", "application_id"):
                match = re.match(r"[Aa]?\d+", str(value))
                if match:
                    val = match.group()
                    if not val.startswith("A"):
                        val = f"A{val}"
                    return val
        return None

    def _extract_app_id_from_host_group(self, host_group: str) -> Optional[str]:
        """Try to extract AppID from host group name."""
        if not host_group:
            return None
        # Pattern: usually contains the app name or ID
        match = re.search(r"[Aa](\d{4,5})", host_group)
        if match:
            return f"A{match.group(1)}"
        return None

    def _process_entities(self, entities: List[dict], entity_type: str) -> List[dict]:
        """Process a list of Dynatrace entities into standardised result dicts."""
        results = []
        for entity in entities:
            entity_id = entity.get("entityId", "")
            display_name = entity.get("displayName", "")
            tags = entity.get("tags", [])
            properties = entity.get("properties", {})
            mgmt_zones = entity.get("managementZones", [])

            # Try to map to AppID
            app_id = self._extract_app_id_from_tags(tags)

            # Fallback: try host group from tags (dt.host_group.id)
            host_group = ""
            if not app_id:
                for tag in tags:
                    if tag.get("key") == "dt.host_group.id":
                        host_group = tag.get("value", "")
                        break
                if not host_group:
                    host_group = properties.get("HOST_GROUP_NAME", "")
                if host_group:
                    app_id = self._extract_app_id_from_host_group(host_group)

            # Fallback: try management zone names (often contain AppID)
            mz_names = [mz.get("name", "") for mz in mgmt_zones]
            if not app_id:
                for mz_name in mz_names:
                    app_id = self._extract_app_id_from_host_group(mz_name)
                    if app_id:
                        break

            # Enrich with team mapping
            mapping = self.team_mapping.get(app_id, {}) if app_id else {}
            mz_names = [mz.get("name", "") for mz in mgmt_zones]
            tag_strs = [
                f"{t.get('key', '')}:{t.get('value', '')}" if t.get("value") else t.get("key", "")
                for t in tags
            ]

            results.append({
                "entity_id": entity_id,
                "display_name": display_name,
                "entity_type": entity_type,
                "app_id": app_id or "",
                "app_name": mapping.get("app_name", ""),
                "team": mapping.get("team", ""),
                "host_group": host_group,
                "management_zones": "; ".join(mz_names),
                "technology_type": ", ".join(
                    properties.get("serviceTechnologyTypes", [])
                ) or properties.get("SERVICE_TECHNOLOGY_TYPE", ""),
                "discovered_name": properties.get("detectedName", "")
                    or properties.get("DISCOVERED_NAME", ""),
                "tags": "; ".join(tag_strs),
            })

        return results

    def scan_services(self) -> List[dict]:
        """Discover all services and cloud custom devices from Dynatrace.

        Scans two entity types:
        - SERVICE: Application Observability services (Java, .NET, Node.js etc.)
        - CUSTOM_DEVICE: Infrastructure Observability / Clouds services
          (Logic Apps, Function Apps, Data Factories, etc.)
        """
        console.print("[bold]Discovering Dynatrace services...[/bold]")

        entity_types = [
            ("SERVICE", "Application Observability services"),
            ("CUSTOM_DEVICE", "Infrastructure / Cloud services"),
        ]

        all_results = []
        for etype, label in entity_types:
            entities = self._api_get_paginated(
                "entities",
                {
                    "entitySelector": f'type("{etype}")',
                    "fields": "+tags,+properties,+managementZones",
                    "pageSize": "500",
                },
            )

            if entities:
                console.print(f"[green]Found {len(entities)} {label} ({etype})[/green]")
                all_results.extend(self._process_entities(entities, etype))
            else:
                console.print(f"[yellow]No {label} found.[/yellow]")

        if not all_results:
            console.print("[yellow]No entities found at all.[/yellow]")

        return all_results

    def scan_alerts(self) -> List[dict]:
        """Discover all anomaly detection / metric event alert configurations."""
        console.print("[bold]Scanning Dynatrace alert configurations...[/bold]")

        # Fetch metric event settings (custom alerts)
        schema_ids = [
            "builtin:anomaly-detection.metric-events",
            "builtin:anomaly-detection.infrastructure-hosts",
            "builtin:anomaly-detection.infrastructure-disks",
        ]

        all_alerts = []
        for schema_id in schema_ids:
            if self.verbose:
                console.print(f"[dim]  Scanning schema: {schema_id}[/dim]")

            items = self._api_get_paginated(
                "settings/objects",
                {"schemaIds": schema_id, "pageSize": "500"},
            )

            for item in items:
                obj_id = item.get("objectId", "")
                value = item.get("value", {})
                scope = item.get("scope", "")

                # Extract useful fields depending on schema
                alert_name = value.get("summary", value.get("eventName", ""))
                if not alert_name:
                    alert_name = value.get("name", obj_id)

                enabled = value.get("enabled", True)
                severity = value.get("eventType", value.get("severity", ""))
                description = value.get("description", "")

                # Determine scope entity
                scope_entity = ""
                if scope and scope.startswith("HOST-"):
                    scope_entity = scope
                elif scope == "environment":
                    scope_entity = "environment-wide"

                all_alerts.append({
                    "object_id": obj_id,
                    "schema_id": schema_id,
                    "alert_name": alert_name,
                    "alert_type": schema_id.split(".")[-1],
                    "severity": severity,
                    "enabled": str(enabled),
                    "scope": scope,
                    "scope_entity": scope_entity,
                    "description": description[:200] if description else "",
                })

        console.print(f"[green]Found {len(all_alerts)} alert configurations[/green]")
        return all_alerts

    def scan(self) -> tuple:
        """Run full Dynatrace scan: services + alerts."""
        services = self.scan_services()
        alerts = self.scan_alerts()
        return services, alerts

    def generate_services_csv(self, results: List[dict], output_path: str) -> None:
        """Write services to CSV."""
        write_csv(results, SERVICE_FIELDNAMES, output_path, label="services")

    def generate_alerts_csv(self, results: List[dict], output_path: str) -> None:
        """Write alerts to CSV."""
        write_csv(results, ALERT_FIELDNAMES, output_path, label="alerts")

    def print_summary(self, services: List[dict], alerts: List[dict]) -> None:
        """Print a summary of discovered entities."""
        # Services by AppID
        mapped = [s for s in services if s["app_id"]]
        unmapped = [s for s in services if not s["app_id"]]

        table = Table(title="Dynatrace Discovery Summary", show_lines=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="green")

        table.add_row("Total services", str(len(services)))
        table.add_row("Mapped to AppID", str(len(mapped)))
        table.add_row("Unmapped (no AppID)", str(len(unmapped)))
        table.add_row("Unique AppIDs", str(len(set(s["app_id"] for s in mapped))))
        table.add_row("Alert configurations", str(len(alerts)))
        table.add_row("Alerts enabled", str(sum(1 for a in alerts if a["enabled"] == "True")))
        table.add_row("Alerts disabled", str(sum(1 for a in alerts if a["enabled"] != "True")))
        console.print(table)

        # Top AppIDs by service count
        by_app: Dict[str, int] = {}
        for s in mapped:
            key = f"{s['app_id']} ({s['app_name']})" if s["app_name"] else s["app_id"]
            by_app[key] = by_app.get(key, 0) + 1

        if by_app:
            app_table = Table(title="\nTop 15 AppIDs by Service Count")
            app_table.add_column("AppID", style="cyan")
            app_table.add_column("Services", justify="right")
            for key, count in sorted(by_app.items(), key=lambda x: -x[1])[:15]:
                app_table.add_row(key, str(count))
            console.print(app_table)


@click.command("scan")
@click.option(
    "--env-url",
    envvar="DYNATRACE_ENV_URL",
    required=True,
    help="Dynatrace environment URL (e.g. https://abc12345.live.dynatrace.com).",
)
@click.option(
    "--api-token",
    envvar="DYNATRACE_API_TOKEN",
    required=True,
    help="Dynatrace API token with entities.read and settings.read scopes.",
)
@click.option(
    "--team-mapping",
    default=str(DATA_DIR / "team_app_mapping.csv"),
    show_default=True,
    help="Path to team/app mapping CSV.",
)
@click.option(
    "--services-output",
    default=str(OUTPUTS_DIR / "csv" / "dynatrace_services.csv"),
    show_default=True,
    help="Output CSV for services.",
)
@click.option(
    "--alerts-output",
    default=str(OUTPUTS_DIR / "csv" / "dynatrace_alerts.csv"),
    show_default=True,
    help="Output CSV for alert configurations.",
)
@click.option("--verbose", is_flag=True, help="Show detailed progress.")
@click.option("--no-verify-ssl", is_flag=True, help="Disable SSL verification (for Zscaler proxy).")
def dynatrace_scan_command(
    env_url: str, api_token: str, team_mapping: str,
    services_output: str, alerts_output: str, verbose: bool,
    no_verify_ssl: bool,
):
    """Scan Dynatrace for all Foods services and alert configurations.

    Discovers services via the Dynatrace API v2 entities endpoint,
    maps them to AppIDs via tags or host groups, and exports alert
    configurations for the entire environment.

    Replaces the 898 manual baseline rows with live data from Dynatrace.

    \\b
    Examples:
        # Full scan
        data-scripts dynatrace scan --verbose

        # With explicit credentials
        data-scripts dynatrace scan \\
          --env-url https://abc12345.live.dynatrace.com \\
          --api-token dt0c01.XXXX

    \\b
    Environment Variables:
        DYNATRACE_ENV_URL    Dynatrace environment URL
        DYNATRACE_API_TOKEN  API token (entities.read + settings.read)
    """
    if no_verify_ssl:
        import warnings
        import urllib3
        warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

    scanner = DynatraceScanner(
        env_url, api_token, team_mapping,
        verbose=verbose, verify_ssl=not no_verify_ssl,
    )
    services, alerts = scanner.scan()

    if services:
        scanner.generate_services_csv(services, services_output)
    else:
        console.print("[yellow]No services discovered.[/yellow]")

    if alerts:
        scanner.generate_alerts_csv(alerts, alerts_output)
    else:
        console.print("[yellow]No alert configurations found.[/yellow]")

    scanner.print_summary(services, alerts)
    console.print("\n[bold green]Done.[/bold green]")
