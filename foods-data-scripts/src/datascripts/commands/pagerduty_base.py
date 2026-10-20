"""Base PagerDuty command functionality with caching and HTTP client."""

import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

import click
import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.panel import Panel

from ..services.token_manager import TokenManager
from ..services.csv_helper import CsvHelper
from .error_messages import (
    get_pagerduty_auth_error_message,
    get_pagerduty_permission_error_message,
)


class PagerDutyAuthError(Exception):
    """Raised when PagerDuty API authentication fails."""
    pass


def handle_auth_error(console: Optional[Console] = None, subdomain: Optional[str] = None) -> None:
    """Display helpful message when API token is invalid or expired.
    
    Args:
        console: Rich console for formatted output.
        subdomain: PagerDuty subdomain (e.g., 'mycompany' from 'mycompany.pagerduty.com').
    """
    if console is None:
        console = Console()
    
    error_message = get_pagerduty_auth_error_message(subdomain)
    console.print(Panel(error_message, title="🔐 PagerDuty Authentication Error", border_style="red"))


def handle_permission_error(console: Optional[Console] = None, subdomain: Optional[str] = None) -> None:
    """Display helpful message when API token lacks required permissions.
    
    Args:
        console: Rich console for formatted output.
        subdomain: PagerDuty subdomain (e.g., 'mycompany' from 'mycompany.pagerduty.com').
    """
    if console is None:
        console = Console()
    
    error_message = get_pagerduty_permission_error_message(subdomain)
    console.print(Panel(error_message, title="🔒 PagerDuty Permission Error", border_style="yellow"))


