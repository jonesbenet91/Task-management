"""Heat Map Confluence Publisher - auto-publishes weekly report to Confluence.

Reads heat_map_scores.json and pushes an HTML report to the FOODS Confluence
space. Updates the page in-place (versioned) so there's a single living page
that always shows the latest data.
"""

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import click
import requests
from rich.console import Console

console = Console()

OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"
if not OUTPUTS_DIR.exists():
    OUTPUTS_DIR = Path.cwd() / "outputs"

DIMENSIONS = [
    "Delivery", "System Health", "Incidents", "Code Quality",
    "Estate Hygiene", "Ops Readiness", "People",
]

DEFAULT_BASE_URL = "https://jira-marksandspencer-app.atlassian.net/wiki"
DEFAULT_SPACE_KEY = "FOODS"
DEFAULT_PAGE_TITLE = "Foods D&T Engineering Intelligence - Heat Map"


def _score_color(v: float) -> str:
    if v >= 75:
        return "#34d399"
    if v >= 50:
        return "#fbbf24"
    return "#ef4444"


def _score_emoji(v: float) -> str:
    if v >= 75:
        return "&#9989;"  # green tick
    if v >= 50:
        return "&#9888;&#65039;"  # warning
    return "&#10060;"  # red cross


def _build_confluence_html(scores: dict, dashboard_url: str = "") -> str:
    """Build Confluence storage format HTML from scores."""
    week = html.escape(scores.get("week", ""))
    generated = scores.get("generated_at", "")
    domain = scores.get("domain", {})
    subdomains = scores.get("subdomains", [])
    teams = sorted(scores.get("teams", []), key=lambda t: t["overall"], reverse=True)
    vacancies = scores.get("vacancies", [])

    html_content = f"""<h2>Engineering Health Report - {week}</h2>
<p><em>Auto-generated on {generated[:10] if generated else 'N/A'} by the Foods Data Scripts pipeline.</em></p>

<ac:structured-macro ac:name="info">
  <ac:rich-text-body>
    <p><strong>Overall Domain Score: {domain.get('overall', 'N/A')}</strong> across {len(teams)} teams and {len(subdomains)} sub-domains.</p>
  </ac:rich-text-body>
</ac:structured-macro>

<h3>Sub-domain Summary</h3>
<table>
  <thead>
    <tr>
      <th>Sub-domain</th>
      <th>Teams</th>
"""
    for dim in DIMENSIONS:
        html_content += f"      <th>{dim}</th>\n"
    html_content += """      <th>Overall</th>
    </tr>
  </thead>
  <tbody>
"""
    for sd in subdomains:
        subdomain_name = html.escape(sd['subdomain_name'])
        html_content += f"    <tr>\n      <td><strong>{subdomain_name}</strong></td>\n      <td>{sd['team_count']}</td>\n"
        for dim in DIMENSIONS:
            v = sd["scores"].get(dim, 50)
            html_content += f'      <td style="text-align:center;color:{_score_color(v)}"><strong>{v}</strong></td>\n'
        html_content += f'      <td style="text-align:center;color:{_score_color(sd["overall"])}"><strong>{sd["overall"]}</strong></td>\n    </tr>\n'
    html_content += """  </tbody>
</table>

<h3>Team Health Matrix</h3>
<table>
  <thead>
    <tr>
      <th>Team</th>
      <th>Sub-domain</th>
"""
    for dim in DIMENSIONS:
        html_content += f"      <th>{dim}</th>\n"
    html_content += """      <th>Overall</th>
    </tr>
  </thead>
  <tbody>
"""
    for t in teams:
        team_name = html.escape(t['team_name'])
        subdomain_name = html.escape(t['subdomain_name'])
        html_content += f"    <tr>\n      <td>{team_name}</td>\n      <td>{subdomain_name}</td>\n"
        for dim in DIMENSIONS:
            v = t["scores"].get(dim, 50)
            html_content += f'      <td style="text-align:center;color:{_score_color(v)}">{round(v)}</td>\n'
        ov = t["overall"]
        html_content += f'      <td style="text-align:center;color:{_score_color(ov)}"><strong>{ov}</strong></td>\n    </tr>\n'
    html_content += """  </tbody>
</table>
"""

    # Top and bottom performers
    top3 = teams[:3]
    bottom3 = teams[-3:] if len(teams) >= 3 else teams
    html_content += """
<ac:structured-macro ac:name="expand">
  <ac:parameter ac:name="title">Top Performers &amp; Areas Needing Attention</ac:parameter>
  <ac:rich-text-body>
    <h4>Top Performers</h4>
    <ul>
"""
    for t in top3:
        team_name = html.escape(t['team_name'])
        subdomain_name = html.escape(t['subdomain_name'])
        html_content += f"      <li><strong>{team_name}</strong> ({subdomain_name}): {t['overall']}</li>\n"
    html_content += """    </ul>
    <h4>Needs Attention</h4>
    <ul>
"""
    for t in reversed(bottom3):
        team_name = html.escape(t['team_name'])
        subdomain_name = html.escape(t['subdomain_name'])
        html_content += f"      <li><strong>{team_name}</strong> ({subdomain_name}): {t['overall']}</li>\n"
    html_content += """    </ul>
  </ac:rich-text-body>
</ac:structured-macro>
"""

    if vacancies:
        html_content += f"""
<h3>Open Vacancies ({len(vacancies)})</h3>
<table>
  <thead><tr><th>Role</th><th>Team</th><th>Sub-domain</th></tr></thead>
  <tbody>
"""
        for v in vacancies:
            role = html.escape(v.get('role', 'Vacancy'))
            team = html.escape(v.get('team', ''))
            subdomain = html.escape(v.get('subdomain', ''))
            html_content += f"    <tr><td>{role}</td><td>{team}</td><td>{subdomain}</td></tr>\n"
        html_content += "  </tbody>\n</table>\n"

    if dashboard_url:
        dashboard_url_escaped = html.escape(dashboard_url)
        html_content += f'\n<p><a href="{dashboard_url_escaped}">View Interactive Dashboard</a></p>\n'

    html_content += """
<hr/>
<p><em>Data sources: GitHub, Jira, PagerDuty, Dynatrace, ITSM/BMC, Azure. Generated by <code>foods-data-scripts</code> pipeline.</em></p>
"""
    return html_content


