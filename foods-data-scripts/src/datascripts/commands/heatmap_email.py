"""Foods Engineering Vitals Email Report - weekly executive summary for Foods D&T leadership.

Reads heat_map_scores.json AND raw CSV data to produce a comprehensive report covering:
  - Health scores with real charts (matplotlib PNG images base64-embedded in email)
  - Per-subdomain breakdown with dimension radar charts and team comparisons
  - Jira delivery metrics (backlog breakdown, aging, composition)
  - Incident summary (priority breakdown)
  - Change requests (volume, lead time)
  - GitHub activity (commits, PRs, contributors)
  - Automated sending via SendGrid API (when --send flag is used)

Charts are base64-embedded directly in the email HTML, keeping size well under Gmail's 102KB clipping threshold.

Uses SendGrid (same pattern as DigitalInnovation/events-platform-resource-selfserve).
Set SENDGRID_API_KEY env var or pass --sendgrid-api-key.
"""

import base64
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import click
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import numpy as np
from rich.console import Console

from datascripts.utils import safe_float as _safe_float

console = Console()

OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"
if not OUTPUTS_DIR.exists():
    OUTPUTS_DIR = Path.cwd() / "outputs"

DIMENSIONS = [
    "Delivery", "System Health", "Incidents", "Code Quality",
    "Estate Hygiene", "Ops Readiness", "People",
]

DIM_SHORT = {
    "Delivery": "Delivery",
    "System Health": "System",
    "Incidents": "Incidents",
    "Code Quality": "Code Qual",
    "Estate Hygiene": "Estate",
    "Ops Readiness": "Ops Ready",
    "People": "People",
}

# No unicode icons in matplotlib labels (DejaVu Sans can't render them)
DIM_ICONS = {
    "Delivery": "",
    "System Health": "",
    "Incidents": "",
    "Code Quality": "",
    "Estate Hygiene": "",
    "Ops Readiness": "",
    "People": "",
}
# HTML-safe icons for email body (not chart images)
DIM_HTML_ICONS = {
    "Delivery": "&#128640;",
    "System Health": "&#128154;",
    "Incidents": "&#128680;",
    "Code Quality": "&#128270;",
    "Estate Hygiene": "&#128736;",
    "Ops Readiness": "&#9989;",
    "People": "&#128101;",
}

DEFAULT_RECIPIENTS = [
    "rameshreddy.adutla@mnscorp.net",
]

JIRA_BASE = "https://jira-marksandspencer-app.atlassian.net"
GITHUB_ORG = "https://github.com/DigitalInnovation"

# Sub-domain visual identity
SD_COLORS = {
    "Design, Buy & Make": ("#7c3aed", "#a78bfa"),
    "Commercial Planning": ("#2563eb", "#60a5fa"),
    "Supply Chain & Logistics": ("#0d9488", "#5eead4"),
}
SD_EMOJIS = {
    "Design, Buy & Make": "&#127981;",
    "Commercial Planning": "&#128200;",
    "Supply Chain & Logistics": "&#128666;",
}


# ---------------------------------------------------------------------------
# URL / style helpers
# ---------------------------------------------------------------------------

def _jql_url(jql: str) -> str:
    return f"{JIRA_BASE}/issues/?jql={quote(jql)}"


def _linked(url: str, text: str, color: str = "#4f46e5", extra_style: str = "") -> str:
    style = f"text-decoration:none;font-weight:600;color:{color};"
    if extra_style:
        style += extra_style
    return f'<a href="{url}" style="{style}">{text}</a>'


def _score_color(v: float) -> str:
    if v >= 75:
        return "#16a34a"
    if v >= 50:
        return "#d97706"
    return "#dc2626"


def _score_color_rgb(v: float):
    if v >= 75:
        return (0.086, 0.639, 0.290)
    if v >= 50:
        return (0.851, 0.467, 0.024)
    return (0.863, 0.149, 0.149)


def _load_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Chart generation (matplotlib -> base64 data URIs embedded in email)
# ---------------------------------------------------------------------------

_PAGES_BASE = "https://digitalinnovation.github.io/foods-data-scripts"


def _fig_to_b64(fig, dpi: int = 150) -> str:
    """Render a matplotlib figure to a base64 data URI string."""
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                transparent=False, facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _save_chart(fig, name: str, dpi: int = 150) -> str:
    """Render chart to base64 data URI (replaces file-based save)."""
    return _fig_to_b64(fig, dpi=dpi)


def _img_tag(data_uri: str, alt: str = "", width: int = 600) -> str:
    """Return an <img> tag with an embedded base64 data URI."""
    return (f'<!--[if !mso]><!-->'
            f'<img src="{data_uri}" alt="{alt}" '
            f'width="{width}" style="display:block;margin:0 auto;max-width:100%;height:auto;" />'
            f'<!--<![endif]-->')


def _chart_gauge(score: float, label: str = "Overall Score", size: int = 200) -> str:
    """Semicircular gauge chart for overall score."""
    fig, ax = plt.subplots(figsize=(4.0, 2.6))
    ax.set_xlim(-1.4, 1.4)
    ax.set_ylim(-0.4, 1.4)
    ax.set_aspect("equal")
    ax.axis("off")

    # Background arc (grey)
    theta = np.linspace(np.pi, 0, 100)
    x_bg = np.cos(theta)
    y_bg = np.sin(theta)
    ax.plot(x_bg, y_bg, color="#e2e8f0", linewidth=24, solid_capstyle="round")

    # Score arc (coloured)
    pct = min(score / 100.0, 1.0)
    theta_score = np.linspace(np.pi, np.pi - pct * np.pi, 100)
    x_sc = np.cos(theta_score)
    y_sc = np.sin(theta_score)
    color = _score_color(score)
    ax.plot(x_sc, y_sc, color=color, linewidth=24, solid_capstyle="round")

    # Score text in centre
    ax.text(0, 0.38, f"{score:.0f}", ha="center", va="center",
            fontsize=44, fontweight="bold", color=color, fontfamily="sans-serif")
    ax.text(0, 0.02, "/100", ha="center", va="center",
            fontsize=14, color="#94a3b8", fontfamily="sans-serif")
    ax.text(0, -0.25, label, ha="center", va="center",
            fontsize=11, color="#64748b", fontfamily="sans-serif")

    fname = _save_chart(fig, "gauge")
    return _img_tag(fname, f"{label}: {score:.0f}", 340)


def _chart_dimension_bars(scores: Dict[str, float], title: str = "Health Dimensions") -> str:
    """Horizontal bar chart for 7 dimension scores."""
    dims = list(DIMENSIONS)
    vals = [scores.get(d, 50.0) for d in dims]
    colors = [_score_color(v) for v in vals]
    labels = [DIM_SHORT.get(d, d) for d in dims]

    fig, ax = plt.subplots(figsize=(7.5, 3.5))
    y_pos = np.arange(len(dims))
    bars = ax.barh(y_pos, vals, color=colors, height=0.55, edgecolor="none")
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=11, fontfamily="sans-serif")
    ax.set_xlim(0, 100)
    ax.invert_yaxis()
    ax.set_xlabel("")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#e2e8f0")
    ax.spines["left"].set_color("#e2e8f0")
    ax.tick_params(axis="x", colors="#94a3b8", labelsize=9)
    ax.tick_params(axis="y", length=0)
    ax.set_axisbelow(True)
    ax.xaxis.grid(True, color="#f1f5f9", linewidth=0.5)

    for bar, val in zip(bars, vals, strict=False):
        x_pos = bar.get_width() + 1.5
        if x_pos > 95:
            x_pos = bar.get_width() - 4
        ax.text(x_pos, bar.get_y() + bar.get_height() / 2,
                f"{val:.0f}", va="center", ha="left", fontsize=10,
                fontweight="bold", color=_score_color(val), fontfamily="sans-serif")

    if title:
        ax.set_title(title, fontsize=12, fontweight="bold", color="#0f172a",
                      loc="left", pad=10, fontfamily="sans-serif")

    fig.tight_layout()
    fname = _save_chart(fig, "dim_bars")
    return _img_tag(fname, title, 660)