class PagerDutyBaseCommand:
    """Base class for PagerDuty commands with common functionality."""
    
    BASE_URL = "https://api.pagerduty.com"
    CACHE_TTL_MINUTES = 1440  # 24 hours
    CACHE_DIR = "pagerduty_cache"
    
    def __init__(self):
        """Initialize base command."""
        self.token: Optional[str] = None
        self.skip_cache: bool = False
        self.session = requests.Session()
        self._cache_dir = Path(tempfile.gettempdir()) / self.CACHE_DIR
        self.subdomain: Optional[str] = None
    
    def set_token(self, token: str) -> None:
        """Set API token and update session headers.
        
        Args:
            token: PagerDuty API token.
        """
        self.token = token
        self.session.headers.update(self._build_headers())
    
    def _extract_subdomain(self, url: str) -> None:
        """Extract and store PagerDuty subdomain from URL.
        
        Args:
            url: URL containing PagerDuty subdomain (e.g., https://mycompany.pagerduty.com/...).
        """
        if not url or self.subdomain:
            return
        
        import re
        match = re.search(r'https?://([^.]+)\.pagerduty\.com', url)
        if match:
            self.subdomain = match.group(1)
    
    def _extract_subdomain_from_response(self, data: dict) -> None:
        """Recursively search for html_url fields to extract subdomain.
        
        Args:
            data: API response data.
        """
        if self.subdomain:
            return
        
        if isinstance(data, dict):
            if 'html_url' in data:
                self._extract_subdomain(data['html_url'])
                return
            for value in data.values():
                if isinstance(value, (dict, list)):
                    self._extract_subdomain_from_response(value)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._extract_subdomain_from_response(item)
                if self.subdomain:
                    return
    
    def validate_token(self, console: Optional[Console] = None) -> bool:
        """Validate the API token by making a test request.
        
        Uses the /abilities endpoint which reliably returns 401 for invalid tokens.
        
        Args:
            console: Rich console for output messages.
        
        Returns:
            True if token is valid, False otherwise.
        
        Raises:
            PagerDutyAuthError: If token is invalid or expired.
        """
        if console is None:
            console = Console()
        
        try:
            response = self.session.request(
                "GET",
                f"{self.BASE_URL}/abilities",
                timeout=10
            )
            
            if response.status_code == 401:
                handle_auth_error(console)
                raise PagerDutyAuthError("Invalid or expired API token")
            
            if response.status_code == 403:
                console.print("[red]Error: Access forbidden. Your API token may not have sufficient permissions.[/red]")
                console.print("[yellow]Ensure your token has read access to the required resources.[/yellow]")
                raise PagerDutyAuthError("Insufficient API token permissions")
            
            if response.status_code != 200:
                console.print(f"[red]Error: Unexpected response from PagerDuty API (status {response.status_code})[/red]")
                return False
            
            return True
            
        except requests.exceptions.RequestException as e:
            console.print(f"[red]Error: Failed to connect to PagerDuty API: {e}[/red]")
            return False
    
    def _build_headers(self) -> Dict[str, str]:
        """Build standard PagerDuty API headers."""
        return {
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Authorization": f"Token token={self.token}",
            "Content-Type": "application/json",
        }
    
    def _generate_cache_key(self, method: str, url: str, params: Optional[Dict] = None) -> str:
        """Generate cache key for request."""
        # Normalize params to ensure consistent cache keys
        normalized_params = {}
        if params:
            for key, value in params.items():
                # Sort list values to ensure consistent cache keys
                if isinstance(value, list):
                    normalized_params[key] = sorted([str(v) for v in value])
                else:
                    normalized_params[key] = value
        
        key_data = {
            "method": method,
            "url": url,
            "params": normalized_params,
        }
        return hashlib.sha256(json.dumps(key_data, sort_keys=True).encode()).hexdigest()
    
    def _get_cache_path(self, cache_key: str) -> Path:
        """Get cache file path for key."""
        return self._cache_dir / f"{cache_key}.json"
    
    def _get_cached_response(self, cache_key: str) -> Optional[Dict]:
        """Get cached response if valid."""
        cache_path = self._get_cache_path(cache_key)
        if not cache_path.exists():
            return None
        
        try:
            with open(cache_path) as f:
                cache_data = json.load(f)
            
            timestamp = cache_data.get("timestamp", 0)
            age = time.time() - timestamp
            
            if age > (self.CACHE_TTL_MINUTES * 60):
                cache_path.unlink()
                return None
            
            return cache_data.get("response")
        except (json.JSONDecodeError, IOError):
            return None
    
    def _cache_response(self, cache_key: str, response_data: Dict) -> None:
        """Cache response to disk."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        
        cache_data = {
            "timestamp": time.time(),
            "response": response_data,
        }
        
        try:
            with open(self._get_cache_path(cache_key), 'w') as f:
                json.dump(cache_data, f)
        except IOError:
            pass  # Silently ignore cache write failures
    
    def make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        silent_auth_errors: bool = False,
        **kwargs
    ) -> requests.Response:
        """Make API request with optional caching.
        
        Args:
            method: HTTP method.
            endpoint: API endpoint (relative or absolute URL).
            params: Query parameters.
            silent_auth_errors: If True, don't display error panels for 403/401 (just raise exception).
            **kwargs: Additional requests arguments.
        
        Returns:
            Response object.
        
        Raises:
            requests.HTTPError: On API errors.
        """
        # Handle absolute URLs
        if endpoint.startswith("http"):
            url = endpoint
        else:
            url = f"{self.BASE_URL}{endpoint}"
        
        cache_key = None
        
        # Check cache for GET requests
        if method.upper() == "GET" and not self.skip_cache:
            cache_key = self._generate_cache_key(method, url, params)
            cached = self._get_cached_response(cache_key)
            if cached is not None:
                # Fallback to API if cached response is empty or has no results
                # Check common result keys for emptiness
                has_results = False
                for key in ['services', 'teams', 'incidents', 'users', 'escalation_policies', 'schedules']:
                    if key in cached and cached[key]:
                        has_results = True
                        break
                
                if has_results:
                    # Create a mock response object
                    response = requests.Response()
                    response.status_code = 200
                    response._content = json.dumps(cached).encode()
                    return response
                # If no results in cache, fall through to make actual API request
        
        # Make actual request
        response = self.session.request(method, url, params=params, timeout=30, **kwargs)
        
        # Extract subdomain from any successful response with html_url
        if response.status_code == 200:
            try:
                data = response.json()
                self._extract_subdomain_from_response(data)
            except (json.JSONDecodeError, KeyError):
                pass
        
        # Check for authentication errors
        if response.status_code == 401:
            if not silent_auth_errors:
                handle_auth_error(subdomain=self.subdomain)
            raise PagerDutyAuthError("Invalid or expired API token")
        
        if response.status_code == 403:
            if not silent_auth_errors:
                handle_permission_error(subdomain=self.subdomain)
            raise PagerDutyAuthError("Insufficient API token permissions - full access token required")
        
        # Cache successful GET responses
        if method.upper() == "GET" and response.status_code == 200 and not self.skip_cache and cache_key:
            try:
                data = response.json()
                self._cache_response(cache_key, data)
            except json.JSONDecodeError:
                pass
        
        return response
    
    def paginated_request(
        self,
        endpoint: str,
        params: Optional[Dict] = None,
        fetch_all: bool = True,
        result_key: str = None
    ) -> List[Dict]:
        """Make paginated API request.
        
        Args:
            endpoint: API endpoint.
            params: Base query parameters.
            fetch_all: Whether to fetch all pages.
            result_key: Key in response containing results array (auto-detected if None).
        
        Returns:
            List of all results.
        """
        params = params or {}
        all_results = []
        offset = 0
        page_size = params.get('limit', 25)
        
        while True:
            query = {**params, "offset": offset}
            
            response = self.make_request("GET", endpoint, params=query)
            response.raise_for_status()
            data = response.json()
            
            # Auto-detect result key if not provided
            if result_key is None:
                # Common PagerDuty result keys
                for key in ['services', 'teams', 'incidents', 'users', 'escalation_policies', 'schedules']:
                    if key in data:
                        result_key = key
                        break
            
            results = data.get(result_key, []) if result_key else []
            
            if results:
                all_results.extend(results)
            
            # Check for more pages
            more = data.get("more", False)
            
            if not fetch_all or not more:
                break
            
            if len(results) < page_size:
                break
            
            offset += page_size
            
            # Small delay to avoid rate limiting
            time.sleep(0.2)
        
        return all_results
    
    def filter_services_by_teams(
        self,
        services: List[Dict],
        team_ids: List[str]
    ) -> List[Dict]:
        """Filter services to only those belonging to specified teams.
        
        Args:
            services: List of service dictionaries.
            team_ids: List of team IDs to filter by.
        
        Returns:
            Filtered list of services.
        """
        if not team_ids:
            return services
        
        filtered = []
        for service in services:
            service_teams = service.get('teams', [])
            if isinstance(service_teams, list):
                for team in service_teams:
                    if isinstance(team, dict) and team.get('id') in team_ids:
                        filtered.append(service)
                        break
        
        return filtered


class PagerDutyClient:
    """HTTP client for PagerDuty API with caching support."""
    
    BASE_URL = "https://api.pagerduty.com"
    CACHE_TTL_MINUTES = 1440  # 24 hours
    CACHE_DIR = "pagerduty_cache"
    DEFAULT_TIMEOUT = 90  # seconds; generous to survive slow/large window queries
    MAX_RETRIES = 3       # retries on read/connection timeouts and 5xx errors
    RETRY_BACKOFF = 2     # seconds; multiplied by attempt number
    
    def __init__(self, token: str, skip_cache: bool = False):
        """Initialize PagerDuty client.
        
        Args:
            token: PagerDuty API token.
            skip_cache: Whether to skip API response cache.
        """
        self.token = token
        self.skip_cache = skip_cache
        self.timeout = self.DEFAULT_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update(self._build_headers())
        self._cache_dir = Path(tempfile.gettempdir()) / self.CACHE_DIR
        self.subdomain: Optional[str] = None
    
    def validate_token(self, console: Optional[Console] = None) -> bool:
        """Validate the API token by making a test request.
        
        Uses the /abilities endpoint which reliably returns 401 for invalid tokens.
        
        Args:
            console: Rich console for output messages.
        
        Returns:
            True if token is valid, False otherwise.
        
        Raises:
            PagerDutyAuthError: If token is invalid or expired.
        """
        if console is None:
            console = Console()
        
        try:
            response = self.session.request(
                "GET",
                f"{self.BASE_URL}/abilities",
                timeout=10
            )
            
            if response.status_code == 401:
                handle_auth_error(console)
                raise PagerDutyAuthError("Invalid or expired API token")
            
            if response.status_code == 403:
                console.print("[red]Error: Access forbidden. Your API token may not have sufficient permissions.[/red]")
                console.print("[yellow]Ensure your token has read access to the required resources.[/yellow]")
                raise PagerDutyAuthError("Insufficient API token permissions")
            
            if response.status_code != 200:
                console.print(f"[red]Error: Unexpected response from PagerDuty API (status {response.status_code})[/red]")
                return False
            
            return True
            
        except requests.exceptions.RequestException as e:
            console.print(f"[red]Error: Failed to connect to PagerDuty API: {e}[/red]")
            return False
    
    def _build_headers(self) -> Dict[str, str]:
        """Build standard PagerDuty API headers."""
        return {
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Authorization": f"Token token={self.token}",
            "Content-Type": "application/json",
        }
    
    def _extract_subdomain(self, url: str) -> None:
        """Extract and store PagerDuty subdomain from URL.
        
        Args:
            url: URL containing PagerDuty subdomain (e.g., https://mycompany.pagerduty.com/...).
        """
        if not url or self.subdomain:
            return
        
        import re
        match = re.search(r'https?://([^.]+)\.pagerduty\.com', url)
        if match:
            self.subdomain = match.group(1)
    
    def _extract_subdomain_from_response(self, data: dict) -> None:
        """Recursively search for html_url fields to extract subdomain.
        
        Args:
            data: API response data.
        """
        if self.subdomain:
            return
        
        if isinstance(data, dict):
            if 'html_url' in data:
                self._extract_subdomain(data['html_url'])
                return
            for value in data.values():
                if isinstance(value, (dict, list)):
                    self._extract_subdomain_from_response(value)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._extract_subdomain_from_response(item)
                if self.subdomain:
                    return

    
    def _generate_cache_key(self, method: str, url: str, params: Optional[Dict] = None) -> str:
        """Generate cache key for request."""
        # Normalize params to ensure consistent cache keys
        normalized_params = {}
        if params:
            for key, value in params.items():
                # Sort list values to ensure consistent cache keys
                if isinstance(value, list):
                    normalized_params[key] = sorted([str(v) for v in value])
                else:
                    normalized_params[key] = value
        
        key_data = {
            "method": method,
            "url": url,
            "params": normalized_params,
        }
        return hashlib.sha256(json.dumps(key_data, sort_keys=True).encode()).hexdigest()
    
    def _get_cache_path(self, cache_key: str) -> Path:
        """Get cache file path for key."""
        return self._cache_dir / f"{cache_key}.json"
    
    def _get_cached_response(self, cache_key: str) -> Optional[Dict]:
        """Get cached response if valid."""
        cache_path = self._get_cache_path(cache_key)
        if not cache_path.exists():
            return None
        
        try:
            with open(cache_path) as f:
                cache_data = json.load(f)
            
            timestamp = cache_data.get("timestamp", 0)
            age = time.time() - timestamp
            
            if age > (self.CACHE_TTL_MINUTES * 60):
                cache_path.unlink()
                return None
            
            return cache_data.get("response")
        except (json.JSONDecodeError, IOError):
            return None
    
    def _cache_response(self, cache_key: str, response_data: Dict) -> None:
        """Cache response to disk."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        
        cache_data = {
            "timestamp": time.time(),
            "response": response_data,
        }
        
        try:
            with open(self._get_cache_path(cache_key), 'w') as f:
                json.dump(cache_data, f)
        except IOError:
            pass  # Silently ignore cache write failures
    
    def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        use_cache: bool = True,
        **kwargs
    ) -> Dict:
        """Make API request with optional caching.
        
        Args:
            method: HTTP method.
            endpoint: API endpoint (relative or absolute URL).
            params: Query parameters.
            use_cache: Set False for volatile queries (e.g. a date window that runs
                up to "now") so a stale cached response is never reused.
            **kwargs: Additional requests arguments.
        
        Returns:
            Response data as dictionary.
        
        Raises:
            requests.HTTPError: On API errors.
        """
        # Handle absolute URLs
        if endpoint.startswith("http"):
            url = endpoint
        else:
            url = f"{self.BASE_URL}{endpoint}"
        
        caching_enabled = use_cache and not self.skip_cache

        # Check cache for GET requests
        cache_key = None
        if method.upper() == "GET" and caching_enabled:
            cache_key = self._generate_cache_key(method, url, params)
            cached = self._get_cached_response(cache_key)
            if cached is not None:
                # Fallback to API if cached response is empty or has no results
                # Check common result keys for emptiness
                has_results = False
                for key in ['services', 'teams', 'incidents', 'users', 'escalation_policies', 'schedules']:
                    if key in cached and cached[key]:
                        has_results = True
                        break
                
                if has_results:
                    return cached
                # If no results in cache, fall through to make actual API request
        
        # Make actual request (with retries on timeouts / transient 5xx errors)
        response = None
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method, url, params=params, timeout=self.timeout, **kwargs
                )
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                if attempt < self.MAX_RETRIES:
                    time.sleep(self.RETRY_BACKOFF * attempt)
                    continue
                raise
            # Retry transient server errors
            if response.status_code >= 500 and attempt < self.MAX_RETRIES:
                time.sleep(self.RETRY_BACKOFF * attempt)
                continue
            break

        if response is None:
            # Should not happen, but guard against it
            raise last_exc if last_exc else RuntimeError("Request failed with no response")
        
        # Check for authentication errors
        if response.status_code == 401:
            handle_auth_error()
            raise PagerDutyAuthError("Invalid or expired API token")
        
        if response.status_code == 403:
            handle_permission_error()
            raise PagerDutyAuthError("Insufficient API token permissions - full access token required")
        
        response.raise_for_status()
        
        data = response.json()
        
        # Cache successful GET responses
        if method.upper() == "GET" and caching_enabled and cache_key:
            self._cache_response(cache_key, data)
        
        return data
    
    def paginated_request(
        self,
        endpoint: str,
        result_key: str,
        params: Optional[Dict] = None,
        limit: int = 0,
        fetch_all: bool = True,
        progress_callback: Optional[Callable[[int], None]] = None
    ) -> List[Dict]:
        """Make paginated API request.
        
        Args:
            endpoint: API endpoint.
            result_key: Key in response containing results array.
            params: Base query parameters.
            limit: Max results (0 = unlimited).
            fetch_all: Whether to fetch all pages.
            progress_callback: Optional callback for progress updates.
        
        Returns:
            List of all results.
        """
        params = params or {}
        all_results = []
        offset = 0
        
        is_unlimited = limit == 0
        page_size = 100 if is_unlimited else min(limit, 100)
        
        if is_unlimited:
            fetch_all = True
        
        page_count = 0
        
        while True:
            page_count += 1
            if progress_callback:
                progress_callback(page_count)
            
            query = {**params, "offset": offset, "limit": page_size}
            
            data = self.request("GET", endpoint, params=query)
            results = data.get(result_key, [])
            
            if results:
                all_results.extend(results)
            
            # Check for more pages
            more = data.get("more", False)
            
            if not fetch_all or not more:
                break
            
            if len(results) < page_size:
                break
            
            offset += page_size
            
            # Small delay to avoid rate limiting
            time.sleep(0.2)
        
        return all_results
    
    def clear_expired_cache(self) -> None:
        """Clear expired cache files."""
        if not self._cache_dir.exists():
            return
        
        now = time.time()
        for cache_file in self._cache_dir.glob("*.json"):
            try:
                with open(cache_file) as f:
                    cache_data = json.load(f)
                
                age = now - cache_data.get("timestamp", 0)
                if age > (self.CACHE_TTL_MINUTES * 60):
                    cache_file.unlink()
            except (json.JSONDecodeError, IOError):
                pass


