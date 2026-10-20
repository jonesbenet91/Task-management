"""Heat Map Scoring Engine - aggregates all data sources into team-level scores.

Reads outputs from existing collectors (GitHub metrics, Jira backlog health,
PagerDuty standards/incidents, ITSM incidents/change requests, Dynatrace,
repo health, actions health) and the canonical org config to produce a
unified heat_map_scores.json file with scores across 7 dimensions:

  1. Delivery     - sprint velocity, throughput, cycle time
  2. System Health - Dynatrace alerts, PagerDuty service standards
  3. Incidents    - ITSM incidents, PagerDuty incidents, MTTR
  4. Code Quality - repo health score, PR hygiene, stale branches
  5. Estate Hygiene - catalog-info coverage, Azure cost, KV expiry
  6. Ops Readiness  - PagerDuty standards, runbooks, on-call coverage
  7. People       - vacancy ratio, team size, capacity indicators

Scores are 0-100 per dimension, aggregated: system > team > sub-domain > domain.
"""

import csv
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import yaml
from rich.console import Console
from rich.table import Table

from datascripts.utils import safe_float as _safe_float

console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"

if not DATA_DIR.exists():
    DATA_DIR = Path.cwd() / "data"
    OUTPUTS_DIR = Path.cwd() / "outputs"

DIMENSIONS = [
    "Delivery",
    "System Health",
    "Incidents",
    "Code Quality",
    "Estate Hygiene",
    "Ops Readiness",
    "People",
]


def _load_csv(path: Path, label: str, verbose: bool = False) -> List[dict]:
    """Safely load a CSV file, returning empty list if missing."""
    if not path.exists():
        if verbose:
            console.print(f"[dim]  {label}: not found at {path}[/dim]")
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if verbose:
        console.print(f"  {label}: {len(rows)} rows")
    return rows


def _load_yaml_config(path: Path) -> dict:
    """Load the canonical org YAML config."""
    if not path.exists():
        console.print(f"[red]Config not found: {path}[/red]")
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