def _chart_team_heatmap(teams: List[dict]) -> str:
    """Grid heatmap of all teams colour-coded by score."""
    sorted_teams = sorted(teams, key=lambda t: t["overall"], reverse=True)
    n = len(sorted_teams)
    cols = 6
    rows = (n + cols - 1) // cols

    fig, ax = plt.subplots(figsize=(7.0, rows * 0.95 + 0.8))
    ax.set_xlim(-0.5, cols - 0.5)
    ax.set_ylim(rows - 0.5, -0.5)
    ax.axis("off")
    ax.set_aspect("equal")

    for i, team in enumerate(sorted_teams):
        row = i // cols
        col = i % cols
        score = team["overall"]
        color = _score_color_rgb(score)

        rect = FancyBboxPatch((col - 0.42, row - 0.38), 0.84, 0.76,
                              boxstyle="round,pad=0.05",
                              facecolor=color, edgecolor="white", linewidth=1.5,
                              alpha=0.9)
        ax.add_patch(rect)

        ax.text(col, row - 0.08, f"{score:.0f}", ha="center", va="center",
                fontsize=14, fontweight="bold", color="white", fontfamily="sans-serif")

        name = team["team_name"]
        if len(name) > 16:
            name = name[:15] + ".."
        ax.text(col, row + 0.2, name, ha="center", va="center",
                fontsize=5.5, color="white", fontfamily="sans-serif", alpha=0.9)

    # Legend
    legend_patches = [
        mpatches.Patch(color=_score_color_rgb(80), label="75+ (Good)"),
        mpatches.Patch(color=_score_color_rgb(60), label="50-74 (Needs Work)"),
        mpatches.Patch(color=_score_color_rgb(30), label="<50 (Critical)"),
    ]
    ax.legend(handles=legend_patches, loc="upper right", fontsize=8,
              frameon=False, ncol=3, bbox_to_anchor=(1.0, -0.02))

    ax.set_title("Team Health at a Glance", fontsize=12, fontweight="bold",
                 color="#0f172a", loc="left", pad=8, fontfamily="sans-serif")
    fig.tight_layout()
    fname = _save_chart(fig, "team_heatmap")
    return _img_tag(fname, "Team Health Heatmap", 660)


def _chart_stacked_bar(segments: List[tuple], title: str = "", width: int = 580) -> str:
    """Horizontal stacked bar with labelled segments. segments = [(label, value, color), ...]"""
    total = sum(s[1] for s in segments)
    if total == 0:
        return ""

    fig, ax = plt.subplots(figsize=(6, 0.9))
    left = 0
    for label, val, color in segments:
        pct = val / total * 100
        ax.barh(0, pct, left=left, color=color, height=0.5, edgecolor="white", linewidth=0.5)
        if pct > 8:
            ax.text(left + pct / 2, 0, f"{label}\n{val:,}", ha="center", va="center",
                    fontsize=7.5, color="white", fontweight="bold", fontfamily="sans-serif")
        left += pct

    ax.set_xlim(0, 100)
    ax.set_ylim(-0.5, 0.5)
    ax.axis("off")
    if title:
        ax.set_title(title, fontsize=11, fontweight="bold", color="#0f172a",
                      loc="left", pad=6, fontfamily="sans-serif")
    fig.tight_layout()
    fname = _save_chart(fig, "stacked")
    return _img_tag(fname, title, width)


def _chart_subdomain_comparison(subdomains: List[dict]) -> str:
    """Grouped bar chart comparing sub-domain scores across dimensions."""
    n_dims = len(DIMENSIONS)
    n_sd = len(subdomains)
    x = np.arange(n_dims)
    bar_width = 0.25

    fig, ax = plt.subplots(figsize=(7.5, 3.5))

    for i, sd in enumerate(subdomains):
        name = sd["subdomain_name"]
        vals = [sd["scores"].get(d, 50.0) for d in DIMENSIONS]
        primary_color = SD_COLORS.get(name, ("#6366f1", "#a5b4fc"))[0]
        offset = (i - n_sd / 2 + 0.5) * bar_width
        ax.bar(x + offset, vals, bar_width, label=name, color=primary_color,
               edgecolor="none", alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels([DIM_SHORT.get(d, d) for d in DIMENSIONS], fontsize=9,
                        fontfamily="sans-serif", rotation=15, ha="right")
    ax.set_ylim(0, 100)
    ax.set_ylabel("Score", fontsize=9, color="#64748b")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#e2e8f0")
    ax.spines["left"].set_color("#e2e8f0")
    ax.tick_params(axis="y", colors="#94a3b8", labelsize=9)
    ax.yaxis.grid(True, color="#f1f5f9", linewidth=0.5)
    ax.set_axisbelow(True)
    ax.legend(fontsize=8, loc="upper right", frameon=False)
    ax.set_title("Sub-domain Comparison", fontsize=12, fontweight="bold",
                 color="#0f172a", loc="left", pad=10, fontfamily="sans-serif")
    fig.tight_layout()
    fname = _save_chart(fig, "sd_compare")
    return _img_tag(fname, "Sub-domain Comparison", 660)


def _chart_team_bars(teams: List[dict], sd_name: str, color: str) -> str:
    """Horizontal bar chart of team scores within a sub-domain."""
    sorted_teams = sorted(teams, key=lambda t: t["overall"], reverse=True)
    names = [t["team_name"][:25] for t in sorted_teams]
    vals = [t["overall"] for t in sorted_teams]
    colors = [_score_color(v) for v in vals]

    fig_height = max(1.5, len(names) * 0.4 + 0.5)
    fig, ax = plt.subplots(figsize=(6.5, fig_height))
    y_pos = np.arange(len(names))
    bars = ax.barh(y_pos, vals, color=colors, height=0.55, edgecolor="none")
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=9, fontfamily="sans-serif")
    ax.set_xlim(0, 100)
    ax.invert_yaxis()
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#e2e8f0")
    ax.spines["left"].set_color("#e2e8f0")
    ax.tick_params(axis="x", colors="#94a3b8", labelsize=8)
    ax.tick_params(axis="y", length=0)
    ax.xaxis.grid(True, color="#f1f5f9", linewidth=0.5)
    ax.set_axisbelow(True)

    for bar, val in zip(bars, vals, strict=False):
        ax.text(bar.get_width() + 1.5, bar.get_y() + bar.get_height() / 2,
                f"{val:.0f}", va="center", ha="left", fontsize=9,
                fontweight="bold", color=_score_color(val), fontfamily="sans-serif")

    fig.tight_layout()
    fname = _save_chart(fig, "team_bars")
    return _img_tag(fname, f"{sd_name} Teams", 620)


def _chart_github_activity(commits: int, prs_merged: int, prs_open: int) -> str:
    """Simple bar chart for GitHub activity."""
    labels = ["Commits", "PRs Merged", "PRs Open"]
    values = [commits, prs_merged, prs_open]
    colors = ["#6366f1", "#16a34a", "#f59e0b"]

    fig, ax = plt.subplots(figsize=(6, 2.8))
    bars = ax.bar(labels, values, color=colors, width=0.5, edgecolor="none", alpha=0.9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#e2e8f0")
    ax.spines["left"].set_color("#e2e8f0")
    ax.tick_params(axis="both", colors="#94a3b8", labelsize=9)
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, color="#f1f5f9", linewidth=0.5)

    for bar, val in zip(bars, values, strict=False):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                f"{val:,}", ha="center", va="bottom", fontsize=10,
                fontweight="bold", color="#334155", fontfamily="sans-serif")

    ax.set_title("GitHub Activity", fontsize=11, fontweight="bold",
                 color="#0f172a", loc="left", pad=8, fontfamily="sans-serif")
    fig.tight_layout()
    fname = _save_chart(fig, "github_activity")
    return _img_tag(fname, "GitHub Activity", 520)