def _find_page(base_url: str, space_key: str, title: str, auth: tuple) -> Optional[dict]:
    """Find an existing Confluence page by title.
    
    Returns:
        dict: The page metadata if found
        None: If page doesn't exist (404)
    
    Raises:
        requests.HTTPError: On any non-404 error (401, 429, 500, etc.)
    """
    url = f"{base_url}/rest/api/content"
    params = {"spaceKey": space_key, "title": title, "expand": "version"}
    try:
        resp = requests.get(url, params=params, auth=auth, timeout=30)
    except requests.RequestException as e:
        raise requests.HTTPError(f"Network error searching for page: {e}")
    
    if resp.status_code == 404:
        return None
    
    if resp.status_code != 200:
        raise requests.HTTPError(
            f"Failed to search for Confluence page. Status {resp.status_code}: {resp.text}"
        )
    
    results = resp.json().get("results", [])
    return results[0] if results else None


def _create_page(base_url: str, space_key: str, title: str, body: str, auth: tuple) -> dict:
    """Create a new Confluence page."""
    url = f"{base_url}/rest/api/content"
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {
            "storage": {
                "value": body,
                "representation": "storage",
            }
        },
    }
    resp = requests.post(url, json=payload, auth=auth, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _update_page(base_url: str, page_id: str, title: str, body: str, version: int, auth: tuple) -> dict:
    """Update an existing Confluence page."""
    url = f"{base_url}/rest/api/content/{page_id}"
    payload = {
        "type": "page",
        "title": title,
        "version": {"number": version + 1},
        "body": {
            "storage": {
                "value": body,
                "representation": "storage",
            }
        },
    }
    resp = requests.put(url, json=payload, auth=auth, timeout=30)
    resp.raise_for_status()
    return resp.json()


@click.command("confluence-publish")
@click.option(
    "--scores",
    "scores_path",
    default="outputs/json/heat_map_scores.json",
    show_default=True,
    help="Path to heat_map_scores.json.",
)
@click.option("--base-url", envvar="CONFLUENCE_BASE_URL", default=DEFAULT_BASE_URL, show_default=True)
@click.option("--space-key", default=DEFAULT_SPACE_KEY, show_default=True)
@click.option("--page-title", default=DEFAULT_PAGE_TITLE, show_default=True)
@click.option("--email", envvar="JIRA_EMAIL", help="Confluence/Jira email for auth.")
@click.option("--api-token", envvar="JIRA_API_TOKEN", help="Confluence/Jira API token.")
@click.option(
    "--dashboard-url",
    default="",
    help="URL to the interactive dashboard.",
)
@click.option("--dry-run", is_flag=True, help="Generate HTML but do not push to Confluence.")
@click.option("--verbose", is_flag=True, help="Verbose output.")
def confluence_publish_command(
    scores_path: str, base_url: str, space_key: str, page_title: str,
    email: str, api_token: str, dashboard_url: str, dry_run: bool, verbose: bool
):
    """Publish heat map report to Confluence.

    Creates or updates a page in the FOODS Confluence space with the latest
    engineering health data. The page is versioned (each update increments
    the version number) so history is preserved.

    \b
    Examples:
        data-scripts heatmap confluence-publish --dry-run
        data-scripts heatmap confluence-publish --email user@mnscorp.net --api-token TOKEN
    """
    console.print("[bold]Heat Map Confluence Publisher[/bold]")

    # Load scores
    sp = Path(scores_path)
    if not sp.exists():
        console.print(f"[red]Scores file not found: {sp}[/red]")
        raise SystemExit(1)

    with open(sp, encoding="utf-8") as f:
        scores = json.load(f)

    week = scores.get("week", "unknown")
    week_slug = week.replace(" ", "_")
    console.print(f"  Scores from: {week}")

    # Auto-construct dashboard URL if not provided
    if not dashboard_url:
        dashboard_url = (
            "https://digitalinnovation.github.io/foods-data-scripts"
            f"/outputs/html/heatmap/{week_slug}.html"
        )
    console.print(f"  Dashboard URL: {dashboard_url}")

    # Build Confluence HTML
    body = _build_confluence_html(scores, dashboard_url)

    if dry_run:
        out_path = OUTPUTS_DIR / "html" / "heatmap_confluence_preview.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"<html><body>{body}</body></html>")
        console.print(f"[green]Dry run: wrote preview to {out_path}[/green]")
        return

    # Validate auth
    if not email or not api_token:
        console.print("[red]Missing Confluence auth. Set JIRA_EMAIL and JIRA_API_TOKEN env vars.[/red]")
        raise SystemExit(1)

    auth = (email, api_token)

    # Find or create page
    try:
        existing = _find_page(base_url, space_key, page_title, auth)
    except requests.HTTPError as e:
        console.print(f"[red]Error searching for Confluence page: {e}[/red]")
        raise SystemExit(1)
    
    if existing:
        page_id = existing["id"]
        version = existing["version"]["number"]
        console.print(f"  Updating existing page (ID: {page_id}, version {version})")
        result = _update_page(base_url, page_id, page_title, body, version, auth)
        console.print(f"[green]Updated page to version {result['version']['number']}[/green]")
    else:
        console.print(f"  Creating new page in {space_key}")
        result = _create_page(base_url, space_key, page_title, body, auth)
        console.print(f"[green]Created page (ID: {result['id']})[/green]")

    page_url = f"{base_url}/spaces/{space_key}/pages/{result['id']}"
    console.print(f"[bold]Page URL:[/bold] {page_url}")