class HeatMapScorer:
    """Aggregates all data source CSVs into per-team heat map scores."""

    def __init__(self, config_path: Path, verbose: bool = False):
        self.verbose = verbose
        self.config = _load_yaml_config(config_path)
        self.team_configs: Dict[str, dict] = {}
        self.subdomain_configs: Dict[str, dict] = {}
        self._parse_config()

        # Data sources
        self.backlog_health: List[dict] = []
        self.github_metrics: List[dict] = []
        self.repo_health: List[dict] = []
        self.pagerduty_standards: List[dict] = []
        self.pagerduty_incidents: List[dict] = []
        self.itsm_incidents: List[dict] = []
        self.itsm_changes: List[dict] = []
        self.dynatrace_services: List[dict] = []
        self.dynatrace_alerts: List[dict] = []
        self.catalog_info: List[dict] = []
        self.actions_health: List[dict] = []
        self.azure_costs: List[dict] = []
        self.kv_expiry: List[dict] = []
        
        # Match tracking for diagnostics
        self._match_log: Dict[str, Dict[str, Any]] = {}

    def _parse_config(self):
        """Parse YAML config into team/subdomain lookup maps."""
        if not self.config:
            return
        for sd in self.config.get("sub_domains", []):
            sd_id = sd.get("id", "")
            self.subdomain_configs[sd_id] = sd
            for team in sd.get("teams", []):
                team_id = team.get("id", "")
                team["subdomain_id"] = sd_id
                team["subdomain_name"] = sd.get("name", "")
                self.team_configs[team_id] = team

    def load_all_sources(self):
        """Load all available CSV data sources."""
        csv_dir = OUTPUTS_DIR / "csv"
        data_dir = DATA_DIR

        # Track CSV file timestamps for freshness reporting
        self._csv_timestamps: Dict[str, str] = {}

        def _load_and_track(path, label):
            rows = _load_csv(path, label, self.verbose)
            if path.exists():
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                self._csv_timestamps[label] = mtime.strftime("%d %b %Y %H:%M")
            return rows

        self.backlog_health = _load_and_track(csv_dir / "backlog_health.csv", "backlog_health")
        self.github_metrics = _load_and_track(csv_dir / "github_metrics.csv", "github_metrics")
        self.repo_health = _load_and_track(csv_dir / "repo_health.csv", "repo_health")
        self.pagerduty_standards = _load_and_track(csv_dir / "pagerduty_standard.csv", "pagerduty_standards")
        self.pagerduty_incidents = _load_and_track(csv_dir / "pagerduty_incidents.csv", "pagerduty_incidents")
        self.itsm_incidents = _load_and_track(csv_dir / "incident_dump.csv", "itsm_incidents")
        self.itsm_changes = _load_and_track(csv_dir / "change_requests_dump.csv", "itsm_changes")
        self.dynatrace_services = _load_and_track(csv_dir / "dynatrace_services.csv", "dynatrace_services")
        self.dynatrace_alerts = _load_and_track(csv_dir / "dynatrace_alerts.csv", "dynatrace_alerts")
        self.catalog_info = _load_and_track(csv_dir / "catalog_info.csv", "catalog_info")
        self.actions_health = _load_and_track(csv_dir / "actions_health.csv", "actions_health")
        self.azure_costs = _load_and_track(csv_dir / "azure_costs.csv", "azure_costs")
        self.kv_expiry = _load_and_track(csv_dir / "kv_expiry.csv", "kv_expiry")

        # Build AppID -> AppName mapping from repos.csv
        self.appid_to_appname: Dict[str, str] = {}
        repos_csv = _load_csv(data_dir / "repos.csv", "repos_mapping", self.verbose)
        for r in repos_csv:
            aid = (r.get("AppID", "") or "").strip()
            aname = (r.get("AppName", "") or "").strip().upper()
            if aid and aname:
                self.appid_to_appname[aid] = aname

    def _index_by_repo(self, rows: List[dict], key: str = "repo") -> Dict[str, List[dict]]:
        """Group rows by repository name (handles full URLs and short names)."""
        index: Dict[str, List[dict]] = defaultdict(list)
        for row in rows:
            repo_val = row.get(key, "") or row.get("Repository", "") or row.get("repository", "")
            if "/" in repo_val:
                repo_val = repo_val.rstrip("/").split("/")[-1]
            repo_val = repo_val.strip().lower()
            if repo_val:
                index[repo_val].append(row)
        return index

    def _index_by_project(self, rows: List[dict], key: str = "project_key") -> Dict[str, List[dict]]:
        """Group rows by Jira project key."""
        index: Dict[str, List[dict]] = defaultdict(list)
        for row in rows:
            pk = (row.get(key, "") or row.get("Project Key", "") or row.get("projectKey", "")).strip().upper()
            if pk:
                index[pk].append(row)
        return index

    def _index_by_pd_team(self, rows: List[dict]) -> Dict[str, List[dict]]:
        """Group rows by PagerDuty team ID."""
        index: Dict[str, List[dict]] = defaultdict(list)
        for row in rows:
            tid = (row.get("team_id", "") or row.get("Team ID", "")).strip()
            if tid:
                index[tid].append(row)
        return index

    def _index_by_appname(self, rows: List[dict]) -> Dict[str, List[dict]]:
        """Group rows by AppName (upper-cased for matching against systems)."""
        index: Dict[str, List[dict]] = defaultdict(list)
        for row in rows:
            app = (row.get("AppName", "") or row.get("appName", "") or row.get("app_name", "")).strip().upper()
            if app:
                index[app].append(row)
        return index

    def _index_by_pd_team_name(self, rows: List[dict]) -> Dict[str, List[dict]]:
        """Group rows by PagerDuty team_name (lowercased)."""
        index: Dict[str, List[dict]] = defaultdict(list)
        for row in rows:
            tn = (row.get("team_name", "") or row.get("Team Name", "")).strip().lower()
            if tn:
                index[tn].append(row)
        return index

    def _index_by_jira_team(self, rows: List[dict]) -> Dict[str, List[dict]]:
        """Group rows by Jira 'team' column (lowercased)."""
        index: Dict[str, List[dict]] = defaultdict(list)
        for row in rows:
            tn = (row.get("team", "") or row.get("Team", "")).strip().lower()
            if tn:
                index[tn].append(row)
        return index

    def _get_rows_for_systems(self, index_by_app: Dict[str, List[dict]], systems: list) -> List[dict]:
        """Retrieve all rows matching any of a team's system codes."""
        rows = []
        for sys_code in systems:
            key = sys_code.strip().upper()
            rows.extend(index_by_app.get(key, []))
        return rows

    def _print_match_diagnostics(self):
        """Print verbose diagnostics about team/data source matching."""
        console.print("\n[bold]Fuzzy Matching Diagnostics:[/bold]")

        unmatched_counts = defaultdict(int)
        matched_counts = defaultdict(int)
        teams_with_unmatched = []
        
        for team_id, log in self._match_log.items():
            team_cfg = self.team_configs.get(team_id, {})
            team_name = team_cfg.get("name", team_id)
            
            if log.get("unmatched"):
                teams_with_unmatched.append(team_id)
                for source, reason in log["unmatched"].items():
                    unmatched_counts[source] += 1
                    if self.verbose:
                        console.print(f"  [yellow]⚠ {team_name} ({source})[/yellow]: {reason}")
            
            for source in log.get("matched", {}):
                matched_counts[source] += 1
        
        # Print summary
        console.print("\n[bold]Match Summary:[/bold]")
        for source in ["delivery", "pagerduty"]:
            total_matched = matched_counts.get(source, 0)
            total_unmatched = unmatched_counts.get(source, 0)
            total = total_matched + total_unmatched
            if total > 0:
                rate = (total_matched / total) * 100
                icon = "[green]✓[/green]" if rate >= 70 else "[yellow]⚠[/yellow]"
                console.print(f"  {icon} {source.title()}: {total_matched}/{total} matched ({rate:.0f}%)")
                if rate < 70:
                    console.print("    [red]  WARNING: Match rate below 70%[/red]")

    def _match_pd_team(self, team_cfg: dict) -> List[dict]:
        """Match PagerDuty standards rows for a team via team_ids or team_name fuzzy match."""
        team_id = team_cfg.get("id", "unknown")
        if team_id not in self._match_log:
            self._match_log[team_id] = {"matched": {}, "unmatched": {}}
        
        rows = []
        pd_cfg = team_cfg.get("pagerduty", {})
        pd_team_ids = pd_cfg.get("team_ids", [])

        # Match by team_id first
        if pd_team_ids and self.pagerduty_standards:
            pd_index = self._index_by_pd_team(self.pagerduty_standards)
            for tid in pd_team_ids:
                found = pd_index.get(tid, [])
                if found:
                    rows.extend(found)
                    self._match_log[team_id]["matched"]["pd_by_team_id"] = tid

        # Fallback: match by system codes against team_name
        if not rows and self.pagerduty_standards:
            systems = team_cfg.get("systems", [])
            team_name = team_cfg.get("name", "").lower()
            pd_name_index = self._index_by_pd_team_name(self.pagerduty_standards)
            matched_by = None
            
            # Check system codes as substrings in PD team names
            for sys_code in systems:
                code_lower = sys_code.strip().lower()
                for pd_name, pd_rows in pd_name_index.items():
                    if code_lower in pd_name or pd_name in code_lower:
                        rows.extend(pd_rows)
                        matched_by = f"system_code_fuzzy:{sys_code}→{pd_name}"
                        break
                if rows:
                    break
            
            # Also match team name
            if not rows:
                for pd_name, pd_rows in pd_name_index.items():
                    if team_name in pd_name or pd_name in team_name:
                        rows.extend(pd_rows)
                        matched_by = f"team_name_fuzzy:{pd_name}"
                        break
            
            if rows and matched_by:
                self._match_log[team_id]["matched"]["pd_fuzzy"] = matched_by
            elif not rows and (team_name or systems):
                self._match_log[team_id]["unmatched"]["pagerduty"] = f"team:{team_name},systems:{systems}"

        return rows

    def _score_delivery(self, team_cfg: dict) -> float:
        """Score delivery from Jira backlog health data (per-ticket rows)."""
        team_id = team_cfg.get("id", "unknown")
        if team_id not in self._match_log:
            self._match_log[team_id] = {"matched": {}, "unmatched": {}}
        
        if not self.backlog_health:
            return 50.0

        rows = []

        # Strategy 1: Match by Jira project_key
        jira_cfg = team_cfg.get("jira", {})
        pk = jira_cfg.get("project_key", "")
        if pk:
            project_index = self._index_by_project(self.backlog_health)
            rows.extend(project_index.get(pk.upper(), []))
            if rows:
                self._match_log[team_id]["matched"]["delivery_by_project"] = pk.upper()

        # Strategy 2: Match by 'team' column (fuzzy name match)
        if not rows:
            jira_team_index = self._index_by_jira_team(self.backlog_health)
            team_name = team_cfg.get("name", "").lower()
            systems = team_cfg.get("systems", [])
            matched_by = None
            
            # Direct team name match
            for jt_name, jt_rows in jira_team_index.items():
                if team_name in jt_name or jt_name in team_name:
                    rows.extend(jt_rows)
                    matched_by = f"team_name_fuzzy:{jt_name}"
                    break
            # System code match against team column
            if not rows:
                for sys_code in systems:
                    code_lower = sys_code.strip().lower()
                    for jt_name, jt_rows in jira_team_index.items():
                        if code_lower in jt_name or jt_name in code_lower:
                            rows.extend(jt_rows)
                            matched_by = f"system_code_fuzzy:{sys_code}→{jt_name}"
                            break
                    if rows:
                        break
            
            if rows and matched_by:
                self._match_log[team_id]["matched"]["delivery_fuzzy"] = matched_by
            elif not rows and (team_name or systems):
                self._match_log[team_id]["unmatched"]["delivery"] = f"team:{team_name},systems:{systems}"

        if not rows:
            return 50.0

        # Aggregate per-ticket data into a delivery score
        total = len(rows)
        blocked = sum(1 for r in rows if (r.get("status", "")).lower() in ("blocked",))
        overdue_threshold = 90  # days
        overdue = sum(1 for r in rows if _safe_float(r.get("days_old", 0)) > overdue_threshold)
        quality_scores = [_safe_float(r.get("quality_score", 0)) for r in rows if _safe_float(r.get("quality_score", 0)) > 0]
        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 50.0

        blocked_ratio = (blocked / total) * 100
        overdue_ratio = (overdue / total) * 100

        # Blend: quality score weight (0.6) + blockers/overdue penalty (0.4)
        health_score = 100 - (blocked_ratio * 2) - (overdue_ratio * 1.5)
        score = (avg_quality * 0.6) + (health_score * 0.4)
        return _clamp(score)

    def _score_system_health(self, team_cfg: dict) -> float:
        """Score system health from Dynatrace + PagerDuty standards."""
        scores = []
        systems = team_cfg.get("systems", [])

        # PagerDuty standards score (via team_ids or team_name matching)
        pd_rows = self._match_pd_team(team_cfg)
        for row in pd_rows:
            raw_score = _safe_float(row.get("overall_score", row.get("standards_score", row.get("Score", ""))))
            std_score = raw_score * 100 if raw_score <= 1.0 else raw_score
            if std_score > 0:
                scores.append(_clamp(std_score))

        # Dynatrace service health (match by system codes)
        if systems and self.dynatrace_services:
            systems_lower = [s.strip().lower() for s in systems]
            for svc in self.dynatrace_services:
                svc_name = (svc.get("name", "") or svc.get("displayName", "")).lower()
                if any(s in svc_name for s in systems_lower):
                    health = svc.get("healthStatus", svc.get("health_status", ""))
                    if health.upper() in ("HEALTHY", "OK", "GREEN"):
                        scores.append(90.0)
                    elif health.upper() in ("WARNING", "AMBER"):
                        scores.append(60.0)
                    else:
                        scores.append(30.0)

        return sum(scores) / len(scores) if scores else 50.0

    def _score_incidents(self, team_cfg: dict) -> float:
        """Score incidents (fewer = better) from ITSM + PagerDuty."""
        scores = []
        systems = team_cfg.get("systems", [])

        # PagerDuty incidents (via team_ids or team_name matching)
        pd_cfg = team_cfg.get("pagerduty", {})
        pd_team_ids = pd_cfg.get("team_ids", [])
        matched_pd = False

        if pd_team_ids and self.pagerduty_incidents:
            pd_index = self._index_by_pd_team(self.pagerduty_incidents)
            for tid in pd_team_ids:
                rows = pd_index.get(tid, [])
                if rows:
                    matched_pd = True
                    count = len(rows)
                    score = max(20, 95 - (count * 4))
                    scores.append(_clamp(score))

        # Fallback: match PD incidents by team_name
        if not matched_pd and self.pagerduty_incidents:
            pd_name_index = self._index_by_pd_team_name(self.pagerduty_incidents)
            team_name = team_cfg.get("name", "").lower()
            for sys_code in systems:
                code_lower = sys_code.strip().lower()
                for pd_name, pd_rows in pd_name_index.items():
                    if code_lower in pd_name or pd_name in code_lower:
                        count = len(pd_rows)
                        score = max(20, 95 - (count * 4))
                        scores.append(_clamp(score))
            for pd_name, pd_rows in pd_name_index.items():
                if team_name in pd_name or pd_name in team_name:
                    count = len(pd_rows)
                    score = max(20, 95 - (count * 4))
                    scores.append(_clamp(score))

        # ITSM incidents (match by team name or system codes in assignment_group)
        if self.itsm_incidents:
            team_name = team_cfg.get("name", "").lower()
            systems_lower = [s.strip().lower() for s in systems]
            team_incidents = [
                r for r in self.itsm_incidents
                if any(
                    needle in (r.get("assignment_group", "") or r.get("Assigned Group", "") or r.get("assigned_group", "")).lower()
                    for needle in [team_name] + systems_lower
                )
            ]
            if team_incidents:
                count = len(team_incidents)
                score = max(20, 95 - (count * 3))
                scores.append(_clamp(score))

        return sum(scores) / len(scores) if scores else 50.0

    def _score_code_quality(self, team_cfg: dict) -> float:
        """Score code quality from repo health data."""
        systems = team_cfg.get("systems", [])
        repos = [r.split("/")[-1].lower() for r in team_cfg.get("github", {}).get("repos", [])]

        if not self.repo_health and not self.actions_health:
            return 50.0

        scores = []

        # Match repo_health by AppName (system codes) or by repo name
        if self.repo_health:
            app_index = self._index_by_appname(self.repo_health)
            matched_rows = self._get_rows_for_systems(app_index, systems)
            # Also match by explicit repos
            if repos:
                repo_index = self._index_by_repo(self.repo_health)
                for repo_name in repos:
                    matched_rows.extend(repo_index.get(repo_name, []))

            for row in matched_rows:
                stale_prs = _safe_float(row.get("StalePRCount", row.get("stale_prs", 0)))
                stale_branches = _safe_float(row.get("StaleBranchCount", row.get("stale_branches", 0)))
                open_prs = _safe_float(row.get("OpenPRCount", row.get("open_prs", 0)))
                penalty = (stale_prs * 5) + (stale_branches * 2) + max(0, (open_prs - 5) * 3)
                scores.append(_clamp(90 - penalty))

        # Actions health by AppName or repo
        if self.actions_health:
            app_index = self._index_by_appname(self.actions_health)
            matched_rows = self._get_rows_for_systems(app_index, systems)
            if repos:
                ah_index = self._index_by_repo(self.actions_health)
                for repo_name in repos:
                    matched_rows.extend(ah_index.get(repo_name, []))
            for row in matched_rows:
                success_rate = _safe_float(row.get("success_rate", row.get("Success Rate", "")))
                if success_rate > 0:
                    scores.append(_clamp(success_rate))

        return sum(scores) / len(scores) if scores else 50.0

    def _score_estate_hygiene(self, team_cfg: dict) -> float:
        """Score estate hygiene from catalog-info coverage."""
        if not self.catalog_info:
            return 50.0

        systems = team_cfg.get("systems", [])
        systems_upper = [s.strip().upper() for s in systems]
        if not systems_upper:
            return 50.0

        # Find catalog_info rows belonging to this team's systems.
        # catalog_info uses app_id (e.g. A2001), which maps to AppName via repos.csv.
        # Build reverse lookup: AppName -> set of AppIDs
        appname_to_ids: Dict[str, set] = defaultdict(set)
        for aid, aname in self.appid_to_appname.items():
            appname_to_ids[aname].add(aid)

        # Collect AppIDs for this team's systems
        team_app_ids: set = set()
        for sys_code in systems_upper:
            for aid in appname_to_ids.get(sys_code, set()):
                team_app_ids.add(aid)

        if not team_app_ids:
            return 50.0

        # Find catalog_info rows for those AppIDs
        team_repos = []
        for row in self.catalog_info:
            # system_ref is like "ap-A2001" or just "A2001"
            sys_ref = (row.get("system_ref", "") or "").replace("ap-", "")
            app_id = (row.get("app_id", "") or "").strip()
            if sys_ref in team_app_ids or app_id in team_app_ids:
                team_repos.append(row)

        if not team_repos:
            return 50.0

        # Score: % of repos that have catalog-info.yaml
        total = len(team_repos)
        has_ci = sum(1 for r in team_repos if r.get("has_catalog_info", "").lower() in ("true", "yes", "1"))
        coverage = (has_ci / total) * 100 if total > 0 else 50
        return _clamp(coverage)

    def _score_ops_readiness(self, team_cfg: dict) -> float:
        """Score ops readiness from PagerDuty standards + service config."""
        pd_rows = self._match_pd_team(team_cfg)
        if not pd_rows:
            return 50.0

        scores = []
        for row in pd_rows:
            raw_score = _safe_float(row.get("overall_score", row.get("standards_score", row.get("Score", ""))))
            std_score = raw_score * 100 if raw_score <= 1.0 else raw_score
            if std_score > 0:
                scores.append(_clamp(std_score))

        return sum(scores) / len(scores) if scores else 50.0

    def _score_people(self, team_cfg: dict) -> float:
        """Score people/capacity based on team config (vacancies, size)."""
        people = team_cfg.get("people", {})
        staff = people.get("staff_engineers", [])
        seniors = people.get("senior_engineers", [])
        engineers = people.get("engineers", [])
        tpms = people.get("tpms", [])
        qa = people.get("qa", [])

        all_people = staff + seniors + engineers + tpms + qa
        total = len(all_people)
        vacancies = sum(1 for p in all_people if "vacancy" in str(p).lower())

        if total == 0:
            return 50.0

        filled_ratio = ((total - vacancies) / total) * 100
        # Small teams get a slight penalty (bus factor risk)
        size_bonus = min(10, (total - vacancies) * 2) if (total - vacancies) < 5 else 10
        return _clamp(filled_ratio * 0.85 + size_bonus)

    def score_team(self, team_id: str) -> Dict[str, Any]:
        """Compute all dimension scores for a single team."""
        team_cfg = self.team_configs.get(team_id, {})
        scores = {
            "Delivery": round(self._score_delivery(team_cfg), 1),
            "System Health": round(self._score_system_health(team_cfg), 1),
            "Incidents": round(self._score_incidents(team_cfg), 1),
            "Code Quality": round(self._score_code_quality(team_cfg), 1),
            "Estate Hygiene": round(self._score_estate_hygiene(team_cfg), 1),
            "Ops Readiness": round(self._score_ops_readiness(team_cfg), 1),
            "People": round(self._score_people(team_cfg), 1),
        }
        # Track which dimensions have real data vs default sentinel (50.0)
        has_data = {dim: val != 50.0 for dim, val in scores.items()}
        # People always has data (derived from YAML config)
        has_data["People"] = True
        
        # Calculate average excluding dimensions without data
        dims_with_data = [dim for dim, has_d in has_data.items() if has_d]
        if dims_with_data:
            avg = round(sum(scores[dim] for dim in dims_with_data) / len(dims_with_data), 1)
        else:
            avg = round(sum(scores.values()) / len(scores), 1)
        
        return {
            "team_id": team_id,
            "team_name": team_cfg.get("name", team_id),
            "subdomain_id": team_cfg.get("subdomain_id", ""),
            "subdomain_name": team_cfg.get("subdomain_name", ""),
            "scores": scores,
            "has_data": has_data,
            "overall": avg,
        }

    def score_all(self) -> Dict[str, Any]:
        """Compute scores for all teams, sub-domains, and domain."""
        teams = []
        for team_id in self.team_configs:
            teams.append(self.score_team(team_id))
        
        # Print verbose match diagnostics if enabled
        if self.verbose:
            self._print_match_diagnostics()

        # Aggregate sub-domains
        subdomains = []
        for sd_id, sd_cfg in self.subdomain_configs.items():
            sd_teams = [t for t in teams if t["subdomain_id"] == sd_id]
            if not sd_teams:
                continue
            sd_scores = {}
            for dim in DIMENSIONS:
                vals = [t["scores"][dim] for t in sd_teams]
                sd_scores[dim] = round(sum(vals) / len(vals), 1)
            sd_avg = round(sum(sd_scores.values()) / len(sd_scores), 1)
            subdomains.append({
                "subdomain_id": sd_id,
                "subdomain_name": sd_cfg.get("name", sd_id),
                "team_count": len(sd_teams),
                "scores": sd_scores,
                "overall": sd_avg,
            })

        # Domain aggregate
        all_scores = {}
        for dim in DIMENSIONS:
            vals = [t["scores"][dim] for t in teams]
            all_scores[dim] = round(sum(vals) / len(vals), 1) if vals else 50.0
        domain_avg = round(sum(all_scores.values()) / len(all_scores), 1)

        # Leadership from config
        leadership = []
        for leader in self.config.get("leadership", []):
            leadership.append({
                "name": leader.get("name", ""),
                "role": leader.get("role", ""),
            })

        timestamp = datetime.now(timezone.utc).isoformat()
        return {
            "generated_at": timestamp,
            "week": datetime.now(timezone.utc).strftime("W%V %G"),
            "domain": {
                "name": self.config.get("domain", "Foods D&T"),
                "scores": all_scores,
                "overall": domain_avg,
            },
            "subdomains": subdomains,
            "teams": teams,
            "leadership": leadership,
            "dimensions": DIMENSIONS,
            "data_sources": {
                k: {"rows": v, "updated": self._csv_timestamps.get(k, "unknown")}
                for k, v in {
                    "backlog_health": len(self.backlog_health),
                    "github_metrics": len(self.github_metrics),
                    "repo_health": len(self.repo_health),
                    "pagerduty_standards": len(self.pagerduty_standards),
                    "pagerduty_incidents": len(self.pagerduty_incidents),
                    "itsm_incidents": len(self.itsm_incidents),
                    "itsm_changes": len(self.itsm_changes),
                    "dynatrace_services": len(self.dynatrace_services),
                    "catalog_info": len(self.catalog_info),
                    "actions_health": len(self.actions_health),
                }.items()
            },
        }