def _chart_incident_priority(priority_counts: Dict[str, int]) -> str:
    """Donut chart for incident priority breakdown."""
    labels = []
    sizes = []
    colors_list = ["#dc2626", "#f59e0b", "#3b82f6", "#94a3b8"]
    priority_order = ["P1", "P2", "P3", "P4"]

    for p in priority_order:
        if priority_counts.get(p, 0) > 0:
            labels.append(f"{p} ({priority_counts[p]})")
            sizes.append(priority_counts[p])

    if not sizes:
        return ""

    fig, ax = plt.subplots(figsize=(3.5, 3.5))
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=colors_list[:len(sizes)],
        autopct="%1.0f%%", startangle=90, pctdistance=0.75,
        wedgeprops=dict(width=0.35, edgecolor="white", linewidth=2)
    )
    for t in texts:
        t.set_fontsize(8)
        t.set_fontfamily("sans-serif")
    for t in autotexts:
        t.set_fontsize(7)
        t.set_fontweight("bold")
        t.set_color("white")
        t.set_fontfamily("sans-serif")

    total = sum(sizes)
    ax.text(0, 0, f"{total}", ha="center", va="center", fontsize=20,
            fontweight="bold", color="#0f172a", fontfamily="sans-serif")
    ax.text(0, -0.18, "incidents", ha="center", va="center", fontsize=8,
            color="#64748b", fontfamily="sans-serif")

    ax.set_title("Incidents by Priority", fontsize=11, fontweight="bold",
                 color="#0f172a", pad=10, fontfamily="sans-serif")
    fig.tight_layout()
    fname = _save_chart(fig, "incidents")
    return _img_tag(fname, "Incidents by Priority", 340)


# ---------------------------------------------------------------------------
# Data analysis helpers
# ---------------------------------------------------------------------------

def _analyse_backlog(csv_dir: Path) -> dict:
    rows = _load_csv(csv_dir / "backlog_health.csv")
    if not rows:
        return {}

    total = len(rows)
    status_counts = Counter()
    project_counts = Counter()
    aging_buckets = Counter()
    blocked_project_keys = set()

    for r in rows:
        st = r.get("status", "Unknown")
        status_counts[st] += 1
        pk = r.get("project_key", "")
        if pk:
            project_counts[pk] += 1
        if st == "Blocked":
            blocked_project_keys.add(pk)

        days = _safe_float(r.get("days_in_status", 0))
        if days > 90:
            aging_buckets[">90 days"] += 1
        elif days > 30:
            aging_buckets["30-90 days"] += 1
        elif days > 14:
            aging_buckets["14-30 days"] += 1
        else:
            aging_buckets["<14 days"] += 1

    return {
        "total": total,
        "status": dict(status_counts),
        "projects": dict(project_counts),
        "aging": dict(aging_buckets),
        "blocked_project_keys": list(blocked_project_keys),
    }


def _analyse_incidents(csv_dir: Path) -> dict:
    rows = _load_csv(csv_dir / "itsm_incidents.csv")
    if not rows:
        return {}

    priority = Counter()
    state = Counter()
    for r in rows:
        p = r.get("priority", "Unknown")
        if "1" in p:
            priority["P1"] += 1
        elif "2" in p:
            priority["P2"] += 1
        elif "3" in p:
            priority["P3"] += 1
        else:
            priority["P4"] += 1
        state[r.get("state", "Unknown")] += 1

    return {
        "total": len(rows),
        "priority": dict(priority),
        "state": dict(state),
    }


def _analyse_changes(csv_dir: Path) -> dict:
    rows = _load_csv(csv_dir / "itsm_changes.csv")
    if not rows:
        return {}
    state = Counter()
    for r in rows:
        state[r.get("state", "Unknown")] += 1
    return {"total": len(rows), "state": dict(state)}


def _analyse_github(csv_dir: Path) -> dict:
    rows = _load_csv(csv_dir / "github_metrics.csv")
    if not rows:
        return {}
    commits = sum(int(_safe_float(r.get("commits_30d", 0))) for r in rows)
    prs_merged = sum(int(_safe_float(r.get("prs_merged_30d", 0))) for r in rows)
    prs_open = sum(int(_safe_float(r.get("open_prs", 0))) for r in rows)
    contributors = sum(int(_safe_float(r.get("contributors_30d", 0))) for r in rows)
    repos = len(rows)
    return {
        "commits": commits, "prs_merged": prs_merged, "prs_open": prs_open,
        "contributors": contributors, "repos": repos,
    }


# ---------------------------------------------------------------------------
# HTML assembly - full-width executive report (matching sprint-report-dashboard style)
# ---------------------------------------------------------------------------

# Palette (aligned with sprint-report-dashboard)
_NAVY = '#101820'
_CREAM = '#F4F0EC'
_ACCENT = '#C0C0E3'        # lavender
_BAND = '#C0C0E3'
_BG = '#ffffff'
_FOOTBG = '#F6F6F6'
_BORDER = '#D8D8EE'
_SEC = 'rgba(16,24,32,0.55)'
_GOOD = '#10b981'
_WARN = '#C47A1E'
_BAD = '#ef4444'
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def _sc(v: float) -> str:
    """Score colour: green/amber/red."""
    if v >= 75:
        return _GOOD
    if v >= 50:
        return _WARN
    return _BAD


def _cell_bg(v: float) -> str:
    """Subtle pastel background for heatmap cells."""
    if v >= 75:
        return "#e3fcef"
    if v >= 60:
        return "#fff8e1"
    if v >= 50:
        return "#fff0e0"
    return "#fce4ec"


def _sec_title(title: str, desc: str = "") -> str:
    """Section title with accent underline (matching sprint-report style)."""
    desc_html = (f'<p style="font-size:8pt;color:{_SEC};margin:0 0 14px;'
                 f'font-style:italic;">{desc}</p>' if desc else '')
    return (f'<h2 style="font-size:11pt;font-weight:700;color:{_NAVY};'
            f'margin:0 0 {"4px" if desc else "16px"};padding-bottom:8px;'
            f'border-bottom:2px solid {_ACCENT};">{title}</h2>{desc_html}')


def _th(txt: str) -> str:
    """Table header cell."""
    return (f'<th style="text-align:left;padding:6px 8px;background:{_CREAM};'
            f'font-size:7pt;text-transform:uppercase;letter-spacing:0.5px;'
            f'color:{_SEC};font-weight:600;">{txt}</th>')


def _td(txt, extra: str = "") -> str:
    """Table data cell."""
    return f'<td style="padding:8px;{extra}">{txt}</td>'


def _svg_donut(value: float, size: int = 64, stroke: int = 7, label: str = "") -> str:
    """Inline SVG donut chart. Works in Gmail, Apple Mail, iOS. Outlook gets fallback text."""
    r = (size - stroke) / 2
    circ = 2 * 3.14159 * r
    filled = circ * (value / 100.0)
    gap = circ - filled
    c = _sc(value)
    # Track colour (light grey)
    track = '#e8e8ef'
    return (
        f'<!--[if !mso]><!-->'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" style="display:inline-block;vertical-align:middle;">'
        f'<circle cx="{size//2}" cy="{size//2}" r="{r}" fill="none" '
        f'stroke="{track}" stroke-width="{stroke}"/>'
        f'<circle cx="{size//2}" cy="{size//2}" r="{r}" fill="none" '
        f'stroke="{c}" stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-dasharray="{filled:.1f} {gap:.1f}" '
        f'transform="rotate(-90 {size//2} {size//2})"/>'
        f'<text x="{size//2}" y="{size//2 + 5}" text-anchor="middle" '
        f'font-size="{size//4}px" font-weight="700" fill="{c}" '
        f'font-family="{_FONT}">{value:.0f}</text>'
        f'</svg>'
        f'<!--<![endif]-->'
        f'<!--[if mso]><span style="font-size:{size//3}pt;font-weight:700;'
        f'color:{c};">{value:.0f}</span><![endif]-->'
    )


def _svg_mini_bar(value: float, width: int = 100, height: int = 10) -> str:
    """Inline SVG gradient progress bar. Richer than CSS-only bars."""
    c = _sc(value)
    fill_w = max(1, int(width * value / 100))
    bar_id = f'gb{int(value * 10)}'
    return (
        f'<!--[if !mso]><!-->'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="display:inline-block;vertical-align:middle;">'
        f'<defs><linearGradient id="{bar_id}" x1="0%" y1="0%" x2="100%" y2="0%">'
        f'<stop offset="0%" stop-color="{c}" stop-opacity="0.6"/>'
        f'<stop offset="100%" stop-color="{c}"/>'
        f'</linearGradient></defs>'
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="{height//2}" '
        f'fill="#e8e8ef"/>'
        f'<rect x="0" y="0" width="{fill_w}" height="{height}" rx="{height//2}" '
        f'fill="url(#{bar_id})"/>'
        f'</svg>'
        f'<!--<![endif]-->'
        f'<!--[if mso]>'
        f'<span style="display:inline-block;width:{fill_w}px;height:{height}px;'
        f'background:{c};border-radius:{height//2}px;">&nbsp;</span>'
        f'<![endif]-->'
    )


