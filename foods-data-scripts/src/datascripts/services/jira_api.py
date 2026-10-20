"""JIRA API service for ticket queries."""

import base64
import time
from typing import Any, Dict, List, Optional

import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel

from ..commands.error_messages import get_jira_auth_error_message


console = Console()


class JiraAuthError(Exception):
    """Raised when JIRA API authentication fails."""
    pass


class JiraApiService:
    """Service for interacting with JIRA API."""
    
    def __init__(self, host: str, username: str, token: str):
        """Initialize JIRA API service.
        
        Args:
            host: JIRA base URL (e.g., https://yourorg.atlassian.net)
            username: Username/email for Basic auth
            token: JIRA API token
        """
        self.host = host.rstrip("/")
        self.username = username
        self.token = token
        self.session = requests.Session()
        self.session.headers.update(self._build_auth_headers())
    
    def _build_auth_headers(self) -> Dict[str, str]:
        """Build authentication headers."""
        credentials = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
        return {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    
    def search_issues(
        self,
        jql: str,
        max_results: int = 50,
        fetch_all: bool = True
    ) -> List[Dict]:
        """Search for issues using JQL with pagination.
        
        Args:
            jql: JQL query string
            max_results: Maximum results per page (1-100)
            fetch_all: Whether to fetch all pages
        
        Returns:
            List of issue dictionaries
        
        Raises:
            RuntimeError: On API errors
        """
        collected = []
        next_page_token = None
        
        max_results = max(1, min(100, max_results))
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Fetching JIRA issues: 0", total=None)
            
            while True:
                params = {
                    "jql": jql,
                    "maxResults": max_results,
                    "fields": "*all",
                }
                
                if next_page_token:
                    params["nextPageToken"] = next_page_token
                
                try:
                    response = self.session.get(
                        f"{self.host}/rest/api/3/search/jql",
                        params=params,
                        timeout=60
                    )
                except requests.RequestException as e:
                    raise RuntimeError(f"HTTP request failed: {e}")
                
                if response.status_code != 200:
                    # Check for authentication errors
                    if response.status_code in [401, 403]:
                        error_message = get_jira_auth_error_message(self.host)
                        console.print(Panel(error_message, title="🔐 JIRA Authentication Error", border_style="red"))
                        raise JiraAuthError(f"Authentication failed (HTTP {response.status_code})")
                    raise RuntimeError(f"JIRA API returned HTTP {response.status_code}: {response.text}")
                
                # Check authentication headers
                login_reason = response.headers.get("x-seraph-loginreason", "")
                if login_reason == "AUTHENTICATION_DENIED":
                    error_message = get_jira_auth_error_message(self.host)
                    console.print(Panel(error_message, title="🔐 JIRA Authentication Error", border_style="red"))
                    console.print("\n[yellow]Note: CAPTCHA may have been triggered. Try logging in via browser first.[/yellow]")
                    raise JiraAuthError("Authentication denied: CAPTCHA required or invalid credentials")
                if login_reason == "AUTHENTICATION_FAILED":
                    error_message = get_jira_auth_error_message(self.host)
                    console.print(Panel(error_message, title="🔐 JIRA Authentication Error", border_style="red"))
                    console.print("\n[yellow]Note: Check that you're using your email address (not username) and a valid API token.[/yellow]")
                    raise JiraAuthError("Authentication failed: invalid credentials or insufficient token scope")
                
                data = response.json()
                issues = data.get("issues", [])
                
                if issues:
                    collected.extend(issues)
                
                progress.update(task, description=f"Fetching JIRA issues: {len(collected)}")
                
                next_page_token = data.get("nextPageToken")
                is_last = data.get("isLast", True)
                
                if not fetch_all or is_last or not issues:
                    break
        
        console.print(f"Fetched {len(collected)} JIRA issues.\n")
        return collected
    
    def build_jql(
        self,
        projects: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        since: str = "",
        until: str = "",
        custom_jql: str = ""
    ) -> str:
        """Build JQL query from filter components.
        
        Args:
            projects: Project keys to filter by
            statuses: Status names to filter by
            since: Created since date (YYYY-MM-DD)
            until: Created until date (YYYY-MM-DD)
            custom_jql: Additional JQL to AND with other filters
        
        Returns:
            Complete JQL query
        """
        parts = []
        
        if projects:
            escaped = [f'"{p}"' if any(c in p for c in " -()[]") else p for p in projects]
            parts.append(f"project in ({','.join(escaped)})")
        
        if since:
            parts.append(f'created >= "{since}"')
        
        if until:
            parts.append(f'created <= "{until}"')
        
        if statuses:
            escaped = [f'"{s}"' if any(c in s for c in " -()[]") else s for s in statuses]
            parts.append(f"status in ({','.join(escaped)})")
        
        if custom_jql:
            parts.append(f"({custom_jql})")
        
        return " AND ".join(parts)
    
    def validate_custom_jql(
        self,
        custom_jql: str,
        projects: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        since: str = "",
        until: str = ""
    ) -> List[str]:
        """Validate custom JQL for conflicts.
        
        Args:
            custom_jql: Custom JQL to validate
            projects: Projects being filtered
            statuses: Statuses being filtered
            since: Since date being used
            until: Until date being used
        
        Returns:
            List of warning messages
        """
        import re
        warnings = []
        
        if re.search(r"\bproject\s*[=!]|project\s+in\s*\(", custom_jql, re.IGNORECASE) and projects:
            warnings.append("Custom JQL contains project filters which may conflict with project options.")
        
        if re.search(r"\bcreated\s*[><=]", custom_jql, re.IGNORECASE) and (since or until):
            warnings.append("Custom JQL contains date filters which may conflict with date options.")
        
        if re.search(r"\bstatus\s*[=!]|status\s+in\s*\(", custom_jql, re.IGNORECASE) and statuses:
            warnings.append("Custom JQL contains status filters which may conflict with status options.")
        
        return warnings
    
    def test_connection(self) -> bool:
        """Test connection to JIRA API.
        
        Returns:
            True if connection successful
        
        Raises:
            RuntimeError: On connection failure
        """
        try:
            response = self.session.get(f"{self.host}/rest/api/3/myself", timeout=10)
            return response.status_code == 200
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to connect to JIRA: {e}")