@click.command("heatmap")
@click.option(
    "--config",
    "config_path",
    default="data/foods_org_config.yaml",
    show_default=True,
    help="Path to the canonical org config YAML.",
)
@click.option(
    "--output",
    "output_path",
    default="outputs/json/heat_map_scores.json",
    show_default=True,
    help="Output path for the JSON scores file.",
)
@click.option("--verbose", is_flag=True, help="Verbose output.")
def heatmap_command(config_path: str, output_path: str, verbose: bool):
    """Generate heat map scores from all data sources.

    Reads existing CSV outputs from the daily pipeline and the canonical org
    config, then produces a unified heat_map_scores.json with per-team,
    per-subdomain, and domain-level scores across 7 dimensions.

    \b
    Examples:
        data-scripts heatmap
        data-scripts heatmap --verbose
        data-scripts heatmap --config data/foods_org_config.yaml --output outputs/json/heat_map_scores.json
    """
    console.print("[bold]Heat Map Scoring Engine[/bold]")
    console.print(f"Config: {config_path}")

    scorer = HeatMapScorer(Path(config_path), verbose=verbose)
    console.print(f"  Teams in config: {len(scorer.team_configs)}")
    console.print(f"  Sub-domains: {len(scorer.subdomain_configs)}")

    console.print("\n[bold]Loading data sources...[/bold]")
    scorer.load_all_sources()

    console.print("\n[bold]Scoring...[/bold]")
    result = scorer.score_all()

    # Write JSON - versioned + latest
    week = result.get("week", datetime.now(timezone.utc).strftime("W%V_%G"))
    week_slug = week.replace(" ", "_")

    # Versioned directory: outputs/json/heat_map_scores/{week}.json
    versioned_dir = Path("outputs/json/heat_map_scores")
    versioned_dir.mkdir(parents=True, exist_ok=True)
    versioned_path = versioned_dir / f"{week_slug}.json"
    with open(versioned_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    console.print(f"[green]Wrote versioned scores to {versioned_path}[/green]")

    # Latest copy (backward compat)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    console.print(f"[green]Wrote latest scores to {out_path}[/green]")

    # Update index.json (list of all available weeks)
    index_path = versioned_dir / "index.json"
    existing_weeks = []
    if index_path.exists():
        try:
            existing_weeks = json.loads(index_path.read_text())
        except (json.JSONDecodeError, ValueError):
            existing_weeks = []
    if week_slug not in existing_weeks:
        existing_weeks.append(week_slug)
        existing_weeks.sort()
    index_path.write_text(json.dumps(existing_weeks, indent=2))
    console.print(f"[green]Updated index: {len(existing_weeks)} weeks tracked[/green]")

    # Summary table
    table = Table(title="Heat Map Summary")
    table.add_column("Team", style="bold")
    table.add_column("Sub-domain", style="dim")
    for dim in DIMENSIONS:
        table.add_column(dim[:8], justify="center")
    table.add_column("Overall", justify="center", style="bold")

    for team in sorted(result["teams"], key=lambda t: t["overall"], reverse=True):
        row = [team["team_name"], team["subdomain_name"]]
        for dim in DIMENSIONS:
            v = team["scores"][dim]
            colour = "green" if v >= 75 else "yellow" if v >= 50 else "red"
            row.append(f"[{colour}]{v}[/{colour}]")
        ov = team["overall"]
        colour = "green" if ov >= 75 else "yellow" if ov >= 50 else "red"
        row.append(f"[{colour}]{ov}[/{colour}]")
        table.add_row(*row)

    console.print(table)
    console.print(f"\n[bold]Domain overall:[/bold] {result['domain']['overall']}")

    # Generate the HTML dashboard (latest + versioned)
    _generate_dashboard_html(result, verbose)


def _generate_dashboard_html(scores: dict, verbose: bool = False):
    """Generate the interactive heat map HTML dashboard from scores."""
    week = scores.get("week", "latest")
    week_slug = week.replace(" ", "_")

    # Build the HTML
    template_path = Path(__file__).resolve().parents[3] / "outputs" / "html" / "heatmap_template.html"
    if template_path.exists():
        with open(template_path, encoding="utf-8") as f:
            template = f.read()
        scores_json = json.dumps(scores).replace('</','<\\/')
        html = template.replace("__SCORES_DATA__", scores_json)
    else:
        scores_json = json.dumps(scores).replace('</','<\\/')
        html = _build_standalone_html(scores_json)

    # Write latest (backward compat)
    html_path = OUTPUTS_DIR / "html" / "heatmap.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    console.print(f"[green]Wrote dashboard to {html_path}[/green]")

    # Write versioned copy (adjust nav links for subdirectory)
    versioned_dir = OUTPUTS_DIR / "html" / "heatmap"
    versioned_dir.mkdir(parents=True, exist_ok=True)
    versioned_path = versioned_dir / f"{week_slug}.html"
    # Versioned pages live one level deeper, so relative nav links need ../
    versioned_html = re.sub(
        r'href="([a-z][a-z0-9_]*\.html)"',
        r'href="../\1"',
        html,
    )
    with open(versioned_path, "w", encoding="utf-8") as f:
        f.write(versioned_html)
    console.print(f"[green]Wrote versioned dashboard to {versioned_path}[/green]")


def _build_standalone_html(scores_json: str) -> str:
    """Build the complete standalone HTML dashboard."""
    from ..services.site_nav import NAV_CSS, render_nav
    nav_css = NAV_CSS
    nav_html = render_nav("heatmap.html")
    # This embeds the scores data directly into the HTML
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Foods Engineering Vitals - Foods D&T</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<script>window.d3 || document.write('<script src="./d3.min.js"><\\/script>')</script>
<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
<script>window.ApexCharts || document.write('<script src="./apexcharts.min.js"><\\/script>')</script>
<style>
:root {{
  --bg: #0a0a0f; --bg2: #111118;
  --surface: rgba(22,22,32,0.72); --surface-solid: #16161f;
  --surface-hover: rgba(30,30,45,0.85);
  --glass: rgba(255,255,255,0.03); --glass-border: rgba(255,255,255,0.06);
  --border: rgba(255,255,255,0.08);
  --text: #e8e8ef; --text2: #9898a8; --text3: #5c5c6e;
  --accent: #6366f1; --accent2: #818cf8;
  --green: #34d399; --amber: #fbbf24; --red: #f87171;
  --green-bg: rgba(52,211,153,0.1); --amber-bg: rgba(251,191,36,0.1); --red-bg: rgba(248,113,113,0.1);
  --radius: 16px; --radius-sm: 10px; --radius-xs: 6px;
  --shadow: 0 4px 24px rgba(0,0,0,0.3); --shadow-lg: 0 8px 40px rgba(0,0,0,0.4);
  --transition: 0.25s cubic-bezier(0.4,0,0.2,1);
}}
html[data-theme="light"] {{
  --bg: #f0f0f5; --bg2: #e8e8ef;
  --surface: rgba(255,255,255,0.72); --surface-solid: #ffffff;
  --surface-hover: rgba(255,255,255,0.9);
  --glass: rgba(255,255,255,0.5); --glass-border: rgba(0,0,0,0.06);
  --border: rgba(0,0,0,0.08);
  --text: #1a1a2e; --text2: #5a5a72; --text3: #9898a8;
  --shadow: 0 4px 24px rgba(0,0,0,0.08); --shadow-lg: 0 8px 40px rgba(0,0,0,0.12);
}}
*,*::before,*::after {{ margin:0;padding:0;box-sizing:border-box; }}
html {{ scroll-behavior:smooth; }}
body {{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',system-ui,sans-serif;
  background:var(--bg); color:var(--text); letter-spacing:-0.3px; min-height:100vh; overflow-x:hidden;
  padding-top:48px;
}}
body::before {{
  content:''; position:fixed; inset:0; z-index:-1;
  background: radial-gradient(ellipse 80% 60% at 20% 10%,rgba(99,102,241,0.08),transparent),
    radial-gradient(ellipse 60% 50% at 80% 80%,rgba(52,211,153,0.06),transparent),
    radial-gradient(ellipse 70% 40% at 50% 50%,rgba(248,113,113,0.04),transparent);
  animation:bgShift 20s ease-in-out infinite alternate;
}}
@keyframes bgShift {{ 0%{{opacity:.7;transform:scale(1)}} 100%{{opacity:1;transform:scale(1.05)}} }}
/* Site-wide nav bar (consistent with other dashboards) */
{nav_css}
nav {{
  position:sticky;top:0;z-index:100;display:flex;align-items:center;justify-content:space-between;
  padding:12px 32px;background:var(--surface);backdrop-filter:blur(20px) saturate(180%);
  -webkit-backdrop-filter:blur(20px) saturate(180%);border-bottom:1px solid var(--glass-border);
}}
.nav-left{{display:flex;align-items:center;gap:16px}}
.nav-logo{{width:36px;height:36px;background:linear-gradient(135deg,var(--accent),var(--green));border-radius:10px;display:grid;place-items:center;font-weight:800;font-size:16px;color:#fff}}
.nav-title{{font-weight:700;font-size:18px}}
.nav-sub{{color:var(--text2);font-size:13px;font-weight:400}}
.nav-tabs{{display:flex;gap:4px}}
.nav-tab{{padding:8px 18px;border-radius:8px;font-size:13px;font-weight:500;color:var(--text2);cursor:pointer;transition:var(--transition);border:none;background:transparent}}
.nav-tab:hover{{color:var(--text);background:var(--glass)}}
.nav-tab.active{{color:var(--text);background:rgba(99,102,241,0.15);box-shadow:inset 0 0 0 1px rgba(99,102,241,0.3)}}
.theme-toggle{{width:36px;height:36px;border-radius:10px;border:1px solid var(--border);background:var(--glass);color:var(--text);cursor:pointer;font-size:16px;display:grid;place-items:center;transition:var(--transition)}}
.theme-toggle:hover{{background:var(--surface-hover)}}
.container{{max-width:1440px;margin:0 auto;padding:24px 32px}}
.view{{display:none;animation:fadeIn .4s ease}} .view.active{{display:block}}
@keyframes fadeIn{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
.kpi-strip{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:28px}}
.kpi-card{{background:var(--surface);backdrop-filter:blur(16px);border:1px solid var(--glass-border);border-radius:var(--radius);padding:20px;transition:var(--transition)}}
.kpi-card:hover{{transform:translateY(-2px);box-shadow:var(--shadow)}}
.kpi-label{{font-size:12px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}}
.kpi-row{{display:flex;align-items:flex-end;justify-content:space-between}}
.kpi-value{{font-size:32px;font-weight:800;line-height:1}}
.kpi-spark{{width:80px;height:36px}}
.kpi-change{{font-size:12px;font-weight:600;margin-top:6px}}
.kpi-change.up{{color:var(--green)}} .kpi-change.down{{color:var(--red)}}
.subdomain-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-bottom:28px}}
.subdomain-card{{background:var(--surface);backdrop-filter:blur(16px);border:1px solid var(--glass-border);border-radius:var(--radius);padding:24px;transition:var(--transition);cursor:pointer}}
.subdomain-card:hover{{transform:translateY(-3px);box-shadow:var(--shadow-lg);border-color:rgba(99,102,241,0.2)}}
.subdomain-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px}}
.subdomain-name{{font-size:15px;font-weight:700}} .subdomain-teams{{font-size:12px;color:var(--text3);margin-top:2px}}
.subdomain-score{{font-size:28px;font-weight:800}}
.subdomain-chart{{height:160px;margin:0 -8px}}
.subdomain-dims{{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-top:14px}}
.subdomain-dim{{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text2)}}
.subdomain-dim .dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.subdomain-dim .dim-val{{margin-left:auto;font-weight:600;font-size:12px}}
.heatmap-section{{background:var(--surface);backdrop-filter:blur(16px);border:1px solid var(--glass-border);border-radius:var(--radius);padding:24px}}
.section-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}}
.section-title{{font-size:16px;font-weight:700}} .section-sub{{font-size:12px;color:var(--text3)}}
#heatmap-container svg{{width:100%}} #heatmap-container text{{font-family:inherit}}
.dim-tabs{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:24px}}
.dim-tab{{padding:6px 16px;border-radius:20px;font-size:12px;font-weight:500;color:var(--text2);border:1px solid var(--border);background:transparent;cursor:pointer;transition:var(--transition)}}
.dim-tab:hover{{border-color:var(--accent);color:var(--text)}}
.dim-tab.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.ranked-list{{display:flex;flex-direction:column;gap:10px}}
.ranked-item{{display:grid;grid-template-columns:32px 1.2fr 80px 1fr 60px 50px;align-items:center;gap:12px;background:var(--surface);backdrop-filter:blur(16px);border:1px solid var(--glass-border);border-radius:var(--radius-sm);padding:14px 18px;transition:var(--transition);cursor:pointer}}
.ranked-item:hover{{transform:translateX(4px);border-color:rgba(99,102,241,0.2)}}
.rank{{font-size:14px;font-weight:700;color:var(--text3);text-align:center}}
.ranked-team{{font-size:13px;font-weight:600}} .ranked-sub{{font-size:11px;color:var(--text3)}}
.ranked-spark{{height:32px}}
.ranked-bar-wrap{{height:6px;background:var(--glass);border-radius:3px;overflow:hidden}}
.ranked-bar{{height:100%;border-radius:3px;transition:width .8s cubic-bezier(.4,0,.2,1)}}
.ranked-score{{font-size:18px;font-weight:800;text-align:right}}
.ranked-trend{{font-size:11px;font-weight:600;text-align:center;padding:2px 8px;border-radius:12px}}
.trend-up{{color:var(--green);background:var(--green-bg)}}
.trend-down{{color:var(--red);background:var(--red-bg)}}
.trend-flat{{color:var(--amber);background:var(--amber-bg)}}
.people-section{{margin-bottom:24px}} .people-title{{font-size:14px;font-weight:700;margin-bottom:14px;color:var(--text2)}}
.people-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}}
.person-card{{display:flex;align-items:center;gap:12px;background:var(--surface);border:1px solid var(--glass-border);border-radius:var(--radius-sm);padding:14px 16px;transition:var(--transition)}}
.person-card:hover{{transform:translateY(-2px);box-shadow:var(--shadow)}}