def resolve_pagerduty_token(
    token: Optional[str] = None,
    reset_token: bool = False,
    verbose: bool = False
) -> Optional[str]:
    """Resolve PagerDuty API token using TokenManager.
    
    Args:
        token: Token from CLI option.
        reset_token: Whether to reset cached token.
        verbose: Whether to show verbose output.
    
    Returns:
        Resolved token or None.
    """
    manager = TokenManager("pagerduty_token.cache")
    return manager.get_token(
        cli_token=token,
        env_vars=["PAGERDUTY_API_TOKEN", "PAGERDUTY_TOKEN"],
        reset=reset_token,
        prompt_text="PagerDuty API token (will be cached; set PAGERDUTY_API_TOKEN env to override)",
        verbose=verbose
    )


def output_csv(rows: List[List[Any]], headers: List[str]) -> None:
    """Output data as CSV to stdout.
    
    Args:
        rows: Data rows.
        headers: Column headers.
    """
    import csv
    
    writer = csv.writer(sys.stdout)
    writer.writerow(headers)
    writer.writerows(rows)


def output_json(data: Any, key: str = "results") -> None:
    """Output data as JSON to stdout.
    
    Args:
        data: Data to output.
        key: Key to wrap data under.
    """
    click.echo(json.dumps({key: data}, indent=2))


