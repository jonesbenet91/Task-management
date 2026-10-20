"""Shared error message helpers for CLI commands."""


def get_pagerduty_auth_error_message(subdomain: str = None) -> str:
    """Return a formatted PagerDuty authentication error message."""
    target = f"[bold]{subdomain}.pagerduty.com[/bold]" if subdomain else "PagerDuty"
    return (
        f"Failed to authenticate with {target}\n\n"
        "Please check the following:\n"
        "  • Your PagerDuty API token is valid and not expired\n"
        "  • The token was created under [bold]Integrations → API Access Keys[/bold]\n"
        "  • The token has [bold]Read[/bold] permissions at minimum\n\n"
        "To reset your cached token, re-run with [bold]--reset-token[/bold]."
    )


def get_pagerduty_permission_error_message(subdomain: str = None) -> str:
    """Return a formatted PagerDuty permission error message."""
    target = f"[bold]{subdomain}.pagerduty.com[/bold]" if subdomain else "PagerDuty"
    return (
        f"Insufficient permissions to access {target}\n\n"
        "Please check the following:\n"
        "  • Your API token has the required permissions for this operation\n"
        "  • You are not restricted to a specific team scope\n"
        "  • Contact your PagerDuty admin if you need elevated access\n\n"
        "To reset your cached token, re-run with [bold]--reset-token[/bold]."
    )


def get_jira_auth_error_message(host: str) -> str:
    """Return a formatted JIRA authentication error message."""
    return (
        f"Failed to authenticate with JIRA at [bold]{host}[/bold]\n\n"
        "Please check the following:\n"
        "  • Your JIRA token is valid and not expired\n"
        "  • You are using your [bold]email address[/bold] (not username) as the login\n"
        "  • The token has sufficient permissions for the requested projects\n"
        "  • Your account is not locked or CAPTCHA-restricted\n\n"
        "To reset your cached token, re-run with [bold]--reset-token[/bold]."
    )