.person-avatar{{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;font-weight:700;font-size:14px;color:#fff;flex-shrink:0}}
.person-name{{font-size:13px;font-weight:600}} .person-role{{font-size:11px;color:var(--text3)}}
.modal-overlay{{position:fixed;inset:0;z-index:200;background:rgba(0,0,0,0.6);backdrop-filter:blur(8px);display:none;place-items:center;animation:fadeIn .2s ease}}
.modal-overlay.open{{display:grid}}
.modal{{background:var(--surface-solid);border:1px solid var(--glass-border);border-radius:20px;width:90%;max-width:640px;max-height:85vh;overflow-y:auto;padding:32px;animation:modalIn .3s cubic-bezier(.4,0,.2,1)}}
@keyframes modalIn{{from{{opacity:0;transform:scale(.95)}}to{{opacity:1;transform:none}}}}
.modal-close{{float:right;width:32px;height:32px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--text);font-size:18px;cursor:pointer;display:grid;place-items:center}}
.modal-close:hover{{background:var(--glass)}}
.modal h2{{font-size:20px;font-weight:700;margin-bottom:4px}}
.modal .modal-sub{{font-size:13px;color:var(--text2);margin-bottom:20px}}
.modal-dims{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.modal-dim-card{{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--radius-sm);padding:14px}}
.modal-dim-label{{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}}
.modal-dim-score{{font-size:24px;font-weight:800}}
.modal-dim-spark{{height:40px;margin-top:8px}}
.data-sources{{margin-top:20px;padding:16px;background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--radius-sm)}}
.data-sources-title{{font-size:12px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}}
.ds-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px}}
.ds-item{{font-size:11px;color:var(--text2);display:flex;justify-content:space-between}}
.ds-item .ds-count{{font-weight:700;color:var(--text)}}
.hm-tooltip{{position:fixed;z-index:10000;pointer-events:none;opacity:0;transition:opacity 0.15s;max-width:320px;padding:14px 16px;border-radius:12px;background:var(--surface-solid);border:1px solid var(--border);box-shadow:0 12px 32px rgba(0,0,0,0.4);font-size:12px;line-height:1.5;color:var(--text)}}
.hm-tooltip.visible{{opacity:1}}
.hm-tooltip .tt-header{{display:flex;align-items:center;gap:8px;margin-bottom:8px}}
.hm-tooltip .tt-dim{{font-weight:700;font-size:13px}}
.hm-tooltip .tt-score{{font-weight:800;font-size:18px;margin-left:auto}}
.hm-tooltip .tt-team{{font-size:11px;color:var(--text2);margin-bottom:6px}}
.hm-tooltip .tt-desc{{color:var(--text2);margin-bottom:6px}}
.hm-tooltip .tt-meter{{height:4px;border-radius:2px;background:var(--glass);margin-bottom:6px}}
.hm-tooltip .tt-meter-fill{{height:100%;border-radius:2px;transition:width 0.3s}}
.hm-tooltip .tt-label{{font-size:10px;color:var(--text2);font-style:italic}}
.hm-tooltip .tt-sources{{font-size:10px;color:var(--text2);margin-top:4px;padding-top:4px;border-top:1px solid var(--border)}}
.subdomain-card{{cursor:pointer;transition:var(--transition)}}
.subdomain-card:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,0.15)}}
.subdomain-card.sd-active{{ring:2px solid var(--accent);outline:2px solid var(--accent);outline-offset:2px}}
.kpi-card{{cursor:pointer;transition:var(--transition)}}
.kpi-card:hover{{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,0.12)}}
.sd-filter-bar{{display:none;align-items:center;gap:10px;padding:10px 16px;margin-bottom:12px;background:var(--glass);border-radius:10px;border:1px solid var(--border)}}
.sd-filter-bar.visible{{display:flex}}
.sd-filter-bar .filter-label{{font-size:12px;color:var(--text2)}}
.sd-filter-bar .filter-name{{font-size:13px;font-weight:700;color:var(--accent)}}
.sd-filter-bar .filter-clear{{margin-left:auto;font-size:11px;color:var(--text2);cursor:pointer;padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:transparent;transition:var(--transition)}}
.sd-filter-bar .filter-clear:hover{{background:var(--glass);color:var(--text)}}
::-webkit-scrollbar{{width:6px}} ::-webkit-scrollbar-track{{background:transparent}} ::-webkit-scrollbar-thumb{{background:var(--border);border-radius:3px}}
@media(max-width:1024px){{.kpi-strip{{grid-template-columns:repeat(2,1fr)}}.subdomain-grid{{grid-template-columns:1fr}}.ranked-item{{grid-template-columns:32px 1fr 60px 50px}}.ranked-spark,.ranked-bar-wrap{{display:none}}}}
@media(max-width:640px){{nav{{padding:12px 16px;flex-wrap:wrap;gap:8px}}.container{{padding:16px}}.kpi-strip{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
{nav_html}
<nav>
  <div class="nav-left">
    <div class="nav-logo">F</div>
    <div>
      <div class="nav-title">Foods Engineering Vitals</div>
      <div class="nav-sub" id="navSub">Engineering Health</div>
    </div>
  </div>
  <div class="nav-tabs">
    <button class="nav-tab active" data-view="overview">Overview</button>
    <button class="nav-tab" data-view="dimension">By Dimension</button>
    <button class="nav-tab" data-view="trends">Trends</button>
    <button class="nav-tab" data-view="people">People</button>
  </div>
  <button class="theme-toggle" id="themeToggle">&#9790;</button>
  <select class="week-selector" id="weekSelector" title="View historical data" style="background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 8px;font-size:12px;cursor:pointer;margin-left:8px;">
    <option value="current">This week</option>
  </select>
</nav>
<div class="container">
  <div class="view active" id="view-overview">
    <div class="kpi-strip" id="kpiStrip"></div>
    <div class="subdomain-grid" id="subdomainGrid"></div>
    <div class="heatmap-section">
      <div class="section-header">
        <div><div class="section-title">Team Health Matrix</div><div class="section-sub">Hover cells for details, click to drill down. Click a sub-domain card above to filter.</div></div>
      </div>
      <div class="sd-filter-bar" id="sdFilterBar">
        <span class="filter-label">Filtering:</span>
        <span class="filter-name" id="sdFilterName"></span>
        <button class="filter-clear" id="sdFilterClear">Clear filter</button>
      </div>
      <div id="heatmap-container"></div>
    </div>
    <div class="heatmap-section" style="margin-top:20px">
      <div class="section-header">
        <div><div class="section-title">Estate Treemap</div><div class="section-sub">Area = team count, colour = health score</div></div>
      </div>
      <div id="treemap-container" style="height:300px"></div>
    </div>
    <div class="data-sources" style="margin-top:20px">
      <div class="data-sources-title">Data Sources</div>
      <div class="ds-grid" id="dsGrid"></div>
    </div>
  </div>
  <div class="view" id="view-dimension">
    <div class="dim-tabs" id="dimTabs"></div>
    <div class="ranked-list" id="rankedList"></div>
  </div>
  <div class="view" id="view-trends"><div id="trendsContainer"></div></div>
  <div class="view" id="view-people"><div id="peopleContainer"></div></div>
</div>
<div class="modal-overlay" id="modalOverlay"><div class="modal" id="modal"></div></div>
<div class="hm-tooltip" id="hmTooltip"></div>
<script>
var DATA = {scores_json};
var DIMENSIONS = DATA.dimensions;
var DIM_COLORS = ['#6366f1','#34d399','#f87171','#818cf8','#fbbf24','#06b6d4','#f472b6'];
var teamData = DATA.teams;
var subdomainData = DATA.subdomains;
var overallScore = DATA.domain.overall;
var activeSubdomain = null;

var DIM_INFO = {{
  'Delivery': {{
    desc: 'Measures backlog health from Jira: ticket quality scores, blocked ratio, and overdue items.',
    sources: 'Jira backlog health CSV',
    good: '75+: Clean backlog, few blocked/overdue items',
    bad: 'Below 50: High blocked ratio or many stale tickets'
  }},
  'System Health': {{
    desc: 'PagerDuty service standards compliance and Dynatrace service health status.',
    sources: 'PagerDuty Standards, Dynatrace Services',
    good: '75+: Services meet PD standards, healthy in Dynatrace',
    bad: 'Below 50: Missing escalation policies or degraded services'
  }},
  'Incidents': {{
    desc: 'Fewer incidents = higher score. Counts PagerDuty alerts and ITSM tickets assigned to the team.',
    sources: 'PagerDuty Incidents, ITSM Incident Dump',
    good: '75+: Minimal incidents (stable systems)',
    bad: 'Below 50: High incident volume, needs attention'
  }},
  'Code Quality': {{
    desc: 'Repo hygiene: stale PRs, stale branches, open PR count, and CI/CD success rates.',
    sources: 'Repo Health CSV, Actions Health CSV',
    good: '75+: Clean repos, few stale PRs/branches',
    bad: 'Below 50: Many stale PRs or failing pipelines'
  }},
  'Estate Hygiene': {{
    desc: 'Percentage of repos with catalog-info.yaml (Backstage/service catalogue coverage).',
    sources: 'Catalog Info CSV, Repos CSV',
    good: '75+: Most repos have catalog-info',
    bad: 'Below 50: Poor service catalogue coverage'
  }},
  'Ops Readiness': {{
    desc: 'PagerDuty standards: escalation policies, on-call setup, schedule coverage, notification rules.',
    sources: 'PagerDuty Standards CSV',
    good: '75+: Well-configured on-call and escalation',
    bad: 'Below 50: Missing on-call, no escalation policy'
  }},
  'People': {{
    desc: 'Team capacity: filled vs vacant positions, team size (bus factor risk for small teams).',
    sources: 'Foods Org Config YAML',
    good: '75+: Fully staffed, adequate team size',
    bad: 'Below 50: Multiple vacancies or single-person team'
  }}
}};

document.getElementById('navSub').textContent = DATA.week + ' \\u00b7 Updated ' + new Date(DATA.generated_at).toLocaleDateString('en-GB',{{day:'numeric',month:'short',year:'numeric'}}) + ' \\u00b7 ' + teamData.length + ' teams';

function scoreColor(v) {{ return v >= 75 ? 'var(--green)' : v >= 50 ? 'var(--amber)' : 'var(--red)'; }}
function scoreColorHex(v) {{ return v >= 75 ? '#34d399' : v >= 50 ? '#fbbf24' : '#f87171'; }}
function trendClass(v) {{ return v > 2 ? 'trend-up' : v < -2 ? 'trend-down' : 'trend-flat'; }}
function trendText(v) {{ return v > 0 ? '+' + v.toFixed(1) : v.toFixed(1); }}
function initials(n) {{ return n.split(' ').map(function(w){{return w[0]}}).join('').slice(0,2); }}
// Deprecated: retained for backwards compatibility only. Prefer getHistoricalSparkData().
function sparkData(n, base) {{ return Array.from({{length:n}}, function(){{return Math.round(base)}}); }}

// Load historical data for trends and sparklines
var HISTORICAL_WEEKS = [];
var HISTORICAL_DATA = {{}};
var CURRENT_WEEK = DATA.week;

async function loadHistoricalData() {{
  try {{
    var indexUrl = 'index.json';
    var response = await fetch(indexUrl);
    if (!response.ok) return;
    var weeks = await response.json();
    HISTORICAL_WEEKS = weeks;
    
    // Load available history for trends and sparklines
    for (var i = 0; i < weeks.length; i++) {{
      var week = weeks[i];
      try {{
        var weekUrl = week + '.json';
        var wr = await fetch(weekUrl);
        if (wr.ok) {{
          var data = await wr.json();
          HISTORICAL_DATA[week] = data;
        }}
      }} catch(e) {{}}
    }}
  }} catch(e) {{}}
}}

function getTrendDelta(teamName, dimension) {{
  // Calculate real trend delta from historical data
  var weeks = Object.keys(HISTORICAL_DATA).sort();
  if (weeks.length < 2) return null;
  
  var currentWeek = weeks[weeks.length - 1];
  var prevWeek = weeks[weeks.length - 2];
  
  var currentData = HISTORICAL_DATA[currentWeek];
  var prevData = HISTORICAL_DATA[prevWeek];
  
  if (!currentData || !prevData) return null;
  
  var currentTeam = currentData.teams.find(function(t) {{ return t.team_name === teamName; }});
  var prevTeam = prevData.teams.find(function(t) {{ return t.team_name === teamName; }});
  
  if (!currentTeam || !prevTeam) return null;
  
  var currentScore = currentTeam.scores[dimension];
  var prevScore = prevTeam.scores[dimension];
  
  return currentScore - prevScore;
}}

function getHistoricalSparkData(teamName, dimension, n, fallbackValue) {{
  // Build real sparkline data from historical weeks
  var weeks = Object.keys(HISTORICAL_DATA).sort().slice(-Math.min(n, 10));
  
  if (weeks.length === 0) {{
    return fallbackValue !== undefined ? flatSparkData(n, fallbackValue) : [];
  }}
  
  var data = [];
  weeks.forEach(function(week) {{
    var weekData = HISTORICAL_DATA[week];
    if (!weekData) return;
    if (teamName === '__domain__') {{
      if (dimension === '__overall__' && weekData.domain) {{
        data.push(Math.round(weekData.domain.overall));
      }} else if (dimension === '__team_count__' && weekData.teams) {{
        data.push(weekData.teams.length);
      }} else if (dimension === '__areas_below_50__' && weekData.teams) {{
        data.push(weekData.teams.reduce(function(total, team) {{
          return total + (team.scores['Incidents'] < 50 ? 1 : 0);
        }}, 0));
      }} else if (dimension === '__lowest_dimension__' && weekData.teams && weekData.teams.length) {{
        var lowest = DIMENSIONS.reduce(function(low, d) {{
          var avg = weekData.teams.reduce(function(sum, team) {{ return sum + team.scores[d]; }}, 0) / weekData.teams.length;
          return avg < low ? avg : low;
        }}, 999);
        data.push(Math.round(lowest));
      }} else if (weekData.domain && weekData.domain.scores && weekData.domain.scores[dimension] !== undefined) {{
        data.push(Math.round(weekData.domain.scores[dimension]));
      }}
    }} else if (weekData.teams) {{
      var team = weekData.teams.find(function(t) {{ return t.team_name === teamName; }});
      if (team && team.scores && team.scores[dimension] !== undefined) {{
        data.push(Math.round(team.scores[dimension]));
      }}
    }}
  }});
  
  return data.length > 0 ? data : (fallbackValue !== undefined ? flatSparkData(n, fallbackValue) : []);
}}

function getHistoricalKPI(extractFn, n, fallbackValue) {{
  // Build a KPI sparkline from historical week data using a custom extractor
  var weeks = Object.keys(HISTORICAL_DATA).sort().slice(-Math.min(n, 10));
  if (weeks.length === 0) return sparkData(n, fallbackValue);
  var data = [];
  weeks.forEach(function(week) {{
    var wd = HISTORICAL_DATA[week];
    if (wd) {{
      var val = extractFn(wd);
      if (val !== null && val !== undefined) data.push(Math.round(val));
    }}
  }});
  return data.length > 0 ? data : sparkData(n, fallbackValue);
}}

function renderKPIs() {{
  var activeIncidents = teamData.reduce(function(s,t){{return s + (t.scores['Incidents'] < 50 ? 1 : 0)}}, 0);
  var lowestDim = DIMENSIONS.reduce(function(low,d){{var avg=teamData.reduce(function(s,t){{return s+t.scores[d]}},0)/teamData.length;return avg<low.v?{{d:d,v:avg}}:low}},{{d:'',v:999}});
  // Build KPI sparklines from historical data when available
  var kpiOverallHistory = getHistoricalKPI(function(wd){{ return wd.domain ? wd.domain.overall : null; }}, 8, overallScore);
  var kpiTeamsHistory = getHistoricalKPI(function(wd){{ return wd.teams ? wd.teams.length : null; }}, 8, teamData.length);
  var kpiBelow50History = getHistoricalKPI(function(wd){{
    if (!wd.teams) return null;
    return wd.teams.reduce(function(s,t){{return s + (t.scores && t.scores['Incidents'] < 50 ? 1 : 0)}}, 0);
  }}, 8, activeIncidents);
  var kpiLowestHistory = getHistoricalKPI(function(wd){{
    if (!wd.teams) return null;
    var dims = ['Delivery','System Health','Incidents','Code Quality','Estate Hygiene','Ops Readiness','People'];
    var low = 999;
    dims.forEach(function(d){{ var avg=wd.teams.reduce(function(s,t){{return s+(t.scores?t.scores[d]:0)}},0)/wd.teams.length; if(avg<low)low=avg; }});
    return low < 999 ? Math.round(low) : null;
  }}, 8, Math.round(lowestDim.v));
  var kpis = [
    {{label:'Overall Score',value:overallScore,data:kpiOverallHistory,tip:'Weighted average across all teams and dimensions'}},
    {{label:'Teams Tracked',value:teamData.length,data:kpiTeamsHistory,tip:'Total teams with health scores'}},
    {{label:'Areas Below 50',value:activeIncidents,data:kpiBelow50History,tip:'Teams scoring below 50 on any dimension (click to view)'}},
    {{label:'Needs Attention',value:Math.round(lowestDim.v),data:kpiLowestHistory,tip:lowestDim.d+': lowest average score across all teams (click to view)'}}
  ];
  var strip = document.getElementById('kpiStrip');
  strip.innerHTML = kpis.map(function(k,i) {{
    return '<div class="kpi-card" data-kpi="'+i+'" title="'+k.tip+'"><div class="kpi-label">' + k.label + '</div><div class="kpi-row"><div class="kpi-value" style="color:' + scoreColor(typeof k.value==='number'&&k.value<=100?k.value:75) + '">' + k.value + '</div><div class="kpi-spark" id="kpi-spark-' + i + '"></div></div></div>';
  }}).join('');
  kpis.forEach(function(k,i) {{
    new ApexCharts(document.getElementById('kpi-spark-'+i), {{
      chart:{{type:'area',height:36,width:80,sparkline:{{enabled:true}},animations:{{enabled:true,speed:600}}}},
      series:[{{data:k.data}}], stroke:{{width:2,curve:'smooth'}},
      fill:{{type:'gradient',gradient:{{shadeIntensity:1,opacityFrom:0.4,opacityTo:0}}}},
      colors:[scoreColorHex(typeof k.value==='number'&&k.value<=100?k.value:75)], tooltip:{{enabled:false}}
    }}).render();
  }});
  strip.querySelectorAll('.kpi-card').forEach(function(card) {{
    card.addEventListener('click', function() {{
      var idx = parseInt(this.dataset.kpi);
      if (idx === 2) {{
        activeDim = 'Incidents';
        document.querySelectorAll('.nav-tab').forEach(function(t){{t.classList.remove('active')}});
        document.querySelectorAll('.view').forEach(function(v){{v.classList.remove('active')}});
        document.querySelector('[data-view="dimension"]').classList.add('active');
        document.getElementById('view-dimension').classList.add('active');
        renderDimTabs(); renderRankedList();
      }} else if (idx === 3) {{
        activeDim = lowestDim.d;
        document.querySelectorAll('.nav-tab').forEach(function(t){{t.classList.remove('active')}});
        document.querySelectorAll('.view').forEach(function(v){{v.classList.remove('active')}});
        document.querySelector('[data-view="dimension"]').classList.add('active');
        document.getElementById('view-dimension').classList.add('active');
        renderDimTabs(); renderRankedList();
      }}
    }});
  }});
}}

function renderSubdomains() {{
  var grid = document.getElementById('subdomainGrid');
  grid.innerHTML = subdomainData.map(function(sd) {{
    var dims = DIMENSIONS.map(function(d,i) {{
      var v = sd.scores[d];
      return '<div class="subdomain-dim"><span class="dot" style="background:'+DIM_COLORS[i]+'"></span><span>'+d+'</span><span class="dim-val" style="color:'+scoreColor(v)+'">'+v+'</span></div>';
    }}).join('');
    return '<div class="subdomain-card" data-sd="'+sd.subdomain_id+'"><div class="subdomain-header"><div><div class="subdomain-name">'+sd.subdomain_name+'</div><div class="subdomain-teams">'+sd.team_count+' teams</div></div><div class="subdomain-score" style="color:'+scoreColor(sd.overall)+'">'+sd.overall+'</div></div><div class="subdomain-chart" id="radial-'+sd.subdomain_id+'"></div><div class="subdomain-dims">'+dims+'</div></div>';
  }}).join('');
  subdomainData.forEach(function(sd) {{
    new ApexCharts(document.getElementById('radial-'+sd.subdomain_id), {{
      chart:{{type:'radialBar',height:160,animations:{{enabled:true,speed:800}}}},
      series:DIMENSIONS.map(function(d){{return sd.scores[d]}}), labels:DIMENSIONS, colors:DIM_COLORS,
      plotOptions:{{radialBar:{{hollow:{{size:'30%'}},track:{{background:'var(--glass)',strokeWidth:'100%'}},dataLabels:{{name:{{show:false}},value:{{show:true,fontSize:'14px',fontWeight:700,color:'var(--text)',offsetY:5,formatter:function(){{return sd.overall}}}}}}}}}},
      stroke:{{lineCap:'round'}}, legend:{{show:false}}
    }}).render();
  }});
  grid.querySelectorAll('.subdomain-card').forEach(function(card) {{
    card.addEventListener('click', function() {{
      var sdId = this.dataset.sd;
      if (activeSubdomain === sdId) {{
        activeSubdomain = null;
        grid.querySelectorAll('.subdomain-card').forEach(function(c){{c.classList.remove('sd-active')}});
        document.getElementById('sdFilterBar').classList.remove('visible');
      }} else {{
        activeSubdomain = sdId;
        grid.querySelectorAll('.subdomain-card').forEach(function(c){{c.classList.remove('sd-active')}});
        this.classList.add('sd-active');
        var sdName = subdomainData.find(function(s){{return s.subdomain_id===sdId}}).subdomain_name;
        document.getElementById('sdFilterName').textContent = sdName;
        document.getElementById('sdFilterBar').classList.add('visible');
      }}
      renderHeatmap();
    }});
  }});
  document.getElementById('sdFilterClear').addEventListener('click', function() {{
    activeSubdomain = null;
    grid.querySelectorAll('.subdomain-card').forEach(function(c){{c.classList.remove('sd-active')}});
    document.getElementById('sdFilterBar').classList.remove('visible');
    renderHeatmap();
  }});
}}

function renderHeatmap() {{
  var container = document.getElementById('heatmap-container');
  container.innerHTML = '';
  var isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  var textCol = isDark ? '#9898a8' : '#5a5a72';
  var textColBright = isDark ? '#e8e8ef' : '#1a1a2e';
  var gridCol = isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)';
  var filteredTeams = activeSubdomain ? teamData.filter(function(t){{return t.subdomain_id===activeSubdomain}}) : teamData;
  var margin = {{top:40,right:20,bottom:10,left:200}};
  var cellH = 36, cellPad = 3;
  var width = container.clientWidth - margin.left - margin.right;
  var height = filteredTeams.length * (cellH + cellPad);
  var svg = d3.select('#heatmap-container').append('svg')
    .attr('width',width+margin.left+margin.right).attr('height',height+margin.top+margin.bottom)
    .append('g').attr('transform','translate('+margin.left+','+margin.top+')');
  var x = d3.scaleBand().domain(DIMENSIONS).range([0,width]).padding(0.08);
  var y = d3.scaleBand().domain(filteredTeams.map(function(d){{return d.team_name}})).range([0,height]).padding(0.08);
  var colorScale = d3.scaleLinear().domain([30,55,75,95]).range(['#ef4444','#f59e0b','#34d399','#22c55e']);
  svg.selectAll('.col-label').data(DIMENSIONS).enter().append('text')
    .attr('x',function(d){{return x(d)+x.bandwidth()/2}}).attr('y',-12).attr('text-anchor','middle')
    .attr('fill',textCol).attr('font-size','11px').attr('font-weight','600').text(function(d){{return d}})
    .attr('cursor','pointer')
    .on('click',function(ev,d){{
      activeDim = d;
      document.querySelectorAll('.nav-tab').forEach(function(t){{t.classList.remove('active')}});
      document.querySelectorAll('.view').forEach(function(v){{v.classList.remove('active')}});
      document.querySelector('[data-view="dimension"]').classList.add('active');
      document.getElementById('view-dimension').classList.add('active');
      renderDimTabs(); renderRankedList();
    }});
  svg.selectAll('.row-label').data(filteredTeams).enter().append('text')
    .attr('x',-12).attr('y',function(d){{return y(d.team_name)+y.bandwidth()/2+4}}).attr('text-anchor','end')
    .attr('fill',textCol).attr('font-size','12px').attr('font-weight','500').text(function(d){{return d.team_name}})
    .attr('cursor','pointer')
    .on('click',function(ev,d){{openModal(d)}});
  svg.selectAll('.row-bg').data(filteredTeams).enter().append('rect')
    .attr('x',-8).attr('y',function(d){{return y(d.team_name)-2}}).attr('width',width+16).attr('height',y.bandwidth()+4)
    .attr('fill','transparent').attr('rx',6)
    .on('mouseenter',function(){{d3.select(this).attr('fill',gridCol)}})
    .on('mouseleave',function(){{d3.select(this).attr('fill','transparent')}});
  var cells = [];
  filteredTeams.forEach(function(t){{DIMENSIONS.forEach(function(d){{cells.push({{team:t.team_name,dim:d,value:t.scores[d],hasData:t.has_data?t.has_data[d]:true,teamData:t}})}});}});
  // Diagonal stripe pattern for no-data cells
  var defs = d3.select('#heatmap-container svg').append('defs');
  var pat = defs.append('pattern').attr('id','nodata-stripe').attr('width',6).attr('height',6).attr('patternUnits','userSpaceOnUse').attr('patternTransform','rotate(45)');
  pat.append('rect').attr('width',6).attr('height',6).attr('fill',isDark?'rgba(255,255,255,0.04)':'rgba(0,0,0,0.04)');
  pat.append('line').attr('x1',0).attr('y1',0).attr('x2',0).attr('y2',6).attr('stroke',isDark?'rgba(255,255,255,0.08)':'rgba(0,0,0,0.08)').attr('stroke-width',2);
  var tooltip = document.getElementById('hmTooltip');
  svg.selectAll('.cell').data(cells).enter().append('rect')
    .attr('x',function(d){{return x(d.dim)}}).attr('y',function(d){{return y(d.team)}})
    .attr('width',x.bandwidth()).attr('height',y.bandwidth()).attr('rx',6)
    .attr('fill',function(d){{return d.hasData?colorScale(d.value):'url(#nodata-stripe)'}}).attr('opacity',function(d){{return d.hasData?0.85:0.6}}).attr('cursor','pointer')
    .on('mouseenter',function(ev,d){{
      d3.select(this).attr('opacity',1).attr('stroke','#fff').attr('stroke-width',2);
      var info = DIM_INFO[d.dim] || {{}};
      if (!d.hasData) {{
        tooltip.innerHTML = '<div class="tt-header"><span class="tt-dim">' + d.dim + '</span><span class="tt-score" style="color:var(--text2)">N/A</span></div>'
          + '<div class="tt-team">' + d.team + '</div>'
          + '<div class="tt-desc">No data available for this dimension. The team may not be mapped in the config, or the data source has no matching records.</div>'
          + '<div class="tt-sources">Expected: ' + (info.sources||'N/A') + '</div>';
      }} else {{
        var label = d.value >= 75 ? info.good : d.value >= 50 ? 'Moderate: room for improvement' : info.bad;
        tooltip.innerHTML = '<div class="tt-header"><span class="tt-dim">' + d.dim + '</span><span class="tt-score" style="color:'+scoreColorHex(d.value)+'">' + Math.round(d.value) + '</span></div>'
          + '<div class="tt-team">' + d.team + '</div>'
          + '<div class="tt-meter"><div class="tt-meter-fill" style="width:'+d.value+'%;background:'+scoreColorHex(d.value)+'"></div></div>'
          + '<div class="tt-desc">' + (info.desc||'') + '</div>'
          + '<div class="tt-label">' + (label||'') + '</div>'
          + '<div class="tt-sources">Data: ' + (info.sources||'N/A') + '</div>';
      }}
      tooltip.classList.add('visible');
    }})
    .on('mousemove',function(ev){{
      var tx = ev.clientX + 16, ty = ev.clientY - 10;
      if (tx + 330 > window.innerWidth) tx = ev.clientX - 336;
      if (ty + 200 > window.innerHeight) ty = ev.clientY - 200;
      tooltip.style.left = tx + 'px';
      tooltip.style.top = ty + 'px';
    }})
    .on('mouseleave',function(){{
      d3.select(this).attr('opacity',function(d){{return d.hasData?0.85:0.6}}).attr('stroke','none');
      tooltip.classList.remove('visible');
    }})
    .on('click',function(ev,d){{tooltip.classList.remove('visible');openModal(d.teamData)}});
  svg.selectAll('.cell-text').data(cells).enter().append('text')
    .attr('x',function(d){{return x(d.dim)+x.bandwidth()/2}}).attr('y',function(d){{return y(d.team)+y.bandwidth()/2+4}})
    .attr('text-anchor','middle').attr('fill',function(d){{return !d.hasData?(isDark?'#555':'#bbb'):d.value>60?'#fff':textColBright}})
    .attr('font-size','12px').attr('font-weight','700').attr('pointer-events','none')
    .text(function(d){{return d.hasData?Math.round(d.value):'-'}});
}}

function renderDataSources() {{
  var ds = DATA.data_sources;
  var grid = document.getElementById('dsGrid');
  grid.innerHTML = Object.keys(ds).map(function(k) {{
    var info = ds[k];
    var rows = typeof info === 'object' ? info.rows : info;
    var updated = typeof info === 'object' ? info.updated : '';
    return '<div class="ds-item"><span>' + k.replace(/_/g,' ') + '</span><span class="ds-count">' + rows + ' rows' + (updated ? ' \\u00b7 ' + updated : '') + '</span></div>';
  }}).join('');
}}

function renderTreemap() {{
  var series = teamData.map(function(t) {{
    return {{ x: t.team_name, y: t.overall }};
  }});
  new ApexCharts(document.getElementById('treemap-container'), {{
    chart: {{ type: 'treemap', height: 300, toolbar: {{ show: false }}, animations: {{ enabled: true, speed: 600 }} }},
    series: [{{ data: series }}],
    colors: ['#ef4444', '#f59e0b', '#34d399', '#22c55e'],
    plotOptions: {{
      treemap: {{
        distributed: false,
        colorScale: {{
          ranges: [
            {{ from: 0, to: 49, color: '#ef4444', name: 'Critical' }},
            {{ from: 50, to: 64, color: '#f59e0b', name: 'Needs Attention' }},
            {{ from: 65, to: 74, color: '#fbbf24', name: 'Moderate' }},
            {{ from: 75, to: 100, color: '#34d399', name: 'Healthy' }}
          ]
        }}
      }}
    }},
    dataLabels: {{
      enabled: true,
      style: {{ fontSize: '12px', fontWeight: 600 }},
      formatter: function(text, op) {{ return text + ' (' + op.value + ')'; }}
    }},
    tooltip: {{
      y: {{ formatter: function(v) {{ return v + '/100'; }} }}
    }}
  }}).render();
}}

var activeDim = 'Delivery';
function renderDimTabs() {{
  var tabs = document.getElementById('dimTabs');
  tabs.innerHTML = DIMENSIONS.map(function(d,i) {{
    return '<button class="dim-tab'+(d===activeDim?' active':'')+'" data-dim="'+d+'" style="'+(d===activeDim?'background:'+DIM_COLORS[i]:'')+'">' + d + '</button>';
  }}).join('');
  tabs.querySelectorAll('.dim-tab').forEach(function(btn) {{
    btn.addEventListener('click',function(){{ activeDim=this.dataset.dim; renderDimTabs(); renderRankedList(); }});
  }});
}}

function renderRankedList() {{
  var sorted = teamData.slice().sort(function(a,b){{return b.scores[activeDim]-a.scores[activeDim]}});
  var list = document.getElementById('rankedList');
  list.innerHTML = sorted.map(function(t,i) {{
    var v = t.scores[activeDim];
    // Use real trend delta from historical data, fallback to "—" if no history
    var tr = getTrendDelta(t.team_name, activeDim);
    var trendHtml = tr !== null ? '<div class="ranked-trend '+trendClass(tr)+'">'+trendText(tr)+'</div>' : '<div class="ranked-trend trend-flat">—</div>';
    return '<div class="ranked-item" data-team="'+t.team_name+'"><div class="rank">#'+(i+1)+'</div><div><div class="ranked-team">'+t.team_name+'</div><div class="ranked-sub">'+t.subdomain_name+'</div></div><div class="ranked-spark" id="rank-spark-'+i+'"></div><div class="ranked-bar-wrap"><div class="ranked-bar" style="width:'+v+'%;background:'+scoreColorHex(v)+'"></div></div><div class="ranked-score" style="color:'+scoreColor(v)+'">'+Math.round(v)+'</div>'+trendHtml+'</div>';
  }}).join('');
  sorted.forEach(function(t,i) {{
    var el = document.getElementById('rank-spark-'+i);
    if(el) {{
      var sparkData = getHistoricalSparkData(t.team_name, activeDim, 8);
      if (sparkData.length > 0) {{
        new ApexCharts(el,{{chart:{{type:'line',height:32,width:80,sparkline:{{enabled:true}},animations:{{enabled:false}}}},series:[{{data:sparkData}}],stroke:{{width:2,curve:'smooth'}},colors:[scoreColorHex(t.scores[activeDim])],tooltip:{{enabled:false}}}}).render();
      }}
    }}
  }});
  list.querySelectorAll('.ranked-item').forEach(function(item) {{
    item.addEventListener('click',function(){{var team=teamData.find(function(t){{return t.team_name===item.dataset.team}});if(team)openModal(team);}});
  }});
}}

function renderPeople() {{
  var container = document.getElementById('peopleContainer');
  var colors = ['#6366f1','#34d399','#818cf8','#06b6d4','#f472b6','#fbbf24'];
  var leaderCards = (DATA.leadership||[]).map(function(p,i) {{
    return '<div class="person-card"><div class="person-avatar" style="background:linear-gradient(135deg,'+colors[i%colors.length]+','+colors[i%colors.length]+'88)">'+initials(p.name)+'</div><div><div class="person-name">'+p.name+'</div><div class="person-role">'+p.role+'</div></div></div>';
  }}).join('');
  // Team headcount per sub-domain
  var sdGroups = {{}};
  teamData.forEach(function(t) {{
    if (!sdGroups[t.subdomain_name]) sdGroups[t.subdomain_name] = [];
    sdGroups[t.subdomain_name].push(t);
  }});
  var headcountHtml = Object.keys(sdGroups).map(function(sdName) {{
    var teams = sdGroups[sdName];
    var rows = teams.map(function(t) {{
      var pScore = t.scores['People'];
      return '<div class="ranked-item people-team-row" data-team="'+t.team_name+'" style="cursor:pointer;grid-template-columns:1fr 60px 80px"><div class="ranked-team">'+t.team_name+'</div><div class="ranked-score" style="color:'+scoreColor(pScore)+'">'+Math.round(pScore)+'</div><div style="font-size:11px;color:var(--text2)">People score</div></div>';
    }}).join('');
    return '<div class="people-section"><div class="people-title">'+sdName+' ('+teams.length+' teams)</div>'+rows+'</div>';
  }}).join('');
  container.innerHTML = '<div class="people-section"><div class="people-title">Leadership</div><div class="people-grid">'+leaderCards+'</div></div>' + headcountHtml;
  container.querySelectorAll('.people-team-row').forEach(function(row) {{
    row.addEventListener('click', function() {{
      var team = teamData.find(function(x){{return x.team_name === row.dataset.team}});
      if (team) openModal(team);
    }});
  }});
}}

function renderTrends() {{
  var container = document.getElementById('trendsContainer');
  if (!container) return;
  if (Object.keys(HISTORICAL_DATA).length < 2) {{
    container.innerHTML = '<div style="text-align:center;padding:60px 20px;color:var(--text2)"><p style="font-size:16px;font-weight:600;margin-bottom:8px">Not enough historical data yet</p><p style="font-size:13px">Trends will appear after 2+ weeks of data are collected.</p></div>';
    return;
  }}
  var weeks = Object.keys(HISTORICAL_DATA).sort();
  var overallSeries = weeks.map(function(w) {{ return {{ x: w.replace('_', ' '), y: Math.round(HISTORICAL_DATA[w].domain.overall) }}; }});
  var dimSeries = DIMENSIONS.map(function(d) {{
    return {{
      name: d,
      data: weeks.map(function(w) {{ return Math.round(HISTORICAL_DATA[w].domain.scores[d]); }})
    }};
  }});

  container.innerHTML = '<div class="heatmap-section" style="margin-bottom:20px"><div class="section-header"><div><div class="section-title">Overall Score Trend</div><div class="section-sub">Week-over-week domain health</div></div></div><div id="trend-overall" style="height:200px"></div></div>'
    + '<div class="heatmap-section"><div class="section-header"><div><div class="section-title">Dimension Trends</div><div class="section-sub">All 7 dimensions over time</div></div></div><div id="trend-dims" style="height:300px"></div></div>';

  new ApexCharts(document.getElementById('trend-overall'), {{
    chart: {{ type: 'area', height: 200, toolbar: {{ show: false }}, animations: {{ enabled: true, speed: 600 }} }},
    series: [{{ name: 'Overall', data: overallSeries.map(function(p) {{ return p.y; }}) }}],
    xaxis: {{ categories: weeks.map(function(w) {{ return w.replace('_', ' '); }}), labels: {{ style: {{ fontSize: '11px', colors: 'var(--text2)' }} }} }},
    yaxis: {{ min: 0, max: 100, labels: {{ style: {{ fontSize: '11px', colors: 'var(--text2)' }} }} }},
    colors: ['#6366f1'],
    fill: {{ type: 'gradient', gradient: {{ shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0 }} }},
    stroke: {{ width: 3, curve: 'smooth' }},
    markers: {{ size: 5 }},
    grid: {{ borderColor: 'var(--border)', strokeDashArray: 4 }},
    tooltip: {{ theme: 'dark' }}
  }}).render();

  new ApexCharts(document.getElementById('trend-dims'), {{
    chart: {{ type: 'line', height: 300, toolbar: {{ show: false }}, animations: {{ enabled: true, speed: 600 }} }},
    series: dimSeries,
    xaxis: {{ categories: weeks.map(function(w) {{ return w.replace('_', ' '); }}), labels: {{ style: {{ fontSize: '11px', colors: 'var(--text2)' }} }} }},
    yaxis: {{ min: 0, max: 100, labels: {{ style: {{ fontSize: '11px', colors: 'var(--text2)' }} }} }},
    colors: DIM_COLORS,
    stroke: {{ width: 2, curve: 'smooth' }},
    markers: {{ size: 3 }},
    grid: {{ borderColor: 'var(--border)', strokeDashArray: 4 }},
    legend: {{ position: 'top', horizontalAlign: 'center', fontSize: '12px', labels: {{ colors: 'var(--text2)' }} }},
    tooltip: {{ theme: 'dark' }}
  }}).render();
}}

function openModal(team) {{
  var overlay = document.getElementById('modalOverlay');
  var modal = document.getElementById('modal');
  var radarId = 'modal-radar';
  var dims = DIMENSIONS.map(function(d,i) {{
    var v = team.scores[d];
    var info = DIM_INFO[d] || {{}};
    return '<div class="modal-dim-card" style="cursor:default">'
      + '<div class="modal-dim-label">' + d + '</div>'
      + '<div class="modal-dim-score" style="color:'+scoreColor(v)+'">'+Math.round(v)+'</div>'
      + '<div style="height:4px;border-radius:2px;background:var(--glass);margin:6px 0"><div style="height:100%;border-radius:2px;width:'+v+'%;background:'+scoreColorHex(v)+'"></div></div>'
      + '<div class="modal-dim-spark" id="modal-spark-'+i+'"></div>'
      + '<div style="font-size:10px;color:var(--text2);margin-top:4px">' + (info.desc||'') + '</div>'
      + '</div>';
  }}).join('');
  modal.innerHTML = '<button class="modal-close" id="modalClose">&times;</button>'
    + '<h2>'+team.team_name+'</h2>'
    + '<div class="modal-sub">'+team.subdomain_name+' \\u00b7 Overall: <span style="font-weight:700;color:'+scoreColor(team.overall)+'">'+team.overall+'</span></div>'
    + '<div class="modal-dims">'+dims+'</div>'
    + '<div style="margin-top:20px;"><div style="font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">Team Profile</div><div id="' + radarId + '"></div></div>';
  overlay.classList.add('open');
  DIMENSIONS.forEach(function(d,i) {{
    var el = document.getElementById('modal-spark-'+i);
    if(el) {{ var sd = getHistoricalSparkData(team.team_name, d, 8); if(sd.length===0) sd=sparkData(8,team.scores[d]); new ApexCharts(el,{{chart:{{type:'area',height:40,sparkline:{{enabled:true}},animations:{{enabled:true,speed:500}}}},series:[{{data:sd}}],stroke:{{width:2,curve:'smooth'}},fill:{{type:'gradient',gradient:{{shadeIntensity:1,opacityFrom:0.3,opacityTo:0}}}},colors:[DIM_COLORS[i]],tooltip:{{enabled:false}}}}).render(); }}
  }});
  new ApexCharts(document.getElementById(radarId), {{
    chart: {{ type: 'radar', height: 280, toolbar: {{ show: false }}, animations: {{ enabled: true, speed: 600 }} }},
    series: [{{ name: team.team_name, data: DIMENSIONS.map(function(d) {{ return Math.round(team.scores[d]); }}) }}],
    xaxis: {{ categories: DIMENSIONS.map(function(d) {{ return d.split(' ')[0]; }}) }},
    yaxis: {{ show: false, min: 0, max: 100 }},
    colors: ['#6366f1'],
    fill: {{ opacity: 0.2 }},
    stroke: {{ width: 2 }},
    markers: {{ size: 4, colors: ['#6366f1'], strokeWidth: 0 }},
    plotOptions: {{ radar: {{ polygons: {{ strokeColors: 'var(--border)', connectorColors: 'var(--border)', fill: {{ colors: ['transparent', 'var(--glass)'] }} }} }} }},
    dataLabels: {{ enabled: true, background: {{ enabled: false }}, style: {{ fontSize: '11px', colors: ['var(--text)'] }}, formatter: function(v) {{ return Math.round(v); }} }}
  }}).render();
  document.getElementById('modalClose').addEventListener('click',function(){{overlay.classList.remove('open')}});
  overlay.addEventListener('click',function(e){{if(e.target===overlay)overlay.classList.remove('open')}});
}}

document.querySelectorAll('.nav-tab').forEach(function(tab) {{
  tab.addEventListener('click',function() {{
    document.querySelectorAll('.nav-tab').forEach(function(t){{t.classList.remove('active')}});
    document.querySelectorAll('.view').forEach(function(v){{v.classList.remove('active')}});
    this.classList.add('active');
    document.getElementById('view-'+this.dataset.view).classList.add('active');
    if(this.dataset.view==='dimension'){{renderDimTabs();renderRankedList();}}
    if(this.dataset.view==='trends'){{renderTrends();}}
  }});
}});
document.getElementById('themeToggle').addEventListener('click',function() {{
  var html = document.documentElement;
  var isLight = html.getAttribute('data-theme')==='light';
  html.setAttribute('data-theme',isLight?'dark':'light');
  this.textContent = isLight?'\\u263E':'\\u2600';
  renderHeatmap();
}});

renderKPIs(); renderSubdomains(); renderHeatmap(); renderPeople(); renderDataSources(); renderTreemap();

// Load historical data asynchronously for trends and sparklines
loadHistoricalData().then(function() {{ renderKPIs(); renderTrends(); }});

// Week selector - load available weeks from index.json
(function() {{
  var sel = document.getElementById('weekSelector');
  if (!sel) return;
  var base = '/foods-data-scripts/outputs/';
  fetch(base + 'json/heat_map_scores/index.json')
    .then(function(r) {{ return r.ok ? r.json() : []; }})
    .catch(function() {{ return []; }})
    .then(function(weeks) {{
      if (!weeks || !weeks.length) return;
      weeks.slice().reverse().forEach(function(w) {{
        var opt = document.createElement('option');
        opt.value = w;
        opt.textContent = w.replace('_', ' ');
        sel.appendChild(opt);
      }});
    }});
  sel.addEventListener('change', function() {{
    if (this.value === 'current') return;
    window.location.href = base + 'html/heatmap/' + this.value + '.html';
  }});
}})();
</script>
</body>
</html>"""
