#!/usr/bin/env python3
"""One-off seed tool: populate the ``JIRA Board ID`` column in ``data/jira.csv``.

For every ``JIRA Board Name/Key`` in the CSV this resolves a board id using the
**same first-board logic** the ``jira sprint-performance`` command uses
(``resolve_board`` -> first scrum board, else first board), and writes the id
into the ``JIRA Board ID`` column.

By default it only fills **blank** cells, so hand-made corrections are never
clobbered and the script is safe to re-run. This is a single-use bootstrap:
after seeding, hand-correct any known-wrong rows (e.g. teams whose active board
is not the first one Jira returns for the project key) and treat the CSV column
as the source of truth. The command uses that column directly and skips
resolution whenever a board id is present.

Credentials are resolved exactly like the command (and share its caches):
  * email : --email  -> $JIRA_EMAIL / $JIRA_USER -> cached -> prompt
  * token : --token  -> $JIRA_API_TOKEN -> cached (TokenManager) -> prompt
  * host  : --host   -> $JIRA_HOST -> the command's DEFAULT_HOST

Usage:
    JIRA_EMAIL=you@example.com JIRA_API_TOKEN=... python scripts/seed_board_ids.py
    python scripts/seed_board_ids.py --projects-csv data/jira.csv --dry-run
    python scripts/seed_board_ids.py --overwrite   # also refresh non-blank cells
"""

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path

# Allow running straight from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from datascripts.commands.jira_sprint_performance import DEFAULT_HOST, resolve_board
from datascripts.services import JiraApiService, TokenManager

BOARD_KEY_COL = "JIRA Board Name/Key"
BOARD_ID_COL = "JIRA Board ID"


def resolve_credentials(args: argparse.Namespace) -> tuple:
    """Resolve (email, token), reusing the sprint-performance command's caches."""
    email_cache = Path(tempfile.gettempdir()) / "jira_email.cache"

    email = args.email or os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USER")
    if not email and email_cache.exists():
        email = email_cache.read_text().strip() or None
    if not email and sys.stdin.isatty():
        email = input("JIRA email address (will be cached): ").strip() or None
        if email:
            email_cache.write_text(email)
    if not email:
        sys.exit("Error: JIRA email required (set JIRA_EMAIL or pass --email).")

    token = TokenManager("jira_token.cache").get_token(
        cli_token=args.token,
        env_vars=["JIRA_API_TOKEN"],
        reset=False,
        prompt_text="JIRA API token (will be cached)",
    )
    if not token:
        sys.exit("Error: JIRA API token required (set JIRA_API_TOKEN or pass --token).")

    return email, token


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-off: seed the 'JIRA Board ID' column in data/jira.csv.",
    )
    parser.add_argument("--projects-csv", default="data/jira.csv",
                        help="Path to the projects CSV (default: data/jira.csv)")
    parser.add_argument("--host", default=os.environ.get("JIRA_HOST") or DEFAULT_HOST,
                        help="JIRA base URL (default: $JIRA_HOST or the command default)")
    parser.add_argument("--email", help="JIRA user email for Basic auth")
    parser.add_argument("--token", help="JIRA API token")
    parser.add_argument("--overwrite", action="store_true",
                        help="Also overwrite cells that already contain a board id.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing the file.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    csv_path = Path(args.projects_csv)
    if not csv_path.exists():
        sys.exit(f"Error: {csv_path} not found.")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if BOARD_KEY_COL not in fieldnames:
        sys.exit(f"Error: column '{BOARD_KEY_COL}' not found in {csv_path}.")
    if BOARD_ID_COL not in fieldnames:
        fieldnames.append(BOARD_ID_COL)
    for r in rows:
        r.setdefault(BOARD_ID_COL, "")

    email, token = resolve_credentials(args)
    service = JiraApiService(args.host, email, token)

    # Resolve each distinct key once (using the old first-board behaviour).
    keys = list(dict.fromkeys(
        (r.get(BOARD_KEY_COL) or "").strip()
        for r in rows
        if (r.get(BOARD_KEY_COL) or "").strip()
    ))
    print(f"Resolving {len(keys)} board key(s) against {service.host} ...")
    resolved_ids: dict = {}
    unresolved: list = []
    for key in keys:
        resolved = resolve_board(service, key, verbose=True)
        if resolved:
            resolved_ids[key] = resolved[0]
            print(f"  {key} -> board {resolved[0]} ({resolved[1]})")
        else:
            unresolved.append(key)
            print(f"  {key} -> (no board found)")

    filled = 0
    skipped_existing = 0
    for r in rows:
        key = (r.get(BOARD_KEY_COL) or "").strip()
        if not key or key not in resolved_ids:
            continue
        existing = (r.get(BOARD_ID_COL) or "").strip()
        if existing and not args.overwrite:
            skipped_existing += 1
            continue
        new_id = resolved_ids[key]
        if existing != new_id:
            r[BOARD_ID_COL] = new_id
            filled += 1

    verb = "would be filled" if args.dry_run else "filled"
    print(f"\n{filled} cell(s) {verb}; {skipped_existing} already set (kept).")
    if unresolved:
        print(f"Unresolved key(s) ({len(unresolved)}): {', '.join(unresolved)}")
        print("  -> fill these rows' 'JIRA Board ID' by hand from the board URL.")

    if args.dry_run:
        print("Dry run — no file written.")
        return 0

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {csv_path}.")
    print("Next: hand-correct any known-wrong rows (e.g. RELEX, Pricing), then commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
