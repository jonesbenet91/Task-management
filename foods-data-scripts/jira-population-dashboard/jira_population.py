#!/usr/bin/env python3
"""JIRA Ticket Population Reconciliation — standalone dashboard.

Reconciles two competing "completion" signals for every completed JIRA ticket
and flags the ones that disagree:

    completed_at  -> the ticket's `resolutiondate` field
    first_done    -> first changelog transition into a Done-category status

When these land in different months the ticket is flagged
`COMPLETED AND FIRST DONE DIFFER`.

This is a fully self-contained tool. It reads the team -> project mapping from
`data/jira.csv`, queries the JIRA REST API, classifies each ticket's
"population", writes `outputs/csv/jirapopulation.csv`, and renders the
`outputs/html/jirapopulation.html` dashboard.

Usage examples
--------------
    # Current month, all projects in data/jira.csv, write CSV + HTML
    python jira_population.py --csv --html

    # A specific reporting month
    python jira_population.py --month 2026-07 --csv --html

    # An explicit window, restricted to two projects
    python jira_population.py --since 2026-06-01 --until 2026-06-30 \
        --project FSCPC --project FSMTC --csv --html

    # Specific tickets only (period filter is ignored)
    python jira_population.py --ticket FSCPC-1022 --ticket FSMTC-905 --csv

    # Build the dashboard and serve it locally
    python jira_population.py --month 2026-07 --html --serve 8000

Authentication (env vars -> CLI options -> cached -> interactive prompt):
    JIRA_HOST       e.g. https://your-org.atlassian.net
    JIRA_EMAIL      you@company.com
    JIRA_API_TOKEN  <api-token>
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import tempfile
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
# Trust the operating-system certificate store (e.g. the Windows cert store) so
# requests accepts corporate proxy / MITM root CAs without a manual CA bundle.
# No-op if `truststore` is not installed.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - optional dependency / older Python
    pass

# ── Paths (resolved relative to this file so it runs from anywhere) ──────────
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECTS_CSV = BASE_DIR / "data" / "jira.csv"
OUTPUT_CSV = BASE_DIR / "outputs" / "csv" / "jirapopulation.csv"
OUTPUT_HTML = BASE_DIR / "outputs" / "html" / "jirapopulation.html"

DEFAULT_HOST = "https://jira-marksandspencer-app.atlassian.net"

# Population labels
POP_DIFFER = "COMPLETED AND FIRST DONE DIFFER"
POP_ALIGNED = "ALIGNED"
POP_NO_FIRST_DONE = "NO FIRST DONE"
POP_FIRST_DONE_ONLY = "FIRST DONE ONLY"

CSV_COLUMNS = [
    "product_group",
    "team_name",
    "board_key",
    "project_key",
    "ticket_key",
    "issue_type",
    "summary",
    "status",
    "status_category",
    "created_at",
    "completed_at",
    "first_done",
    "created_month",
    "completed_month",
    "first_done_month",
    "created_in_period",
    "completed_in_period",
    "first_done_in_period",
    "reopen_count",
    "reopened_at",
    "population",
    "url",
]


# ── Small console helpers (no third-party deps required) ─────────────────────
def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_COLOR = _supports_color()


def _c(text: str, code: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def info(msg: str) -> None:
    print(msg)


def ok(msg: str) -> None:
    print(_c(msg, "32"))


def warn(msg: str) -> None:
    print(_c(msg, "33"))


def err(msg: str) -> None:
    print(_c(msg, "31"), file=sys.stderr)


# ── JIRA API client ──────────────────────────────────────────────────────────
class JiraAuthError(Exception):
    """Raised when JIRA authentication fails."""


class JiraClient:
    """Minimal JIRA Cloud REST client for the reconciliation workflow."""

    def __init__(self, host: str, email: str, token: str, verbose: bool = False):
        self.host = host.rstrip("/")
        self.email = email
        self.token = token
        self.verbose = verbose
        self.session = requests.Session()
        retry = Retry(
            total=6, connect=3, read=3, backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True, raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.session.headers.update(
            {
                "Authorization": f"Basic {credentials}",
                "Accept": "application/json",
            }
        )

    def verify_auth(self) -> str:
        """Confirm credentials; return the authenticated display name."""
        resp = self.session.get(f"{self.host}/rest/api/3/myself", timeout=20)
        if resp.status_code in (401, 403):
            raise JiraAuthError(
                f"Authentication failed (HTTP {resp.status_code}). "
                "Check JIRA_EMAIL / JIRA_API_TOKEN (use --reset-token to re-enter)."
            )
        resp.raise_for_status()
        data = resp.json()
        return data.get("displayName") or data.get("emailAddress") or "unknown"

    def status_category_map(self) -> Dict[str, str]:
        """Map lowercase status name -> Jira category key (new/indeterminate/done)."""
        mapping: Dict[str, str] = {}
        try:
            resp = self.session.get(f"{self.host}/rest/api/3/status", timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            if self.verbose:
                warn(f"  status catalogue fetch failed: {exc}")
            return mapping
        for status in resp.json():
            name = (status.get("name") or "").strip().lower()
            category = ((status.get("statusCategory") or {}).get("key") or "").lower()
            if name and category:
                mapping[name] = category
        if self.verbose:
            info(f"  loaded {len(mapping)} statuses from catalogue")
        return mapping

    def search(self, jql: str, fields: List[str]) -> List[dict]:
        """Search issues with pagination via the /search/jql endpoint."""
        collected: List[dict] = []
        next_token: Optional[str] = None
        field_param = ",".join(fields)
        while True:
            params = {"jql": jql, "maxResults": 100, "fields": field_param}
            if next_token:
                params["nextPageToken"] = next_token
            resp = self.session.get(
                f"{self.host}/rest/api/3/search/jql", params=params, timeout=60
            )
            if resp.status_code in (401, 403):
                raise JiraAuthError(
                    f"Authentication failed while searching (HTTP {resp.status_code})."
                )
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("issues", [])
            collected.extend(issues)
            next_token = data.get("nextPageToken")
            if data.get("isLast", True) or not issues or not next_token:
                break
        return collected

    def issue_changelog(self, issue_key: str) -> List[dict]:
        """Fetch the full (paginated) changelog for a single issue."""
        histories: List[dict] = []
        start_at = 0
        while True:
            resp = self.session.get(
                f"{self.host}/rest/api/3/issue/{issue_key}/changelog",
                params={"startAt": start_at, "maxResults": 100},
                timeout=45,
            )
            if resp.status_code in (401, 403):
                raise JiraAuthError(
                    f"Authentication failed reading changelog (HTTP {resp.status_code})."
                )
            resp.raise_for_status()
            data = resp.json()
            values = data.get("values", [])
            histories.extend(values)
            total = data.get("total", len(histories))
            start_at += len(values)
            if not values or start_at >= total:
                break
        return histories

    def board_sprints(self, board_id: str, states: str = "active,closed") -> List[dict]:
        """Fetch sprints for an Agile board (paginated). Empty for Kanban boards."""
        sprints: List[dict] = []
        start_at = 0
        while True:
            try:
                resp = self.session.get(
                    f"{self.host}/rest/agile/1.0/board/{board_id}/sprint",
                    params={"startAt": start_at, "maxResults": 50, "state": states},
                    timeout=45,
                )
            except requests.RequestException:
                break
            if resp.status_code in (401, 403):
                raise JiraAuthError(
                    f"Authentication failed reading sprints (HTTP {resp.status_code})."
                )
            # 400 -> board has no sprints (e.g. Kanban); 404 -> board not found.
            if resp.status_code in (400, 404):
                break
            resp.raise_for_status()
            data = resp.json()
            values = data.get("values", [])
            sprints.extend(values)
            if data.get("isLast", True) or not values:
                break
            start_at += len(values)
        return sprints

    def sprint_report(self, board_id: str, sprint_id) -> dict:
        """Greenhopper sprint report `contents` (scope-change analysis)."""
        try:
            resp = self.session.get(
                f"{self.host}/rest/greenhopper/1.0/rapid/charts/sprintreport",
                params={"rapidViewId": board_id, "sprintId": sprint_id},
                timeout=45,
            )
        except requests.RequestException:
            return {}
        if resp.status_code in (401, 403):
            raise JiraAuthError(
                f"Authentication failed reading sprint report (HTTP {resp.status_code})."
            )
        if resp.status_code >= 400:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data.get("contents", {}) or {}

    def story_points_field(self) -> Optional[str]:
        """Discover the custom field id used for Story Points on this instance."""
        try:
            resp = self.session.get(f"{self.host}/rest/api/3/field", timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            if self.verbose:
                warn(f"  story-points field lookup failed: {exc}")
            return None
        preferred = ("story points", "story point estimate")
        fallback: Optional[str] = None
        for field in resp.json():
            name = (field.get("name") or "").strip().lower()
            fid = field.get("id")
            if not fid:
                continue
            if name in preferred:
                if self.verbose:
                    info(f"  story-points field: {fid} ({field.get('name')})")
                return fid
            if fallback is None and "story point" in name:
                fallback = fid
        if self.verbose and fallback:
            info(f"  story-points field (fallback): {fallback}")
        return fallback

    def story_points_fields(self) -> List[str]:
        """Discover every custom field id that holds story points.

        Instances often define both 'Story Points' (company-managed) and
        'Story point estimate' (team-managed). A story may populate either one,
        so both must be considered.
        """
        try:
            resp = self.session.get(f"{self.host}/rest/api/3/field", timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            if self.verbose:
                warn(f"  story-points field lookup failed: {exc}")
            return []
        ids: List[str] = []
        for field in resp.json():
            name = (field.get("name") or "").strip().lower()
            fid = field.get("id")
            if not fid:
                continue
            if name in ("story points", "story point estimate") or "story point" in name:
                if fid not in ids:
                    ids.append(fid)
        if self.verbose:
            info(f"  story-points fields: {ids or 'none found'}")
        return ids

    def sprint_field(self) -> Optional[str]:
        """Discover the custom field id used for the Sprint field."""
        try:
            resp = self.session.get(f"{self.host}/rest/api/3/field", timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            if self.verbose:
                warn(f"  sprint field lookup failed: {exc}")
            return None
        fallback: Optional[str] = None
        for field in resp.json():
            name = (field.get("name") or "").strip().lower()
            fid = field.get("id")
            if not fid:
                continue
            schema = (field.get("schema") or {}).get("custom") or ""
            if "gh-sprint" in schema.lower() or "gh_sprint" in schema.lower():
                if self.verbose:
                    info(f"  sprint field: {fid} ({field.get('name')})")
                return fid
            if fallback is None and name == "sprint":
                fallback = fid
        if self.verbose and fallback:
            info(f"  sprint field (fallback): {fallback}")
        return fallback


# ── Date helpers ─────────────────────────────────────────────────────────────
def parse_jira_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse a JIRA timestamp into an aware datetime (UTC-normalised)."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # JIRA emits e.g. 2026-07-14T09:31:22.000+0000 — normalise the offset.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
    return None


def month_of(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m") if dt else ""


def date_str(dt: Optional[datetime]) -> str:
    return dt.date().isoformat() if dt else ""


def in_window(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    return bool(dt) and start <= dt <= end


# ── Reporting window resolution ──────────────────────────────────────────────
def resolve_window(
    month: Optional[str], since: Optional[str], until: Optional[str]
) -> Tuple[datetime, datetime]:
    """Return an inclusive [start, end] UTC window from --month or --since/--until."""
    if since or until:
        if not (since and until):
            raise SystemExit("Error: --since and --until must be provided together.")
        start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(until).replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        return start, end

    if month:
        try:
            year, mon = (int(p) for p in month.split("-"))
        except ValueError:
            raise SystemExit("Error: --month must be in YYYY-MM format.")
    else:
        now = datetime.now(timezone.utc)
        year, mon = now.year, now.month

    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    return start, end


# ── CSV mapping ──────────────────────────────────────────────────────────────
def load_project_mapping(path: Path) -> Dict[str, Dict[str, str]]:
    """Parse data/jira.csv -> {board_key: {team_name, product_group, tpm, board_key}}."""
    mapping: Dict[str, Dict[str, str]] = {}
    if not path.exists():
        return mapping
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not any((v or "").strip() for v in row.values()):
                continue
            row = {k: (v.strip() if v else "") for k, v in row.items()}
            board_key = row.get("JIRA Board Name/Key", "")
            if not board_key or board_key in mapping:
                continue
            mapping[board_key] = {
                "board_key": board_key,
                "board_id": (row.get("JIRA Board ID") or "").strip(),
                "team_name": row.get("Team Name", "") or "Unknown",
                "product_group": row.get("Product Group", "") or "Unknown",
                "tpm": (row.get("TPM ") or row.get("TPM") or "").strip(),
                "project_name": row.get("JIRA Project Name", ""),
            }
    return mapping


def resolve_boards(tokens: List[str], mapping: Dict[str, Dict[str, str]]) -> List[str]:
    """Map user-supplied board identifiers to canonical board keys.

    Accepts a board key, JIRA Board ID (number), JIRA project name, or team
    name (case-insensitive; names may be a partial/substring match).
    """
    resolved: List[str] = []
    for token in tokens:
        raw = token.strip()
        needle = raw.lower()
        match: Optional[str] = None
        # Exact match first (key / id / full name).
        for key, meta in mapping.items():
            exact = {
                key.lower(),
                str(meta.get("board_id", "")).lower(),
                (meta.get("project_name") or "").lower(),
                (meta.get("team_name") or "").lower(),
            }
            if needle in exact:
                match = key
                break
        # Fall back to a partial name match.
        if match is None:
            for key, meta in mapping.items():
                names = (
                    (meta.get("project_name") or "").lower(),
                    (meta.get("team_name") or "").lower(),
                )
                if needle and any(needle in name for name in names if name):
                    match = key
                    break
        if match is None:
            raise SystemExit(
                f"Error: no board matches '{raw}'. "
                f"Known boards: {', '.join(mapping)}"
            )
        if match not in resolved:
            resolved.append(match)
    return resolved


# ── Reconciliation core ──────────────────────────────────────────────────────
def find_first_done(
    changelog: List[dict], status_categories: Dict[str, str]
) -> Optional[datetime]:
    """Earliest changelog transition INTO a Done-category status."""
    earliest: Optional[datetime] = None
    for history in changelog:
        created = parse_jira_datetime(history.get("created"))
        if not created:
            continue
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue
            to_status = (item.get("toString") or "").strip().lower()
            if status_categories.get(to_status) == "done":
                if earliest is None or created < earliest:
                    earliest = created
    return earliest


def find_reopens(
    changelog: List[dict], status_categories: Dict[str, str]
) -> Tuple[int, Optional[datetime]]:
    """Detect reopens by counting how many times the issue entered a Done status.

    A ticket that transitions INTO a Done-category status 2 or more times was
    reopened after its first completion. Returns (reopen_count, last_done_date)
    where reopen_count = (number of Done entries - 1), i.e. 0 when never
    reopened, 1 when it came back to Done once, and so on.
    """
    done_dates: List[datetime] = []
    for history in changelog:
        created = parse_jira_datetime(history.get("created"))
        if not created:
            continue
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue
            to_status = (item.get("toString") or "").strip().lower()
            if status_categories.get(to_status) == "done":
                done_dates.append(created)
    if len(done_dates) < 2:
        return 0, None
    done_dates.sort()
    return len(done_dates) - 1, done_dates[-1]


def classify(
    completed_at: Optional[datetime], first_done: Optional[datetime]
) -> str:
    if completed_at and first_done:
        if month_of(completed_at) != month_of(first_done):
            return POP_DIFFER
        return POP_ALIGNED
    if completed_at and not first_done:
        return POP_NO_FIRST_DONE
    if first_done and not completed_at:
        return POP_FIRST_DONE_ONLY
    return POP_ALIGNED


def build_row(
    issue: dict,
    meta: Dict[str, str],
    project_key: str,
    host: str,
    status_categories: Dict[str, str],
    first_done: Optional[datetime],
    reopen_count: int,
    reopened_at: Optional[datetime],
    window_start: datetime,
    window_end: datetime,
) -> dict:
    fields = issue.get("fields", {})
    key = issue.get("key", "")
    status_obj = fields.get("status") or {}
    status_name = status_obj.get("name", "")
    status_category = (status_obj.get("statusCategory") or {}).get("name", "")
    completed_at = parse_jira_datetime(fields.get("resolutiondate"))
    created_at = parse_jira_datetime(fields.get("created"))

    completed_in = in_window(completed_at, window_start, window_end)
    first_done_in = in_window(first_done, window_start, window_end)
    created_in = in_window(created_at, window_start, window_end)

    return {
        "product_group": meta.get("product_group", "Unknown"),
        "team_name": meta.get("team_name", project_key),
        "board_key": meta.get("board_key", project_key),
        "board_id": meta.get("board_id", ""),
        "project_name": meta.get("project_name", ""),
        "project_key": project_key,
        "ticket_key": key,
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "summary": fields.get("summary", "") or "",
        "status": status_name,
        "status_category": status_category,
        "created_at": date_str(created_at),
        "completed_at": date_str(completed_at),
        "first_done": date_str(first_done),
        "created_month": month_of(created_at),
        "completed_month": month_of(completed_at),
        "first_done_month": month_of(first_done),
        "created_in_period": created_in,
        "completed_in_period": completed_in,
        "first_done_in_period": first_done_in,
        "reopen_count": reopen_count,
        "reopened_at": date_str(reopened_at),
        "population": classify(completed_at, first_done),
        "url": f"{host}/browse/{key}",
        "_owner": meta.get("tpm", ""),
    }


def process_project(
    client: JiraClient,
    project_key: str,
    meta: Dict[str, str],
    window_start: datetime,
    window_end: datetime,
    status_categories: Dict[str, str],
    verbose: bool,
) -> List[dict]:
    """Fetch all tickets CREATED within the reporting window and reconcile each."""
    start = window_start.date().isoformat()
    end = window_end.date().isoformat()
    jql = (
        f'project = "{project_key}" '
        f'AND created >= "{start}" '
        f'AND created <= "{end} 23:59" '
        f"ORDER BY created ASC"
    )
    fields = ["summary", "status", "issuetype", "resolutiondate", "created"]
    issues = client.search(jql, fields)
    if verbose:
        info(f"  {project_key}: {len(issues)} tickets created in window")

    rows: List[dict] = []
    for issue in issues:
        key = issue.get("key", "")
        try:
            changelog = client.issue_changelog(key)
        except JiraAuthError:
            raise
        except requests.RequestException as exc:
            if verbose:
                warn(f"    {key}: changelog fetch failed: {exc}")
            changelog = []
        first_done = find_first_done(changelog, status_categories)
        reopen_count, reopened_at = find_reopens(changelog, status_categories)
        row = build_row(
            issue, meta, project_key, client.host, status_categories,
            first_done, reopen_count, reopened_at, window_start, window_end,
        )
        # Only keep tickets created within the reporting window.
        if row["created_in_period"]:
            rows.append(row)
    return rows


def process_tickets(
    client: JiraClient,
    ticket_keys: List[str],
    mapping: Dict[str, Dict[str, str]],
    status_categories: Dict[str, str],
    window_start: datetime,
    window_end: datetime,
    verbose: bool,
) -> List[dict]:
    """Reconcile an explicit list of ticket keys (period filter ignored)."""
    rows: List[dict] = []
    fields = ["summary", "status", "issuetype", "resolutiondate", "project"]
    for key in ticket_keys:
        project_key = key.split("-")[0] if "-" in key else key
        meta = mapping.get(project_key, {
            "board_key": project_key,
            "team_name": project_key,
            "product_group": "Unknown",
            "tpm": "",
        })
        try:
            issues = client.search(f'issuekey = "{key}"', fields)
        except JiraAuthError:
            raise
        except requests.RequestException as exc:
            warn(f"  {key}: lookup failed: {exc}")
            continue
        if not issues:
            warn(f"  {key}: not found")
            continue
        issue = issues[0]
        try:
            changelog = client.issue_changelog(key)
        except requests.RequestException:
            changelog = []
        first_done = find_first_done(changelog, status_categories)
        reopen_count, reopened_at = find_reopens(changelog, status_categories)
        row = build_row(
            issue, meta, project_key, client.host, status_categories,
            first_done, reopen_count, reopened_at, window_start, window_end,
        )
        rows.append(row)
        if verbose:
            info(f"  {key}: {row['population']}")
    return rows


# ── Sprint closure delay ─────────────────────────────────────────────────────
def _delay_days(end: Optional[datetime], actual: Optional[datetime]) -> Optional[int]:
    """Whole days the actual close ran past the expected (planned) end date."""
    if not end or not actual:
        return None
    return (actual.date() - end.date()).days


def _detect_added_mid_sprint(
    client: JiraClient,
    sprint: dict,
    start_date: datetime,
    window_start: datetime,
    window_end: datetime,
) -> List[dict]:
    """Issues ADDED to a sprint after it started, within the reporting window.

    An issue is 'added mid-sprint' when either:
      * it was CREATED after the sprint start (CREATED_MID_SPRINT), or
      * a changelog Sprint-field change moved it INTO this sprint after the
        sprint start (MOVED_MID_SPRINT).
    Only additions whose timestamp falls in [window_start, window_end] count.
    """
    sprint_id = sprint.get("id")
    sprint_name = (sprint.get("name") or "").strip()
    try:
        issues = client.search(f"sprint = {sprint_id}", ["created", "summary"])
    except JiraAuthError:
        raise
    except requests.RequestException:
        return []

    additions: List[dict] = []
    for issue in issues:
        key = issue.get("key", "")
        fields = issue.get("fields", {})
        created_at = parse_jira_datetime(fields.get("created"))
        added_at: Optional[datetime] = None
        reason: Optional[str] = None

        # 1. Created after the sprint started.
        if created_at and created_at > start_date:
            added_at = created_at
            reason = "CREATED_MID_SPRINT"
        else:
            # 2. Moved into this sprint via a Sprint-field change after start.
            try:
                changelog = client.issue_changelog(key)
            except JiraAuthError:
                raise
            except requests.RequestException:
                changelog = []
            for history in changelog:
                hist_created = parse_jira_datetime(history.get("created"))
                if not hist_created or hist_created <= start_date:
                    continue
                for item in history.get("items", []):
                    if (item.get("field") or "").lower() != "sprint":
                        continue
                    to_str = item.get("toString") or ""
                    to_ids = [
                        s.strip()
                        for s in str(item.get("to") or "").split(",")
                        if s.strip()
                    ]
                    if (sprint_name and sprint_name in to_str) or (
                        str(sprint_id) in to_ids
                    ):
                        added_at = hist_created
                        reason = "MOVED_MID_SPRINT"
                        break
                if added_at:
                    break

        if added_at and window_start <= added_at <= window_end:
            additions.append({
                "key": key,
                "summary": fields.get("summary", "") or "",
                "added_at": date_str(added_at),
                "reason": reason,
                "url": f"{client.host}/browse/{key}",
            })
    return additions


def collect_sprint_delays(
    client: JiraClient,
    mapping: Dict[str, Dict[str, str]],
    projects: List[str],
    window_start: datetime,
    window_end: datetime,
    verbose: bool,
) -> Tuple[List[dict], List[dict], List[dict], List[dict], List[dict]]:
    """Inspect sprints whose planned end falls inside the reporting window.

    Returns three lists:
      * delayed  — sprints that did NOT close on their expected day
                   (CLOSED with completeDate after endDate, or ACTIVE and
                   already past endDate / overdue).
      * early    — sprints CLOSED before their planned endDate. Closing a
                   sprint early means remaining issues were removed/returned
                   to the backlog after sprint start.
      * added    — sprints with issues ADDED after the sprint started
                   (scope creep), per the sprint report.
    Only sprints whose planned end date falls inside [window_start, window_end]
    are considered.
    """
    now = datetime.now(timezone.utc)
    seen_boards: set = set()
    delayed: List[dict] = []
    early: List[dict] = []
    added: List[dict] = []
    removed: List[dict] = []
    all_sprints: List[dict] = []

    def _record(sp, state, closure, actual_label, days, extra=None):
        rec = {
            "sprint": sp.get("name", "") or f"Sprint {sp.get('id', '')}",
            "sprint_id": sp.get("id", ""),
            "board_id": board_id,
            "board_key": meta.get("board_key", project_key),
            "team_name": meta.get("team_name", project_key),
            "product_group": meta.get("product_group", "Unknown"),
            "owner": meta.get("tpm", ""),
            "state": state,
            "closure": closure,
            "start_date": date_str(parse_jira_datetime(sp.get("startDate"))),
            "end_date": date_str(parse_jira_datetime(sp.get("endDate"))),
            "complete_date": actual_label,
            "goal": (sp.get("goal") or "").strip(),
            "url": (
                f"{client.host}/secure/RapidBoard.jspa?rapidView={board_id}"
                f"&view=reporting&chart=sprintRetrospective&sprint={sp.get('id', '')}"
            ),
        }
        rec.update(extra or {})
        rec["delay_days"] = days
        return rec

    for project_key in projects:
        meta = mapping.get(project_key, {})
        board_id = str(meta.get("board_id", "") or "").strip()
        if not board_id or not board_id.isdigit() or board_id in seen_boards:
            continue
        seen_boards.add(board_id)
        try:
            sprints = client.board_sprints(board_id)
        except JiraAuthError:
            raise
        except requests.RequestException as exc:
            if verbose:
                warn(f"    {project_key}: sprint fetch failed: {exc}")
            continue
        if verbose and sprints:
            info(f"  {project_key} (board {board_id}): {len(sprints)} sprint(s)")

        for sp in sprints:
            state = (sp.get("state") or "").lower()
            end = parse_jira_datetime(sp.get("endDate"))
            complete = parse_jira_datetime(sp.get("completeDate"))
            if not end:
                continue

            # Only sprints whose planned end date is inside the reporting window.
            if not (window_start <= end <= window_end):
                continue

            # Every in-window sprint counts toward the total (any state).
            all_sprints.append({
                "sprint": sp.get("name", "") or f"Sprint {sp.get('id', '')}",
                "sprint_id": sp.get("id", ""),
                "board_key": meta.get("board_key", project_key),
                "team_name": meta.get("team_name", project_key),
                "product_group": meta.get("product_group", "Unknown"),
                "state": state,
                "start_date": date_str(parse_jira_datetime(sp.get("startDate"))),
                "end_date": date_str(end),
            })

            # Scope creep: issues added after the sprint started (closed/active).
            if state in ("closed", "active"):
                contents = client.sprint_report(board_id, sp.get("id"))
                lookup: Dict[str, str] = {}
                for bucket in (
                    "completedIssues",
                    "issuesNotCompletedInCurrentSprint",
                    "puntedIssues",
                    "issuesCompletedInAnotherSprint",
                ):
                    for it in (contents.get(bucket) or []):
                        k = it.get("key")
                        if k:
                            lookup[k] = it.get("summary", "") or ""
                # Issues added after start, detected from each issue's changelog
                # (created-after-start OR a Sprint-field move into this sprint),
                # counted only when the add event lands inside the window.
                sp_start = parse_jira_datetime(sp.get("startDate"))
                added_issue_list = (
                    _detect_added_mid_sprint(
                        client, sp, sp_start, window_start, window_end
                    )
                    if sp_start
                    else []
                )
                if added_issue_list:
                    added.append(
                        _record(
                            sp, state, "Scope added",
                            date_str(complete) if state == "closed" else "",
                            len(added_issue_list),
                            {"added_count": len(added_issue_list),
                             "issues": added_issue_list},
                        )
                    )
                # Scope removed: issues punted out of the sprint after it started.
                punted = contents.get("puntedIssues") or []
                removed_keys = [it.get("key") for it in punted if it.get("key")]
                if removed_keys:
                    r_issues = [
                        {
                            "key": k,
                            "summary": lookup.get(k, ""),
                            "url": f"{client.host}/browse/{k}",
                        }
                        for k in sorted(removed_keys)
                    ]
                    removed.append(
                        _record(
                            sp, state, "Scope removed",
                            date_str(complete) if state == "closed" else "",
                            len(removed_keys),
                            {"removed_count": len(removed_keys), "issues": r_issues},
                        )
                    )

            if state == "closed":
                diff = _delay_days(end, complete)
                if diff is None:
                    continue
                if diff >= 1:
                    delayed.append(
                        _record(sp, state, "Closed late", date_str(complete), diff)
                    )
                elif diff <= -1:
                    early.append(
                        _record(sp, state, "Closed early", date_str(complete),
                                abs(diff), {"early_days": abs(diff)})
                    )
            elif state == "active":
                if now <= end:
                    continue  # still within its planned window
                diff = _delay_days(end, now)
                if diff is None or diff < 1:
                    continue
                delayed.append(_record(sp, state, "Open — overdue", "", diff))

    delayed.sort(key=lambda s: (-s["delay_days"], s["team_name"].lower()))
    early.sort(key=lambda s: (-s["delay_days"], s["team_name"].lower()))
    # Deduplicate added issues across sprints so each ticket is counted once,
    # at the sprint where its in-window addition timestamp is earliest. Without
    # this, a rolled-over ticket that re-enters each subsequent sprint would be
    # counted 2-3 times, inflating the "issues added mid-sprint" total.
    earliest_sprint_for_key: Dict[str, Tuple[str, int]] = {}
    for idx, rec in enumerate(added):
        for it in rec.get("issues", []):
            key = it.get("key")
            if not key:
                continue
            added_at = it.get("added_at") or ""
            prev = earliest_sprint_for_key.get(key)
            if prev is None or added_at < prev[0]:
                earliest_sprint_for_key[key] = (added_at, idx)
    deduped_added: List[dict] = []
    for idx, rec in enumerate(added):
        kept = [
            it
            for it in rec.get("issues", [])
            if earliest_sprint_for_key.get(it.get("key"), ("", -1))[1] == idx
        ]
        if kept:
            rec["issues"] = kept
            rec["added_count"] = len(kept)
            rec["delay_days"] = len(kept)
            deduped_added.append(rec)
    added = deduped_added
    added.sort(key=lambda s: (-s["added_count"], s["team_name"].lower()))
    removed.sort(key=lambda s: (-s["removed_count"], s["team_name"].lower()))
    all_sprints.sort(key=lambda s: (s["team_name"].lower(), s["end_date"]))
    return delayed, early, added, removed, all_sprints


def collect_stories_no_sp(
    client: JiraClient,
    mapping: Dict[str, Dict[str, str]],
    projects: List[str],
    window_start: datetime,
    window_end: datetime,
    sp_fields: List[str],
    verbose: bool,
) -> List[dict]:
    """Stories created within the reporting window that have no story points."""
    stories: List[dict] = []
    if not sp_fields:
        warn("  story-points field not found — skipping 'Stories with No SP'.")
        return stories
    start = window_start.date().isoformat()
    end = window_end.date().isoformat()
    for project_key in dict.fromkeys(projects):
        meta = mapping.get(project_key, {})
        jql = (
            f'project = "{project_key}" AND issuetype = Story '
            f'AND created >= "{start}" AND created <= "{end} 23:59" '
            f"ORDER BY created DESC"
        )
        try:
            issues = client.search(
                jql, ["summary", "status", "created", *sp_fields]
            )
        except JiraAuthError:
            raise
        except requests.RequestException as exc:
            if verbose:
                warn(f"    {project_key}: story lookup failed: {exc}")
            continue
        missing = 0
        for issue in issues:
            fields = issue.get("fields", {}) or {}
            # A story counts as "no SP" only when every candidate field is empty.
            has_sp = any(
                fields.get(fid) not in (None, "") for fid in sp_fields
            )
            if has_sp:
                continue
            key = issue.get("key", "")
            status_obj = fields.get("status") or {}
            stories.append({
                "ticket": key,
                "summary": fields.get("summary", "") or "",
                "team_name": meta.get("team_name", project_key),
                "product_group": meta.get("product_group", "Unknown"),
                "board_key": meta.get("board_key", project_key),
                "status": status_obj.get("name", "") or "",
                "created": date_str(parse_jira_datetime(fields.get("created"))),
                "url": f"{client.host}/browse/{key}",
            })
            missing += 1
        if verbose and issues:
            info(f"  {project_key}: {missing}/{len(issues)} stories missing story points")
    stories.sort(key=lambda s: (s["team_name"].lower(), s["ticket"]))
    return stories


LARGE_SP_THRESHOLD = 8
MULTI_SPRINT_MIN = 3


def _cf_ref(field_id: str) -> str:
    """customfield_10033 -> cf[10033] (JQL custom-field reference)."""
    if field_id.startswith("customfield_"):
        return f"cf[{field_id[len('customfield_'):]}]"
    return field_id


def _sp_value(fields: dict, sp_fields: List[str]) -> Optional[float]:
    """Largest numeric story-point value across candidate fields."""
    best: Optional[float] = None
    for fid in sp_fields:
        raw = fields.get(fid)
        if raw in (None, ""):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if best is None or val > best:
            best = val
    return best


def collect_stories_large_sp(
    client: JiraClient,
    mapping: Dict[str, Dict[str, str]],
    projects: List[str],
    window_start: datetime,
    window_end: datetime,
    sp_fields: List[str],
    verbose: bool,
) -> List[dict]:
    """Issues with story points greater than 8 created or updated in the window."""
    stories: List[dict] = []
    if not sp_fields:
        warn("  story-points field not found — skipping 'Stories with SP > 8'.")
        return stories
    start = window_start.date().isoformat()
    end = window_end.date().isoformat()
    sp_clause = " OR ".join(f"{_cf_ref(fid)} > {LARGE_SP_THRESHOLD}" for fid in sp_fields)
    for project_key in dict.fromkeys(projects):
        meta = mapping.get(project_key, {})
        jql = (
            f'project = "{project_key}" AND ({sp_clause}) AND ('
            f'(created >= "{start}" AND created <= "{end} 23:59") OR '
            f'(updated >= "{start}" AND updated <= "{end} 23:59")) '
            f"ORDER BY created DESC"
        )
        try:
            issues = client.search(
                jql, ["summary", "status", "issuetype", "created", "updated", *sp_fields]
            )
        except JiraAuthError:
            raise
        except requests.RequestException as exc:
            warn(f"    {project_key}: large-SP lookup failed: {exc}")
            continue
        for issue in issues:
            fields = issue.get("fields", {}) or {}
            sp = _sp_value(fields, sp_fields)
            if sp is None or sp <= LARGE_SP_THRESHOLD:
                continue
            key = issue.get("key", "")
            status_obj = fields.get("status") or {}
            type_obj = fields.get("issuetype") or {}
            stories.append({
                "ticket": key,
                "summary": fields.get("summary", "") or "",
                "team_name": meta.get("team_name", project_key),
                "product_group": meta.get("product_group", "Unknown"),
                "board_key": meta.get("board_key", project_key),
                "status": status_obj.get("name", "") or "",
                "issue_type": type_obj.get("name", "") or "",
                "story_points": int(sp) if float(sp).is_integer() else sp,
                "created": date_str(parse_jira_datetime(fields.get("created"))),
                "updated": date_str(parse_jira_datetime(fields.get("updated"))),
                "url": f"{client.host}/browse/{key}",
            })
        if verbose:
            info(f"  {project_key}: {len(issues)} issue(s) with SP > {LARGE_SP_THRESHOLD}")
    stories.sort(key=lambda s: (s["team_name"].lower(), -float(s["story_points"]), s["ticket"]))
    return stories


def collect_stories_multi_sprint(
    client: JiraClient,
    mapping: Dict[str, Dict[str, str]],
    projects: List[str],
    window_start: datetime,
    window_end: datetime,
    sprint_field: Optional[str],
    verbose: bool,
) -> List[dict]:
    """Stories in 3+ sprints (sprint history) that touched a sprint running in the window."""
    stories: List[dict] = []
    if not sprint_field:
        warn("  sprint field not found — skipping 'Stories in More Than 2 Sprints'.")
        return stories
    start = window_start.date().isoformat()
    board_cache: Dict[str, dict] = {}
    for project_key in dict.fromkeys(projects):
        meta = mapping.get(project_key, {})
        board_id = meta.get("board_id", "") or ""
        sprint_map = _build_sprint_map(client, board_id, board_cache)

        # Sprints that overlap the reporting window.
        overlapping = set()
        for sid, sp in sprint_map.items():
            s, e = sp.get("start"), sp.get("end")
            if s and s <= window_end and (e is None or e >= window_start):
                overlapping.add(sid)

        if overlapping:
            ids = ", ".join(sorted(overlapping))
            jql = (f'project = "{project_key}" AND issuetype = Story '
                   f'AND sprint in ({ids}) ORDER BY created DESC')
        else:
            jql = (f'project = "{project_key}" AND issuetype = Story '
                   f'AND sprint is not EMPTY AND updated >= "{start}" ORDER BY created DESC')
        try:
            issues = client.search(jql, ["summary", "status", "created", sprint_field])
        except JiraAuthError:
            raise
        except requests.RequestException as exc:
            warn(f"    {project_key}: multi-sprint lookup failed: {exc}")
            continue

        for issue in issues:
            fields = issue.get("fields", {}) or {}
            entries = [sp for sp in (fields.get(sprint_field) or [])
                       if isinstance(sp, dict) and sp.get("id") is not None]
            distinct: Dict[str, dict] = {}
            for sp in entries:
                distinct[str(sp.get("id"))] = sp
            if len(distinct) < MULTI_SPRINT_MIN:
                continue
            ordered = sorted(
                distinct.values(),
                key=lambda sp: parse_jira_datetime(sp.get("startDate")) or _EPOCH,
            )
            key = issue.get("key", "")
            status_obj = fields.get("status") or {}
            stories.append({
                "ticket": key,
                "summary": fields.get("summary", "") or "",
                "team_name": meta.get("team_name", project_key),
                "product_group": meta.get("product_group", "Unknown"),
                "board_key": meta.get("board_key", project_key),
                "status": status_obj.get("name", "") or "",
                "sprint_count": len(distinct),
                "sprints": [sp.get("name", "") or str(sp.get("id")) for sp in ordered],
                "created": date_str(parse_jira_datetime(fields.get("created"))),
                "url": f"{client.host}/browse/{key}",
            })
        if verbose:
            info(f"  {project_key}: {len(issues)} stories in window sprints, "
                 f"{sum(1 for s in stories if s['board_key'] == meta.get('board_key', project_key))} in 3+ sprints")
    stories.sort(key=lambda s: (s["team_name"].lower(), -s["sprint_count"], s["ticket"]))
    return stories


def _sprint_starts(sprint_value) -> List[datetime]:
    """Extract sprint startDate values from an issue's Sprint field."""
    starts: List[datetime] = []
    for entry in (sprint_value or []):
        raw = None
        if isinstance(entry, dict):
            raw = entry.get("startDate")
        elif isinstance(entry, str):
            # Legacy string form: ...[id=1,...,startDate=2026-08-01T...,...]
            match = re.search(r"startDate=([^,\]]+)", entry)
            if match:
                raw = match.group(1)
                if raw in ("<null>", "null", ""):
                    raw = None
        dt = parse_jira_datetime(raw) if raw else None
        if dt:
            starts.append(dt)
    return starts


