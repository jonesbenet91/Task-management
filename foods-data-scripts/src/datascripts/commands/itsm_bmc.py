#!/usr/bin/env python3
"""
BMC Remedy ITSM Report Extractor

Extracts incident and change request data from BMC Remedy REST API.

Usage:
    python itsm_bmc.py --group "MTP ASO SUPPORT" --summary
    python itsm_bmc.py --groups-csv groups.csv --csv
    python itsm_bmc.py --change-requests --days 30 --csv
    python itsm_bmc.py --change-requests --cr-product-tier1 "Food" --csv --no-verify-ssl
    python itsm_bmc.py --change-requests --cr-coordinator-group "MTP ASO SUPPORT" --csv
"""

import os
import sys
import json
import csv
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests
from requests.auth import HTTPBasicAuth
import click
from click.core import ParameterSource
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
import urllib3

from ..services.date_range import parse_datetime_value

console = Console()


def resolve_since(since: Optional[datetime], since_days: int) -> datetime:
    """Resolve the lower bound of a query window.

    Args:
        since: Explicit start of the window (takes precedence when set).
        since_days: Number of days to look back when `since` is not given.

    Returns:
        Timezone-aware start of the window. BMC expects epoch seconds, so the
        value only has to be aware for `.timestamp()` to be unambiguous.
    """
    if since is not None:
        return since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timedelta(days=since_days)


def window_days(since: datetime) -> int:
    """Return the width of a window in whole days, for display and summaries.

    Args:
        since: Start of the window.

    Returns:
        Number of days between `since` and now (minimum 1).
    """
    delta = datetime.now(timezone.utc) - (
        since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    )
    return max(1, round(delta.total_seconds() / 86400))


def describe_window(since: datetime) -> str:
    """Render a human-readable description of the query window.

    Args:
        since: Start of the window.

    Returns:
        Text such as ``last 30 day(s) (since 2026-07-26 14:00 UTC)``.
    """
    return (
        f"last {window_days(since)} day(s) "
        f"(since {since.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC)"
    )


