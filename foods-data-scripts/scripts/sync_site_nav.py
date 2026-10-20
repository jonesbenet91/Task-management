#!/usr/bin/env python3
"""Stamp the canonical top navigation into every dashboard in ``outputs/html/``.

The link list, labels and markup all come from a single source of truth
(:mod:`datascripts.services.site_nav`). Run this after the HTML dashboards are
(re)generated - locally or as a CI step - so every report shares an identical
top links menu. The operation is idempotent: files already carrying the
canonical nav are left untouched.

Usage:
    python scripts/sync_site_nav.py            # update files in place
    python scripts/sync_site_nav.py --check    # exit 1 if any file is stale
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HTML_DIR = REPO_ROOT / "outputs" / "html"


def _load_site_nav():
    """Load the site_nav module directly, bypassing the heavier package __init__."""
    module_path = REPO_ROOT / "src" / "datascripts" / "services" / "site_nav.py"
    spec = importlib.util.spec_from_file_location("_site_nav", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_site_nav = _load_site_nav()
HOME_FILE = _site_nav.HOME_FILE
NAV_PAGES = _site_nav.NAV_PAGES
inject_site_nav = _site_nav.inject_site_nav

# Service Catalogue uses a distinct light Tailwind theme; it gets the same link
# list rendered for that design system rather than the dark bar.
TAILWIND_FILES = {"servicecatalogue.html"}
# Not standalone dashboards - no shared header to stamp.
SKIP_FILES = {"heatmap_confluence_preview.html"}

_KNOWN_PAGES = {f for f, _ in NAV_PAGES} | {HOME_FILE}


def _active_for(name: str) -> str:
    return name if name in _KNOWN_PAGES else ""


def _sync(path: Path, *, active: str, prefix: str, tailwind: bool, check: bool) -> bool:
    original = path.read_text(encoding="utf-8")
    updated = inject_site_nav(original, active, prefix=prefix, tailwind=tailwind)
    if updated == original:
        return False
    if not check:
        path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Report stale files and exit 1 without writing.")
    args = parser.parse_args()

    if not HTML_DIR.is_dir():
        print(f"error: {HTML_DIR} not found", file=sys.stderr)
        return 2

    stale: list[str] = []

    # Top-level dashboards.
    for path in sorted(HTML_DIR.glob("*.html")):
        if path.name in SKIP_FILES:
            continue
        changed = _sync(path, active=_active_for(path.name), prefix="",
                        tailwind=path.name in TAILWIND_FILES, check=args.check)
        if changed:
            stale.append(path.relative_to(REPO_ROOT).as_posix())

    # Versioned heat map snapshots live one directory deeper.
    for path in sorted((HTML_DIR / "heatmap").glob("*.html")):
        changed = _sync(path, active="heatmap.html", prefix="../",
                        tailwind=False, check=args.check)
        if changed:
            stale.append(path.relative_to(REPO_ROOT).as_posix())

    verb = "stale" if args.check else "updated"
    for name in stale:
        print(f"{verb}: {name}")

    if args.check and stale:
        print(f"{len(stale)} file(s) out of sync; run scripts/sync_site_nav.py",
              file=sys.stderr)
        return 1
    print(f"done: {len(stale)} file(s) {verb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
