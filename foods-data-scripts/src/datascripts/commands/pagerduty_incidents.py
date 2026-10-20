"""PagerDuty incidents command."""

import csv as csv_module
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import click
import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .pagerduty_base import (
    PagerDutyClient,
    resolve_pagerduty_token,
    output_csv,
    output_json,
    format_duration,
    calculate_incident_duration,
)
from ..services.csv_helper import CsvHelper
from ..services.date_range import format_timestamp, parse_datetime_value


# Column order for the persistent incidents dataset (outputs/csv/pagerduty_incidents.csv).
INCIDENT_CSV_HEADERS = [
    "id",
    "incident_number",
    "status",
    "urgency",
    "created_at",
    "last_status_change_at",
    "resolved_at",
    "seconds_to_resolve",
    "title",
    "service_name",
    "team_name",
    "escalation_policy",
    "assignees",
    "acknowledgers",
    "html_url",
]

# A date window ending this close to "now" is still filling up, so its responses
# must never be served from (or written to) the local cache.
CLOSED_WINDOW_MARGIN = timedelta(minutes=15)


def resolve_since_value(value: Optional[str]) -> Optional[str]:
    """Resolve a --since/--until value into an absolute UTC ISO8601 timestamp.

    Args:
        value: Raw option value (duration, date, or timestamp).

    Returns:
        An absolute UTC ISO8601 timestamp, or None when no value was supplied.
    """
    parsed = parse_datetime_value(value)
    return format_timestamp(parsed) if parsed else None


def incident_created_at(incident: Dict) -> Optional[datetime]:
    """Return an incident's creation time as a timezone-aware UTC datetime.

    Args:
        incident: Raw incident dictionary from the API.

    Returns:
        Parsed creation time, or None when it is missing/unparseable.
    """
    raw = incident.get("created_at") or incident.get("first_triggered_at")
    if not raw:
        return None
    try:
        return parse_datetime_value(raw, "created_at")
    except click.BadParameter:
        return None


def filter_incidents_to_window(
    incidents: List[Dict],
    since: Optional[datetime],
    until: Optional[datetime],
) -> List[Dict]:
    """Drop incidents that fall outside the requested date window.

    PagerDuty silently clamps oversized or open-ended ranges, and a locally cached
    response may have been recorded for a neighbouring window. Filtering on
    ``created_at`` guarantees the returned dataset always matches the window that
    was actually asked for.

    Args:
        incidents: Incidents returned by the API (or the local cache).
        since: Inclusive start of the requested window.
        until: Inclusive end of the requested window.

    Returns:
        Incidents created within the window.
    """
    if since is None and until is None:
        return incidents

    filtered = []
    for incident in incidents:
        created = incident_created_at(incident)
        if created is None:
            # Cannot place it in time; keep it rather than silently losing data.
            filtered.append(incident)
            continue
        if since is not None and created < since:
            continue
        if until is not None and created > until:
            continue
        filtered.append(incident)
    return filtered