def _svg_sparkline(values: list, width: int = 80, height: int = 24) -> str:
    """Inline SVG sparkline from a list of values. Shows trend."""
    if not values or len(values) < 2:
        return ''
    mn = min(values)
    mx = max(values)
    rng = mx - mn if mx != mn else 1
    step = width / (len(values) - 1)
    points = []
    for i, v in enumerate(values):
        x = i * step
        y = height - ((v - mn) / rng) * (height - 4) - 2
        points.append(f'{x:.1f},{y:.1f}')
    polyline = ' '.join(points)
    c = _sc(values[-1])
    return (
        f'<!--[if !mso]><!-->'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="display:inline-block;vertical-align:middle;">'
        f'<polyline points="{polyline}" fill="none" stroke="{c}" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{points[-1].split(",")[0]}" cy="{points[-1].split(",")[1]}" '
        f'r="2" fill="{c}"/>'
        f'</svg>'
        f'<!--<![endif]-->'
    )


def _build_html(scores: dict, csv_dir: Path, dashboard_url: str, week: str) -> str:
    d = scores["domain"]
    sds = scores["subdomains"]
    teams = scores["teams"]
    ds = d["scores"]
    ov = d["overall"]

    backlog = _analyse_backlog(csv_dir)
    incidents = _analyse_incidents(csv_dir)
    _analyse_changes(csv_dir)  # loaded for future use
    github = _analyse_github(csv_dir)

    ov_c = _sc(ov)

    # Dimension keys and rich descriptions for narrative
    dim_keys = [
        ("delivery", "Delivery"), ("system_health", "System Health"),
        ("incidents", "Incidents"), ("code_quality", "Code Quality"),
        ("estate_hygiene", "Estate Hygiene"), ("ops_readiness", "Ops Readiness"),
        ("people", "People"),
    ]

    _DIM_CONTEXT = {
        "delivery": {
            "good": "Clean backlogs with low blocked ratios and tickets moving through the pipeline.",
            "ok": "Some backlog hygiene concerns. Blocked or stale tickets need review.",
            "bad": "High blocked ratio or many stale tickets. Immediate backlog grooming needed.",
            "wgl": "All tickets estimated, prioritised, and moving within their sprint cycle.",
        },
        "system_health": {
            "good": "Services meet PagerDuty standards and Dynatrace shows healthy performance.",
            "ok": "Some services missing escalation policies or showing degraded health.",
            "bad": "Critical gaps in monitoring coverage or multiple degraded services.",
            "wgl": "100% PagerDuty standards compliance. All services green in Dynatrace.",
        },
        "incidents": {
            "good": "Low incident volume across the estate. Systems are stable.",
            "ok": "Moderate incident activity. Keep an eye on repeat offenders.",
            "bad": "High incident volume, particularly P1/P2s. Reliability needs investment.",
            "wgl": "Zero P1s. P2s rare and resolved within SLA. Trend line heading down.",
        },
        "code_quality": {
            "good": "Repos are clean with few stale PRs/branches and healthy CI pipelines.",
            "ok": "Some repos have stale PRs or branches that need housekeeping.",
            "bad": "Many stale PRs, failing pipelines, or poor branch hygiene across repos.",
            "wgl": "No PRs older than 5 days. CI green across all repos. Branch count under control.",
        },
        "estate_hygiene": {
            "good": "Strong service catalogue coverage. Most repos have catalog-info.yaml.",
            "ok": "Partial service catalogue adoption. Some repos still missing catalog-info.",
            "bad": "Poor service catalogue coverage. Teams can't find or understand services.",
            "wgl": "100% catalog-info.yaml coverage. Every service discoverable and documented.",
        },
        "ops_readiness": {
            "good": "On-call schedules, escalation policies, and notification rules well configured.",
            "ok": "Some teams missing on-call coverage or incomplete escalation paths.",
            "bad": "Critical gaps in on-call setup. Incidents risk going unnoticed.",
            "wgl": "Every service has on-call, escalation policy, and tested runbooks.",
        },
        "people": {
            "good": "Teams are well-staffed with good coverage across roles.",
            "ok": "Some vacancies or small teams that present a bus-factor risk.",
            "bad": "Multiple vacancies or single-person teams creating key-person dependencies.",
            "wgl": "No single-person teams. Vacancy ratio below 10%. Knowledge spread across the team.",
        },
    }

    # Build rich strengths and concerns
    strengths = []
    concerns = []
    for key, label in dim_keys:
        v = ds.get(key, 50.0)
        ctx = _DIM_CONTEXT.get(key, {})
        if v >= 75:
            strengths.append((label, v, ctx.get("good", "")))
        elif v < 50:
            concerns.append((label, v, ctx.get("bad", "")))
        elif v < 60:
            concerns.append((label, v, ctx.get("ok", "")))

    if backlog:
        aging = backlog.get("aging", {})
        stale = aging.get(">90 days", 0)
        if stale > 0:
            concerns.append(("Backlog Aging", 0, f'{stale:,} tickets stale for 90+ days. These need closing, re-prioritising, or actively working.'))
        blocked_count = backlog.get("status", {}).get("Blocked", 0)
        if blocked_count > 0:
            concerns.append(("Blocked Tickets", 0, f'{blocked_count:,} blocked tickets across the estate are slowing delivery.'))

    if incidents:
        p1 = incidents.get("priority", {}).get("P1", 0)
        if p1 > 0:
            concerns.append(("P1 Incidents", 0, f'{p1} P1 incident{"s" if p1 > 1 else ""} in the 60-day window. These demand root-cause analysis.'))

    # ── Executive summary narrative ────────────────────────────────
    top_sd = max(sds, key=lambda s: s["overall"])
    bottom_sd = min(sds, key=lambda s: s["overall"])
    weakest_dim = min(dim_keys, key=lambda x: ds.get(x[0], 50.0))
    strongest_dim = max(dim_keys, key=lambda x: ds.get(x[0], 50.0))

    exec_parts = []
    if ov >= 75:
        exec_parts.append(f'The Foods engineering estate is in good shape this week, scoring <strong style="color:{ov_c}">{ov:.0f}/100</strong> overall.')
    elif ov >= 50:
        exec_parts.append(f'The Foods engineering estate scores <strong style="color:{ov_c}">{ov:.0f}/100</strong> overall this week. There\'s room for improvement in a few areas.')
    else:
        exec_parts.append(f'The Foods engineering estate scores <strong style="color:{ov_c}">{ov:.0f}/100</strong> overall this week. Several areas need attention.')

    if top_sd["overall"] != bottom_sd["overall"]:
        exec_parts.append(
            f'{top_sd["subdomain_name"]} leads at <strong>{top_sd["overall"]:.0f}</strong>, '
            f'while {bottom_sd["subdomain_name"]} trails at <strong>{bottom_sd["overall"]:.0f}</strong>.'
        )

    sv = ds.get(strongest_dim[0], 50.0)
    wv = ds.get(weakest_dim[0], 50.0)
    exec_parts.append(
        f'Our strongest dimension is {strongest_dim[1]} ({sv:.0f}), '
        f'and {weakest_dim[1]} ({wv:.0f}) is where we should focus improvement.'
    )

    if incidents and incidents.get("total", 0) > 0:
        exec_parts.append(f'{incidents["total"]:,} incidents logged in the 60-day window.')
    if github and github.get("prs_merged", 0) > 0:
        exec_parts.append(f'{github["prs_merged"]:,} PRs merged in the last 30 days across the estate.')

    exec_summary = ' '.join(exec_parts)

    # ── BUILD HTML ─────────────────────────────────────────────
    h = []

    h.append(f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Foods Engineering Vitals - Foods D&amp;T</title>
<style>
  @page {{ size: A4; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: {_FONT};
         background: {_BG}; color: {_NAVY}; font-size: 10pt; line-height: 1.5; }}
  @media print {{
    body {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
    .no-break {{ page-break-inside: avoid; }}
  }}
</style>
</head>
<body>''')

    # ═══ HEADER ═══════════════════════════════════════════════════
    h.append(f'''
<div style="background:{_NAVY};color:{_CREAM};padding:22px 32px;">
  <table cellpadding="0" cellspacing="0" style="width:100%;">
    <tr>
      <td style="vertical-align:bottom;">
        <p style="font-size:14pt;font-weight:700;margin:0 0 4px;color:{_CREAM};">Foods Engineering Vitals</p>
        <p style="font-size:8.5pt;opacity:0.65;margin:0;">Foods D&amp;T &middot; {week}</p>
        {f'<p style="font-size:8.5pt;margin:6px 0 0;"><a href="{dashboard_url}" style="color:{_CREAM};text-decoration:none;border-bottom:1px solid {_CREAM};">&#128202; View interactive dashboard</a></p>' if dashboard_url else ''}
      </td>
      <td style="text-align:right;vertical-align:middle;" width="120">
        {_svg_donut(ov, size=80, stroke=8)}
        <div style="font-size:7pt;color:{_CREAM};opacity:0.6;text-align:center;margin-top:4px;">/100 overall</div>
      </td>
    </tr>
  </table>
</div>''')

    # ═══ EXECUTIVE SUMMARY ════════════════════════════════════════
    h.append(f'''
<div style="background:{_BG};padding:24px 32px;border-bottom:1px solid {_BORDER};" class="no-break">
  <p style="font-size:8.5pt;color:rgba(16,24,32,0.65);line-height:1.7;padding:14px 18px;
     background:rgba(192,192,227,0.15);border-left:3px solid {_ACCENT};margin:0;">
    {exec_summary}
  </p>
</div>''')

    # ═══ HERO CARDS ═══════════════════════════════════════════════
    hero_items = []
    for sd in sds:
        c = _sc(sd["overall"])
        t_count = len([t for t in teams if t.get("subdomain") == sd["subdomain_name"]])
        hero_items.append({
            "label": sd["subdomain_name"],
            "value": f'{sd["overall"]:.0f}',
            "sub": f'{t_count} teams',
            "color": c,
            "donut": _svg_donut(sd["overall"], size=52, stroke=6),
        })
    if backlog:
        hero_items.append({
            "label": "Backlog",
            "value": f'{backlog["total"]:,}',
            "sub": "active tickets",
            "color": _NAVY,
            "donut": "",
        })
    if incidents:
        hero_items.append({
            "label": "Incidents",
            "value": f'{incidents["total"]:,}',
            "sub": "60-day window",
            "color": _BAD if incidents.get("priority", {}).get("P1", 0) > 0 else _NAVY,
            "donut": "",
        })
    if github:
        hero_items.append({
            "label": "PRs Merged",
            "value": f'{github.get("prs_merged", 0):,}',
            "sub": "last 30 days",
            "color": _GOOD,
            "donut": "",
        })

    if hero_items:
        cols = len(hero_items)
        w_pct = f'{100 // cols}%'
        h.append('<div style="padding:24px 32px;" class="no-break">')
        h.append('<table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;"><tr>')
        for item in hero_items:
            value_display = item.get("donut") or f'<div style="font-size:22pt;font-weight:700;line-height:1;color:{item["color"]};">{item["value"]}</div>'
            h.append(
                f'<td style="width:{w_pct};padding:0 5px;">'
                f'<div style="padding:14px 8px;text-align:center;border-top:3px solid {_ACCENT};">'
                f'{value_display}'
                f'<div style="font-size:6.5pt;color:{_SEC};text-transform:uppercase;letter-spacing:0.8px;'
                f'font-weight:600;margin-top:6px;">{item["sub"]}</div>'
                f'<div style="font-size:7pt;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;'
                f'color:{_NAVY};margin-top:6px;padding-top:6px;">{item["label"]}</div>'
                f'</div></td>'
            )
        h.append('</tr></table></div>')

    # ═══ HEALTH SUMMARY ══════════════════════════════════════════
    if strengths or concerns:
        h.append(f'''
<div style="background:{_BG};padding:24px 32px;border-bottom:1px solid {_BORDER};" class="no-break">
  {_sec_title('Health Summary')}
  <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">
    <tr>''')
        if strengths:
            items_html = ''
            for label, v, desc in strengths[:5]:
                items_html += (
                    f'<li style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid rgba(16,24,32,0.07);">'
                    f'<strong style="color:{_NAVY};">{label}</strong> '
                    f'<span style="color:{_GOOD};font-weight:700;font-size:8pt;margin-left:4px;">{v:.0f}/100</span>'
                    f'<br><span style="color:rgba(16,24,32,0.55);font-size:8pt;">{desc}</span></li>'
                )
            h.append(
                f'<td style="width:50%;vertical-align:top;padding-right:20px;">'
                f'<p style="font-size:7pt;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;'
                f'color:{_GOOD};margin-bottom:10px;">&#10003; Performing Well</p>'
                f'<ul style="list-style:none;font-size:8.5pt;line-height:1.5;">{items_html}</ul></td>'
            )
        if concerns:
            items_html = ''
            for label, v, desc in concerns[:5]:
                score_html = f' <span style="color:{_sc(v) if v > 0 else _BAD};font-weight:700;font-size:8pt;margin-left:4px;">{v:.0f}/100</span>' if v > 0 else ''
                items_html += (
                    f'<li style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid rgba(16,24,32,0.07);">'
                    f'<strong style="color:{_NAVY};">{label}</strong>{score_html}'
                    f'<br><span style="color:rgba(16,24,32,0.55);font-size:8pt;">{desc}</span></li>'
                )
            h.append(
                f'<td style="width:50%;vertical-align:top;{"padding-left:20px;" if strengths else ""}">'
                f'<p style="font-size:7pt;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;'
                f'color:{_WARN};margin-bottom:10px;">&#9888; Needs Attention</p>'
                f'<ul style="list-style:none;font-size:8.5pt;line-height:1.5;">{items_html}</ul></td>'
            )
        h.append('</tr></table></div>')

    # ── Health insight band ──
    h.append(
        f'<div style="background:{_BAND};padding:14px 32px;border-bottom:1px solid {_BORDER};">'
        f'<p style="font-size:8.5pt;color:{_NAVY};margin:0;">'
        f'<strong>What good looks like:</strong> All 7 dimensions above 75. '
        f'No team below 50 on any dimension. Consistent scores across sub-domains.</p></div>'
    )

    # ═══ HEALTH DIMENSIONS ═══════════════════════════════════════
    h.append(
        f'<div style="background:{_BG};padding:24px 32px;border-bottom:1px solid {_BORDER};" class="no-break">'
        f'{_sec_title("Health by Dimension", "7-dimension scoring across the entire Foods estate.")}'
        f'<table style="width:100%;border-collapse:collapse;font-size:8.5pt;">'
        f'<thead><tr>{_th("Dimension")}{_th("")}{_th("Score")}{_th("")}{_th("Summary")}</tr></thead><tbody>'
    )
    for key, label in dim_keys:
        v = ds.get(key, 50.0)
        c = _sc(v)
        ctx = _DIM_CONTEXT.get(key, {})
        summary = ctx.get("good" if v >= 75 else "ok" if v >= 50 else "bad", "")
        donut = _svg_donut(v, size=40, stroke=5)
        bar = _svg_mini_bar(v, width=120, height=8)
        h.append(
            f'<tr><td style="padding:10px 8px;font-weight:600;">{label}</td>'
            f'<td style="padding:10px 4px;width:44px;">{donut}</td>'
            f'<td style="padding:10px 8px;font-weight:700;color:{c};font-size:14pt;">{v:.0f}</td>'
            f'<td style="padding:10px 8px;width:25%;">{bar}</td>'
            f'<td style="padding:10px 8px;color:{_SEC};font-size:8pt;">{summary}</td></tr>'
        )
    h.append('</tbody></table></div>')

    # ── Dimension insight band ──
    wv = ds.get(weakest_dim[0], 50.0)
    wctx = _DIM_CONTEXT.get(weakest_dim[0], {})
    h.append(
        f'<div style="background:{_BAND};padding:14px 32px;border-bottom:1px solid {_BORDER};">'
        f'<p style="font-size:8.5pt;color:{_NAVY};margin:0;">'
        f'<strong>Focus area:</strong> {weakest_dim[1]} is the lowest-scoring dimension at '
        f'<span style="color:{_sc(wv)};font-weight:700;">{wv:.0f}/100</span>. '
        f'{wctx.get("wgl", "Improvement here has the biggest impact on overall health.")}</p></div>'
    )

    # ═══ SUB-DOMAIN COMPARISON ═══════════════════════════════════
    h.append(
        f'<div style="background:{_BG};padding:24px 32px;border-bottom:1px solid {_BORDER};" class="no-break">'
        f'{_sec_title("Sub-domain Comparison", "How the three Foods sub-domains compare across all dimensions.")}'
        f'<table style="width:100%;border-collapse:collapse;font-size:8.5pt;">'
        f'<thead><tr>{_th("Sub-domain")}{_th("Overall")}'
    )
    for _, label in dim_keys:
        short = label.split()[0][:4]
        h.append(f'{_th(short)}')
    h.append(f'{_th("")}</tr></thead><tbody>')
    for sd in sds:
        c = _sc(sd["overall"])
        t_count = len([t for t in teams if t.get("subdomain") == sd["subdomain_name"]])
        bar = _svg_mini_bar(sd["overall"], width=80, height=8)
        h.append(
            f'<tr><td style="padding:10px 8px;"><strong>{sd["subdomain_name"]}</strong>'
            f'<br><span style="font-size:7pt;color:{_SEC};">{t_count} teams</span></td>'
            f'<td style="padding:10px 8px;font-weight:700;color:{c};font-size:14pt;">{sd["overall"]:.0f}</td>'
        )
        for key, _ in dim_keys:
            v = sd["scores"].get(key, 50.0)
            vc = _sc(v)
            bg = _cell_bg(v)
            h.append(f'<td style="padding:6px;text-align:center;font-weight:700;color:{vc};background:{bg};font-size:9pt;">{v:.0f}</td>')
        h.append(f'<td style="padding:8px;width:15%;">{bar}</td>')
        h.append('</tr>')
    h.append('</tbody></table></div>')

    # ── Sub-domain insight band ──
    gap = top_sd["overall"] - bottom_sd["overall"]
    if gap > 10:
        h.append(
            f'<div style="background:{_BAND};padding:14px 32px;border-bottom:1px solid {_BORDER};">'
            f'<p style="font-size:8.5pt;color:{_NAVY};margin:0;">'
            f'<strong>What good looks like:</strong> Sub-domains within 10 points of each other. '
            f'Currently there\'s a {gap:.0f}-point gap between {top_sd["subdomain_name"]} and {bottom_sd["subdomain_name"]}.</p></div>'
        )

    # ═══ TEAM HEALTH AT A GLANCE ═════════════════════════════════
    dims = ["delivery", "system_health", "incidents", "code_quality",
            "estate_hygiene", "ops_readiness", "people"]
    hdrs = ["Del", "Sys", "Inc", "Code", "Est", "Ops", "Ppl"]
    h.append(
        f'<div style="background:{_BG};padding:24px 32px;border-bottom:1px solid {_BORDER};" class="no-break">'
        f'{_sec_title("Team Health at a Glance", f"{len(teams)} teams across 7 dimensions. Green (75+), amber (50-74), red (&lt;50).")}'
        f'<table style="width:100%;border-collapse:collapse;font-size:8pt;">'
        f'<thead><tr>{_th("Team")}{_th("SD")}'
    )
    for hdr in hdrs:
        h.append(_th(hdr))
    h.append(f'{_th("Avg")}</tr></thead><tbody>')

    sorted_teams = sorted(teams, key=lambda t: t["overall"], reverse=True)
    for i, t in enumerate(sorted_teams):
        stripe = _BG if i % 2 == 0 else _FOOTBG
        sd_name = t.get("subdomain", "")
        sd_short = sd_name[:3] if sd_name else ""
        h.append(f'<tr style="background:{stripe};">')
        h.append(f'<td style="padding:6px 8px;font-weight:500;white-space:nowrap;">{t["team_name"]}</td>')
        h.append(f'<td style="padding:4px 6px;font-size:7pt;color:{_SEC};text-align:center;">{sd_short}</td>')
        for dm in dims:
            v = t["scores"].get(dm, 50.0)
            bg = _cell_bg(v)
            fg = _sc(v)
            h.append(f'<td style="padding:4px 6px;text-align:center;font-weight:700;'
                     f'color:{fg};background:{bg};">{v:.0f}</td>')
        to = t["overall"]
        h.append(f'<td style="padding:4px 8px;text-align:right;font-weight:700;color:{_sc(to)};">{to:.0f}</td>')
        h.append('</tr>')
    h.append('</tbody></table></div>')

    # ── Team insight band ──
    below_50 = [t for t in teams if t["overall"] < 50]
    above_75 = [t for t in teams if t["overall"] >= 75]
    if below_50:
        names = ', '.join(t["team_name"] for t in sorted(below_50, key=lambda x: x["overall"])[:3])
        h.append(
            f'<div style="background:{_BAND};padding:14px 32px;border-bottom:1px solid {_BORDER};">'
            f'<p style="font-size:8.5pt;color:{_NAVY};margin:0;">'
            f'<strong>Attention needed:</strong> {len(below_50)} team{"s" if len(below_50) > 1 else ""} '
            f'scoring below 50: {names}. These teams likely need support or data coverage review.</p></div>'
        )
    elif above_75:
        h.append(
            f'<div style="background:{_BAND};padding:14px 32px;border-bottom:1px solid {_BORDER};">'
            f'<p style="font-size:8.5pt;color:{_NAVY};margin:0;">'
            f'<strong>What good looks like:</strong> {len(above_75)} of {len(teams)} teams are scoring 75+. '
            f'Target: every team above 75 on all dimensions.</p></div>'
        )

    # ═══ PER-SUBDOMAIN DEEP DIVE ═════════════════════════════════
    for sd in sds:
        sd_name = sd["subdomain_name"]
        sd_teams = [t for t in teams if t.get("subdomain") == sd_name]
        if not sd_teams:
            continue
        c = _sc(sd["overall"])
        sorted_sd_teams = sorted(sd_teams, key=lambda x: x["overall"], reverse=True)
        top_team = sorted_sd_teams[0]
        bottom_team = sorted_sd_teams[-1] if len(sorted_sd_teams) > 1 else None

        score_span = f'<span style="color:{c};font-weight:700;font-style:normal;">{sd["overall"]:.0f}/100</span>'
        h.append(
            f'<div style="background:{_BG};padding:24px 32px;border-bottom:1px solid {_BORDER};" class="no-break">'
            f'{_sec_title(sd_name, "Score: " + score_span + f" &middot; {len(sd_teams)} teams")}'
            f'<table style="width:100%;border-collapse:collapse;font-size:8.5pt;">'
            f'<thead><tr>{_th("Team")}{_th("Score")}{_th("")}{_th("Weakest Area")}</tr></thead><tbody>'
        )
        for t in sorted_sd_teams:
            tc = _sc(t["overall"])
            bar = _svg_mini_bar(t["overall"], width=100, height=6)
            # Find weakest dimension for this team
            worst_key = min(dim_keys, key=lambda x: t["scores"].get(x[0], 50.0))
            worst_v = t["scores"].get(worst_key[0], 50.0)
            worst_label = f'{worst_key[1]} ({worst_v:.0f})'
            h.append(
                f'<tr><td style="padding:8px;font-weight:500;">{t["team_name"]}</td>'
                f'<td style="padding:8px;font-weight:700;color:{tc};font-size:12pt;">{t["overall"]:.0f}</td>'
                f'<td style="padding:8px;width:30%;">{bar}</td>'
                f'<td style="padding:8px;color:{_sc(worst_v)};font-size:8pt;font-weight:600;">{worst_label}</td></tr>'
            )
        h.append('</tbody></table>')
        # Mini insight for the sub-domain
        if bottom_team and top_team["overall"] - bottom_team["overall"] > 15:
            h.append(
                f'<p style="font-size:8pt;color:{_SEC};margin-top:12px;padding:8px 12px;'
                f'background:rgba(192,192,227,0.1);border-left:3px solid {_ACCENT};">'
                f'{top_team["team_name"]} ({top_team["overall"]:.0f}) and {bottom_team["team_name"]} ({bottom_team["overall"]:.0f}) '
                f'have a {top_team["overall"] - bottom_team["overall"]:.0f}-point gap. '
                f'Worth investigating what practices the top team does differently.</p>'
            )
        h.append('</div>')

    # ═══ OPERATIONAL METRICS ═════════════════════════════════════
    if incidents or github:
        h.append(
            f'<div style="background:{_BG};padding:24px 32px;border-bottom:1px solid {_BORDER};" class="no-break">'
            f'{_sec_title("Operational Metrics", "Incident trends and GitHub activity across the Foods estate.")}'
            f'<table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;"><tr>'
        )
        if incidents:
            pri = incidents.get("priority", {})
            h.append(
                f'<td style="width:50%;vertical-align:top;padding-right:20px;">'
                f'<p style="font-size:8pt;font-weight:600;color:{_SEC};text-transform:uppercase;'
                f'letter-spacing:0.5px;margin:0 0 10px;">Incidents by Priority</p>'
                f'<table style="width:100%;border-collapse:collapse;font-size:8.5pt;">'
                f'<thead><tr>{_th("Priority")}{_th("Count")}{_th("")}</tr></thead><tbody>'
            )
            total_inc = max(incidents.get("total", 1), 1)
            for p_label in ["P1", "P2", "P3", "P4"]:
                count = pri.get(p_label, 0)
                if count > 0:
                    pc = _BAD if p_label == "P1" else _WARN if p_label == "P2" else _SEC
                    pct = int(count / total_inc * 100)
                    h.append(
                        f'<tr><td style="padding:8px;font-weight:600;color:{pc};">{p_label}</td>'
                        f'<td style="padding:8px;font-weight:700;">{count}</td>'
                        f'<td style="padding:8px;width:40%;">'
                        f'<div style="height:6px;background:{_BORDER};border-radius:3px;">'
                        f'<div style="height:6px;background:{pc};width:{pct}%;border-radius:3px;"></div></div>'
                        f'</td></tr>'
                    )
            h.append('</tbody></table></td>')

        if github:
            repos = github.get('repos', 0)
            contribs = github.get('contributors', 0)
            commits = github.get('commits', 0)
            prs_merged = github.get('prs_merged', 0)
            prs_open = github.get('prs_open', 0)
            h.append(
                f'<td style="width:50%;vertical-align:top;{"padding-left:20px;" if incidents else ""}">'
                f'<p style="font-size:8pt;font-weight:600;color:{_SEC};text-transform:uppercase;'
                f'letter-spacing:0.5px;margin:0 0 10px;">GitHub Activity (30 days)</p>'
                f'<table style="width:100%;border-collapse:collapse;font-size:8.5pt;">'
                f'<tbody>'
                f'<tr><td style="padding:8px;">Commits</td><td style="padding:8px;font-weight:700;text-align:right;">{commits:,}</td></tr>'
                f'<tr><td style="padding:8px;">PRs Merged</td><td style="padding:8px;font-weight:700;text-align:right;color:{_GOOD};">{prs_merged:,}</td></tr>'
                f'<tr><td style="padding:8px;">Open PRs</td><td style="padding:8px;font-weight:700;text-align:right;'
                f'color:{_WARN if prs_open > 20 else _NAVY};">{prs_open:,}</td></tr>'
                f'<tr><td style="padding:8px;">Active Repos</td><td style="padding:8px;font-weight:700;text-align:right;">{repos:,}</td></tr>'
                f'<tr><td style="padding:8px;">Contributors</td><td style="padding:8px;font-weight:700;text-align:right;">{contribs:,}</td></tr>'
                f'</tbody></table></td>'
            )
        h.append('</tr></table></div>')

        # Ops insight band
        h.append(
            f'<div style="background:{_BAND};padding:14px 32px;border-bottom:1px solid {_BORDER};">'
            f'<p style="font-size:8.5pt;color:{_NAVY};margin:0;">'
            f'<strong>What good looks like:</strong> Zero P1 incidents. PR cycle time under 48 hours. '
            f'Open PRs trending down, merged PRs trending up.</p></div>'
        )

    # ═══ BACKLOG HEALTH ══════════════════════════════════════════
    if backlog and backlog.get("aging"):
        aging = backlog["aging"]
        total = backlog["total"]
        h.append(
            f'<div style="background:{_BG};padding:24px 32px;border-bottom:1px solid {_BORDER};" class="no-break">'
            f'{_sec_title("Backlog Health", "Ticket aging across the Foods backlog.")}'
            f'<table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;"><tr>'
            f'<td style="width:50%;vertical-align:top;padding-right:20px;">'
            f'<table style="width:100%;border-collapse:collapse;font-size:8.5pt;">'
            f'<thead><tr>{_th("Age Bucket")}{_th("Count")}{_th("% of Backlog")}{_th("")}</tr></thead><tbody>'
        )
        for bucket in [">90 days", "30-90 days", "14-30 days", "<14 days"]:
            count = aging.get(bucket, 0)
            pct = int(count / total * 100) if total > 0 else 0
            fg = _BAD if ">90" in bucket else _WARN if "30-90" in bucket else _GOOD if "<14" in bucket else _NAVY
            h.append(
                f'<tr><td style="padding:8px;">{bucket}</td>'
                f'<td style="padding:8px;font-weight:700;color:{fg};">{count:,}</td>'
                f'<td style="padding:8px;color:{_SEC};">{pct}%</td>'
                f'<td style="padding:8px;width:25%;">'
                f'<div style="height:6px;background:{_BORDER};border-radius:3px;">'
                f'<div style="height:6px;background:{fg};width:{pct}%;border-radius:3px;"></div></div>'
                f'</td></tr>'
            )
        h.append('</tbody></table></td>')
        all_keys = list(backlog.get("projects", {}).keys())
        keys_jql = ", ".join(all_keys[:20]) if all_keys else "FPAPC"
        h.append(
            f'<td style="width:50%;vertical-align:top;padding-left:20px;">'
            f'<p style="font-size:8.5pt;color:{_SEC};margin-bottom:12px;line-height:1.6;">'
            f'Total backlog: <span style="font-weight:700;color:{_NAVY};">{total:,}</span> tickets '
            f'across <span style="font-weight:700;">{len(all_keys)}</span> projects.</p>'
        )
        blocked_count = backlog.get("status", {}).get("Blocked", 0)
        if blocked_count > 0:
            h.append(
                f'<p style="font-size:8.5pt;color:{_BAD};font-weight:600;margin-bottom:8px;">'
                f'&#9888; {blocked_count:,} tickets currently blocked</p>'
            )
        stale = aging.get(">90 days", 0)
        if stale > 0:
            pct_stale = int(stale / total * 100) if total > 0 else 0
            h.append(
                f'<p style="font-size:8.5pt;color:{_BAD};font-weight:600;margin-bottom:8px;">'
                f'{stale:,} tickets ({pct_stale}%) are over 90 days old</p>'
            )
        jql_url = _jql_url(f"project in ({keys_jql}) AND statusCategory != Done")
        h.append(
            f'<p style="margin-top:12px;"><a href="{jql_url}" style="font-size:8.5pt;'
            f'color:{_NAVY};font-weight:600;text-decoration:none;border-bottom:1px solid {_ACCENT};">'
            f'View full backlog in Jira &#8599;</a></p>'
            f'</td>'
        )
        h.append('</tr></table></div>')

        h.append(
            f'<div style="background:{_BAND};padding:14px 32px;border-bottom:1px solid {_BORDER};">'
            f'<p style="font-size:8.5pt;color:{_NAVY};margin:0;">'
            f'<strong>What good looks like:</strong> Zero tickets over 90 days old. '
            f'Blocked tickets resolved within 48 hours. New tickets estimated and prioritised within a sprint.</p></div>'
        )

    # ═══ FOOTER ══════════════════════════════════════════════════
    h.append(f'''
<div style="background:{_FOOTBG};padding:24px 32px;border-bottom:1px solid {_BORDER};">
  <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">
    <tr>
      <td style="width:50%;vertical-align:top;padding-right:24px;">
        <p style="font-size:9pt;font-weight:700;color:{_NAVY};margin:0 0 4px;">&#128218; Further Reading</p>
        <p style="font-size:7.5pt;color:{_SEC};font-style:italic;margin:0 0 12px;">Useful resources for the team.</p>
        <table style="border-collapse:collapse;font-size:8.5pt;width:100%;">
          <tr><td style="padding:5px 0;"><a href="https://jira-marksandspencer-app.atlassian.net/jira/software/c/projects/FPAPC/boards/793" style="color:{_NAVY};">Foods Engineering Jira</a></td></tr>
          <tr><td style="padding:5px 0;"><a href="https://jira-marksandspencer-app.atlassian.net/wiki/spaces/FOODS/overview" style="color:{_NAVY};">Foods Confluence Space</a></td></tr>
          {f'<tr><td style="padding:5px 0;"><a href="{dashboard_url}" style="color:{_NAVY};">Interactive Dashboard</a></td></tr>' if dashboard_url else ''}
        </table>
      </td>
      <td style="width:50%;vertical-align:top;padding-left:24px;border-left:4px solid {_BORDER};">
        <p style="font-size:9pt;font-weight:700;color:{_NAVY};margin:0 0 8px;">Data Sources</p>
        <table style="border-collapse:collapse;font-size:8.5pt;width:100%;">
          <tr><td style="padding:3px 0;color:{_SEC};width:40%;">Delivery</td><td style="padding:3px 0;">Jira REST API (daily)</td></tr>
          <tr><td style="padding:3px 0;color:{_SEC};">System Health</td><td style="padding:3px 0;">PagerDuty + Dynatrace</td></tr>
          <tr><td style="padding:3px 0;color:{_SEC};">Incidents</td><td style="padding:3px 0;">ITSM + PagerDuty (60-day)</td></tr>
          <tr><td style="padding:3px 0;color:{_SEC};">Code Quality</td><td style="padding:3px 0;">GitHub API (daily)</td></tr>
          <tr><td style="padding:3px 0;color:{_SEC};">Estate Hygiene</td><td style="padding:3px 0;">GitHub catalog-info scan</td></tr>
          <tr><td style="padding:3px 0;color:{_SEC};">Ops Readiness</td><td style="padding:3px 0;">PagerDuty Standards</td></tr>
          <tr><td style="padding:3px 0;color:{_SEC};">People</td><td style="padding:3px 0;">Org config (from Miro)</td></tr>
        </table>
      </td>
    </tr>
  </table>
</div>

<div style="background:{_NAVY};color:rgba(255,255,255,0.65);padding:14px 32px;font-size:8pt;">
  <table cellpadding="0" cellspacing="0" style="width:100%;"><tr>
    <td style="color:rgba(255,255,255,0.65);font-size:8pt;">
      Generated: {datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")}
      &middot; Scores computed from live data refreshed daily via GitHub Actions</td>
    <td style="text-align:right;color:rgba(255,255,255,0.65);font-size:8pt;">
      Foods D&amp;T &middot; Foods Engineering Vitals &middot; {week}</td>
  </tr></table>
</div>

</body>
</html>''')

    return "\n".join(h)


# ---------------------------------------------------------------------------
# Sender display name
# ---------------------------------------------------------------------------

_SENDER_NAMES = {
    "grp-foods-engineering@mnscorp.net": "Foods Engineering",
    "rameshreddy.adutla@mnscorp.net": "Ramesh Reddy Adutla",
}


def _sender_display_name(email: str) -> str:
    return _SENDER_NAMES.get(email, email.split("@")[0].replace(".", " ").title())


# ---------------------------------------------------------------------------
# SendGrid sending
# ---------------------------------------------------------------------------

def _send_via_sendgrid(html: str, subject: str, recipients: list,
                       sender: str, api_key: str, sender_name: str = "Foods Engineering") -> bool:
    """Send email via SendGrid. Pure HTML, no attachments needed."""
    import requests
    payload = {
        "personalizations": [{"to": [{"email": r} for r in recipients]}],
        "from": {"email": sender, "name": sender_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": "View this email in an HTML-capable client."},
            {"type": "text/html", "value": html},
        ],
    }
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    return 200 <= resp.status_code < 300


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------

@click.command("email-report")
@click.option("--scores-json", type=click.Path(exists=True), default=None,
              help="Path to heat_map_scores.json (default: outputs/json/heat_map_scores.json)")
@click.option("--csv-dir", type=click.Path(exists=True), default=None,
              help="Directory containing CSV outputs (default: outputs/csv/)")
@click.option("--dashboard-url", default="", help="URL to the interactive dashboard")
@click.option("--send/--no-send", default=False, help="Send via SendGrid")
@click.option("--sendgrid-api-key", envvar="SENDGRID_API_KEY", default=None)
@click.option("--recipients", envvar="ENGINEERING_VITALS_RECIPIENTS", default=None,
              help="Comma-separated recipient emails")
@click.option("--sender", envvar="ENGINEERING_VITALS_SENDER",
              default="grp-foods-engineering@mnscorp.net")
@click.option("--verbose", is_flag=True)
def email_report_command(scores_json, csv_dir, dashboard_url, send, sendgrid_api_key,
                         recipients, sender, verbose):
    """Generate the Engineering Vitals email report with embedded charts."""

    scores_path = Path(scores_json) if scores_json else OUTPUTS_DIR / "json" / "heat_map_scores.json"
    csv_path = Path(csv_dir) if csv_dir else OUTPUTS_DIR / "csv"

    if not scores_path.exists():
        console.print(f"[red]Scores file not found: {scores_path}[/red]")
        raise SystemExit(1)

    with open(scores_path) as f:
        scores = json.load(f)

    week = scores.get("week", datetime.now(timezone.utc).strftime("W%V %G"))
    week_slug = week.replace(" ", "_")
    console.print("[bold]Foods Engineering Vitals Email Report[/bold]")
    console.print(f"  Scores from: {week}")

    # Build week-specific dashboard URL if not provided
    if not dashboard_url:
        dashboard_url = f"{_PAGES_BASE}/outputs/html/heatmap/{week_slug}.html"
    console.print(f"  Dashboard URL: {dashboard_url}")

    html = _build_html(scores, csv_path, dashboard_url, week)

    # Write standalone HTML
    report_dir = OUTPUTS_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    html_path = report_dir / f"foods_engineering_report_{week.replace(' ', '_')}.html"
    html_path.write_text(html, encoding="utf-8")
    console.print(f"Wrote standalone HTML: {html_path.relative_to(OUTPUTS_DIR.parent)}")

    # Write .eml
    subject = f"Foods Engineering Vitals - Foods D&T - {week}"
    plain = "View this report in an HTML-capable email client."
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    sender_name = _sender_display_name(sender)
    msg["From"] = f"{sender_name} <{sender}>"

    recip_list = [r.strip() for r in (recipients or ",".join(DEFAULT_RECIPIENTS)).split(",") if r.strip()]
    msg["To"] = ", ".join(recip_list)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    eml_path = report_dir / f"foods_engineering_report_{week.replace(' ', '_')}.eml"
    eml_path.write_text(msg.as_string(), encoding="utf-8")
    console.print(f"Wrote .eml: {eml_path.relative_to(OUTPUTS_DIR.parent)}")

    console.print(f"\nRecipients: {', '.join(recip_list)}")

    if send:
        if not sendgrid_api_key:
            console.print("[red]SENDGRID_API_KEY required for --send[/red]")
            raise SystemExit(1)
        console.print("Sending via SendGrid...")
        ok = _send_via_sendgrid(html, subject, recip_list, sender, sendgrid_api_key,
                                sender_name=_sender_display_name(sender))
        if ok:
            console.print("[green]Email sent successfully.[/green]")
        else:
            console.print("[red]SendGrid send failed.[/red]")
            raise SystemExit(1)
    else:
        console.print("Use --send to dispatch via SendGrid")