def format_duration(seconds: int) -> str:
    """Format duration in human-readable format.
    
    Args:
        seconds: Duration in seconds.
    
    Returns:
        Formatted duration string (e.g., "2d 5h", "3h 30m", "45m 30s").
    """
    if seconds < 60:
        return f"{seconds}s"
    
    minutes = seconds // 60
    if minutes < 60:
        remaining_seconds = seconds % 60
        return f"{minutes}m {remaining_seconds}s" if remaining_seconds else f"{minutes}m"
    
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours < 24:
        return f"{hours}h {remaining_minutes}m" if remaining_minutes else f"{hours}h"
    
    days = hours // 24
    remaining_hours = hours % 24
    return f"{days}d {remaining_hours}h" if remaining_hours else f"{days}d"


def calculate_incident_duration(incident: Dict) -> int:
    """Calculate incident duration in seconds.
    
    Args:
        incident: Incident data dictionary.
    
    Returns:
        Duration in seconds.
    """
    created_at = incident.get("created_at") or incident.get("first_triggered_at")
    resolved_at = incident.get("resolved_at")
    
    if not created_at:
        return 0
    
    if not resolved_at:
        resolved_at = datetime.utcnow().isoformat() + "Z"
    
    try:
        # Parse ISO8601 dates
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        resolved = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
        return max(0, int((resolved - created).total_seconds()))
    except (ValueError, TypeError):
        return 0
