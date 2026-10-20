"""Estate Report - unified cross-source dashboard for the Foods portfolio.

Reads outputs from all data sources (Azure Resource Graph, catalog-info,
repos.csv, asset_mappings baseline, PagerDuty standards, repo health) and
generates an interactive HTML dashboard showing the complete estate.

The dashboard provides:
  - Portfolio-wide KPIs (repos, Azure resources, AppIDs, coverage scores)
  - Per-AppID drill-down (repos, resources, PagerDuty services, lifecycle)
  - Coverage heatmap (which AppIDs have gaps in which data sources)
  - Drift indicators (resources discovered but not tracked, and vice versa)
  - Orphan detection (repos without AppID, resources without owner)

This is the executive view for the Foods Engineering Director.
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import click
from rich.console import Console

console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"

# Fall back to CWD-relative paths if the package-relative paths don't exist
# (handles both installed-package and CWD-based invocation)
if not DATA_DIR.exists():
    DATA_DIR = Path.cwd() / "data"
    OUTPUTS_DIR = Path.cwd() / "outputs"


class EstateReportGenerator:
    """Aggregates all data sources into a unified estate view."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.repos: List[dict] = []
        self.azure_resources: List[dict] = []
        self.catalog_info: List[dict] = []
        self.pagerduty: List[dict] = []
        self.repo_health: List[dict] = []
        self.asset_baseline: List[dict] = []
        self.dynatrace_services: List[dict] = []
        self.actions_health: List[dict] = []
        self.azure_costs: List[dict] = []
        self.kv_expiry: List[dict] = []
        self.team_mapping: Dict[str, dict] = {}
        self.app_registry: Dict[str, dict] = {}

    def load_all_sources(self):
        """Load all available data sources."""
        self._load_repos_csv()
        self._load_team_mapping()
        self._load_azure_resources()
        self._load_catalog_info()
        self._load_pagerduty()
        self._load_repo_health()
        self._load_asset_baseline()
        self._load_dynatrace_services()
        self._load_actions_health()
        self._load_azure_costs()
        self._load_kv_expiry()
        self._build_app_registry()

    def _load_csv(self, path: Path, label: str) -> List[dict]:
        """Safely load a CSV file."""
        if not path.exists():
            if self.verbose:
                console.print(f"[dim]  {label}: not found at {path}[/dim]")
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if self.verbose:
            console.print(f"  {label}: {len(rows)} rows")
        return rows

    def _load_repos_csv(self):
        self.repos = self._load_csv(DATA_DIR / "repos.csv", "repos.csv")

    def _load_team_mapping(self):
        rows = self._load_csv(DATA_DIR / "team_app_mapping.csv", "team_app_mapping")
        for row in rows:
            app_id = row.get("AppID", "").strip()
            if app_id:
                self.team_mapping[app_id] = row

    def _load_azure_resources(self):
        self.azure_resources = self._load_csv(
            OUTPUTS_DIR / "csv" / "azure_resources.csv", "azure_resources"
        )

    def _load_catalog_info(self):
        self.catalog_info = self._load_csv(
            OUTPUTS_DIR / "csv" / "catalog_info.csv", "catalog_info"
        )

    def _load_pagerduty(self):
        self.pagerduty = self._load_csv(
            OUTPUTS_DIR / "csv" / "pagerduty_standard.csv", "pagerduty"
        )

    def _load_repo_health(self):
        self.repo_health = self._load_csv(
            OUTPUTS_DIR / "csv" / "repo_health.csv", "repo_health"
        )

    def _load_asset_baseline(self):
        baseline_path = DATA_DIR / "asset_mappings_baseline.csv"
        self.asset_baseline = self._load_csv(baseline_path, "asset_baseline")

    def _load_dynatrace_services(self):
        self.dynatrace_services = self._load_csv(
            OUTPUTS_DIR / "csv" / "dynatrace_services.csv", "dynatrace_services"
        )

    def _load_actions_health(self):
        self.actions_health = self._load_csv(
            OUTPUTS_DIR / "csv" / "actions_health.csv", "actions_health"
        )

    def _load_azure_costs(self):
        self.azure_costs = self._load_csv(
            OUTPUTS_DIR / "csv" / "azure_costs.csv", "azure_costs"
        )

    def _load_kv_expiry(self):
        self.kv_expiry = self._load_csv(
            OUTPUTS_DIR / "csv" / "kv_expiry.csv", "kv_expiry"
        )

    def _build_app_registry(self):
        """Build unified per-AppID registry from all sources."""
        all_app_ids: Set[str] = set()

        # From team_mapping
        all_app_ids.update(self.team_mapping.keys())

        # From repos.csv
        for r in self.repos:
            app_id = r.get("AppID", "").strip()
            if app_id:
                all_app_ids.add(app_id)

        # From azure resources
        for r in self.azure_resources:
            app_id = r.get("app_id", "").strip()
            if app_id:
                all_app_ids.add(app_id)

        # From catalog-info
        for r in self.catalog_info:
            app_id = r.get("app_id", "").strip()
            if app_id:
                all_app_ids.add(app_id)

        # From baseline
        for r in self.asset_baseline:
            app_id = r.get("app_id", "").strip()
            if app_id:
                all_app_ids.add(app_id)

        # From live Dynatrace scan
        for r in self.dynatrace_services:
            app_id = r.get("app_id", "").strip()
            if app_id:
                all_app_ids.add(app_id)

        # Build registry
        # Pre-build catalog-info team lookup for fallback enrichment
        catalog_teams: dict[str, str] = {}
        catalog_descriptions: dict[str, str] = {}
        for r in self.catalog_info:
            aid = r.get("app_id", "").strip()
            team = r.get("owner_team", "").strip()
            desc = r.get("description", "").strip()
            if aid and team and aid not in catalog_teams:
                catalog_teams[aid] = team
            if aid and desc and aid not in catalog_descriptions:
                catalog_descriptions[aid] = desc

        # Pre-build repos.csv AppName lookup for fallback app names
        repo_app_names: dict[str, str] = {}
        for r in self.repos:
            aid = r.get("AppID", "").strip()
            name = r.get("AppName", "").strip()
            if aid and name and aid not in repo_app_names:
                repo_app_names[aid] = name

        # Pre-build baseline app_name lookup for remaining fallbacks
        baseline_app_names: dict[str, str] = {}
        for r in self.asset_baseline:
            aid = r.get("app_id", "").strip()
            name = r.get("app_name", "").strip()
            if aid and name and name != aid and aid not in baseline_app_names:
                baseline_app_names[aid] = name

        # Pre-build baseline team lookup
        baseline_teams: dict[str, str] = {}
        for r in self.asset_baseline:
            aid = r.get("app_id", "").strip()
            team_val = r.get("team", "").strip()
            if aid and team_val and team_val != "Unknown" and aid not in baseline_teams:
                baseline_teams[aid] = team_val

        # Pre-build Dynatrace name/team lookups for AppIDs only visible via Dynatrace
        dynatrace_app_names: dict[str, str] = {}
        dynatrace_teams: dict[str, str] = {}
        for r in self.dynatrace_services:
            aid = r.get("app_id", "").strip()
            name = r.get("app_name", "").strip()
            team_val = r.get("team", "").strip()
            if aid and name and name != aid and aid not in dynatrace_app_names:
                dynatrace_app_names[aid] = name
            if aid and team_val and team_val != "Unknown" and aid not in dynatrace_teams:
                dynatrace_teams[aid] = team_val

        for app_id in sorted(all_app_ids):
            mapping = self.team_mapping.get(app_id, {})

            # Resolve app_name: team_mapping > repos.csv > baseline > dynatrace > AppID
            app_name = mapping.get("Application", "").strip()
            if not app_name or app_name == app_id:
                app_name = repo_app_names.get(app_id, "")
            if not app_name or app_name == app_id:
                app_name = baseline_app_names.get(app_id, "")
            if not app_name or app_name == app_id:
                app_name = dynatrace_app_names.get(app_id, app_id)

            # Resolve team: team_mapping > catalog-info > baseline > dynatrace > "Unknown"
            team = mapping.get("Team", "").strip()
            if not team:
                team = catalog_teams.get(app_id, "")
            if not team:
                team = baseline_teams.get(app_id, "")
            if not team:
                team = dynatrace_teams.get(app_id, "Unknown")

            self.app_registry[app_id] = {
                "app_id": app_id,
                "app_name": app_name,
                "team": team,
                "subdomain": mapping.get("Subdomain", ""),
                "developed": mapping.get("M&S Developed", ""),
                "repos": [],
                "azure_resources": 0,
                "azure_resource_types": defaultdict(int),
                "dynatrace_services": 0,
                "databricks_jobs": 0,
                "pagerduty_services": [],
                "catalog_info_repos": [],
                "lifecycle": set(),
                "has_repos": False,
                "has_azure": False,
                "has_dynatrace": False,
                "has_pagerduty": False,
                "has_catalog_info": False,
                "coverage_score": 0,
                # New dimensions
                "ci_total_runs": 0,
                "ci_success_count": 0,
                "ci_failure_count": 0,
                "ci_success_rate": 0.0,
                "ci_status": "none",
                "monthly_cost_gbp": 0.0,
                "kv_secrets_total": 0,
                "kv_expiring_30d": 0,
                "kv_expiring_60d": 0,
                "kv_expiring_90d": 0,
                "kv_expired": 0,
            }

        # Populate from repos
        for r in self.repos:
            app_id = r.get("AppID", "").strip()
            if app_id and app_id in self.app_registry:
                self.app_registry[app_id]["repos"].append(r.get("RepoName", ""))
                self.app_registry[app_id]["has_repos"] = True

        # Populate from Azure
        for r in self.azure_resources:
            app_id = r.get("app_id", "").strip()
            if app_id and app_id in self.app_registry:
                self.app_registry[app_id]["azure_resources"] += 1
                self.app_registry[app_id]["has_azure"] = True
                rtype = r.get("asset_name", "").split("/")[-1] if r.get("asset_name") else ""
                if rtype:
                    # Extract resource type from resource ID
                    asset_id = r.get("asset_id", "")
                    if "/providers/" in asset_id:
                        provider_part = asset_id.split("/providers/")[-1]
                        parts = provider_part.split("/")
                        if len(parts) >= 2:
                            rtype_key = f"{parts[0]}/{parts[1]}"
                            self.app_registry[app_id]["azure_resource_types"][rtype_key] += 1

        # Populate from baseline (Databricks only; Dynatrace superseded by live scan)
        for r in self.asset_baseline:
            app_id = r.get("app_id", "").strip()
            asset_type = r.get("asset_type", "").strip()
            if app_id and app_id in self.app_registry:
                if asset_type == "DatabricksJob":
                    self.app_registry[app_id]["databricks_jobs"] += 1

        # Populate from live Dynatrace scan (replaces baseline Dynatrace rows)
        if self.dynatrace_services:
            for r in self.dynatrace_services:
                app_id = r.get("app_id", "").strip()
                if app_id and app_id in self.app_registry:
                    self.app_registry[app_id]["dynatrace_services"] += 1
                    self.app_registry[app_id]["has_dynatrace"] = True
        else:
            # Fall back to baseline if live scan data is unavailable
            for r in self.asset_baseline:
                app_id = r.get("app_id", "").strip()
                asset_type = r.get("asset_type", "").strip()
                if app_id and app_id in self.app_registry:
                    if asset_type == "DynatraceService":
                        self.app_registry[app_id]["dynatrace_services"] += 1
                        self.app_registry[app_id]["has_dynatrace"] = True

        # Populate from catalog-info
        for r in self.catalog_info:
            app_id = r.get("app_id", "").strip()
            if app_id and app_id in self.app_registry:
                self.app_registry[app_id]["catalog_info_repos"].append(
                    r.get("repo_name", "")
                )
                self.app_registry[app_id]["has_catalog_info"] = True
                lc = r.get("lifecycle", "")
                if lc:
                    self.app_registry[app_id]["lifecycle"].add(lc)

        # Populate from Actions health (CI metrics)
        for r in self.actions_health:
            app_id = r.get("app_id", "").strip()
            if app_id and app_id in self.app_registry:
                total = int(r.get("total_runs", 0) or 0)
                success = int(r.get("success_count", 0) or 0)
                failures = int(r.get("failure_count", 0) or 0)
                self.app_registry[app_id]["ci_total_runs"] += total
                self.app_registry[app_id]["ci_success_count"] += success
                self.app_registry[app_id]["ci_failure_count"] += failures

        # Calculate CI status per AppID
        for entry in self.app_registry.values():
            total = entry["ci_total_runs"]
            if total > 0:
                entry["ci_success_rate"] = round(
                    entry["ci_success_count"] / total * 100, 1
                )
                rate = entry["ci_success_rate"]
                if rate >= 90:
                    entry["ci_status"] = "green"
                elif rate >= 70:
                    entry["ci_status"] = "amber"
                else:
                    entry["ci_status"] = "red"

        # Populate from Azure costs
        for r in self.azure_costs:
            app_id = r.get("app_id", "").strip()
            if app_id and app_id in self.app_registry:
                cost = float(r.get("cost_gbp", 0) or 0)
                self.app_registry[app_id]["monthly_cost_gbp"] += cost

        # Populate from Key Vault expiry
        for r in self.kv_expiry:
            app_id = r.get("app_id", "").strip()
            if app_id and app_id in self.app_registry:
                self.app_registry[app_id]["kv_secrets_total"] += 1
                band = r.get("expiry_band", "")
                if band == "expired":
                    self.app_registry[app_id]["kv_expired"] += 1
                elif band == "critical_30d":
                    self.app_registry[app_id]["kv_expiring_30d"] += 1
                elif band == "warning_60d":
                    self.app_registry[app_id]["kv_expiring_60d"] += 1
                elif band == "watch_90d":
                    self.app_registry[app_id]["kv_expiring_90d"] += 1

        # Calculate coverage scores
        for _app_id, entry in self.app_registry.items():
            score = 0
            if entry["has_repos"]:
                score += 25
            if entry["has_azure"]:
                score += 25
            if entry["has_catalog_info"]:
                score += 25
            if entry["has_dynatrace"] or entry["has_pagerduty"]:
                score += 25
            entry["coverage_score"] = score
            entry["lifecycle"] = list(entry["lifecycle"])
            entry["azure_resource_types"] = dict(entry["azure_resource_types"])

        # Remove Azure-only AppIDs with no Foods linkage (non-Foods noise)
        azure_only_ids = [
            aid for aid, e in self.app_registry.items()
            if e["team"] == "Unknown"
            and not e["has_repos"]
            and not e["has_catalog_info"]
            and not e["has_dynatrace"]
            and not e["has_pagerduty"]
            and e["has_azure"]
        ]
        for aid in azure_only_ids:
            del self.app_registry[aid]

    def generate_portfolio_stats(self) -> dict:
        """Generate portfolio-wide statistics."""
        total_repos = len(self.repos)
        total_azure = sum(
            e["azure_resources"] for e in self.app_registry.values()
        )
        total_app_ids = len(self.app_registry)
        total_dynatrace = sum(
            e["dynatrace_services"] for e in self.app_registry.values()
        )
        total_databricks = sum(
            e["databricks_jobs"] for e in self.app_registry.values()
        )

        # Coverage scores
        full_coverage = sum(
            1 for e in self.app_registry.values() if e["coverage_score"] == 100
        )
        partial_coverage = sum(
            1 for e in self.app_registry.values()
            if 0 < e["coverage_score"] < 100
        )
        no_coverage = sum(
            1 for e in self.app_registry.values() if e["coverage_score"] == 0
        )

        # Catalog-info adoption (from live scan, not repos.csv metadata)
        repos_with_catalog = len(set(
            r.get("repo_name", "").strip()
            for r in self.catalog_info
            if r.get("repo_name", "").strip()
        )) if self.catalog_info else sum(
            1 for r in self.repos
            if r.get("HasCatalog", "").strip() == "Yes"
        )

        # CI health stats
        ci_total_runs = sum(e["ci_total_runs"] for e in self.app_registry.values())
        ci_total_success = sum(e["ci_success_count"] for e in self.app_registry.values())
        ci_overall_rate = round(ci_total_success / ci_total_runs * 100, 1) if ci_total_runs else 0
        ci_apps_green = sum(1 for e in self.app_registry.values() if e["ci_status"] == "green")
        ci_apps_red = sum(1 for e in self.app_registry.values() if e["ci_status"] == "red")

        # Cost stats
        total_monthly_cost = sum(e["monthly_cost_gbp"] for e in self.app_registry.values())

        # KV expiry stats
        kv_total_secrets = sum(e["kv_secrets_total"] for e in self.app_registry.values())
        kv_expiring_soon = sum(
            e["kv_expiring_30d"] + e["kv_expired"]
            for e in self.app_registry.values()
        )

        return {
            "total_repos": total_repos,
            "total_azure_resources": total_azure,
            "total_app_ids": total_app_ids,
            "total_dynatrace_services": total_dynatrace,
            "total_databricks_jobs": total_databricks,
            "total_pagerduty_services": len(self.pagerduty),
            "full_coverage_apps": full_coverage,
            "partial_coverage_apps": partial_coverage,
            "no_coverage_apps": no_coverage,
            "catalog_info_adoption": repos_with_catalog,
            "catalog_info_total": total_repos,
            "catalog_info_pct": (
                round(repos_with_catalog / total_repos * 100, 1) if total_repos else 0
            ),
            "ci_overall_rate": ci_overall_rate,
            "ci_total_runs": ci_total_runs,
            "ci_apps_green": ci_apps_green,
            "ci_apps_red": ci_apps_red,
            "total_monthly_cost": round(total_monthly_cost, 2),
            "kv_total_secrets": kv_total_secrets,
            "kv_expiring_soon": kv_expiring_soon,
            "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }

    def generate_html_dashboard(self, output_path: str):
        """Generate the interactive HTML estate dashboard."""
        stats = self.generate_portfolio_stats()

        # Sort apps by coverage score (lowest first to highlight gaps)
        apps_sorted = sorted(
            self.app_registry.values(),
            key=lambda x: (-x["azure_resources"], x["app_id"]),
        )

        # Build the JSON data for the dashboard
        apps_json = json.dumps(
            [
                {
                    "app_id": a["app_id"],
                    "app_name": a["app_name"],
                    "team": a["team"],
                    "subdomain": a["subdomain"],
                    "repos": len(a["repos"]),
                    "azure": a["azure_resources"],
                    "dynatrace": a["dynatrace_services"],
                    "databricks": a["databricks_jobs"],
                    "catalog_info": len(a["catalog_info_repos"]),
                    "coverage": a["coverage_score"],
                    "lifecycle": ", ".join(a["lifecycle"]) if a["lifecycle"] else "",
                    "has_repos": a["has_repos"],
                    "has_azure": a["has_azure"],
                    "has_dynatrace": a["has_dynatrace"],
                    "has_catalog": a["has_catalog_info"],
                    "resource_types": a["azure_resource_types"],
                    "ci_status": a["ci_status"],
                    "ci_success_rate": a["ci_success_rate"],
                    "ci_total_runs": a["ci_total_runs"],
                    "ci_failures": a["ci_failure_count"],
                    "monthly_cost": round(a["monthly_cost_gbp"], 2),
                    "kv_secrets": a["kv_secrets_total"],
                    "kv_expiring": a["kv_expiring_30d"] + a["kv_expired"],
                    "kv_expired": a["kv_expired"],
                    "kv_30d": a["kv_expiring_30d"],
                }
                for a in apps_sorted
            ],
            indent=None,
        )
        # Prevent script injection: </script> in data would break out of the tag
        apps_json = apps_json.replace("</", r"<\/")

        html = _build_dashboard_html(stats, apps_json)

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        console.print(f"[green]Dashboard written to {path}[/green]")

    def generate_estate_csv(self, output_path: str):
        """Write the unified estate registry as CSV."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "app_id", "app_name", "team", "subdomain",
            "repo_count", "azure_resources", "dynatrace_services",
            "databricks_jobs", "catalog_info_repos", "coverage_score",
            "ci_success_rate", "ci_status", "ci_total_runs", "ci_failures",
            "monthly_cost_gbp", "kv_secrets_total", "kv_expiring_soon",
            "lifecycle", "repos",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for a in sorted(
                self.app_registry.values(), key=lambda x: x["app_id"]
            ):
                writer.writerow({
                    "app_id": a["app_id"],
                    "app_name": a["app_name"],
                    "team": a["team"],
                    "subdomain": a["subdomain"],
                    "repo_count": len(a["repos"]),
                    "azure_resources": a["azure_resources"],
                    "dynatrace_services": a["dynatrace_services"],
                    "databricks_jobs": a["databricks_jobs"],
                    "catalog_info_repos": len(a["catalog_info_repos"]),
                    "coverage_score": a["coverage_score"],
                    "ci_success_rate": a["ci_success_rate"],
                    "ci_status": a["ci_status"],
                    "ci_total_runs": a["ci_total_runs"],
                    "ci_failures": a["ci_failure_count"],
                    "monthly_cost_gbp": round(a["monthly_cost_gbp"], 2),
                    "kv_secrets_total": a["kv_secrets_total"],
                    "kv_expiring_soon": a["kv_expiring_30d"] + a["kv_expired"],
                    "lifecycle": ", ".join(a["lifecycle"]) if a["lifecycle"] else "",
                    "repos": "; ".join(a["repos"]),
                })

        console.print(f"[green]Estate CSV written to {path} ({len(self.app_registry)} AppIDs)[/green]")


def _build_dashboard_html(stats: dict, apps_json: str) -> str:
    """Build the complete HTML dashboard with CI, cost, and security data."""
    from ..services.site_nav import NAV_CSS, render_nav
    nav_css = NAV_CSS
    nav_html = render_nav("estate.html")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>Foods Estate Overview</title>
<style>
:root {{
  --bg: #0d1117;
  --surface: #161b22;
  --surface2: #21262d;
  --border: #30363d;
  --text: #e6edf3;
  --text-muted: #8b949e;
  --accent: #58a6ff;
  --green: #3fb950;
  --yellow: #d29922;
  --red: #f85149;
  --purple: #bc8cff;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding-top: 48px; }}

/* Nav bar */
{nav_css}

.container {{ max-width: 1800px; margin: 0 auto; padding: 24px; }}

/* Header */
.header {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 32px; margin-bottom: 24px; text-align: center; }}
.header h1 {{ font-size: 28px; margin-bottom: 8px; }}
.header .subtitle {{ color: var(--text-muted); font-size: 14px; }}

/* KPI cards */
.kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 10px; margin-bottom: 24px; }}
.kpi-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; text-align: center; }}
.kpi-value {{ font-size: 28px; font-weight: 700; color: var(--accent); }}
.kpi-label {{ font-size: 11px; color: var(--text-muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
.kpi-card.green .kpi-value {{ color: var(--green); }}
.kpi-card.yellow .kpi-value {{ color: var(--yellow); }}
.kpi-card.red .kpi-value {{ color: var(--red); }}
.kpi-card.purple .kpi-value {{ color: var(--purple); }}

/* Alert panel */
.alert-panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 24px; }}
.alert-panel h3 {{ margin-bottom: 14px; font-size: 16px; }}
.alert-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }}
.alert-card {{ background: var(--surface2); border-radius: 8px; padding: 14px; border-left: 4px solid var(--border); }}
.alert-card.alert-red {{ border-left-color: var(--red); }}
.alert-card.alert-yellow {{ border-left-color: var(--yellow); }}
.alert-card.alert-green {{ border-left-color: var(--green); }}
.alert-card .alert-title {{ font-size: 13px; font-weight: 600; margin-bottom: 4px; }}
.alert-card .alert-detail {{ font-size: 12px; color: var(--text-muted); }}
.alert-count {{ font-size: 20px; font-weight: 700; margin-right: 8px; }}
.alert-count.red {{ color: var(--red); }}
.alert-count.yellow {{ color: var(--yellow); }}
.alert-count.green {{ color: var(--green); }}

/* Coverage bar */
.coverage-bar {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 24px; }}
.coverage-bar h3 {{ margin-bottom: 12px; font-size: 16px; }}
.bar-track {{ background: var(--surface2); border-radius: 6px; height: 28px; display: flex; overflow: hidden; }}
.bar-segment {{ height: 100%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; transition: width 0.5s ease; }}
.bar-full {{ background: var(--green); color: #000; }}
.bar-partial {{ background: var(--yellow); color: #000; }}
.bar-none {{ background: var(--red); color: #fff; }}
.bar-legend {{ display: flex; gap: 20px; margin-top: 10px; font-size: 12px; color: var(--text-muted); }}
.bar-legend span::before {{ content: ''; display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; }}
.legend-full::before {{ background: var(--green) !important; }}
.legend-partial::before {{ background: var(--yellow) !important; }}
.legend-none::before {{ background: var(--red) !important; }}

/* Controls */
.controls {{ display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }}
.search-input {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 8px 14px; color: var(--text); font-size: 14px; min-width: 250px; outline: none; }}
.search-input:focus {{ border-color: var(--accent); }}
.filter-btn {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 6px 14px; color: var(--text-muted); font-size: 12px; cursor: pointer; transition: all 0.15s; }}
.filter-btn:hover,.filter-btn.active {{ background: var(--accent); color: #000; border-color: var(--accent); }}
.result-count {{ margin-left: auto; color: var(--text-muted); font-size: 13px; }}

/* Table */
.table-wrap {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
thead th {{ background: var(--surface2); padding: 8px 10px; text-align: left; font-weight: 600; color: var(--text-muted); text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; position: sticky; top: 0; cursor: pointer; user-select: none; white-space: nowrap; }}
thead th:hover {{ color: var(--accent); }}
thead th.sorted-asc::after {{ content: ' \\25B2'; font-size: 9px; }}
thead th.sorted-desc::after {{ content: ' \\25BC'; font-size: 9px; }}
tbody tr {{ border-top: 1px solid var(--border); transition: background 0.1s; }}
tbody tr:hover {{ background: var(--surface2); }}
td {{ padding: 6px 10px; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}

/* Coverage dots */
.cov-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 2px; }}
.cov-yes {{ background: var(--green); }}
.cov-no {{ background: var(--border); }}

/* Coverage badge */
.cov-badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
.cov-100 {{ background: rgba(63,185,80,0.15); color: var(--green); }}
.cov-75 {{ background: rgba(88,166,255,0.15); color: var(--accent); }}
.cov-50 {{ background: rgba(210,153,34,0.15); color: var(--yellow); }}
.cov-25 {{ background: rgba(248,81,73,0.15); color: var(--red); }}
.cov-0 {{ background: rgba(139,148,158,0.15); color: var(--text-muted); }}

/* CI status indicator */
.ci-pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; }}
.ci-green {{ background: rgba(63,185,80,0.15); color: var(--green); }}
.ci-amber {{ background: rgba(210,153,34,0.15); color: var(--yellow); }}
.ci-red {{ background: rgba(248,81,73,0.15); color: var(--red); }}
.ci-none {{ background: rgba(139,148,158,0.1); color: var(--text-muted); }}

/* Cost cell */
.cost-val {{ font-variant-numeric: tabular-nums; }}

/* KV warning */
.kv-warn {{ color: var(--red); font-weight: 600; }}

/* Scrollbar */
::-webkit-scrollbar {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: var(--bg); }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}

.footer {{ text-align: center; color: var(--text-muted); font-size: 12px; padding: 24px; margin-top: 24px; }}

@media (max-width: 768px) {{
  .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .controls {{ flex-direction: column; }}
  .search-input {{ width: 100%; }}
}}
</style>
</head>
<body>
{nav_html}

<div class="container">
  <div class="header">
    <h1>&#127760; Foods Portfolio Estate Overview</h1>
    <p class="subtitle">Unified view across Azure, GitHub, Dynatrace, Databricks, and Backstage catalog.
    Auto-generated from live data sources. Last refreshed: {stats['generated_at']}</p>
  </div>

  <!-- KPI Cards -->
  <div class="kpi-grid">
    <div class="kpi-card"><div class="kpi-value">{stats['total_app_ids']}</div><div class="kpi-label">AppIDs</div></div>
    <div class="kpi-card"><div class="kpi-value">{stats['total_repos']}</div><div class="kpi-label">Repositories</div></div>
    <div class="kpi-card"><div class="kpi-value">{stats['total_azure_resources']:,}</div><div class="kpi-label">Azure Resources</div></div>
    <div class="kpi-card"><div class="kpi-value">{stats['total_dynatrace_services']}</div><div class="kpi-label">Dynatrace Svcs</div></div>
    <div class="kpi-card"><div class="kpi-value">{stats['total_databricks_jobs']}</div><div class="kpi-label">Databricks Jobs</div></div>
    <div class="kpi-card"><div class="kpi-value">{stats['total_pagerduty_services']}</div><div class="kpi-label">PagerDuty Svcs</div></div>
    <div class="kpi-card green"><div class="kpi-value">{stats['catalog_info_pct']}%</div><div class="kpi-label">Catalog Adoption</div></div>
    <div class="kpi-card {'green' if stats['ci_overall_rate'] >= 90 else 'yellow' if stats['ci_overall_rate'] >= 70 else 'red' if stats['ci_total_runs'] > 0 else ''}">
      <div class="kpi-value">{stats['ci_overall_rate']}%</div><div class="kpi-label">CI Success Rate</div>
    </div>
    <div class="kpi-card purple"><div class="kpi-value">&pound;{stats['total_monthly_cost']:,.0f}</div><div class="kpi-label">Monthly Spend</div></div>
    <div class="kpi-card {'red' if stats['kv_expiring_soon'] > 0 else 'green'}">
      <div class="kpi-value">{stats['kv_expiring_soon']}</div><div class="kpi-label">Secrets Expiring</div>
    </div>
    <div class="kpi-card {'green' if stats['full_coverage_apps'] > stats['no_coverage_apps'] else 'yellow'}">
      <div class="kpi-value">{stats['full_coverage_apps']}</div><div class="kpi-label">Fully Tracked</div>
    </div>
  </div>

  <!-- Alerts Panel -->
  <div class="alert-panel">
    <h3>&#9888;&#65039; Portfolio Alerts</h3>
    <div class="alert-grid" id="alertGrid"></div>
  </div>

  <!-- Coverage Bar -->
  <div class="coverage-bar">
    <h3>AppID Coverage Distribution</h3>
    <div class="bar-track">
      <div class="bar-segment bar-full" style="width:{stats['full_coverage_apps']/max(stats['total_app_ids'],1)*100:.1f}%">{stats['full_coverage_apps']}</div>
      <div class="bar-segment bar-partial" style="width:{stats['partial_coverage_apps']/max(stats['total_app_ids'],1)*100:.1f}%">{stats['partial_coverage_apps']}</div>
      <div class="bar-segment bar-none" style="width:{stats['no_coverage_apps']/max(stats['total_app_ids'],1)*100:.1f}%">{stats['no_coverage_apps']}</div>
    </div>
    <div class="bar-legend">
      <span class="legend-full">Full coverage (repos + Azure + catalog + monitoring): {stats['full_coverage_apps']}</span>
      <span class="legend-partial">Partial: {stats['partial_coverage_apps']}</span>
      <span class="legend-none">No tracking: {stats['no_coverage_apps']}</span>
    </div>
  </div>

  <!-- Controls -->
  <div class="controls">
    <input type="text" class="search-input" id="search" placeholder="Search by AppID, name, team, or subdomain...">
    <button class="filter-btn active" data-filter="all">All</button>
    <button class="filter-btn" data-filter="full">Full Coverage</button>
    <button class="filter-btn" data-filter="partial">Partial</button>
    <button class="filter-btn" data-filter="gaps">Has Gaps</button>
    <button class="filter-btn" data-filter="ci-failing">CI Failing</button>
    <button class="filter-btn" data-filter="kv-expiring">Secrets Expiring</button>
    <button class="filter-btn" data-filter="orphan">No Repos</button>
    <span class="result-count" id="resultCount"></span>
  </div>

  <!-- Main Table -->
  <div class="table-wrap">
    <table id="estateTable">
      <thead>
        <tr>
          <th data-col="app_id">AppID</th>
          <th data-col="app_name">Application</th>
          <th data-col="team">Team</th>
          <th data-col="repos">Repos</th>
          <th data-col="azure">Azure</th>
          <th data-col="dynatrace">DT</th>
          <th data-col="ci_success_rate">CI Health</th>
          <th data-col="monthly_cost">Cost/mo</th>
          <th data-col="kv_expiring">KV Expiry</th>
          <th>Sources</th>
          <th data-col="coverage">Coverage</th>
        </tr>
      </thead>
      <tbody id="tableBody"></tbody>
    </table>
  </div>

  <div class="footer">
    Foods Engineering Estate Report | Auto-generated by <code>data-scripts estate report</code><br>
    Data sources: Azure Resource Graph, Cost Management, Key Vault, GitHub Actions, Backstage catalog-info, PagerDuty, Dynatrace
  </div>
</div>

<script>
const APPS = {apps_json};

function ciPill(app) {{
  if (app.ci_status === 'none' || !app.ci_total_runs) return '<span class="ci-pill ci-none">N/A</span>';
  const cls = app.ci_status === 'green' ? 'ci-green' : app.ci_status === 'amber' ? 'ci-amber' : 'ci-red';
  return `<span class="ci-pill ${{cls}}">${{app.ci_success_rate}}%</span>`;
}}

function costCell(app) {{
  if (!app.monthly_cost) return '<span style="color:var(--text-muted)">-</span>';
  return `<span class="cost-val">&pound;${{app.monthly_cost.toLocaleString(undefined, {{minimumFractionDigits:0, maximumFractionDigits:0}})}}</span>`;
}}

function kvCell(app) {{
  if (!app.kv_secrets) return '<span style="color:var(--text-muted)">-</span>';
  if (app.kv_expiring > 0) return `<span class="kv-warn">${{app.kv_expiring}} &#9888;&#65039;</span>`;
  return `<span style="color:var(--green)">${{app.kv_secrets}} ok</span>`;
}}

function coverageBadge(score) {{
  const cls = score === 100 ? 'cov-100' : score >= 75 ? 'cov-75' : score >= 50 ? 'cov-50' : score >= 25 ? 'cov-25' : 'cov-0';
  return `<span class="cov-badge ${{cls}}">${{score}}%</span>`;
}}

function sourceDots(app) {{
  return [
    ['Repos', app.has_repos],
    ['Azure', app.has_azure],
    ['DT', app.has_dynatrace],
    ['Catalog', app.has_catalog],
  ].map(([label, ok]) => `<span class="cov-dot ${{ok ? 'cov-yes' : 'cov-no'}}" title="${{label}}: ${{ok ? 'Yes' : 'No'}}"></span>`).join('');
}}

let currentFilter = 'all';
let currentSort = {{ col: 'azure', dir: 'desc' }};
let searchTerm = '';

function matchesFilter(app) {{
  if (currentFilter === 'full') return app.coverage === 100;
  if (currentFilter === 'partial') return app.coverage > 0 && app.coverage < 100;
  if (currentFilter === 'gaps') return app.coverage < 100;
  if (currentFilter === 'orphan') return app.repos === 0;
  if (currentFilter === 'ci-failing') return app.ci_status === 'red';
  if (currentFilter === 'kv-expiring') return app.kv_expiring > 0;
  return true;
}}

function matchesSearch(app) {{
  if (!searchTerm) return true;
  const q = searchTerm.toLowerCase();
  return [app.app_id, app.app_name, app.team, app.subdomain].some(v => (v||'').toLowerCase().includes(q));
}}

function sortApps(apps) {{
  const col = currentSort.col;
  const dir = currentSort.dir === 'asc' ? 1 : -1;
  return [...apps].sort((a, b) => {{
    let va = a[col], vb = b[col];
    if (typeof va === 'number') return (va - vb) * dir;
    return String(va || '').localeCompare(String(vb || '')) * dir;
  }});
}}

function renderAlerts() {{
  const alerts = [];

  // Secrets expiring within 30 days
  const kvApps = APPS.filter(a => a.kv_expiring > 0);
  if (kvApps.length > 0) {{
    const total = kvApps.reduce((s, a) => s + a.kv_expiring, 0);
    alerts.push({{
      severity: 'red',
      title: `${{total}} secret(s) expired or expiring within 30 days`,
      detail: `Across ${{kvApps.length}} AppID(s): ${{kvApps.slice(0, 5).map(a => a.app_id).join(', ')}}${{kvApps.length > 5 ? '...' : ''}}`
    }});
  }}

  // CI failing for >3 days
  const ciRed = APPS.filter(a => a.ci_status === 'red');
  if (ciRed.length > 0) {{
    alerts.push({{
      severity: 'red',
      title: `${{ciRed.length}} AppID(s) with CI success rate below 70%`,
      detail: ciRed.slice(0, 5).map(a => `${{a.app_id}} (${{a.ci_success_rate}}%)`).join(', ') + (ciRed.length > 5 ? '...' : '')
    }});
  }}

  // Apps with no repo coverage
  const noRepos = APPS.filter(a => a.repos === 0 && a.azure > 0);
  if (noRepos.length > 0) {{
    alerts.push({{
      severity: 'yellow',
      title: `${{noRepos.length}} AppID(s) have Azure resources but no tracked repos`,
      detail: noRepos.slice(0, 5).map(a => a.app_id).join(', ') + (noRepos.length > 5 ? '...' : '')
    }});
  }}

  // No coverage at all
  const noCov = APPS.filter(a => a.coverage === 0);
  if (noCov.length > 0) {{
    alerts.push({{
      severity: 'yellow',
      title: `${{noCov.length}} AppID(s) with zero coverage across all sources`,
      detail: noCov.slice(0, 5).map(a => a.app_id).join(', ') + (noCov.length > 5 ? '...' : '')
    }});
  }}

  if (alerts.length === 0) {{
    alerts.push({{
      severity: 'green',
      title: 'No critical alerts',
      detail: 'All portfolio health checks are passing.'
    }});
  }}

  const grid = document.getElementById('alertGrid');
  grid.innerHTML = alerts.map(a => `
    <div class="alert-card alert-${{a.severity}}">
      <div class="alert-title">${{a.title}}</div>
      <div class="alert-detail">${{a.detail}}</div>
    </div>
  `).join('');
}}

function render() {{
  const filtered = APPS.filter(a => matchesFilter(a) && matchesSearch(a));
  const sorted = sortApps(filtered);
  const tbody = document.getElementById('tableBody');
  tbody.innerHTML = sorted.map(a => `<tr>
    <td><strong>${{a.app_id}}</strong></td>
    <td>${{a.app_name}}</td>
    <td>${{a.team}}</td>
    <td class="num">${{a.repos}}</td>
    <td class="num">${{a.azure.toLocaleString()}}</td>
    <td class="num">${{a.dynatrace}}</td>
    <td>${{ciPill(a)}}</td>
    <td class="num">${{costCell(a)}}</td>
    <td>${{kvCell(a)}}</td>
    <td>${{sourceDots(a)}}</td>
    <td>${{coverageBadge(a.coverage)}}</td>
  </tr>`).join('');
  document.getElementById('resultCount').textContent = `${{filtered.length}} of ${{APPS.length}} AppIDs`;

  document.querySelectorAll('thead th').forEach(th => {{
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.col === currentSort.col) th.classList.add('sorted-' + currentSort.dir);
  }});
}}

// Search
document.getElementById('search').addEventListener('input', e => {{ searchTerm = e.target.value; render(); }});

// Filters
document.querySelectorAll('.filter-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    render();
  }});
}});

// Sort
document.querySelectorAll('thead th[data-col]').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.col;
    if (currentSort.col === col) currentSort.dir = currentSort.dir === 'asc' ? 'desc' : 'asc';
    else {{ currentSort.col = col; currentSort.dir = 'desc'; }}
    render();
  }});
}});

renderAlerts();
render();
</script>
</body>
</html>"""


@click.group("estate")
def estate_group():
    """Unified estate reporting across all Foods data sources."""
    pass


@estate_group.command("report")
@click.option("--html", "html_output", default=str(OUTPUTS_DIR / "html" / "estate.html"),
              help="Output path for HTML dashboard.")
@click.option("--csv", "csv_output", default=str(OUTPUTS_DIR / "csv" / "estate_registry.csv"),
              help="Output path for estate CSV.")
@click.option("--verbose", is_flag=True, help="Show detailed progress.")
def report_command(html_output: str, csv_output: str, verbose: bool):
    """Generate unified estate dashboard from all data sources.

    Reads Azure scan, catalog-info scan, repos.csv, asset baseline,
    PagerDuty standards, and repo health data to produce:

    \\b
      1. An interactive HTML dashboard (outputs/html/estate.html)
      2. A CSV registry of all AppIDs with coverage scores

    \\b
    Run the individual scanners first:
      data-scripts azure scan
      data-scripts github catalog-scan
    """
    console.print("[bold]Generating Foods Estate Report...[/bold]\n")
    console.print("Loading data sources...")

    generator = EstateReportGenerator(verbose=verbose)
    generator.load_all_sources()

    stats = generator.generate_portfolio_stats()
    console.print("\n[bold cyan]Portfolio Summary[/bold cyan]")
    console.print(f"  AppIDs:            {stats['total_app_ids']}")
    console.print(f"  Repositories:      {stats['total_repos']}")
    console.print(f"  Azure Resources:   {stats['total_azure_resources']:,}")
    console.print(f"  Dynatrace:         {stats['total_dynatrace_services']}")
    console.print(f"  Databricks:        {stats['total_databricks_jobs']}")
    console.print(f"  PagerDuty:         {stats['total_pagerduty_services']}")
    console.print(f"  Catalog-info:      {stats['catalog_info_pct']}%")
    console.print(f"  Full coverage:     {stats['full_coverage_apps']}/{stats['total_app_ids']}")

    generator.generate_html_dashboard(html_output)
    generator.generate_estate_csv(csv_output)

    console.print("\n[bold green]Estate report complete.[/bold green]")