def _sprint_added_times(changelog: List[dict]) -> List[datetime]:
    """Changelog timestamps where the story was added to a sprint.

    Fallback for team-managed projects whose Sprint field value in search
    results does not carry a startDate.
    """
    times: List[datetime] = []
    for history in changelog:
        created = parse_jira_datetime(history.get("created"))
        if not created:
            continue
        for item in (history.get("items") or []):
            field_name = (item.get("field") or "").strip().lower()
            if field_name != "sprint":
                continue
            # Story moved into (or between) a sprint: 'to' has a value.
            if (item.get("toString") or "").strip():
                times.append(created)
    return times


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _norm_sp(value) -> str:
    """Normalise a story-point value to a comparable string ('' for none)."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _id_set(raw) -> set:
    """Parse a comma-separated changelog id list into a set of id strings."""
    if not raw:
        return set()
    return {p.strip() for p in str(raw).split(",") if p.strip()}


def _sprint_intervals(
    changelog: List[dict], story_created: Optional[datetime]
) -> Dict[str, List[list]]:
    """Reconstruct per-sprint membership intervals [added, removed|None].

    Sprint changelog items store comma-separated sprint IDs in from/to.
    """
    seed = story_created or _EPOCH
    entries: List[tuple] = []
    for history in changelog:
        created = parse_jira_datetime(history.get("created"))
        if not created:
            continue
        for item in (history.get("items") or []):
            if (item.get("field") or "").strip().lower() != "sprint":
                continue
            entries.append((created, _id_set(item.get("from")), _id_set(item.get("to"))))
    entries.sort(key=lambda e: e[0])
    intervals: Dict[str, List[list]] = {}
    if entries:
        # Whatever was present in the first 'from' existed before any change.
        for sid in entries[0][1]:
            intervals.setdefault(sid, []).append([seed, None])
    for created, frm, to in entries:
        for sid in (to - frm):  # added to a sprint
            lst = intervals.setdefault(sid, [])
            if not (lst and lst[-1][1] is None):
                lst.append([created, None])
        for sid in (frm - to):  # removed from a sprint
            lst = intervals.setdefault(sid, [])
            if lst and lst[-1][1] is None:
                lst[-1][1] = created
            else:
                lst.append([seed, created])
    return intervals


def _member_after(intervals: List[list], start: datetime) -> bool:
    """True if the story was a sprint member at or after the sprint start."""
    for _added, removed in intervals:
        if removed is None or removed > start:
            return True
    return False


def _member_started_at(
    intervals: Dict[str, List[list]], sprint_map: Dict[str, dict], when: datetime
) -> bool:
    """True if, at time `when`, the story was a member of a sprint that had
    already started (i.e. the change happened mid-sprint)."""
    for sid, ivs in intervals.items():
        info_sp = sprint_map.get(sid)
        if not info_sp or not info_sp.get("start"):
            continue
        if info_sp["start"] > when:
            continue  # sprint had not started yet at the change time
        for added, removed in ivs:
            if added <= when and (removed is None or removed > when):
                return True
    return False


def _sp_value_at(sp_events: List[dict], current_value: str, when: datetime) -> str:
    """Story-point value in effect at time `when` (reconstructed from history)."""
    if not sp_events:
        return current_value or ""
    val = sp_events[0]["from"]  # value before the first recorded change
    for event in sp_events:
        if event["created"] <= when:
            val = event["to"]
        else:
            break
    return val or ""


def _build_sprint_map(
    client: JiraClient, board_id: str, cache: Dict[str, dict]
) -> Dict[str, dict]:
    """Return {sprint_id: {start, end, name, state}} for a board (cached)."""
    if board_id in cache:
        return cache[board_id]
    smap: Dict[str, dict] = {}
    if board_id:
        try:
            for sp in client.board_sprints(board_id):
                sid = str(sp.get("id"))
                smap[sid] = {
                    "start": parse_jira_datetime(sp.get("startDate")),
                    "end": (parse_jira_datetime(sp.get("endDate"))
                            or parse_jira_datetime(sp.get("completeDate"))),
                    "name": sp.get("name", ""),
                    "state": (sp.get("state") or "").lower(),
                }
        except JiraAuthError:
            raise
        except requests.RequestException:
            pass
    cache[board_id] = smap
    return smap



def collect_stories_sp_changed(
    client: JiraClient,
    mapping: Dict[str, Dict[str, str]],
    projects: List[str],
    window_start: datetime,
    window_end: datetime,
    sp_fields: List[str],
    sprint_field: Optional[str],
    verbose: bool,
) -> List[dict]:
    """Stories whose story-point value changed after their sprint started."""
    stories: List[dict] = []
    if not sp_fields:
        warn("  story-points field not found — skipping 'Stories with SP Changed'.")
        return stories
    start = window_start.date().isoformat()
    request_fields = ["summary", "status", "created", *sp_fields]
    if sprint_field:
        request_fields.append(sprint_field)
    board_cache: Dict[str, dict] = {}
    for project_key in dict.fromkeys(projects):
        meta = mapping.get(project_key, {})
        board_id = meta.get("board_id", "") or ""
        sprint_map = _build_sprint_map(client, board_id, board_cache)
        # Gather any story touched on/after the window start. No upper bound —
        # a story can be modified after window_end yet still hold a qualifying
        # Story Points change inside the window; the per-event filter below
        # enforces the strict calendar cut-off.
        jql = (
            f'project = "{project_key}" AND issuetype = Story '
            f'AND updated >= "{start}" '
            f"ORDER BY updated DESC"
        )
        try:
            issues = client.search(jql, request_fields)
        except JiraAuthError:
            raise
        except requests.RequestException as exc:
            if verbose:
                warn(f"    {project_key}: story lookup failed: {exc}")
            continue
        changed = 0
        with_start = 0
        for issue in issues:
            fields = issue.get("fields", {}) or {}
            key = issue.get("key", "")
            try:
                changelog = client.issue_changelog(key)
            except JiraAuthError:
                raise
            except requests.RequestException:
                continue

            story_created = parse_jira_datetime(fields.get("created"))

            # Current story-point value (first populated candidate field).
            current_sp = ""
            for fid in sp_fields:
                val = fields.get(fid)
                if val not in (None, ""):
                    current_sp = _norm_sp(val)
                    break

            # Story-point value timeline from the changelog.
            sp_events: List[dict] = []
            for history in changelog:
                created = parse_jira_datetime(history.get("created"))
                if not created:
                    continue
                for item in (history.get("items") or []):
                    field_name = (item.get("field") or "").strip().lower()
                    field_id = item.get("fieldId") or ""
                    if field_id in sp_fields or "story point" in field_name:
                        sp_events.append({
                            "created": created,
                            "from": _norm_sp(item.get("fromString")),
                            "to": _norm_sp(item.get("toString")),
                        })
            sp_events.sort(key=lambda e: e["created"])

            # Membership intervals per sprint, merged with the current field.
            intervals = _sprint_intervals(changelog, story_created)
            if sprint_field:
                for sp in (fields.get(sprint_field) or []):
                    if not isinstance(sp, dict) or sp.get("id") is None:
                        continue
                    sid = str(sp.get("id"))
                    if sid not in sprint_map or sprint_map[sid].get("start") is None:
                        sprint_map[sid] = {
                            "start": parse_jira_datetime(sp.get("startDate")),
                            "end": (parse_jira_datetime(sp.get("endDate"))
                                    or parse_jira_datetime(sp.get("completeDate"))),
                            "name": sp.get("name", ""),
                        }
                    intervals.setdefault(sid, [[story_created or _EPOCH, None]])

            # Started sprints the story actually participated in (member at or
            # after the real startDate) that overlap the reporting window.
            rel_starts: List[datetime] = []
            for sid in intervals:
                info_sp = sprint_map.get(sid)
                if not info_sp or not info_sp.get("start"):
                    continue
                s_start = info_sp["start"]
                s_end = info_sp.get("end")
                if s_start > window_end:
                    continue
                if s_end is not None and s_end < window_start:
                    continue
                if not _member_after(intervals.get(sid, []), s_start):
                    continue
                rel_starts.append(s_start)
            if not rel_starts:
                continue
            sprint_start = min(rel_starts)
            with_start += 1

            # A qualifying change is any SP change strictly inside the window and
            # after the sprint had started.
            post = [e for e in sp_events
                    if e["created"] > sprint_start
                    and window_start <= e["created"] <= window_end]
            if not post:
                continue

            # Net effect: value at sprint start vs value at window end. Equal ->
            # no real scope change (e.g. a cleared-and-re-entered round-trip).
            v0 = _sp_value_at(sp_events, current_sp, sprint_start)
            vf = _sp_value_at(sp_events, current_sp, window_end)
            if v0 == vf:
                continue

            status_obj = fields.get("status") or {}
            stories.append({
                "ticket": key,
                "summary": fields.get("summary", "") or "",
                "team_name": meta.get("team_name", project_key),
                "product_group": meta.get("product_group", "Unknown"),
                "board_key": meta.get("board_key", project_key),
                "status": status_obj.get("name", "") or "",
                "sp_from": v0 or "\u2014",
                "sp_to": vf or "\u2014",
                "change_count": len(post),
                "changed_at": date_str(post[-1]["created"]),
                "url": f"{client.host}/browse/{key}",
            })
            changed += 1
        if verbose and issues:
            info(f"  {project_key}: {len(issues)} stories, {with_start} in a started sprint, "
                 f"{changed} with SP changed after start")
    stories.sort(key=lambda s: (s["team_name"].lower(), s["ticket"]))
    return stories


def collect_stories_carry_over(
    client: JiraClient,
    mapping: Dict[str, Dict[str, str]],
    projects: List[str],
    window_start: datetime,
    window_end: datetime,
    sprint_field: Optional[str],
    verbose: bool,
) -> List[dict]:
    """Stories carried over from a completed sprint into a later sprint.

    A carry-over is detected when a story is a member of two or more sprints
    whose start dates are distinct and where a later sprint started at/after an
    earlier sprint the story belonged to ended — i.e. the work was not finished
    in its original sprint and rolled forward. Scoped to carries whose
    destination sprint started inside the reporting window.
    """
    stories: List[dict] = []
    start = window_start.date().isoformat()
    request_fields = ["summary", "status", "created"]
    if sprint_field:
        request_fields.append(sprint_field)
    board_cache: Dict[str, dict] = {}
    for project_key in dict.fromkeys(projects):
        meta = mapping.get(project_key, {})
        board_id = meta.get("board_id", "") or ""
        sprint_map = _build_sprint_map(client, board_id, board_cache)
        jql = (
            f'project = "{project_key}" AND issuetype = Story '
            f'AND updated >= "{start}" '
            f"ORDER BY updated DESC"
        )
        try:
            issues = client.search(jql, request_fields)
        except JiraAuthError:
            raise
        except requests.RequestException as exc:
            warn(f"    {project_key}: carry-over story lookup failed: {exc}")
            continue
        carried = 0
        cl_failed = 0
        for issue in issues:
            fields = issue.get("fields", {}) or {}
            key = issue.get("key", "")
            try:
                changelog = client.issue_changelog(key)
            except JiraAuthError:
                raise
            except requests.RequestException:
                cl_failed += 1
                continue

            story_created = parse_jira_datetime(fields.get("created"))
            intervals = _sprint_intervals(changelog, story_created)
            if sprint_field:
                for sp in (fields.get(sprint_field) or []):
                    if not isinstance(sp, dict) or sp.get("id") is None:
                        continue
                    sid = str(sp.get("id"))
                    if sid not in sprint_map or sprint_map[sid].get("start") is None:
                        sprint_map[sid] = {
                            "start": parse_jira_datetime(sp.get("startDate")),
                            "end": (parse_jira_datetime(sp.get("endDate"))
                                    or parse_jira_datetime(sp.get("completeDate"))),
                            "name": sp.get("name", ""),
                            "state": (sp.get("state") or "").lower(),
                        }
                    intervals.setdefault(sid, [[story_created or _EPOCH, None]])

            # Sprints the story belonged to that have a known start date,
            # ordered chronologically.
            seq = []
            for sid in intervals:
                info_sp = sprint_map.get(sid)
                if not info_sp or not info_sp.get("start"):
                    continue
                seq.append((sid, info_sp))
            seq.sort(key=lambda pair: pair[1]["start"])

            # A genuine carry: a later sprint started at/after an earlier
            # sprint (that the story was in) had ended.
            transitions = []
            for i in range(len(seq) - 1):
                _a_sid, a = seq[i]
                _b_sid, b = seq[i + 1]
                a_end = a.get("end")
                if a_end is not None and b["start"] >= a_end:
                    transitions.append((a, b))
                elif a_end is None and b["start"] > a["start"]:
                    transitions.append((a, b))
            if not transitions:
                continue

            # Keep only carries whose destination sprint started in the window.
            in_window = [t for t in transitions
                         if window_start <= t[1]["start"] <= window_end]
            if not in_window:
                continue

            first_from = transitions[0][0]
            last_to = in_window[-1][1]
            status_obj = fields.get("status") or {}
            stories.append({
                "ticket": key,
                "summary": fields.get("summary", "") or "",
                "team_name": meta.get("team_name", project_key),
                "product_group": meta.get("product_group", "Unknown"),
                "board_key": meta.get("board_key", project_key),
                "status": status_obj.get("name", "") or "",
                "from_sprint": first_from.get("name", "") or "\u2014",
                "to_sprint": last_to.get("name", "") or "\u2014",
                "sprint_count": len(seq),
                "carry_count": len(transitions),
                "carried_at": date_str(last_to["start"]),
                "url": f"{client.host}/browse/{key}",
            })
            carried += 1
        if cl_failed:
            warn(f"    {project_key}: changelog fetch failed for {cl_failed}/{len(issues)} "
                 f"stories (carry-over may be undercounted)")
        if verbose and issues:
            info(f"  {project_key}: {len(issues)} stories, {carried} carried over "
                 f"into a later sprint within the window")
    stories.sort(key=lambda s: (s["team_name"].lower(), s["ticket"]))
    return stories


# ── Output writers ───────────────────────────────────────────────────────────
def write_csv(rows: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    ok(f"CSV written: {path} ({len(rows)} rows)")


def build_team_cards(rows: List[dict]) -> List[dict]:
    """Aggregate rows into per-team signal cards for the dashboard."""
    teams: Dict[str, dict] = {}
    for row in rows:
        tid = f"{row['product_group']}||{row['team_name']}||{row['board_key']}"
        team = teams.setdefault(
            tid,
            {
                "team_name": row["team_name"],
                "product_group": row["product_group"],
                "board_key": row["board_key"],
                "board_id": row.get("board_id", ""),
                "project_name": row.get("project_name", ""),
                "owner": row.get("_owner", ""),
                "total": 0,
                "differ": 0,
                "aligned": 0,
                "no_first_done": 0,
                "first_done_only": 0,
                "differences": [],
            },
        )
        team["total"] += 1
        pop = row["population"]
        if pop == POP_DIFFER:
            team["differ"] += 1
            team["differences"].append(
                {
                    "ticket": row["ticket_key"],
                    "summary": row["summary"],
                    "population": pop,
                    "completed_at": row["completed_at"],
                    "first_done": row["first_done"],
                    "url": row["url"],
                }
            )
        elif pop == POP_ALIGNED:
            team["aligned"] += 1
        elif pop == POP_NO_FIRST_DONE:
            team["no_first_done"] += 1
        elif pop == POP_FIRST_DONE_ONLY:
            team["first_done_only"] += 1

    cards: List[dict] = []
    for team in teams.values():
        total = team["total"] or 1
        differ = team["differ"]
        ratio = differ / total
        if differ == 0:
            signal = "Fully aligned"
            status = "aligned"
            meaning = "Every completed ticket's resolution date agrees with its first Done transition. Lead and cycle timing can be trusted."
            action = "No action needed. Keep monitoring monthly."
        elif ratio >= 0.25:
            signal = "High reconciliation risk"
            status = "risk"
            meaning = "A large share of tickets have their resolution date and first Done transition in different months. Delivery timing metrics are unreliable until reconciled."
            action = "Review reopened / re-completed tickets and workflow-category mapping with the team."
        else:
            signal = "Some differences"
            status = "warning"
            meaning = "A minority of tickets disagree between resolution date and first Done transition. Investigate before trusting month-over-month timing."
            action = "Spot-check the differing tickets for reopening or automation side effects."
        team["signal"] = signal
        team["status"] = status
        team["meaning"] = meaning
        team["action"] = action
        team["differences"].sort(key=lambda d: d["ticket"])
        cards.append(team)

    cards.sort(key=lambda c: (-c["differ"], c["team_name"].lower()))
    return cards


def write_html(rows: List[dict], path: Path, window_label: str,
               sprints: Optional[List[dict]] = None,
               early_sprints: Optional[List[dict]] = None,
               added_sprints: Optional[List[dict]] = None,
               removed_sprints: Optional[List[dict]] = None,
               stories_no_sp: Optional[List[dict]] = None,
               stories_sp_changed: Optional[List[dict]] = None,
               stories_carry_over: Optional[List[dict]] = None,
               all_sprints: Optional[List[dict]] = None,
               team_names: Optional[List[str]] = None,
               stories_large_sp: Optional[List[dict]] = None,
               stories_multi_sprint: Optional[List[dict]] = None) -> None:
    cards = build_team_cards(rows)
    team_names = sorted({t for t in (team_names or []) if t}
                        | {c["team_name"] for c in cards}, key=str.lower)
    sprints = sprints or []
    early_sprints = early_sprints or []
    added_sprints = added_sprints or []
    removed_sprints = removed_sprints or []
    stories_no_sp = stories_no_sp or []
    stories_sp_changed = stories_sp_changed or []
    stories_carry_over = stories_carry_over or []
    stories_large_sp = stories_large_sp or []
    stories_multi_sprint = stories_multi_sprint or []
    all_sprints = all_sprints or []
    totals = {
        "tickets": len(rows),
        "differ": sum(1 for r in rows if r["population"] == POP_DIFFER),
        "aligned": sum(1 for r in rows if r["population"] == POP_ALIGNED),
        "no_first_done": sum(1 for r in rows if r["population"] == POP_NO_FIRST_DONE),
        "first_done_only": sum(1 for r in rows if r["population"] == POP_FIRST_DONE_ONLY),
        "teams": len(cards),
        "delayed_sprints": len(sprints),
        "early_sprints": len(early_sprints),
        "added_sprints": len(added_sprints),
        "added_issues": sum(s.get("added_count", 0) for s in added_sprints),
        "removed_sprints": len(removed_sprints),
        "removed_issues": sum(s.get("removed_count", 0) for s in removed_sprints),
        "stories_no_sp": len(stories_no_sp),
        "stories_sp_changed": len(stories_sp_changed),
        "stories_carry_over": len(stories_carry_over),
        "stories_large_sp": len(stories_large_sp),
        "stories_multi_sprint": len(stories_multi_sprint),
        "all_sprints": len(all_sprints),
    }
    product_groups = sorted({c["product_group"] for c in cards})
    reopened: List[dict] = []
    for r in rows:
        if r.get("reopen_count", 0) > 0:
            reopened.append({
                "team_name": r["team_name"],
                "product_group": r["product_group"],
                "board_key": r["board_key"],
                "ticket": r["ticket_key"],
                "summary": r["summary"],
                "reopen_count": r["reopen_count"],
                "reopened_at": r["reopened_at"],
                "status": r["status"],
                "url": r["url"],
            })
    reopened.sort(key=lambda r: (-r["reopen_count"], r["team_name"].lower(), r["ticket"]))
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "window": window_label,
        "totals": totals,
        "productGroups": product_groups,
        "teams": team_names,
        "cards": cards,
        "sprints": sprints,
        "addedSprints": added_sprints,
        "removedSprints": removed_sprints,
        "storiesNoSp": stories_no_sp,
        "storiesSpChanged": stories_sp_changed,
        "storiesCarryOver": stories_carry_over,
        "storiesLargeSp": stories_large_sp,
        "storiesMultiSprint": stories_multi_sprint,
        "allSprints": all_sprints,
        "reopened": reopened,
    }
    data_json = json.dumps(payload, ensure_ascii=False)
    html = _HTML_TEMPLATE.replace("/*__DATA__*/", data_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    ok(f"Dashboard written: {path}")


# ── Credential resolution ────────────────────────────────────────────────────
def resolve_credentials(
    args: argparse.Namespace,
) -> Tuple[str, str, str]:
    email_cache = Path(tempfile.gettempdir()) / "jira_population_email.cache"
    token_cache = Path(tempfile.gettempdir()) / "jira_population_token.cache"

    if args.reset_token:
        for cache in (email_cache, token_cache):
            if cache.exists():
                cache.unlink()
        warn("Cached credentials cleared.")

    host = args.host or os.environ.get("JIRA_HOST") or DEFAULT_HOST

    email = args.email or os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USER")
    if not email and email_cache.exists():
        email = email_cache.read_text().strip() or None
    if not email and sys.stdin.isatty():
        email = input("JIRA email address (cached for future runs): ").strip()
        if email:
            email_cache.write_text(email)
    if not email:
        raise SystemExit("Error: JIRA email required (--email or JIRA_EMAIL).")

    token = args.token or os.environ.get("JIRA_API_TOKEN")
    if not token and token_cache.exists():
        token = token_cache.read_text().strip() or None
    if not token and sys.stdin.isatty():
        import getpass

        token = getpass.getpass("JIRA API token (cached for future runs): ").strip()
        if token:
            token_cache.write_text(token)
    if not token:
        raise SystemExit("Error: JIRA API token required (--token or JIRA_API_TOKEN).")

    return host, email, token


# ── Local dashboard launch ───────────────────────────────────────────────────
def serve_dashboard(port: int) -> None:
    del port

    if not OUTPUT_HTML.exists():
        raise SystemExit(f"Error: HTML dashboard not found at {OUTPUT_HTML}")

    url = OUTPUT_HTML.resolve().as_uri()
    ok(f"Opening dashboard at {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Jira Hygiene Dashboard — ticket population reconciliation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--projects-csv", default=str(DEFAULT_PROJECTS_CSV),
                        help="Team/project mapping CSV (default: data/jira.csv)")
    parser.add_argument("--project", action="append", default=[],
                        help="Restrict to one project key (repeatable)")
    parser.add_argument("--board", action="append", default=[],
                        help="Restrict to a board by key, ID number, or name (repeatable)")
    parser.add_argument("--ticket", action="append", default=[],
                        help="Analyze specific issue keys (repeatable); ignores period filter")
    parser.add_argument("--month", help="Reporting month YYYY-MM (defaults to current month)")
    parser.add_argument("--since", help="Explicit window start YYYY-MM-DD (overrides --month)")
    parser.add_argument("--until", help="Explicit window end YYYY-MM-DD (overrides --month)")
    parser.add_argument("--host", help="JIRA base URL (env JIRA_HOST)")
    parser.add_argument("--email", help="JIRA email for Basic auth (env JIRA_EMAIL, cached)")
    parser.add_argument("--token", help="JIRA API token (env JIRA_API_TOKEN, cached)")
    parser.add_argument("--reset-token", action="store_true",
                        help="Clear cached token and email, then re-prompt")
    parser.add_argument("--csv", action="store_true",
                        help="Export to outputs/csv/jirapopulation.csv")
    parser.add_argument("--csv-path", help="Custom CSV output path")
    parser.add_argument("--html", action="store_true",
                        help="Build outputs/html/jirapopulation.html")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw rows as JSON to stdout")
    parser.add_argument("--serve", nargs="?", const=8000, type=int, metavar="PORT",
                        help="Open the generated HTML dashboard directly; implies --html")
    parser.add_argument("--verbose", action="store_true",
                        help="Detailed per-ticket / API diagnostics")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.serve is not None:
        args.html = True

    window_start, window_end = resolve_window(args.month, args.since, args.until)
    if args.since or args.until:
        window_label = f"{args.since} to {args.until}"
    elif args.month:
        window_label = args.month
    else:
        window_label = window_start.strftime("%Y-%m") + " (current month)"

    mapping = load_project_mapping(Path(args.projects_csv))

    host, email, token = resolve_credentials(args)
    client = JiraClient(host, email, token, verbose=args.verbose)

    try:
        who = client.verify_auth()
    except JiraAuthError as exc:
        err(str(exc))
        return 1
    except requests.RequestException as exc:
        err(f"Could not connect to JIRA at {host}: {exc}")
        return 1
    ok(f"Authenticated as {who}")

    status_categories = client.status_category_map()

    rows: List[dict] = []
    try:
        if args.ticket:
            info(f"Reconciling {len(args.ticket)} specific ticket(s)...")
            rows = process_tickets(
                client, args.ticket, mapping, status_categories,
                window_start, window_end, args.verbose,
            )
        else:
            selected = list(args.project)
            if args.board:
                selected += resolve_boards(args.board, mapping)
            projects = list(dict.fromkeys(selected)) or list(mapping.keys())
            if not projects:
                err("No projects to process. Provide --project or a valid --projects-csv.")
                return 1
            info(f"Reconciling {len(projects)} project(s) for window {window_label}...")
            for project_key in projects:
                meta = mapping.get(project_key, {
                    "board_key": project_key,
                    "team_name": project_key,
                    "product_group": "Unknown",
                    "tpm": "",
                })
                info(f"- {project_key} ({meta.get('team_name', project_key)})")
                try:
                    rows.extend(
                        process_project(
                            client, project_key, meta, window_start, window_end,
                            status_categories, args.verbose,
                        )
                    )
                except JiraAuthError as exc:
                    err(str(exc))
                    return 1
                except requests.RequestException as exc:
                    warn(f"  {project_key}: skipped ({exc})")
    except JiraAuthError as exc:
        err(str(exc))
        return 1

    rows.sort(key=lambda r: (r["product_group"], r["team_name"], r["ticket_key"]))

    # Sprint closure delay — only when building the dashboard / JSON output.
    sprints: List[dict] = []
    early_sprints: List[dict] = []
    added_sprints: List[dict] = []
    removed_sprints: List[dict] = []
    all_sprints: List[dict] = []
    stories_no_sp: List[dict] = []
    stories_sp_changed: List[dict] = []
    stories_carry_over: List[dict] = []
    stories_large_sp: List[dict] = []
    stories_multi_sprint: List[dict] = []
    sp_fields: List[str] = []
    if (args.html or args.json) and not args.ticket:
        projects_for_sprints = list(dict.fromkeys(args.project)) or list(mapping.keys())
        info("Checking sprint closure delays...")
        try:
            sprints, early_sprints, added_sprints, removed_sprints, all_sprints = collect_sprint_delays(
                client, mapping, projects_for_sprints,
                window_start, window_end, args.verbose,
            )
        except JiraAuthError as exc:
            err(str(exc))
            return 1
        except requests.RequestException as exc:
            warn(f"  sprint delay check skipped ({exc})")
        info("Checking stories with no story points...")
        try:
            sp_fields = client.story_points_fields()
            stories_no_sp = collect_stories_no_sp(
                client, mapping, projects_for_sprints,
                window_start, window_end, sp_fields, args.verbose,
            )
        except JiraAuthError as exc:
            err(str(exc))
            return 1
        except requests.RequestException as exc:
            warn(f"  stories-with-no-SP check skipped ({exc})")
        info("Checking stories with SP changed after sprint start...")
        try:
            sprint_field = client.sprint_field()
            stories_sp_changed = collect_stories_sp_changed(
                client, mapping, projects_for_sprints,
                window_start, window_end, sp_fields, sprint_field, args.verbose,
            )
        except JiraAuthError as exc:
            err(str(exc))
            return 1
        except requests.RequestException as exc:
            warn(f"  stories-with-SP-changed check skipped ({exc})")
        info("Checking stories carried over into a later sprint...")
        try:
            sprint_field = client.sprint_field()
            stories_carry_over = collect_stories_carry_over(
                client, mapping, projects_for_sprints,
                window_start, window_end, sprint_field, args.verbose,
            )
        except JiraAuthError as exc:
            err(str(exc))
            return 1
        except requests.RequestException as exc:
            warn(f"  carry-over check skipped ({exc})")
        info(f"Checking stories with SP > {LARGE_SP_THRESHOLD}...")
        try:
            stories_large_sp = collect_stories_large_sp(
                client, mapping, projects_for_sprints,
                window_start, window_end, sp_fields, args.verbose,
            )
        except JiraAuthError as exc:
            err(str(exc))
            return 1
        except requests.RequestException as exc:
            warn(f"  large-SP check skipped ({exc})")
        info("Checking stories in more than 2 sprints...")
        try:
            stories_multi_sprint = collect_stories_multi_sprint(
                client, mapping, projects_for_sprints,
                window_start, window_end, client.sprint_field(), args.verbose,
            )
        except JiraAuthError as exc:
            err(str(exc))
            return 1
        except requests.RequestException as exc:
            warn(f"  multi-sprint check skipped ({exc})")

    # Summary
    differ = sum(1 for r in rows if r["population"] == POP_DIFFER)
    info("")
    ok(f"Reconciled {len(rows)} relevant ticket(s); {differ} flagged '{POP_DIFFER}'.")
    if args.html or args.json:
        overdue = sum(1 for s in sprints if s["state"] == "active")
        late = sum(1 for s in sprints if s["state"] == "closed")
        ok(f"Sprint closure delay: {len(sprints)} delayed sprint(s) "
           f"({late} closed late, {overdue} open/overdue).")
        ok(f"Issues removed after sprint start: {len(early_sprints)} sprint(s) "
           f"closed before their planned end date.")
        added_issues = sum(s.get("added_count", 0) for s in added_sprints)
        ok(f"Issues added after sprint start: {added_issues} issue(s) "
           f"across {len(added_sprints)} sprint(s).")
        removed_issues = sum(s.get("removed_count", 0) for s in removed_sprints)
        ok(f"Issues removed mid-sprint: {removed_issues} issue(s) "
           f"across {len(removed_sprints)} sprint(s).")
        ok(f"Stories with no story points: {len(stories_no_sp)} story(ies) "
           f"created in the window without an estimate.")
        ok(f"Stories with SP changed after sprint start: {len(stories_sp_changed)} story(ies).")
        ok(f"Stories carried over into a later sprint: {len(stories_carry_over)} story(ies).")
        ok(f"Issues with SP > {LARGE_SP_THRESHOLD}: {len(stories_large_sp)} issue(s).")
        ok(f"Stories in more than 2 sprints: {len(stories_multi_sprint)} story(ies).")

    if args.json:
        printable = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
        print(json.dumps({
            "tickets": printable,
            "delayed_sprints": sprints,
            "issues_removed_after_sprint_start": early_sprints,
            "issues_added_after_sprint_start": added_sprints,
            "issues_removed_mid_sprint": removed_sprints,
            "stories_with_no_story_points": stories_no_sp,
            "stories_with_sp_changed_after_sprint_start": stories_sp_changed,
            "stories_carried_over": stories_carry_over,
            "stories_with_sp_over_8": stories_large_sp,
            "stories_in_more_than_2_sprints": stories_multi_sprint,
        }, indent=2, default=str))

    if args.csv or args.csv_path:
        write_csv(rows, Path(args.csv_path) if args.csv_path else OUTPUT_CSV)

    if args.html:
        write_html(rows, OUTPUT_HTML, window_label,
                   sprints, early_sprints, added_sprints, removed_sprints,
                   stories_no_sp, stories_sp_changed, stories_carry_over, all_sprints,
                   team_names=[m.get("team_name", "") for m in mapping.values()],
                   stories_large_sp=stories_large_sp,
                   stories_multi_sprint=stories_multi_sprint)

    if args.serve is not None:
        serve_dashboard(args.serve)

    return 0


# ── Dashboard HTML template (self-contained, vanilla JS) ─────────────────────
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jira Hygiene Dashboard</title>
<style>
  :root {
    --bg:#0d1117; --surface:#161b22; --surface2:#21262d; --border:#30363d;
    --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff;
    --green:#3fb950; --yellow:#d29922; --red:#f85149; --orange:#db6d28;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif; background:var(--bg); color:var(--text); line-height:1.5; }
  .app { max-width:1500px; margin:0 auto; padding:24px; }
  header { display:flex; justify-content:space-between; align-items:flex-end; padding-bottom:18px; border-bottom:1px solid var(--border); margin-bottom:20px; flex-wrap:wrap; gap:12px; }
  header h1 { font-size:22px; font-weight:600; }
  header .meta { color:var(--muted); font-size:13px; text-align:right; }
  .window-banner { display:flex; align-items:center; gap:10px; background:var(--surface); border:1px solid var(--border); border-left:4px solid var(--accent); border-radius:8px; padding:10px 16px; margin-bottom:18px; font-size:13.5px; }
  .window-banner .cal { font-size:16px; }
  .window-banner strong { color:var(--accent); }
  .window-badge { display:inline-block; background:rgba(88,166,255,.16); color:var(--accent); border-radius:12px; padding:2px 10px; font-size:12px; font-weight:600; }
  .stats-bar { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }
  .stat { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:14px 18px; min-width:0; flex:1 1 0; }
  .stats-break { flex-basis:100%; height:0; margin:-14px 0 0; padding:0; }
  .stat .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  .stat .value { font-size:26px; font-weight:700; margin-top:4px; }
  .value.red { color:var(--red); } .value.green { color:var(--green); } .value.yellow { color:var(--yellow); }
  .controls { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; align-items:center; }
  .controls input, .controls select { background:var(--surface); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:6px; font-size:14px; }
  .controls input { min-width:280px; }
  .controls input:focus, .controls select:focus { outline:none; border-color:var(--accent); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(440px,1fr)); gap:16px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
  .card.risk { border-left:4px solid var(--red); }
  .card.warning { border-left:4px solid var(--yellow); }
  .card.aligned { border-left:4px solid var(--green); }
  .card-head { padding:16px 18px; }
  .card-head .team { font-size:16px; font-weight:600; }
  .card-head .group { color:var(--muted); font-size:12px; margin-top:2px; }
  .signal-row { display:flex; align-items:center; gap:8px; margin-top:10px; }
  .pill { display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:600; }
  .pill.risk { background:rgba(248,81,73,.15); color:var(--red); }
  .pill.warning { background:rgba(210,153,34,.15); color:var(--yellow); }
  .pill.aligned { background:rgba(63,185,80,.15); color:var(--green); }
  .kv { margin-top:12px; display:grid; grid-template-columns:120px 1fr; gap:6px 10px; font-size:13px; }
  .kv .k { color:var(--muted); }
  .card-metrics { display:flex; gap:14px; flex-wrap:wrap; padding:0 18px 14px; font-size:12px; color:var(--muted); }
  .card-metrics strong { color:var(--text); }
  .toggle { width:100%; text-align:left; background:var(--surface2); border:none; border-top:1px solid var(--border); color:var(--accent); padding:10px 18px; font-size:13px; cursor:pointer; }
  .toggle:hover { background:#2a3038; }
  .diffs { display:none; padding:0; }
  .diffs.open { display:block; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th { text-align:left; padding:8px 12px; background:var(--surface2); color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.4px; }
  td { padding:8px 12px; border-bottom:1px solid var(--border); vertical-align:top; }
  td.summary { color:var(--muted); max-width:220px; }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .empty { color:var(--muted); padding:40px; text-align:center; }
  .section-title { font-size:18px; font-weight:600; margin:6px 0 2px; }
  .section-sub { color:var(--muted); font-size:13px; margin-bottom:14px; }
  .sprint-section { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:18px; margin-bottom:26px; }
  .sprint-section.risk { border-left:4px solid var(--red); }
  .sprint-section.ok { border-left:4px solid var(--green); }
  .sprint-table-wrap { overflow-x:auto; }
  table.sprints { width:100%; border-collapse:collapse; font-size:13px; }
  table.sprints th { text-align:left; padding:9px 12px; background:var(--surface2); color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.4px; white-space:nowrap; }
  table.sprints td { padding:9px 12px; border-bottom:1px solid var(--border); vertical-align:top; }
  table.sprints tr:hover td { background:var(--surface2); }
  .delay-pill { display:inline-block; padding:2px 10px; border-radius:12px; font-weight:700; font-size:12px; }
  .delay-pill.high { background:rgba(248,81,73,.18); color:var(--red); }
  .delay-pill.mid { background:rgba(210,153,34,.18); color:var(--yellow); }
  .state-pill { display:inline-block; padding:2px 9px; border-radius:12px; font-size:11px; font-weight:600; }
  .state-pill.closed { background:rgba(219,109,40,.18); color:var(--orange); }
  .state-pill.active { background:rgba(88,166,255,.16); color:var(--accent); }
  td.goal { color:var(--muted); max-width:260px; }
  td.issue-links { max-width:340px; line-height:1.9; }
  td.issue-links a { background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:1px 7px; margin:0 2px 2px 0; font-size:12px; display:inline-block; }
  .sprint-empty { color:var(--green); padding:10px 2px; font-size:14px; }
</style>
</head>
<body>
<div class="app">
  <header>
    <div>
      <h1>Jira Hygiene Dashboard</h1>
      <div class="meta" id="subtitle"></div>
    </div>
    <div class="meta" id="generated"></div>
  </header>

  <div class="window-banner" id="windowBanner">
    <span class="cal">&#128197;</span>
    <span>All metrics on this dashboard cover the reporting window
      <strong id="windowRange"></strong>.</span>
  </div>

  <div class="stats-bar" id="stats"></div>

  <div class="controls">
    <input type="text" id="search" placeholder="Search team, board name, board key, board ID, or ticket…">
    <select id="teamFilter" aria-label="Filter by team"><option value="">All teams</option></select>
  </div>

  <section class="sprint-section" id="sprintSection">
    <div class="section-title">Sprint Closure Delay <span class="window-badge" id="sprintWindowBadge"></span></div>
    <div class="section-sub" id="sprintSub"></div>
    <div class="sprint-table-wrap" id="sprintWrap"></div>
  </section>

  <section class="sprint-section" id="addedSection">
    <div class="section-title">Issues Added After Sprint Start <span class="window-badge" id="addedWindowBadge"></span></div>
    <div class="section-sub" id="addedSub"></div>
    <div class="sprint-table-wrap" id="addedWrap"></div>
  </section>

  <section class="sprint-section" id="removedSection">
    <div class="section-title">Issues Removed Mid-Sprint <span class="window-badge" id="removedWindowBadge"></span></div>
    <div class="section-sub" id="removedSub"></div>
    <div class="sprint-table-wrap" id="removedWrap"></div>
  </section>

  <section class="sprint-section" id="noSpSection">
    <div class="section-title">Stories with No SP <span class="window-badge" id="noSpWindowBadge"></span></div>
    <div class="section-sub" id="noSpSub"></div>
    <div class="sprint-table-wrap" id="noSpWrap"></div>
  </section>

  <section class="sprint-section" id="spChangedSection">
    <div class="section-title">Stories with SP Changed After Sprint Start <span class="window-badge" id="spChangedWindowBadge"></span></div>
    <div class="section-sub" id="spChangedSub"></div>
    <div class="sprint-table-wrap" id="spChangedWrap"></div>
  </section>

  <section class="sprint-section" id="carryOverSection">
    <div class="section-title">Carry Over <span class="window-badge" id="carryOverWindowBadge"></span></div>
    <div class="section-sub" id="carryOverSub"></div>
    <div class="sprint-table-wrap" id="carryOverWrap"></div>
  </section>

  <section class="sprint-section" id="largeSpSection">
    <div class="section-title">Stories with SP &gt; 8 <span class="window-badge" id="largeSpWindowBadge"></span></div>
    <div class="section-sub" id="largeSpSub"></div>
    <div class="sprint-table-wrap" id="largeSpWrap"></div>
  </section>

  <section class="sprint-section" id="multiSprintSection">
    <div class="section-title">Stories in More Than 2 Sprints <span class="window-badge" id="multiSprintWindowBadge"></span></div>
    <div class="section-sub" id="multiSprintSub"></div>
    <div class="sprint-table-wrap" id="multiSprintWrap"></div>
  </section>

  <section class="sprint-section" id="reopenedSection" style="margin-top:26px;">
    <div class="section-title">Reopened Issues <span class="window-badge" id="reopenedWindowBadge"></span></div>
    <div class="section-sub" id="reopenedSub"></div>
    <div class="sprint-table-wrap" id="reopenedWrap"></div>
  </section>
</div>

<script>
const DATA = /*__DATA__*/;

function el(tag, attrs, children) {
  const e = document.createElement(tag);
  if (attrs) for (const k in attrs) {
    if (k === 'class') e.className = attrs[k];
    else if (k === 'html') e.innerHTML = attrs[k];
    else e.setAttribute(k, attrs[k]);
  }
  (children || []).forEach(c => e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
  return e;
}

function renderStats() {
  const f = filterState();
  const cards = DATA.cards.filter(c => cardMatches(c, f));
  const teams = cards.length;
  const tickets = cards.reduce((n, c) => n + (c.total || 0), 0);
  const reopened = (DATA.reopened || []).filter(r => reopenedMatches(r, f)).length;
  const added = (DATA.addedSprints || []).filter(s => sprintMatches(s, f))
    .reduce((n, s) => n + (s.added_count || 0), 0);
  const removed = (DATA.removedSprints || []).filter(s => sprintMatches(s, f))
    .reduce((n, s) => n + (s.removed_count || 0), 0);
  const noSp = (DATA.storiesNoSp || []).filter(r => reopenedMatches(r, f)).length;
  const spChanged = (DATA.storiesSpChanged || []).filter(r => reopenedMatches(r, f)).length;
  const carryOver = (DATA.storiesCarryOver || []).filter(r => reopenedMatches(r, f)).length;
  const largeSp = (DATA.storiesLargeSp || []).filter(r => reopenedMatches(r, f)).length;
  const multiSprint = (DATA.storiesMultiSprint || []).filter(r => reopenedMatches(r, f)).length;
  const sprintsTotal = (DATA.allSprints || []).filter(s => sprintMatches(s, f)).length;
  const cfg = [
    ['Teams', teams, ''],
    ['Sprints', sprintsTotal, ''],
    ['Tickets', tickets, ''],
    ['Re-opened Issues', reopened, reopened > 0 ? 'red' : 'green'],
    ['Issues Added Mid-Sprint', added, added > 0 ? 'yellow' : 'green'],
    ['Issues Removed Mid-Sprint', removed, removed > 0 ? 'yellow' : 'green'],
    ['Stories with No SP', noSp, noSp > 0 ? 'red' : 'green'],
    ['Stories with SP Changed After Sprint Start', spChanged, spChanged > 0 ? 'red' : 'green'],
    ['Carry Over', carryOver, carryOver > 0 ? 'yellow' : 'green'],
    ['Stories with SP > 8', largeSp, largeSp > 0 ? 'red' : 'green'],
    ['Stories in More Than 2 Sprints', multiSprint, multiSprint > 0 ? 'red' : 'green'],
  ];
  const bar = document.getElementById('stats');
  bar.innerHTML = '';
  cfg.forEach(([label, value, cls], i) => {
    if (i === 6) bar.appendChild(el('div', {class:'stats-break'}));
    bar.appendChild(el('div', {class:'stat'}, [
      el('div', {class:'label'}, [label]),
      el('div', {class:'value ' + cls}, [String(value)]),
    ]));
  });
}

function escapeHtml(str) {
  return String(str || '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function filterState() {
  return {
    q: (document.getElementById('search').value || '').toLowerCase(),
    team: document.getElementById('teamFilter').value || '',
  };
}

function cardMatches(c, f) {
  if (f.team && c.team_name !== f.team) return false;
  if (f.q) {
    const hay = (c.team_name + ' ' + c.product_group + ' ' + c.board_key + ' ' +
      (c.board_id || '') + ' ' + (c.project_name || '') + ' ' +
      c.differences.map(d => d.ticket + ' ' + d.summary).join(' ')).toLowerCase();
    if (!hay.includes(f.q)) return false;
  }
  return true;
}

function sprintMatches(s, f) {
  if (f.team && s.team_name !== f.team) return false;
  if (f.q) {
    const issues = (s.issues || []).map(it => (it.key || '') + ' ' + (it.summary || '')).join(' ');
    const hay = ((s.sprint || '') + ' ' + (s.team_name || '') + ' ' + (s.product_group || '') + ' ' +
      (s.board_key || '') + ' ' + (s.board_id || '') + ' ' +
      (s.goal || '') + ' ' + (s.closure || '') + ' ' + (s.end_date || '') + ' ' +
      (s.complete_date || '') + ' ' + issues).toLowerCase();
    if (!hay.includes(f.q)) return false;
  }
  return true;
}

function reopenedMatches(r, f) {
  if (f.team && r.team_name !== f.team) return false;
  if (f.q) {
    const hay = ((r.ticket || '') + ' ' + (r.summary || '') + ' ' +
      (r.team_name || '') + ' ' + (r.product_group || '') + ' ' +
      (r.board_key || '')).toLowerCase();
    if (!hay.includes(f.q)) return false;
  }
  return true;
}

function renderSprints() {
  const sprints = (DATA.sprints || []).filter(s => sprintMatches(s, filterState()));
  const section = document.getElementById('sprintSection');
  const sub = document.getElementById('sprintSub');
  const wrap = document.getElementById('sprintWrap');
  wrap.innerHTML = '';
  if (!sprints.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Sprints whose planned end date fell within ' + DATA.window +
      ' that did not close on their planned day.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 No delayed sprints in this window — every sprint closed on or before its planned end date.']));
    return;
  }
  section.className = 'sprint-section risk';
  const late = sprints.filter(s => s.state === 'closed').length;
  const overdue = sprints.filter(s => s.state === 'active').length;
  sub.textContent = sprints.length + ' sprint' + (sprints.length === 1 ? '' : 's') +
    ' with a planned end in ' + DATA.window + ' did not close on the expected day \u2014 ' +
    late + ' closed late, ' + overdue + ' still open past the planned end date.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Sprint']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['State']), el('th', {}, ['Planned End']), el('th', {}, ['Actual Close']),
    el('th', {}, ['Delay']), el('th', {}, ['Goal']),
  ]));
  sprints.forEach(s => {
    const cls = s.delay_days >= 7 ? 'high' : 'mid';
    const stateCls = s.state === 'closed' ? 'closed' : 'active';
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:s.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(s.sprint) + '</strong>'}) ]),
      el('td', {}, [s.team_name]),
      el('td', {}, [s.product_group]),
      el('td', {}, [ el('span', {class:'state-pill ' + stateCls}, [s.closure]) ]),
      el('td', {}, [s.end_date || '\u2014']),
      el('td', {}, [s.complete_date || '\u2014']),
      el('td', {}, [ el('span', {class:'delay-pill ' + cls},
        [s.delay_days + ' day' + (s.delay_days === 1 ? '' : 's')]) ]),
      el('td', {class:'goal'}, [s.goal || '\u2014']),
    ]));
  });
  wrap.appendChild(table);
}

function renderAddedSprints() {
  const sprints = (DATA.addedSprints || []).filter(s => sprintMatches(s, filterState()));
  const section = document.getElementById('addedSection');
  const sub = document.getElementById('addedSub');
  const wrap = document.getElementById('addedWrap');
  wrap.innerHTML = '';
  if (!sprints.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Sprints (planned end within ' + DATA.window +
      ') where issues were added after the sprint started — scope creep.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 No issues were added mid-sprint in this window — sprint scope stayed stable after start.']));
    return;
  }
  section.className = 'sprint-section risk';
  const totalAdded = sprints.reduce((n, s) => n + (s.added_count || 0), 0);
  sub.textContent = totalAdded + ' issue' + (totalAdded === 1 ? '' : 's') +
    ' were added after sprint start across ' + sprints.length + ' sprint' +
    (sprints.length === 1 ? '' : 's') + ' with a planned end in ' + DATA.window + '.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Sprint']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['Planned End']), el('th', {}, ['Added']), el('th', {}, ['Issues Added After Start']),
  ]));
  sprints.forEach(s => {
    const cls = (s.added_count || 0) >= 7 ? 'high' : 'mid';
    const issuesCell = el('td', {class:'issue-links'});
    (s.issues || []).forEach((it, i) => {
      if (i) issuesCell.appendChild(document.createTextNode(' '));
      issuesCell.appendChild(el('a', {href:it.url, target:'_blank', rel:'noopener',
        title:it.summary || it.key}, [it.key]));
    });
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:s.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(s.sprint) + '</strong>'}) ]),
      el('td', {}, [s.team_name]),
      el('td', {}, [s.product_group]),
      el('td', {}, [s.end_date || '\u2014']),
      el('td', {}, [ el('span', {class:'delay-pill ' + cls},
        [(s.added_count || 0) + ' issue' + ((s.added_count || 0) === 1 ? '' : 's')]) ]),
      issuesCell,
    ]));
  });
  wrap.appendChild(table);
}

function renderRemovedSprints() {
  const sprints = (DATA.removedSprints || []).filter(s => sprintMatches(s, filterState()));
  const section = document.getElementById('removedSection');
  const sub = document.getElementById('removedSub');
  const wrap = document.getElementById('removedWrap');
  wrap.innerHTML = '';
  if (!sprints.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Sprints (planned end within ' + DATA.window +
      ') where issues were removed/punted out after the sprint started.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 No issues were removed mid-sprint in this window \u2014 sprint scope stayed stable after start.']));
    return;
  }
  section.className = 'sprint-section risk';
  const totalRemoved = sprints.reduce((n, s) => n + (s.removed_count || 0), 0);
  sub.textContent = totalRemoved + ' issue' + (totalRemoved === 1 ? '' : 's') +
    ' were removed after sprint start across ' + sprints.length + ' sprint' +
    (sprints.length === 1 ? '' : 's') + ' with a planned end in ' + DATA.window + '.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Sprint']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['Planned End']), el('th', {}, ['Removed']), el('th', {}, ['Issues Removed Mid-Sprint']),
  ]));
  sprints.forEach(s => {
    const cls = (s.removed_count || 0) >= 7 ? 'high' : 'mid';
    const issuesCell = el('td', {class:'issue-links'});
    (s.issues || []).forEach((it, i) => {
      if (i) issuesCell.appendChild(document.createTextNode(' '));
      issuesCell.appendChild(el('a', {href:it.url, target:'_blank', rel:'noopener',
        title:it.summary || it.key}, [it.key]));
    });
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:s.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(s.sprint) + '</strong>'}) ]),
      el('td', {}, [s.team_name]),
      el('td', {}, [s.product_group]),
      el('td', {}, [s.end_date || '\u2014']),
      el('td', {}, [ el('span', {class:'delay-pill ' + cls},
        [(s.removed_count || 0) + ' issue' + ((s.removed_count || 0) === 1 ? '' : 's')]) ]),
      issuesCell,
    ]));
  });
  wrap.appendChild(table);
}

function renderStoriesSpChanged() {
  const rows = (DATA.storiesSpChanged || []).filter(r => reopenedMatches(r, filterState()));
  const section = document.getElementById('spChangedSection');
  const sub = document.getElementById('spChangedSub');
  const wrap = document.getElementById('spChangedWrap');
  wrap.innerHTML = '';
  if (!rows.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Stories whose story-point estimate was changed after their sprint started, within ' +
      DATA.window + '.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 No stories had their story points changed after sprint start in this window.']));
    return;
  }
  section.className = 'sprint-section risk';
  sub.textContent = rows.length + (rows.length === 1 ? ' story' : ' stories') +
    ' had their story-point estimate changed after the sprint started, within ' + DATA.window + '.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Story']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['Summary']), el('th', {}, ['SP Change']), el('th', {}, ['Changed']),
  ]));
  rows.forEach(r => {
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:r.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(r.ticket) + '</strong>'}) ]),
      el('td', {}, [r.team_name]),
      el('td', {}, [r.product_group]),
      el('td', {class:'goal'}, [r.summary || '\u2014']),
      el('td', {}, [ el('span', {class:'delay-pill mid'},
        [(r.sp_from || '\u2014') + ' \u2192 ' + (r.sp_to || '\u2014')]) ]),
      el('td', {}, [r.changed_at || '\u2014']),
    ]));
  });
  wrap.appendChild(table);
}

function renderStoriesCarryOver() {
  const rows = (DATA.storiesCarryOver || []).filter(r => reopenedMatches(r, filterState()));
  const section = document.getElementById('carryOverSection');
  const sub = document.getElementById('carryOverSub');
  const wrap = document.getElementById('carryOverWrap');
  wrap.innerHTML = '';
  if (!rows.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Stories carried over from a completed sprint into a later sprint within ' +
      DATA.window + '.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 No stories were carried over into a later sprint in this window.']));
    return;
  }
  section.className = 'sprint-section risk';
  sub.textContent = rows.length + (rows.length === 1 ? ' story' : ' stories') +
    ' were carried over into a later sprint within ' + DATA.window + '.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Story']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['Summary']), el('th', {}, ['Status']), el('th', {}, ['Carried From \u2192 To']),
    el('th', {}, ['Sprints']), el('th', {}, ['Carried']),
  ]));
  rows.forEach(r => {
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:r.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(r.ticket) + '</strong>'}) ]),
      el('td', {}, [r.team_name]),
      el('td', {}, [r.product_group]),
      el('td', {class:'goal'}, [r.summary || '\u2014']),
      el('td', {}, [r.status || '\u2014']),
      el('td', {}, [ el('span', {class:'delay-pill mid'},
        [(r.from_sprint || '\u2014') + ' \u2192 ' + (r.to_sprint || '\u2014')]) ]),
      el('td', {}, [String(r.sprint_count || 0)]),
      el('td', {}, [r.carried_at || '\u2014']),
    ]));
  });
  wrap.appendChild(table);
}

function renderStoriesNoSp() {
  const rows = (DATA.storiesNoSp || []).filter(r => reopenedMatches(r, filterState()));
  const section = document.getElementById('noSpSection');
  const sub = document.getElementById('noSpSub');
  const wrap = document.getElementById('noSpWrap');
  wrap.innerHTML = '';
  if (!rows.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Stories created within ' + DATA.window +
      ' that have no story-point estimate.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 Every story created in this window has a story-point estimate.']));
    return;
  }
  section.className = 'sprint-section risk';
  sub.textContent = rows.length + (rows.length === 1 ? ' story' : ' stories') +
    ' created within ' + DATA.window + ' have no story-point estimate.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Story']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['Summary']), el('th', {}, ['Status']), el('th', {}, ['Created']),
  ]));
  rows.forEach(r => {
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:r.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(r.ticket) + '</strong>'}) ]),
      el('td', {}, [r.team_name]),
      el('td', {}, [r.product_group]),
      el('td', {class:'goal'}, [r.summary || '\u2014']),
      el('td', {}, [r.status || '\u2014']),
      el('td', {}, [r.created || '\u2014']),
    ]));
  });
  wrap.appendChild(table);
}

function renderStoriesLargeSp() {
  const rows = (DATA.storiesLargeSp || []).filter(r => reopenedMatches(r, filterState()));
  const section = document.getElementById('largeSpSection');
  const sub = document.getElementById('largeSpSub');
  const wrap = document.getElementById('largeSpWrap');
  wrap.innerHTML = '';
  if (!rows.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Issues with story points greater than 8 created or updated within ' +
      DATA.window + '.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 No issues with more than 8 story points in this window.']));
    return;
  }
  section.className = 'sprint-section risk';
  sub.textContent = rows.length + (rows.length === 1 ? ' issue has' : ' issues have') +
    ' more than 8 story points and were created or updated within ' + DATA.window + '.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Ticket']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['Summary']), el('th', {}, ['Type']), el('th', {}, ['Status']),
    el('th', {}, ['Story Points']), el('th', {}, ['Created']), el('th', {}, ['Updated']),
  ]));
  rows.forEach(r => {
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:r.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(r.ticket) + '</strong>'}) ]),
      el('td', {}, [r.team_name]),
      el('td', {}, [r.product_group]),
      el('td', {class:'goal'}, [r.summary || '\u2014']),
      el('td', {}, [r.issue_type || '\u2014']),
      el('td', {}, [r.status || '\u2014']),
      el('td', {}, [String(r.story_points)]),
      el('td', {}, [r.created || '\u2014']),
      el('td', {}, [r.updated || '\u2014']),
    ]));
  });
  wrap.appendChild(table);
}

function renderStoriesMultiSprint() {
  const rows = (DATA.storiesMultiSprint || []).filter(r => reopenedMatches(r, filterState()));
  const section = document.getElementById('multiSprintSection');
  const sub = document.getElementById('multiSprintSub');
  const wrap = document.getElementById('multiSprintWrap');
  wrap.innerHTML = '';
  if (!rows.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Stories in sprints running within ' + DATA.window +
      ' that appear in 3 or more sprints.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 No stories span more than 2 sprints in this window.']));
    return;
  }
  section.className = 'sprint-section risk';
  sub.textContent = rows.length + (rows.length === 1 ? ' story' : ' stories') +
    ' in sprints running within ' + DATA.window + ' appear in 3 or more sprints.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Story']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['Summary']), el('th', {}, ['Status']), el('th', {}, ['Sprints']),
    el('th', {}, ['Sprint History']),
  ]));
  rows.forEach(r => {
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:r.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(r.ticket) + '</strong>'}) ]),
      el('td', {}, [r.team_name]),
      el('td', {}, [r.product_group]),
      el('td', {class:'goal'}, [r.summary || '\u2014']),
      el('td', {}, [r.status || '\u2014']),
      el('td', {}, [String(r.sprint_count || 0)]),
      el('td', {class:'goal'}, [(r.sprints || []).join(', ') || '\u2014']),
    ]));
  });
  wrap.appendChild(table);
}

function renderReopened() {
  const rows = (DATA.reopened || []).filter(r => reopenedMatches(r, filterState()));
  const section = document.getElementById('reopenedSection');
  const sub = document.getElementById('reopenedSub');
  const wrap = document.getElementById('reopenedWrap');
  wrap.innerHTML = '';
  if (!rows.length) {
    section.className = 'sprint-section ok';
    sub.textContent = 'Tickets that entered a Done status 2 or more times within ' +
      DATA.window + ' \u2014 they came back to Done after first completion, which means they were reopened.';
    wrap.appendChild(el('div', {class:'sprint-empty'},
      ['\u2713 No reopened issues in this window \u2014 no ticket reached Done more than once.']));
    return;
  }
  section.className = 'sprint-section risk';
  sub.textContent = rows.length + ' ticket' + (rows.length === 1 ? '' : 's') +
    ' reached a Done status more than once (reopened after first completion) within ' + DATA.window + '.';

  const table = el('table', {class:'sprints'});
  table.appendChild(el('tr', {}, [
    el('th', {}, ['Ticket']), el('th', {}, ['Team']), el('th', {}, ['Product Group']),
    el('th', {}, ['Summary']), el('th', {}, ['Status']), el('th', {}, ['Reopens']),
    el('th', {}, ['Last Done']),
  ]));
  rows.forEach(r => {
    table.appendChild(el('tr', {}, [
      el('td', {}, [ el('a', {href:r.url, target:'_blank', rel:'noopener',
        html:'<strong>' + escapeHtml(r.ticket) + '</strong>'}) ]),
      el('td', {}, [r.team_name]),
      el('td', {}, [r.product_group]),
      el('td', {class:'goal'}, [r.summary || '\u2014']),
      el('td', {}, [r.status || '\u2014']),
      el('td', {}, [String(r.reopen_count || 0)]),
      el('td', {}, [r.reopened_at || '\u2014']),
    ]));
  });
  wrap.appendChild(table);
}

function applyFilters() {
  renderStats();
  renderSprints();
  renderAddedSprints();
  renderRemovedSprints();
  renderStoriesNoSp();
  renderStoriesSpChanged();
  renderStoriesCarryOver();
  renderStoriesLargeSp();
  renderStoriesMultiSprint();
  renderReopened();
}

function init() {
  document.getElementById('subtitle').textContent = 'Reporting window: ' + DATA.window;
  document.getElementById('windowRange').textContent = DATA.window;
  document.getElementById('sprintWindowBadge').textContent = DATA.window;
  document.getElementById('addedWindowBadge').textContent = DATA.window;
  document.getElementById('removedWindowBadge').textContent = DATA.window;
  document.getElementById('noSpWindowBadge').textContent = DATA.window;
  document.getElementById('spChangedWindowBadge').textContent = DATA.window;
  document.getElementById('carryOverWindowBadge').textContent = DATA.window;
  document.getElementById('largeSpWindowBadge').textContent = DATA.window;
  document.getElementById('multiSprintWindowBadge').textContent = DATA.window;
  document.getElementById('reopenedWindowBadge').textContent = DATA.window;
  document.getElementById('generated').textContent = 'Generated ' + DATA.generated;
  renderStats();
  renderSprints();
  renderAddedSprints();
  renderRemovedSprints();
  renderStoriesNoSp();
  renderStoriesSpChanged();
  renderStoriesCarryOver();
  renderStoriesLargeSp();
  renderStoriesMultiSprint();
  renderReopened();
  const teamSel = document.getElementById('teamFilter');
  (DATA.teams || []).forEach(t => {
    const o = document.createElement('option');
    o.value = t;
    o.textContent = t;
    teamSel.appendChild(o);
  });
  teamSel.addEventListener('change', applyFilters);
  document.getElementById('search').addEventListener('input', applyFilters);
  applyFilters();
}
init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())