def extract_incident_record(incident: Dict) -> Dict[str, str]:
    """Flatten a PagerDuty incident into a persistent CSV record.

    Args:
        incident: Raw incident dictionary from the API.

    Returns:
        Dictionary keyed by INCIDENT_CSV_HEADERS.
    """
    status = (incident.get("status") or "").lower()
    created_at = incident.get("created_at") or incident.get("first_triggered_at") or ""
    last_change = incident.get("last_status_change_at") or ""

    resolved_at = last_change if status == "resolved" else ""
    seconds_to_resolve = ""
    if status == "resolved" and created_at and resolved_at:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            resolved = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
            seconds_to_resolve = str(max(0, int((resolved - created).total_seconds())))
        except (ValueError, TypeError):
            seconds_to_resolve = ""

    service = incident.get("service") or {}
    service_name = service.get("summary") or service.get("name") or "" if isinstance(service, dict) else ""

    team_names = [t for t in extract_team_names(incident) if t and t != "Unknown"]
    team_name = ";".join(team_names)

    escalation = incident.get("escalation_policy") or {}
    escalation_policy = (
        escalation.get("summary") or escalation.get("name") or "" if isinstance(escalation, dict) else ""
    )

    assignees = []
    for assignment in incident.get("assignments") or []:
        assignee = assignment.get("assignee") if isinstance(assignment, dict) else None
        if isinstance(assignee, dict):
            name = assignee.get("summary") or assignee.get("name")
            if name:
                assignees.append(name)

    acknowledgers = []
    for ack in incident.get("acknowledgements") or []:
        if not isinstance(ack, dict):
            continue
        acknowledger = ack.get("acknowledger") or {}
        name = acknowledger.get("summary") or acknowledger.get("name") if isinstance(acknowledger, dict) else None
        at = ack.get("at") or ""
        if name:
            acknowledgers.append(f"{name}|{at}")

    return {
        "id": incident.get("id", ""),
        "incident_number": str(incident.get("incident_number", "") or ""),
        "status": status,
        "urgency": incident.get("urgency", "") or "",
        "created_at": created_at,
        "last_status_change_at": last_change,
        "resolved_at": resolved_at,
        "seconds_to_resolve": seconds_to_resolve,
        "title": incident.get("title") or incident.get("summary") or "",
        "service_name": service_name,
        "team_name": team_name,
        "escalation_policy": escalation_policy,
        "assignees": ";".join(assignees),
        "acknowledgers": ";".join(acknowledgers),
        "html_url": incident.get("html_url") or incident.get("incident_url") or "",
    }


