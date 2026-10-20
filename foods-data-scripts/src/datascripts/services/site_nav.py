"""Single source of truth for the shared top navigation across all dashboards.

Every HTML report in ``outputs/html/`` shows an identical top links menu. Rather
than hand-maintaining that markup in each file (which drifts), the canonical link
list lives here and is stamped into each report at build time by
``scripts/sync_site_nav.py`` (run in CI after the HTML is generated) and by the
in-repo generators that build their HTML directly.

Two renderers share the same :data:`NAV_PAGES` list:

* :func:`render_nav` - the standard dark GitHub-style bar used by every report.
* :func:`render_nav_tailwind` - the light Tailwind header used by the Service
  Catalogue page, which has a different design system.
"""

from __future__ import annotations

import re

# Home / brand ---------------------------------------------------------------
HOME_FILE = "parent.html"
BRAND = "\U0001F34E Foods Engineering"  # apple + wordmark
HOME_LABEL = "\U0001F3E0 Home"  # house + Home

# Canonical, ordered list of report pages: (filename, menu label).
# This is the single source of truth for the top links menu everywhere.
NAV_PAGES: list[tuple[str, str]] = [
    ("githubmetrics.html", "GitHub Metrics"),
    ("techstackview.html", "Tech Stack"),
    ("repohealth.html", "Repo Health"),
    ("servicecatalogue.html", "Service Catalogue"),
    ("pagerdutystandard.html", "PagerDuty Standards"),
    ("pagerdutyincidents.html", "PagerDuty Incidents"),
    ("itsmbmcincidentview.html", "ITSM Incidents"),
    ("itsmbmcchangerequest.html", "Change Requests"),
    ("snykaging.html", "Snyk Aging"),
    ("backloghealth.html", "Backlog Health"),
    ("sprintperformance.html", "Sprint Performance"),
    ("heatmap.html", "Eng Vitals"),
    ("estate.html", "Estate"),
]

PAGE_LABELS: dict[str, str] = {f: label for f, label in NAV_PAGES}

# Canonical CSS for the dark navigation bar (one rule per line).
NAV_CSS = """\
.site-nav { position:fixed;top:0;left:0;right:0;z-index:9999;background:#161b22;border-bottom:1px solid #30363d;height:48px;padding:0;margin:0;display:block;box-shadow:none;backdrop-filter:none;-webkit-backdrop-filter:none;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
.site-nav-inner { max-width:1900px;margin:0 auto;padding:0 20px;display:flex;align-items:center;justify-content:flex-start;height:100%; }
.site-nav-brand { color:#e6edf3;font-weight:700;font-size:14px;text-decoration:none;display:flex;align-items:center;gap:6px;white-space:nowrap;padding-right:16px;border-right:1px solid #30363d;margin-right:14px; }
.site-nav-brand:hover { color:#58a6ff; }
.site-nav-home { color:#58a6ff;text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap;display:flex;align-items:center;gap:4px; }
.site-nav-home:hover { color:#79b8ff; }
.site-nav-sep { color:#8b949e;margin:0 8px;font-size:14px; }
.site-nav-current { color:#e6edf3;font-size:13px;font-weight:500;white-space:nowrap; }
.site-nav-current-home { color:#58a6ff;font-size:13px;font-weight:600;white-space:nowrap; }
.site-nav-links { margin-left:auto;display:flex;gap:2px;align-items:center;overflow-x:auto;scrollbar-width:none; }
.site-nav-links::-webkit-scrollbar { display:none; }
.site-nav-link { color:#8b949e;text-decoration:none;font-size:12px;font-weight:500;padding:5px 10px;border-radius:6px;white-space:nowrap;transition:all 0.15s; }
.site-nav-link:hover,.site-nav-link.nav-active { background:#21262d;color:#e6edf3; }
.site-nav-link.nav-active { color:#58a6ff; }"""


# --- Rendering --------------------------------------------------------------
def render_nav(active_file: str, *, prefix: str = "") -> str:
    """Return the canonical dark navigation bar as a single line of HTML.

    ``active_file`` is the current page's filename (e.g. ``"estate.html"``);
    that entry is highlighted. ``prefix`` is prepended to every href so pages in
    a subdirectory (e.g. versioned snapshots) can point back up with ``"../"``.
    """
    links = "".join(
        f'<a class="{"site-nav-link nav-active" if f == active_file else "site-nav-link"}"'
        f' href="{prefix}{f}">{label}</a>'
        for f, label in NAV_PAGES
    )
    if active_file == HOME_FILE:
        crumb = f'<span class="site-nav-current-home">{HOME_LABEL}</span>'
    else:
        crumb = (
            f'<a class="site-nav-home" href="{prefix}{HOME_FILE}">{HOME_LABEL}</a>'
            f'<span class="site-nav-sep">\u203a</span>'
            f'<span class="site-nav-current">{PAGE_LABELS.get(active_file, "")}</span>'
        )
    return (
        '<nav class="site-nav"><div class="site-nav-inner">'
        f'<a class="site-nav-brand" href="{prefix}{HOME_FILE}">{BRAND}</a>'
        f'{crumb}<div class="site-nav-links">{links}</div></div></nav>'
    )


