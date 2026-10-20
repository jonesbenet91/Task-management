"""Main CLI entry point for data-scripts."""

import click

from .commands.pagerduty_incidents import incidents_command
from .commands.pagerduty_teams import teams_command
from .commands.pagerduty_services import services_command
from .commands.pagerduty_standards import standards_command
from .commands.github_commits import commits_command
from .commands.github_metrics import metrics_command
from .commands.github_techstack import techstack
from .commands.github_network import network as network_command
from .commands.github_repo_health import repo_health_command
from .commands.jira_tickets import tickets_command
from .commands.jira_sprint_issues import sprint_issues_command
from .commands.jira_sprint_performance import sprint_performance_command
from .commands.jira_backlog_health import backlog_health_command
from .commands.itsm_bmc import main as itsm_bmc_command
from .commands.catalog_info_scan import scan_command as catalog_scan_command
from .commands.dynatrace_scan import dynatrace_scan_command
from .commands.estate_report import estate_group
from .commands.github_actions_health import actions_health_command
from .commands.heatmap_scores import heatmap_command
from .commands.heatmap_email import email_report_command
from .commands.heatmap_confluence import confluence_publish_command
from .commands.snyk_aging import snyk_aging_command


@click.group()
@click.version_option(version="0.1.0", prog_name="data-scripts")
def cli():
    """Data Scripts Bundle - Data extraction toolkit for GitHub, PagerDuty, JIRA, and BMC Remedy APIs."""
    pass


# PagerDuty command group
@cli.group("pagerduty")
def pagerduty():
    """PagerDuty API commands."""
    pass


pagerduty.add_command(incidents_command, name="incidents")
pagerduty.add_command(teams_command, name="teams")
pagerduty.add_command(services_command, name="services")
pagerduty.add_command(standards_command, name="standards")


# GitHub command group
@cli.group("github")
def github():
    """GitHub API commands."""
    pass


github.add_command(commits_command, name="commits")
github.add_command(metrics_command, name="metrics")
github.add_command(techstack, name="techstack")
github.add_command(network_command, name="network")
github.add_command(repo_health_command, name="repo-health")
github.add_command(catalog_scan_command, name="catalog-scan")
github.add_command(actions_health_command, name="actions-health")


# Snyk command group
@cli.group("snyk")
def snyk():
    """Snyk security API commands."""
    pass


snyk.add_command(snyk_aging_command, name="aging")


# JIRA command group
@cli.group("jira")
def jira():
    """JIRA API commands."""
    pass


jira.add_command(tickets_command, name="tickets")
jira.add_command(sprint_issues_command, name="sprint-issues")
jira.add_command(sprint_performance_command, name="sprint-performance")
jira.add_command(backlog_health_command, name="backlog-health")


# BMC Remedy ITSM command group
@cli.group("itsm")
def itsm():
    """BMC Remedy ITSM API commands (incidents and change requests)."""
    pass


itsm.add_command(itsm_bmc_command, name="incidents")


@itsm.command("change-requests")
@click.option('--days', default=30, show_default=True, help='Number of days to look back')
@click.option('--cr-coordinator-group', 'cr_coordinator_group',
              help='Filter by Change Coordinator Group')
@click.option('--cr-product-tier1', 'cr_product_tier1',
              help='Filter by Product Categorization Tier 1 (e.g. "Food")')
@click.option('--cr-product-tier3', 'cr_product_tier3',
              help='Filter by Product Categorization Tier 3')
@click.option('--cr-status', 'cr_status',
              type=click.Choice(['all', 'open', 'closed']), default='all', show_default=True,
              help='Filter by change request status')
@click.option('--csv', 'output_csv', is_flag=True, help='Export to CSV (outputs/csv/change_requests_dump.csv)')
@click.option('--json', 'output_json', is_flag=True, help='Export to JSON')
@click.option('--base-url', envvar='ITSM_BASE_URL',
              default='https://mnscorp-rod-restapi.onbmc.com', show_default=True,
              help='BMC instance URL')
@click.option('--username', envvar='ITSM_USERNAME', help='BMC username')
@click.option('--password', envvar='ITSM_PASSWORD', help='BMC password')
@click.option('--no-verify-ssl', is_flag=True, help='Disable SSL certificate verification')
@click.option('--verbose', is_flag=True, help='Verbose/debug output')
@click.pass_context
def itsm_change_requests_command(ctx, days, cr_coordinator_group, cr_product_tier1,
                                  cr_product_tier3, cr_status, output_csv, output_json,
                                  base_url, username, password, no_verify_ssl, verbose):
    """Extract BMC Remedy change requests.

    Fetches change requests from the CHG:Infrastructure Change form.
    Always exports to outputs/csv/change_requests_dump.csv.

    \b
    Examples:
        # All change requests for last 30 days
        data-scripts itsm change-requests --csv --no-verify-ssl

        # Filter by product tier
        data-scripts itsm change-requests --cr-product-tier1 "Food" --csv --no-verify-ssl

        # Filter by coordinator group
        data-scripts itsm change-requests --cr-coordinator-group "MTP ASO SUPPORT" --csv

        # Open CRs for last 7 days
        data-scripts itsm change-requests --days 7 --cr-status open --csv --no-verify-ssl

    \b
    Environment Variables:
        ITSM_BASE_URL   - BMC instance URL
        ITSM_USERNAME   - BMC username
        ITSM_PASSWORD   - BMC password
    """
    ctx.invoke(
        itsm_bmc_command,
        change_requests=True,
        days=days,
        cr_coordinator_group=cr_coordinator_group,
        cr_product_tier1=cr_product_tier1,
        cr_product_tier3=cr_product_tier3,
        cr_status=cr_status,
        output_csv=output_csv,
        output_json=output_json,
        base_url=base_url,
        username=username,
        password=password,
        no_verify_ssl=no_verify_ssl,
        verbose=verbose,
        # Incident-mode defaults (not used in CR mode)
        group=None,
        groups_csv=None,
        state='all',
        summary=False,
        output=None,
    )


# Dynatrace command group
@cli.group("dynatrace")
def dynatrace():
    """Dynatrace API commands (service discovery and alert scanning)."""
    pass


dynatrace.add_command(dynatrace_scan_command, name="scan")


# Estate command group (unified cross-source reporting)
cli.add_command(estate_group, name="estate")


# Heatmap command group (engineering intelligence)
@cli.group("heatmap")
def heatmap():
    """Engineering Intelligence heat map commands."""
    pass


heatmap.add_command(heatmap_command, name="scores")
heatmap.add_command(email_report_command, name="email-report")
heatmap.add_command(confluence_publish_command, name="confluence-publish")


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