class BMCRemedyClient:
    """Client for BMC Remedy AR System REST API operations."""
    
    def __init__(self, base_url: str, username: str, password: str, verify_ssl: bool = True, verbose: bool = False):
        """
        Initialize BMC Remedy client and authenticate.
        
        Args:
            base_url: BMC instance base URL (e.g., https://mnscorp-rod-restapi.onbmc.com)
            username: BMC username
            password: BMC password
            verify_ssl: Whether to verify SSL certificates
            verbose: Enable verbose debug output
        """
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.verbose = verbose
        self.jwt_token = None
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })
        
        if not self.verify_ssl:
            console.print("[yellow]⚠ Warning: SSL certificate verification is disabled[/yellow]")
            warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
        
        # Authenticate
        self._authenticate()
    
    def _authenticate(self):
        """Authenticate with BMC Remedy and get JWT token."""
        login_url = f'{self.base_url}/api/jwt/login'
        
        if self.verbose:
            console.print(f"[cyan]Debug: Login URL: {login_url}[/cyan]")
            console.print(f"[cyan]Debug: Username: {self.username}[/cyan]")
        
        try:
            # BMC Remedy requires specific headers for JWT login
            login_headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'text/plain'  # BMC returns plain text token
            }
            
            # Prepare form data
            form_data = f'username={self.username}&password={self.password}'
            
            if self.verbose:
                console.print(f"[cyan]Debug: Login Headers: {login_headers}[/cyan]")
            
            response = self.session.post(
                login_url,
                data=form_data,
                headers=login_headers,
                timeout=30,
                verify=self.verify_ssl
            )
            
            if self.verbose:
                console.print(f"[cyan]Debug: Response Status: {response.status_code}[/cyan]")
                console.print(f"[cyan]Debug: Response Headers: {dict(response.headers)}[/cyan]")
                console.print(f"[cyan]Debug: Response: {response.text[:500]}[/cyan]")
            
            response.raise_for_status()
            
            # Extract JWT token (plain text response)
            self.jwt_token = response.text.strip().strip('"')
            
            if self.verbose:
                console.print(f"[cyan]Debug: Token length: {len(self.jwt_token)}[/cyan]")
                console.print(f"[cyan]Debug: Token (first 50 chars): {self.jwt_token[:50]}...[/cyan]")
            
            # Update session headers with AR-JWT token for subsequent requests (BMC Remedy format)
            self.session.headers.update({
                'Authorization': f'AR-JWT {self.jwt_token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            })
            
            console.print("[green]✓ Successfully authenticated with BMC Remedy[/green]")
            
        except Exception as e:
            console.print(f"[red]Authentication failed: {e}[/red]")
            if self.verbose:
                import traceback
                console.print(f"[red]{traceback.format_exc()}[/red]")
            raise
    
    def get_incidents(
        self,
        group_name: str,
        since_days: int = 60,
        state: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch incidents for a specific group from BMC HPD form.
        
        Args:
            group_name: Assignment group name
            since_days: Number of days to look back (ignored when `since` is given)
            state: Filter by state ('open', 'closed', or 'all')
            since: Explicit lower bound on Submit Date, overriding `since_days`
        
        Returns:
            List of incident dictionaries
        """
        # Calculate epoch timestamp for BMC Remedy (seconds since epoch)
        since_datetime = resolve_since(since, since_days)
        since_timestamp = int(since_datetime.timestamp())
        
        # Build BMC query with proper spacing (as per BMC API format)
        query_parts = [f"'Assigned Group' = \"{group_name}\""]
        query_parts.append(f"'Submit Date' > {since_timestamp}")
        
        # Add status filter
        if state and state.lower() != 'all':
            if state.lower() == 'open':
                # Open statuses: New, Assigned, In Progress, Pending
                query_parts.append("'Status' IN (\"New\", \"Assigned\", \"In Progress\", \"Pending\")")
            elif state.lower() == 'closed':
                # Closed statuses: Resolved, Closed, Cancelled
                query_parts.append("'Status' IN (\"Resolved\", \"Closed\", \"Cancelled\")")
        
        bmc_query = ' AND '.join(query_parts)
        
        params = {
            'q': bmc_query,
            'fields': 'values(Incident Number,Description,Status,Priority,Assigned Group,Assignee,Corporate ID,Product Categorization Tier 1,Product Categorization Tier 2,Product Categorization Tier 3,Submit Date,Last Modified Date)',
            'limit': 1000
        }
        
        if self.verbose:
            console.print(f"[cyan]Debug: Query: {bmc_query}[/cyan]")
            console.print(f"[cyan]Debug: Since timestamp: {since_timestamp} ({since_datetime})[/cyan]")
            console.print(f"[cyan]Debug: Request Headers: {dict(self.session.headers)}[/cyan]")
        
        try:
            response = self.session.get(
                f'{self.base_url}/api/arsys/v1/entry/HPD:Help Desk',
                params=params,
                timeout=30,
                verify=self.verify_ssl
            )
            
            if self.verbose:
                console.print(f"[cyan]Debug: Response Status: {response.status_code}[/cyan]")
                console.print(f"[cyan]Debug: Response: {response.text[:500]}[/cyan]")
            
            response.raise_for_status()
            
            data = response.json()
            entries = data.get('entries', [])
            
            # Normalize response
            incidents = []
            for entry in entries:
                values = entry.get('values', {})
                incidents.append({
                    'number': values.get('Incident Number', ''),
                    'short_description': values.get('Description', ''),
                    'state': values.get('Status', ''),
                    'priority': values.get('Priority', ''),
                    'opened_at': values.get('Submit Date', ''),
                    'closed_at': values.get('Last Modified Date', ''),
                    'assignment_group': values.get('Assigned Group', ''),
                    'assigned_to': values.get('Assignee', ''),
                    'corporate_id': values.get('Corporate ID', ''),
                    'category_tier1': values.get('Product Categorization Tier 1', ''),
                    'category_tier2': values.get('Product Categorization Tier 2', ''),
                    'category_tier3': values.get('Product Categorization Tier 3', '')
                })
            
            return incidents
            
        except requests.RequestException as e:
            console.print(f"[red]Error fetching incidents for {group_name}: {e}[/red]")
            return []
    
    def get_incident_summary(
        self,
        group_name: str,
        since_days: int = 60,
        since: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Get incident summary statistics for a group.

        Args:
            group_name: Assignment group name.
            since_days: Number of days to look back (ignored when `since` is given).
            since: Explicit lower bound on Submit Date, overriding `since_days`.

        Returns:
            Summary dictionary including the window actually queried.
        """
        since_datetime = resolve_since(since, since_days)
        incidents = self.get_incidents(group_name, since=since_datetime)
        
        # BMC statuses: New, Assigned, In Progress, Pending, Resolved, Closed, Cancelled
        open_statuses = ['New', 'Assigned', 'In Progress', 'Pending']
        closed_statuses = ['Resolved', 'Closed', 'Cancelled']
        
        open_count = sum(1 for i in incidents if i.get('state', '').strip() in open_statuses)
        closed_count = sum(1 for i in incidents if i.get('state', '').strip() in closed_statuses)
        
        priority_breakdown = {}
        for incident in incidents:
            priority = incident.get('priority', 'Unknown')
            priority_breakdown[priority] = priority_breakdown.get(priority, 0) + 1
        
        return {
            'group': group_name,
            'total': len(incidents),
            'open': open_count,
            'closed': closed_count,
            'priority_breakdown': priority_breakdown,
            'period_days': window_days(since_datetime),
            'since': since_datetime.isoformat(),
        }

    def get_change_requests(
        self,
        since_days: int = 30,
        coordinator_group: Optional[str] = None,
        product_tier1: Optional[str] = None,
        product_tier3: Optional[str] = None,
        status_filter: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch change requests from BMC CHG:Infrastructure Change form.

        Args:
            since_days: Number of days to look back (ignored when `since` is given).
            coordinator_group: Filter by Change Coordinator Group.
            product_tier1: Filter by Product Categorization Tier 1.
            status_filter: Filter by status ('open', 'closed', or 'all').
            since: Explicit lower bound on Submit Date, overriding `since_days`.

        Returns:
            List of change request dictionaries.
        """
        since_datetime = resolve_since(since, since_days)
        since_timestamp = int(since_datetime.timestamp())

        query_parts = [f"'Submit Date' > {since_timestamp}"]

        if coordinator_group:
            query_parts.append(f"'Change Coordinator Group' = \"{coordinator_group}\"")

        if product_tier1:
            # Note: server-side filtering on categorisation fields is unreliable in BMC
            # (field alias varies per instance). We fetch all and post-filter below.
            pass

        if status_filter and status_filter.lower() != 'all':
            if status_filter.lower() == 'open':
                query_parts.append(
                    "'Status' IN (\"Draft\", \"Request For Authorization\", "
                    "\"Request For Change\", \"Planning In Progress\", "
                    "\"Scheduled For Review\", \"Scheduled For Approval\", "
                    "\"Scheduled\", \"Implementation In Progress\")"
                )
            elif status_filter.lower() == 'closed':
                query_parts.append(
                    "'Status' IN (\"Completed\", \"Rejected\", \"Cancelled\", \"Closed\")"
                )

        bmc_query = ' AND '.join(query_parts)

        # Do NOT send a 'fields' filter — some BMC instances reject unknown
        # field names with a 400.  We request all fields and pick what we need.
        PAGE_SIZE = 1000

        if self.verbose:
            console.print(f"[cyan]Debug: CR Query: {bmc_query}[/cyan]")

        # Helper: try several candidate key names and return the first that has a value
        def _pick(v: dict, *candidates: str, default: str = '') -> str:
            for key in candidates:
                val = v.get(key)
                if val is not None and str(val).strip() != '':
                    return str(val)
            return default

        # Helper: convert BMC epoch ms/s to a readable date string
        def _fmt_ts(ts) -> str:
            if ts is None:
                return ''
            try:
                ts_int = int(ts)
                # BMC sometimes returns milliseconds
                if ts_int > 9_999_999_999:
                    ts_int //= 1000
                return datetime.fromtimestamp(ts_int).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                return str(ts)

        try:
            # Paginate through all results using offset
            all_entries = []
            offset = 0
            while True:
                params: Dict[str, Any] = {
                    'q': bmc_query,
                    'limit': PAGE_SIZE,
                    'offset': offset,
                }
                response = self.session.get(
                    f'{self.base_url}/api/arsys/v1/entry/CHG:Infrastructure Change',
                    params=params,
                    timeout=30,
                    verify=self.verify_ssl,
                )

                if self.verbose:
                    console.print(f"[cyan]Debug: CR Response Status (offset={offset}): {response.status_code}[/cyan]")
                    if offset == 0:
                        console.print(f"[cyan]Debug: CR Response (first 1000 chars): {response.text[:1000]}[/cyan]")

                response.raise_for_status()

                data = response.json()
                page_entries = data.get('entries', [])
                all_entries.extend(page_entries)

                if self.verbose:
                    console.print(f"[cyan]Debug: Page offset={offset} returned {len(page_entries)} entries[/cyan]")

                # If fewer than PAGE_SIZE returned, we've reached the last page
                if len(page_entries) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
                console.print(f"[cyan]  Fetched {len(all_entries)} records so far, fetching next page...[/cyan]")

            entries = all_entries
            console.print(f"[cyan]ℹ Total change requests fetched from API: {len(entries)}[/cyan]")

            change_requests = []
            for entry in entries:
                v = entry.get('values', {})

                # Use multiple candidate names per field to tolerate BMC naming variations
                change_requests.append({
                    'cr_number':          _pick(v, 'Infrastructure Change ID', 'Change ID', 'CR ID'),
                    'summary':            _pick(v, 'Description', 'Summary', 'Change Summary'),
                    'author':             _pick(v, 'Requested By', 'Requested By Login',
                                                'First Name', 'Submitter'),
                    'impact':             _pick(v, 'Impact'),
                    'urgency':            _pick(v, 'Urgency'),
                    'status':             _pick(v, 'Status', 'Change Request Status'),
                    'cr_create_date':     _fmt_ts(_pick(v, 'Submit Date', 'Create Date',
                                                        'Date/Time Submitted') or None),
                    'planned_start_date': _fmt_ts(_pick(v, 'Scheduled Start Date',
                                                        'Planned Start Date') or None),
                    'planned_end_date':   _fmt_ts(_pick(v, 'Scheduled End Date',
                                                        'Planned End Date') or None),
                    'actual_start_date':  _fmt_ts(_pick(v, 'Actual Start Date',
                                                        'Actual Start') or None),
                    'cr_end_date':        _fmt_ts(_pick(v, 'Actual End Date', 'Completion Date',
                                                        'Actual End', 'Close Date') or None),
                    'category_tier1':     _pick(v,
                                                'z_Product Categorization Tier 1',
                                                'Product Cat Tier 1',
                                                'Categorization Tier 1',
                                                'z1D Prod Categorization Tier 1'),
                    'category_tier2':     _pick(v,
                                                'z_Product Categorization Tier 2',
                                                'Product Cat Tier 2',
                                                'Categorization Tier 2',
                                                'z1D Prod Categorization Tier 2'),
                    'category_tier3':     _pick(v,
                                                'z_Product Categorization Tier 3',
                                                'Product Cat Tier 3',
                                                'Categorization Tier 3',
                                                'z1D Prod Categorization Tier 3'),
                    'outage_required':    _pick(v, 'Outage Required'),
                    'outage_start_date':  _fmt_ts(_pick(v, 'Outage Start Date') or None),
                    'outage_end_date':    _fmt_ts(_pick(v, 'Outage End Date') or None),
                })

            if product_tier1:
                change_requests = [
                    cr for cr in change_requests
                    if product_tier1.strip().lower() in cr.get('category_tier1', '').strip().lower()
                ]

            if product_tier3:
                change_requests = [
                    cr for cr in change_requests
                    if product_tier3.strip().lower() in cr.get('category_tier3', '').strip().lower()
                ]

            # Always exclude Cancelled CRs from output
            before = len(change_requests)
            change_requests = [
                cr for cr in change_requests
                if cr.get('status', '').strip().lower() != 'cancelled'
            ]
            excluded = before - len(change_requests)
            if excluded:
                console.print(f"[yellow]ℹ Excluded {excluded} Cancelled CR(s) from output[/yellow]")

            return change_requests

        except requests.RequestException as e:
            console.print(f"[red]Error fetching change requests: {e}[/red]")
            return []


def load_groups_from_csv(csv_path: str) -> List[str]:
    """Load group names from CSV file."""
    groups = []
    
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            # Find group column
            group_col = None
            for col in reader.fieldnames:
                if col.lower() in ['group', 'group_name', 'assignment_group', 'team']:
                    group_col = col
                    break
            
            if not group_col:
                console.print(f"[red]No group column found in {csv_path}[/red]")
                return []
            
            for row in reader:
                group = row.get(group_col, '').strip()
                if group:
                    groups.append(group)
        
        return groups
        
    except Exception as e:
        console.print(f"[red]Error reading CSV file: {e}[/red]")
        return []


def export_to_csv(incidents: List[Dict[str, Any]], output_file: str):
    """Export incidents to CSV file."""
    if not incidents:
        console.print("[yellow]No incidents to export[/yellow]")
        return
    
    fieldnames = ['number', 'short_description', 'state', 'priority', 'opened_at', 
                  'closed_at', 'assignment_group', 'assigned_to', 'corporate_id',
                  'category_tier1', 'category_tier2', 'category_tier3']
    
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(incidents)
    
    console.print(f"[green]Exported {len(incidents)} incidents to {output_file}[/green]")


def export_to_json(data: Any, output_file: str):
    """Export data to JSON file."""
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    console.print(f"[green]Exported data to {output_file}[/green]")


def export_change_requests_to_csv(change_requests: List[Dict[str, Any]], output_file: str):
    """Export change requests to CSV file."""
    if not change_requests:
        console.print("[yellow]No change requests to export[/yellow]")
        return

    fieldnames = [
        'cr_number', 'summary', 'author',
        'impact', 'urgency', 'status',
        'cr_create_date', 'planned_start_date', 'planned_end_date',
        'actual_start_date', 'cr_end_date', 'lead_time_hours',
        'category_tier1', 'category_tier2', 'category_tier3',
        'outage_required', 'outage_start_date', 'outage_end_date', 'outage_duration_hours',
    ]

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(change_requests)

    console.print(f"[green]Exported {len(change_requests)} change requests to {output_file}[/green]")


def _compute_lead_time(cr: Dict[str, Any]) -> float:
    """Return lead time in hours between cr_create_date and cr_end_date.
    For open CRs, use current time as the end."""
    def _parse(dt_str: str) -> Optional[datetime]:
        if not dt_str:
            return None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z'):
            try:
                return datetime.strptime(dt_str, fmt)
            except ValueError:
                continue
        return None

    start = _parse(cr.get('cr_create_date', ''))
    if start is None:
        return 0.0
    # Make start timezone-naive for comparison
    if start.tzinfo is not None:
        start = start.replace(tzinfo=None)

    end_str = cr.get('cr_end_date', '')
    end = _parse(end_str) if end_str else None
    if end is None:
        end = datetime.now()
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)

    delta = end - start
    return round(delta.total_seconds() / 3600, 2)


def _compute_outage_duration(cr: Dict[str, Any]) -> float:
    """Return outage duration in hours between outage_start_date and outage_end_date.
    Returns 0.0 if either date is missing or outage_required is not 'Yes'."""
    if cr.get('outage_required', '').strip().lower() not in ('yes', 'true', '1'):
        return 0.0

    def _parse(dt_str: str) -> Optional[datetime]:
        if not dt_str:
            return None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z'):
            try:
                return datetime.strptime(dt_str, fmt)
            except ValueError:
                continue
        return None

    start = _parse(cr.get('outage_start_date', ''))
    end   = _parse(cr.get('outage_end_date', ''))
    if start is None or end is None:
        return 0.0
    if start.tzinfo is not None:
        start = start.replace(tzinfo=None)
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)
    delta = end - start
    return round(max(delta.total_seconds(), 0) / 3600, 2)


def display_cr_summary_table(change_requests: List[Dict[str, Any]]):
    """Display a summary grouped by category_tier3 with counts, lead time and outage stats."""
    # Compute lead time and outage duration on each CR
    for cr in change_requests:
        cr['lead_time_hours']      = _compute_lead_time(cr)
        cr['outage_duration_hours'] = _compute_outage_duration(cr)

    # Group by tier3
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for cr in change_requests:
        key = cr.get('category_tier3', '') or '(uncategorised)'
        groups.setdefault(key, []).append(cr)

    closed_statuses = {'Completed', 'Rejected', 'Closed'}

    table = Table(title=f"Change Request Summary by Category Tier 3 ({len(change_requests)} total)")
    table.add_column("Category Tier 3",      style="cyan",    no_wrap=False, min_width=25)
    table.add_column("Total",                style="magenta", no_wrap=True)
    table.add_column("Open",                 style="yellow",  no_wrap=True)
    table.add_column("Closed",               style="green",   no_wrap=True)
    table.add_column("Avg Lead Time (h)",    style="white",   no_wrap=True)
    table.add_column("Min (h)",              style="white",   no_wrap=True)
    table.add_column("Max (h)",              style="white",   no_wrap=True)
    table.add_column("Outage CRs",           style="red",     no_wrap=True)
    table.add_column("Avg Outage (h)",       style="red",     no_wrap=True)

    for tier3, crs in sorted(groups.items()):
        total        = len(crs)
        closed       = sum(1 for c in crs if c.get('status', '') in closed_statuses)
        open_        = total - closed
        times        = [c['lead_time_hours'] for c in crs]
        avg_lt       = round(sum(times) / len(times), 2) if times else 0.0
        min_lt       = min(times) if times else 0.0
        max_lt       = max(times) if times else 0.0
        outage_crs   = [c for c in crs if c.get('outage_required', '').strip().lower() in ('yes', 'true', '1')]
        outage_times = [c['outage_duration_hours'] for c in outage_crs]
        avg_outage   = round(sum(outage_times) / len(outage_times), 2) if outage_times else 0.0
        table.add_row(
            tier3,
            str(total),
            str(open_),
            str(closed),
            str(avg_lt),
            str(min_lt),
            str(max_lt),
            str(len(outage_crs)),
            str(avg_outage),
        )

    console.print(table)


def display_summary_table(summaries: List[Dict[str, Any]], since: Optional[datetime] = None):
    """Display incident summaries in a rich table.

    Args:
        summaries: Per-group summary dictionaries.
        since: Start of the queried window, used for the table title.
    """
    period = f"Last {window_days(since)} Days" if since else "Query Period"
    table = Table(title=f"Incident Summary by Group ({period})")
    
    table.add_column("Group", style="cyan", no_wrap=True)
    table.add_column("Total", style="magenta")
    table.add_column("Open", style="yellow")
    table.add_column("Closed", style="green")
    table.add_column("Open %", style="red")
    
    for summary in summaries:
        total = summary['total']
        open_count = summary['open']
        closed_count = summary['closed']
        open_pct = f"{(open_count/total*100):.1f}%" if total > 0 else "0%"
        
        table.add_row(
            summary['group'],
            str(total),
            str(open_count),
            str(closed_count),
            open_pct
        )
    
    console.print(table)


@click.command()
@click.option('--group', help='Single group name to query')
@click.option('--groups-csv', type=click.Path(exists=True), help='CSV file with list of groups')
@click.option('--days', default=60, help='Number of days to look back (default: 60)')
@click.option(
    '--since',
    help=(
        'Only include records submitted after this date/time. Accepts an absolute '
        'ISO8601 timestamp (2026-06-01, 2026-06-01T09:30:00Z) or a relative ISO8601 '
        'duration (P1D, P2W, P1M, P1Y). Takes precedence over --days.'
    ),
)
@click.option('--state', type=click.Choice(['all', 'open', 'closed']), default='all', help='Filter by incident state')
@click.option('--csv', 'output_csv', is_flag=True, help='Output as CSV')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.option('--summary', is_flag=True, help='Show summary statistics only')
@click.option('--output', help='Output file path (auto-generated if not specified)')
@click.option('--base-url', envvar='ITSM_BASE_URL', default='https://mnscorp-rod-restapi.onbmc.com', help='BMC instance URL')
@click.option('--username', envvar='ITSM_USERNAME', help='BMC username')
@click.option('--password', envvar='ITSM_PASSWORD', help='BMC password')
@click.option('--no-verify-ssl', is_flag=True, help='Disable SSL certificate verification')
@click.option('--verbose', is_flag=True, help='Verbose output')
# ── Change Request options ──────────────────────────────────────────────────
@click.option('--change-requests', 'change_requests', is_flag=True,
              help='Extract change requests instead of incidents')
@click.option('--cr-coordinator-group', 'cr_coordinator_group',
              help='Filter change requests by Coordinator Group')
@click.option('--cr-product-tier1', 'cr_product_tier1',
              help='Filter change requests by Product Categorization Tier 1')
@click.option('--cr-product-tier3', 'cr_product_tier3',
              help='Filter change requests by Product Categorization Tier 3')
@click.option('--cr-status', 'cr_status',
              type=click.Choice(['all', 'open', 'closed']), default='all',
              help='Filter change requests by status (default: all)')
def main(group, groups_csv, days, since, state, output_csv, output_json,
         summary, output, base_url, username, password, no_verify_ssl, verbose,
         change_requests, cr_coordinator_group, cr_product_tier1, cr_product_tier3, cr_status):
    """
    Extract BMC Remedy incident and change request reports.

    Examples:
        # Single group with summary
        python itsm_bmc.py --group "MTP ASO SUPPORT" --summary

        # Multiple groups from CSV
        python itsm_bmc.py --groups-csv groups.csv --csv

        # Last 30 days, open incidents only
        python itsm_bmc.py --groups-csv groups.csv --days 30 --state open --json

        # Relative window: last quarter / last year of incidents
        python itsm_bmc.py --groups-csv groups.csv --since P3M --csv
        python itsm_bmc.py --groups-csv groups.csv --since P1Y --csv

        # Absolute window: everything submitted since a fixed date
        python itsm_bmc.py --groups-csv groups.csv --since 2026-01-01 --csv

        # All change requests for last 30 days
        python itsm_bmc.py --change-requests --days 30 --csv

        # Change requests filtered by product tier
        python itsm_bmc.py --change-requests --cr-product-tier1 "Food" --csv --no-verify-ssl

        # Change requests filtered by coordinator group
        python itsm_bmc.py --change-requests --cr-coordinator-group "MTP ASO SUPPORT" --csv

    Environment Variables:
        ITSM_BASE_URL    - BMC instance URL (default: https://mnscorp-rod-restapi.onbmc.com)
        ITSM_USERNAME    - BMC username
        ITSM_PASSWORD    - BMC password
    """
    # Resolve the query window. --since wins over --days; passing both explicitly
    # is ambiguous, so it is rejected rather than silently ignoring one.
    ctx = click.get_current_context()
    days_given = ctx.get_parameter_source('days') == ParameterSource.COMMANDLINE
    if since and days_given:
        raise click.ClickException(
            "--since and --days are mutually exclusive; --since already defines "
            "the start of the window."
        )

    since_dt = parse_datetime_value(since, '--since')
    if since_dt and since_dt > datetime.now(timezone.utc):
        raise click.ClickException(
            f"--since value '{since}' is in the future; it must be a past date/time."
        )

    window_start = resolve_since(since_dt, days)

    # Validate required parameters
    if not base_url:
        raise click.ClickException("BMC base URL is required. Set ITSM_BASE_URL env variable or use --base-url")
    
    if not username:
        raise click.ClickException("BMC username is required. Set ITSM_USERNAME env variable or use --username")
    
    if not password:
        raise click.ClickException("BMC password is required. Set ITSM_PASSWORD env variable or use --password")
    
    # Validate group parameters (not required for change-request mode)
    if not change_requests and not group and not groups_csv:
        raise click.ClickException("Either --group, --groups-csv, or --change-requests must be specified")
    
    # Initialize client
    if verbose:
        console.print("[cyan]Authenticating with BMC Remedy API...[/cyan]")
    
    try:
        client = BMCRemedyClient(base_url, username, password, verify_ssl=not no_verify_ssl, verbose=verbose)
    except Exception as e:
        raise click.ClickException(f"Authentication failed: {e}")
    
    # ── Change Request mode ────────────────────────────────────────────────
    if change_requests:
        filters = []
        if cr_coordinator_group:
            filters.append(f"coordinator_group='{cr_coordinator_group}'")
        if cr_product_tier1:
            filters.append(f"product_tier1='{cr_product_tier1}'")
        if cr_product_tier3:
            filters.append(f"product_tier3='{cr_product_tier3}'")
        if cr_status != 'all':
            filters.append(f"status={cr_status}")
        filter_str = ', '.join(filters) if filters else 'none'

        console.print(
            f"[cyan]Fetching change requests for {describe_window(window_start)} "
            f"(filters: {filter_str})...[/cyan]"
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Fetching change requests...", total=None)
            cr_list = client.get_change_requests(
                since=window_start,
                coordinator_group=cr_coordinator_group or None,
                product_tier1=cr_product_tier1 or None,
                product_tier3=cr_product_tier3 or None,
                status_filter=cr_status,
            )
            progress.update(task, completed=True)

        console.print(f"\n[green]Found {len(cr_list)} change request(s)[/green]\n")

        if cr_list:
            # Compute lead times and outage durations (mutates each dict)
            for cr in cr_list:
                cr['lead_time_hours']       = _compute_lead_time(cr)
                cr['outage_duration_hours'] = _compute_outage_duration(cr)

            display_cr_summary_table(cr_list)

            # Always export to fixed file
            cr_output = 'outputs/csv/change_requests_dump.csv'
            Path(cr_output).parent.mkdir(parents=True, exist_ok=True)
            export_change_requests_to_csv(cr_list, cr_output)

            if output_json:
                json_output = output or f"change_requests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                export_to_json(cr_list, json_output)

        return

    # ── Incident mode ──────────────────────────────────────────────────────
    # Determine groups to query
    groups = []
    if group:
        groups = [group]
    elif groups_csv:
        groups = load_groups_from_csv(groups_csv)
        if not groups:
            raise click.ClickException(f"No groups found in {groups_csv}")
    
    if verbose:
        console.print(f"[cyan]Querying {len(groups)} group(s) for {describe_window(window_start)}[/cyan]")
        console.print(f"[cyan]State filter: {state}[/cyan]")
    
    # Fetch data
    all_incidents = []
    summaries = []
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Fetching incidents...", total=len(groups))
        
        for grp in groups:
            progress.update(task, description=f"Fetching incidents for {grp}...")
            
            if summary:
                summary_data = client.get_incident_summary(grp, since=window_start)
                summaries.append(summary_data)
            else:
                incidents = client.get_incidents(grp, state=state, since=window_start)
                
                for incident in incidents:
                    incident['query_group'] = grp
                
                all_incidents.extend(incidents)
            
            progress.advance(task)
    
    # Display/Export results
    if summary:
        display_summary_table(summaries, window_start)
        
        if output_json:
            output_file = output or f"bmc_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            export_to_json(summaries, output_file)
    
    else:
        console.print(f"\n[green]Found {len(all_incidents)} incidents across {len(groups)} group(s)[/green]\n")
        
        # Always export to outputs/csv/incident_dump.csv
        dump_file = 'outputs/csv/incident_dump.csv'
        Path(dump_file).parent.mkdir(parents=True, exist_ok=True)
        export_to_csv(all_incidents, dump_file)
        
        # Also export to custom CSV if specified
        if output_csv and output:
            export_to_csv(all_incidents, output)
        elif output_csv and not output:
            # Already exported to incident_dump.csv
            pass
        
        if output_json:
            output_file = output or f"bmc_incidents_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            export_to_json(all_incidents, output_file)
        
        # Display sample if not empty
        if all_incidents:
            console.print("\n[cyan]Sample incidents (first 5):[/cyan]")
            for i, incident in enumerate(all_incidents[:5], 1):
                console.print(f"\n{i}. {incident.get('number')} - {incident.get('short_description')}")
                console.print(f"   Group: {incident.get('assignment_group')}")
                console.print(f"   State: {incident.get('state')} | Priority: {incident.get('priority')}")
                console.print(f"   Opened: {incident.get('opened_at')}")


if __name__ == '__main__':
    main()