_TW_LINK = ("rounded-md px-3 py-1.5 text-xs font-medium text-gray-400"
            " transition-colors hover:bg-gray-100 hover:text-gray-700")
_TW_ACTIVE = ("rounded-md border border-blue-500/25 bg-blue-500/10 px-3 py-1.5"
              " text-xs font-medium text-blue-600")


def render_nav_tailwind(active_file: str, *, prefix: str = "") -> str:
    """Return the light Tailwind header nav (used by the Service Catalogue page).

    Same canonical link list as :func:`render_nav`, styled for the light theme.
    """
    links = "".join(
        f'<a class="{_TW_ACTIVE if f == active_file else _TW_LINK}"'
        f' href="{prefix}{f}">{label}</a>'
        for f, label in NAV_PAGES
    )
    return f'<nav class="flex items-center gap-1 overflow-x-auto">{links}</nav>'


# --- Injection into existing HTML ------------------------------------------
_NAV_RULE_RE = re.compile(r'^([ \t]*)\.site-nav[^\n{]*\{[^}]*\}[ \t]*\n?', re.MULTILINE)
_DARK_NAV_OPEN_RE = re.compile(r'<(?:nav|div)\s+class="site-nav"[ >]')
_TAG_TOKEN_RE = re.compile(r'</?(?:nav|div)\b[^>]*>')
_TW_NAV_RE = re.compile(
    r'<nav class="flex items-center gap-1 overflow-x-auto">.*?</nav>', re.DOTALL)
_BODY_RE = re.compile(r'<body\b[^>]*>')


def _dark_nav_span(html: str) -> tuple[int, int] | None:
    """Locate the existing dark nav container by balancing nav/div tags."""
    opener = _DARK_NAV_OPEN_RE.search(html)
    if not opener:
        return None
    start = opener.start()
    depth = 0
    for token in _TAG_TOKEN_RE.finditer(html, start):
        depth += -1 if token.group(0).startswith("</") else 1
        if depth == 0:
            return start, token.end()
    return None


def _replace_dark_nav(html: str, new_nav: str) -> str:
    span = _dark_nav_span(html)
    if span:
        return html[:span[0]] + new_nav + html[span[1]:]
    body = _BODY_RE.search(html)
    if body:
        return f"{html[:body.end()]}\n{new_nav}{html[body.end():]}"
    return html


def _indent_css(css_text: str, indent: str) -> str:
    return "\n".join(
        f"{indent}{line}" if line.strip() else line
        for line in css_text.strip("\n").split("\n")
    )


def _replace_nav_css(html: str, css_text: str) -> str:
    """Replace all existing ``.site-nav*`` CSS rules with the canonical block."""
    matches = list(_NAV_RULE_RE.finditer(html))
    if not matches:
        idx = html.find("</style>")
        if idx == -1:
            return html
        return f"{html[:idx]}{_indent_css(css_text, '  ')}\n{html[idx:]}"
    block = _indent_css(css_text, matches[0].group(1))
    seen = False

    def _repl(_match: re.Match) -> str:
        nonlocal seen
        if not seen:
            seen = True
            return f"{block}\n"
        return ""

    return _NAV_RULE_RE.sub(_repl, html)


def _replace_tailwind_nav(html: str, new_nav: str) -> str:
    return _TW_NAV_RE.sub(lambda _m: new_nav, html, count=1)


def inject_site_nav(html: str, active_file: str, *, prefix: str = "",
                    tailwind: bool = False) -> str:
    """Return ``html`` with the canonical nav (and its CSS) stamped in.

    Idempotent: existing nav markup and ``.site-nav`` CSS rules are replaced, so
    re-running produces no further changes.
    """
    if tailwind:
        return _replace_tailwind_nav(html, render_nav_tailwind(active_file, prefix=prefix))
    html = _replace_dark_nav(html, render_nav(active_file, prefix=prefix))
    return _replace_nav_css(html, NAV_CSS)