def load_existing_incidents(csv_path: str) -> Dict[str, Dict[str, str]]:
    """Load an existing incidents CSV keyed by incident id.

    Args:
        csv_path: Path to the persistent incidents CSV.

    Returns:
        Mapping of incident id to record dictionary (empty if file missing).
    """
    path = Path(csv_path)
    if not path.exists():
        return {}

    records: Dict[str, Dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv_module.DictReader(handle)
        for row in reader:
            incident_id = (row.get("id") or "").strip()
            if incident_id:
                records[incident_id] = row
    return records


def write_incidents_csv(csv_path: str, records: List[Dict[str, str]]) -> None:
    """Write incident records to CSV, newest first.

    Args:
        csv_path: Destination path.
        records: List of record dictionaries.
    """
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=lambda r: r.get("created_at") or "", reverse=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv_module.DictWriter(handle, fieldnames=INCIDENT_CSV_HEADERS)
        writer.writeheader()
        for record in ordered:
            writer.writerow({key: record.get(key, "") for key in INCIDENT_CSV_HEADERS})


def refresh_open_incidents(
    client: PagerDutyClient,
    existing: Dict[str, Dict[str, str]],
) -> int:
    """Refresh the status of all non-resolved incidents by fetching them by id.

    Instead of issuing a single windowed query that spans from the *oldest* open
    incident (which explodes into a year-long, multi-page request whenever a stale
    open incident exists and causes read timeouts / HTTP 500s), this fetches each
    open incident directly via ``GET /incidents/{id}``. Each call is small, bounded,
    and independent of how old the incident is. Individual failures are logged and
    skipped so one bad id never aborts the whole refresh.

    Args:
        client: PagerDuty client.
        existing: Mapping of incident id to record (mutated in place).

    Returns:
        Number of incidents whose record was refreshed.
    """
    open_rows = [
        record
        for record in existing.values()
        if (record.get("status") or "").lower() != "resolved" and record.get("id")
    ]
    if not open_rows:
        return 0

    include_key = "include[]"
    include_params = [
        (include_key, "acknowledgers"),
        (include_key, "assignees"),
        (include_key, "teams"),
        (include_key, "services"),
    ]

    updated = 0
    for record in open_rows:
        incident_id = record["id"]
        try:
            response = client.request(
                "GET",
                f"/incidents/{incident_id}",
                params=include_params,
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001 - refresh must be resilient per id
            err_console.print(
                f"[yellow]Warning: could not refresh incident {incident_id}: {exc}[/yellow]"
            )
            continue

        incident = response.get("incident") if isinstance(response, dict) else None
        if incident and incident.get("id"):
            existing[incident_id] = extract_incident_record(incident)
            updated += 1

    return updated


# Result data (CSV/JSON/tables) goes to stdout via `console`.
# Status, progress, and error messages go to stderr via `err_console`,
# so redirecting stdout (e.g. `--csv > out.csv`) yields only the data.
console = Console()
err_console = Console(stderr=True)

AVAILABLE_INCIDENT_STATUSES = ["triggered", "acknowledged", "resolved"]
ALL_INCIDENT_STATUSES_ALIAS = "all"


def normalize_incident_statuses(statuses: Tuple[str, ...]) -> List[str]:
    """Normalize and validate incident status filters.

    Args:
        statuses: Raw status values from Click.

    Returns:
        Normalized list of statuses to send to the API.
    """
    normalized = [status.lower().strip() for status in statuses if status and status.strip()]

    if not normalized:
        return ["triggered", "acknowledged"]

    invalid = [
        status
        for status in normalized
        if status not in AVAILABLE_INCIDENT_STATUSES and status != ALL_INCIDENT_STATUSES_ALIAS
    ]
    if invalid:
        allowed = ", ".join(AVAILABLE_INCIDENT_STATUSES + [ALL_INCIDENT_STATUSES_ALIAS])
        err_console.print(
            f"[red]Error: Invalid status value(s): {', '.join(invalid)}. "
            f"Allowed: {allowed}.[/red]"
        )
        raise SystemExit(1)

    if ALL_INCIDENT_STATUSES_ALIAS in normalized:
        return AVAILABLE_INCIDENT_STATUSES.copy()

    deduplicated = []
    for status in normalized:
        if status not in deduplicated:
            deduplicated.append(status)

    return deduplicated


def fetch_incidents(
    client: PagerDutyClient,
    team_ids: List[str],
    statuses: List[str],
    title_filter: Optional[str],
    since: Optional[str],
    until: Optional[str],
    limit: int,
    fetch_all: bool,
) -> List[Dict]:
    """Fetch incidents with custom pagination due to complex query requirements.
    
    Args:
        client: PagerDuty client.
        team_ids: List of team IDs to filter by.
        statuses: List of statuses to filter by.
        title_filter: Optional title filter (case-insensitive partial match).
        since: Only include incidents after this date/time (absolute ISO8601).
        until: Only include incidents before this date/time (absolute ISO8601).
        limit: Max results (0 = unlimited).
        fetch_all: Whether to fetch all pages.
    
    Returns:
        List of incident dictionaries created inside the requested window.
    """
    base_url = "https://api.pagerduty.com/incidents"
    offset = 0
    page_size = min(limit, 100) if limit > 0 else 100

    window_start = parse_datetime_value(since, "--since")
    window_end = parse_datetime_value(until, "--until")
    # A window that runs up to (or past) "now" still gains incidents, so a cached
    # page for it would be stale. Only closed historic windows are reused.
    use_cache = (
        window_end is not None
        and window_end < datetime.now(timezone.utc) - CLOSED_WINDOW_MARGIN
    )

    collected = []
    page_count = 0
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=err_console,
        transient=True,
    ) as progress:
        task = progress.add_task("Paging incidents: 0 pages", total=None)
        
        while True:
            page_count += 1
            progress.update(task, description=f"Paging incidents: {page_count} pages")
            
            # Build query parameters. The date window is part of the query string,
            # and therefore part of the client's cache key, so a different window
            # can never be answered from another window's cached response.
            query_params: List[Tuple[str, Any]] = [
                ("limit", page_size),
                ("offset", offset),
            ]
            if since:
                query_params.append(("since", since))
            if until:
                query_params.append(("until", until))
            query_params.extend(("team_ids[]", tid) for tid in team_ids)
            query_params.extend(("statuses[]", st) for st in statuses)
            # Enrich responder data used by the incidents dashboard.
            query_params.extend(
                ("include[]", inc_field)
                for inc_field in ("acknowledgers", "assignees", "teams", "services")
            )

            # urlencode is required: raw timestamps carrying a '+HH:MM' offset would
            # otherwise be decoded server-side as a space and silently shift the window.
            url = f"{base_url}?{urlencode(query_params)}"
            
            try:
                data = client.request("GET", url, use_cache=use_cache)
            except requests.HTTPError as e:
                err_console.print(f"[red]Error: {e}[/red]")
                raise
            
            incidents = data.get("incidents", [])
            page_length = len(incidents)

            # Discard anything outside the requested window before it reaches the
            # caller, regardless of whether the API clamped the range or the page
            # came from the local cache.
            incidents = filter_incidents_to_window(incidents, window_start, window_end)

            # Apply title filter if specified
            if title_filter:
                incidents = [
                    inc for inc in incidents
                    if title_filter.lower() in (inc.get("title") or inc.get("summary") or "").lower()
                ]
            
            collected.extend(incidents)
            
            # Check for more pages. Page length must be measured before filtering,
            # otherwise a filtered-out page looks like the end of the result set.
            more = data.get("more", False)
            
            if more or page_length == page_size:
                offset += page_size
            else:
                break
            
            # Stop early if limit requested
            if not fetch_all and limit > 0 and len(collected) >= limit:
                collected = collected[:limit]
                break
            
            time.sleep(0.2)
    
    err_console.print(f"Paging incidents: {page_count} pages\n")
    return collected


def fetch_incidents_windowed(
    client: PagerDutyClient,
    team_ids: List[str],
    statuses: List[str],
    title_filter: Optional[str],
    since: Optional[str],
    until: Optional[str],
    window_days: int = 28,
    limit: int = 0,
    fetch_all: bool = True,
) -> List[Dict]:
    """Fetch incidents across a large date range using bounded time windows.

    The PagerDuty ``/incidents`` endpoint clamps an open-ended or oversized
    ``since``/``until`` range (it will not return more than roughly one month
    when only ``since`` is supplied). To reliably pull long histories this splits
    the range into explicit ``window_days`` windows and de-duplicates by id.

    Args:
        client: PagerDuty client.
        team_ids: Team IDs to filter by.
        statuses: Statuses to filter by.
        title_filter: Optional title filter.
        since: Absolute ISO8601 start timestamp (required for windowing).
        until: Absolute ISO8601 end timestamp (defaults to now).
        window_days: Size of each query window in days.
        limit: Max results (0 = unlimited).
        fetch_all: Whether to fetch every result in the range.

    Returns:
        De-duplicated list of incident dictionaries, newest first.

    Raises:
        click.BadParameter: If the requested range is empty or inverted.
    """
    if not since:
        return fetch_incidents(
            client, team_ids, statuses, title_filter, since, until, limit, fetch_all
        )

    start = parse_datetime_value(since, "--since")
    end = parse_datetime_value(until, "--until") or datetime.now(timezone.utc)
    if end <= start:
        raise click.BadParameter(
            f"--until ({format_timestamp(end)}) must be after "
            f"--since ({format_timestamp(start)}).",
            param_hint="--until",
        )

    windows: List[Tuple[datetime, datetime]] = []
    window = timedelta(days=window_days)
    cursor = start
    while cursor < end:
        window_end = min(cursor + window, end)
        windows.append((cursor, window_end))
        cursor = window_end

    capped = limit > 0 and not fetch_all
    if capped:
        # A capped request should return the most recent incidents in the range.
        windows.reverse()

    collected: Dict[str, Dict] = {}
    for window_start, window_end in windows:
        batch = fetch_incidents(
            client,
            team_ids,
            statuses,
            title_filter,
            format_timestamp(window_start),
            format_timestamp(window_end),
            max(0, limit - len(collected)) if capped else 0,
            False if capped else fetch_all,
        )
        for inc in batch:
            if inc.get("id"):
                collected[inc["id"]] = inc
        if capped and len(collected) >= limit:
            break

    results = sorted(
        collected.values(), key=lambda inc: inc.get("created_at") or "", reverse=True
    )
    return results[:limit] if capped else results


def extract_team_names(incident: Dict) -> List[str]:
    """Extract team names from incident.
    
    Args:
        incident: Incident dictionary.
    
    Returns:
        List of team names.
    """
    team_names = []
    
    teams = incident.get("teams")
    if teams and isinstance(teams, list):
        for t in teams:
            if isinstance(t, dict):
                team_names.append(t.get("summary") or t.get("name") or "Unknown")
            elif isinstance(t, str):
                team_names.append(t)
    elif incident.get("team") and isinstance(incident["team"], dict):
        team_names.append(incident["team"].get("summary") or incident["team"].get("name") or "Unknown")
    
    return team_names or ["Unknown"]


def display_summary(
    incidents: List[Dict],
    group_by: List[str],
    statuses: List[str]
) -> None:
    """Display incident summary with grouping.
    
    Args:
        incidents: List of incidents.
        group_by: Fields to group by.
        statuses: Status filters applied.
    """
    agg: Dict[str, Dict] = {}
    total_duration = 0
    
    for inc in incidents:
        status = (inc.get("status") or "unknown").lower()
        duration = calculate_incident_duration(inc)
        total_duration += duration
        team_names = extract_team_names(inc)
        
        if "status" in group_by and "team" in group_by:
            for tn in team_names:
                key = f"{status}|{tn}"
                if key not in agg:
                    agg[key] = {"status": status, "team": tn, "count": 0, "duration": 0}
                agg[key]["count"] += 1
                agg[key]["duration"] += duration
        elif "status" in group_by:
            if status not in agg:
                agg[status] = {"count": 0, "duration": 0}
            agg[status]["count"] += 1
            agg[status]["duration"] += duration
        elif "team" in group_by:
            for tn in team_names:
                if tn not in agg:
                    agg[tn] = {"count": 0, "duration": 0}
                agg[tn]["count"] += 1
                agg[tn]["duration"] += duration
    
    status_label = ", ".join(statuses) if statuses else "all"
    console.print(f"\n[bold]Incident summary (statuses: {status_label})[/bold]")
    console.print("-" * 40)
    
    table = Table()
    
    if "status" in group_by and "team" in group_by:
        table.add_column("status")
        table.add_column("team")
        table.add_column("count", justify="right")
        table.add_column("total_duration")
        
        rows = sorted(
            [(v["status"], v["team"], v["count"], v["duration"]) for v in agg.values()],
            key=lambda x: x[2],
            reverse=True
        )
        for status, team, count, dur in rows:
            table.add_row(status, team, str(count), format_duration(dur))
    
    elif "status" in group_by:
        table.add_column("status")
        table.add_column("count", justify="right")
        table.add_column("total_duration")
        
        rows = sorted(
            [(k, v["count"], v["duration"]) for k, v in agg.items()],
            key=lambda x: x[1],
            reverse=True
        )
        for status, count, dur in rows:
            table.add_row(status, str(count), format_duration(dur))
    
    else:  # team only
        table.add_column("team")
        table.add_column("count", justify="right")
        table.add_column("total_duration")
        
        rows = sorted(
            [(k, v["count"], v["duration"]) for k, v in agg.items()],
            key=lambda x: x[1],
            reverse=True
        )
        for team, count, dur in rows:
            table.add_row(team, str(count), format_duration(dur))
    
    console.print(table)
    console.print(f"\nTotal duration across all incidents: {format_duration(total_duration)}")


def prepare_incident_rows(incidents: List[Dict]) -> Tuple[List[List[str]], List[str]]:
    """Prepare incident data for table/CSV output.
    
    Args:
        incidents: List of incidents.
    
    Returns:
        Tuple of (rows, headers).
    """
    headers = ["id", "status", "created_at", "title", "teams", "duration", "url"]
    rows = []
    
    for inc in incidents:
        inc_id = inc.get("id", "")
        status = inc.get("status", "")
        created_at = inc.get("created_at") or inc.get("first_triggered_at", "")
        title = inc.get("title") or inc.get("summary", "")
        html_url = inc.get("html_url") or inc.get("incident_url", "")
        
        duration = calculate_incident_duration(inc)
        duration_formatted = format_duration(duration)
        
        team_names = extract_team_names(inc)
        teams_str = ",".join([t for t in team_names if t != "Unknown"])
        
        rows.append([inc_id, status, created_at, title, teams_str, duration_formatted, html_url])
    
    return rows, headers


@click.command("incidents")
@click.option("--teams-csv", help="Path to CSV file listing teams")
@click.option("--team", multiple=True, help="Team ID (can be provided multiple times)")
@click.option(
    "--status", "statuses", multiple=True, default=["triggered", "acknowledged"],
    help=(
        "Incident status filter (repeatable). Available: triggered, acknowledged, resolved. "
        "Use 'all' to include every status. Default: triggered, acknowledged."
    )
)
@click.option("--title", help="Filter incidents by title (case-insensitive partial match)")
@click.option(
    "--since",
    help=(
        "Only include incidents after this date/time. Accepts an absolute ISO8601 "
        "timestamp or a relative ISO8601 duration (e.g. P1D, P2W, P1M, P1Y)."
    ),
)
@click.option(
    "--until",
    help=(
        "Only include incidents before this date/time. Accepts an absolute ISO8601 "
        "timestamp or a relative ISO8601 duration (e.g. P1D). Defaults to now."
    ),
)
@click.option("--summary", is_flag=True, help="Show a summary rather than full list")
@click.option(
    "--group-by", multiple=True, default=["team"],
    help="Group summary by: status, team (repeatable)"
)
@click.option("--limit", type=int, default=0, help="Page size / limit (0 = unlimited)")
@click.option("--all", "fetch_all", is_flag=True, help="Page through all results")
@click.option(
    "--csv-path",
    help=(
        "Write incidents to a persistent CSV dataset at this path (enriched schema). "
        "Used by the incidents dashboard."
    ),
)
@click.option(
    "--append",
    is_flag=True,
    help=(
        "Incremental mode: append newly created incidents to --csv-path and refresh "
        "the status of open incidents already in the dataset (batched). Default --since is P1D."
    ),
)
@click.option("--token", envvar="PAGERDUTY_API_TOKEN", help="PagerDuty API token")
@click.option("--reset-token", is_flag=True, help="Remove cached token")
@click.option("--skip-cache", is_flag=True, help="Skip API response cache")
@click.option("--json", "output_json_flag", is_flag=True, help="Output raw JSON")
@click.option("--csv", "output_csv_flag", is_flag=True, help="Output as CSV to stdout")
def incidents_command(
    teams_csv: Optional[str],
    team: Tuple[str, ...],
    statuses: Tuple[str, ...],
    title: Optional[str],
    since: Optional[str],
    until: Optional[str],
    summary: bool,
    group_by: Tuple[str, ...],
    limit: int,
    fetch_all: bool,
    csv_path: Optional[str],
    append: bool,
    token: Optional[str],
    reset_token: bool,
    skip_cache: bool,
    output_json_flag: bool,
    output_csv_flag: bool
):
    """Query PagerDuty API and list incidents."""
    # Resolve token
    resolved_token = resolve_pagerduty_token(token, reset_token)
    if not resolved_token:
        err_console.print("[red]Error: PagerDuty API token required.[/red]")
        raise SystemExit(1)
    
    # Collect team IDs
    team_ids = list(team)
    if teams_csv:
        csv_teams = CsvHelper.parse_teams_csv(teams_csv)
        for t in csv_teams:
            if t not in team_ids:
                team_ids.append(t)
    
    # The persistent dataset should capture every status by default.
    dataset_mode = bool(csv_path) or append
    if append and not csv_path:
        err_console.print("[red]Error: --append requires --csv-path.[/red]")
        raise SystemExit(1)

    if dataset_mode and set(s.lower() for s in statuses) == {"triggered", "acknowledged"}:
        status_list = AVAILABLE_INCIDENT_STATUSES.copy()
    else:
        status_list = normalize_incident_statuses(statuses)

    # Normalise the requested window up-front so that every downstream query, and
    # therefore every cache key, is derived from absolute UTC timestamps.
    if append and not since:
        since = "P1D"
    try:
        since_dt = parse_datetime_value(since, "--since")
        until_dt = parse_datetime_value(until, "--until")
    except click.BadParameter as e:
        err_console.print(f"[red]Error: {e.format_message()}[/red]")
        raise SystemExit(1)

    if since_dt and until_dt and until_dt <= since_dt:
        err_console.print(
            f"[red]Error: --until ({format_timestamp(until_dt)}) must be after "
            f"--since ({format_timestamp(since_dt)}).[/red]"
        )
        raise SystemExit(1)

    since = format_timestamp(since_dt) if since_dt else None
    until = format_timestamp(until_dt) if until_dt else None
    if since or until:
        err_console.print(
            f"[cyan]Date window: {since or 'API default'} → {until or 'now'}[/cyan]"
        )
    
    # Validate group-by
    group_by_list = [g.lower().strip() for g in group_by if g]
    if summary and not group_by_list:
        group_by_list = ["status"]
    
    allowed_groups = ["status", "team"]
    invalid = [g for g in group_by_list if g not in allowed_groups]
    if invalid:
        err_console.print(f"[red]Error: Invalid group-by value(s): {', '.join(invalid)}. Allowed: status, team.[/red]")
        raise SystemExit(1)
    
    # Create client and validate token
    client = PagerDutyClient(resolved_token, skip_cache=skip_cache)
    client.clear_expired_cache()

    err_console.print("[cyan]Validating API token...[/cyan]")
    try:
        client.validate_token(err_console)
        err_console.print("[green]Token validated successfully[/green]\n")
    except Exception:
        raise SystemExit(1)

    # Persistent dataset mode: write/refresh outputs/csv/pagerduty_incidents.csv.
    if dataset_mode:
        existing: Dict[str, Dict[str, str]] = {}
        if append:
            existing = load_existing_incidents(csv_path)
            err_console.print(
                f"[cyan]Loaded {len(existing)} existing incidents from {csv_path}[/cyan]"
            )
            try:
                refreshed = refresh_open_incidents(client, existing)
                err_console.print(f"[green]Refreshed status of {refreshed} open incidents[/green]")
            except Exception as e:
                err_console.print(
                    f"[yellow]Warning: could not refresh open incidents: {e}. "
                    f"Continuing with existing statuses.[/yellow]"
                )

        try:
            new_incidents = fetch_incidents_windowed(
                client, team_ids, status_list, title,
                since, until,
            )
        except Exception as e:
            err_console.print(f"[red]Failed to fetch incidents: {e}[/red]")
            raise SystemExit(1)

        added = 0
        for inc in new_incidents:
            incident_id = inc.get("id")
            if not incident_id:
                continue
            if incident_id not in existing:
                added += 1
            existing[incident_id] = extract_incident_record(inc)

        write_incidents_csv(csv_path, list(existing.values()))
        err_console.print(
            f"[green]✓ Wrote {len(existing)} incidents to {csv_path} "
            f"({added} new).[/green]"
        )
        return

    try:
        # Windowed fetching is required for the analysis outputs too: a single
        # request for a wide range is silently clamped by the API, which returns
        # roughly a month of data no matter how large a window was requested.
        incidents = fetch_incidents_windowed(
            client, team_ids, status_list, title,
            since, until,
            limit=limit, fetch_all=fetch_all,
        )
    except Exception as e:
        err_console.print(f"[red]Failed to fetch incidents: {e}[/red]")
        raise SystemExit(1)

    # Data outputs write only to stdout; diagnostics above went to stderr.
    if output_csv_flag:
        rows, headers = prepare_incident_rows(incidents)
        output_csv(rows, headers)
        return

    if output_json_flag:
        output_json(incidents, "incidents")
        return

    if not incidents:
        err_console.print("[yellow]No incidents found for the provided filters.[/yellow]")
        return

    if summary:
        display_summary(incidents, group_by_list, status_list)
    else:
        rows, headers = prepare_incident_rows(incidents)
        table = Table()
        for header in headers:
            table.add_column(header)
        for row in rows:
            table.add_row(*row)
        console.print(table)

    err_console.print(f"Fetched {len(incidents)} incidents.")
