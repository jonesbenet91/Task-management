"""Ad-hoc diagnostic for the SP-changed / no-SP metrics.

Run in the terminal that already has $env:JIRA_API_TOKEN set, e.g.:
    cd 'C:\\Users\\2875981\\Downloads\\foods-data-scripts\\jira-population-dashboard'
    python diag_sp.py
"""
import os
from collections import Counter

import jira_population as jp


def main():
    host = os.environ.get("JIRA_HOST") or jp.DEFAULT_HOST
    email = os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USER")
    token = os.environ.get("JIRA_API_TOKEN")
    print(f"host={host} email={email} token={'set' if token else 'MISSING'}")
    if not token:
        print("\nNo JIRA_API_TOKEN in this shell. Run this in the same terminal where "
              "the full pipeline worked (it has the token set).")
        return

    client = jp.JiraClient(host, email, token, verbose=True)
    print("auth:", client.verify_auth())

    sp_field = client.story_points_field()
    sprint_field = client.sprint_field()
    print(f"\nsp_field={sp_field}  sprint_field={sprint_field}\n")

    mapping = jp.load_project_mapping(jp.DEFAULT_PROJECTS_CSV)
    projects = list(mapping.keys())
    print("projects:", projects, "\n")

    start, end = "2026-08-01", "2026-09-30"

    for pk in projects:
        # 1) What issue types exist in this project's recent updates?
        jql_all = (
            f'project = "{pk}" AND updated >= "{start}" AND updated <= "{end}" '
            f"ORDER BY updated DESC"
        )
        try:
            allissues = client.search(
                jql_all,
                ["issuetype", sp_field or "summary", sprint_field or "summary"],
            )
        except Exception as exc:
            print(f"{pk}: ALL query failed: {exc}")
            continue
        types = Counter()
        for iss in allissues:
            t = ((iss.get("fields") or {}).get("issuetype") or {}).get("name") or "?"
            types[t] += 1

        # 2) Story-typed subset
        jql_story = (
            f'project = "{pk}" AND issuetype = Story '
            f'AND updated >= "{start}" AND updated <= "{end}" ORDER BY updated DESC'
        )
        try:
            stories = client.search(
                jql_story,
                ["summary", sp_field or "summary", sprint_field or "summary"],
            )
            story_err = None
        except Exception as exc:
            stories = []
            story_err = exc

        # 3) How many stories have a non-empty sprint field / sp value?
        with_sprint = 0
        with_sp = 0
        for iss in stories:
            f = iss.get("fields") or {}
            if sprint_field and f.get(sprint_field):
                with_sprint += 1
            if sp_field and f.get(sp_field) not in (None, ""):
                with_sp += 1

        print(f"=== {pk} ===")
        print(f"  updated-in-window total: {len(allissues)}")
        print(f"  issue types: {dict(types)}")
        if story_err:
            print(f"  issuetype=Story query ERROR: {story_err}")
        else:
            print(f"  issuetype=Story: {len(stories)}  "
                  f"(with sprint field={with_sprint}, with SP={with_sp})")
        print()


if __name__ == "__main__":
    main()
