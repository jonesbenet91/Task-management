"""Token management service for API authentication with caching."""

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional, List

import click


class TokenManager:
    """Manage API tokens with caching and environment variable support.
    
    Token resolution priority:
    1. Environment variables (first found)
    2. CLI-provided token
    3. Cached token
    4. Interactive prompt (will cache)
    """
    
    def __init__(self, cache_filename: str = "token.cache"):
        """Initialize TokenManager with cache file path.
        
        Args:
            cache_filename: Name of the cache file in temp directory.
        """
        self.cache_file = Path(tempfile.gettempdir()) / cache_filename
    
    def get_token(
        self,
        cli_token: Optional[str] = None,
        env_vars: Optional[List[str]] = None,
        reset: bool = False,
        prompt_text: str = "API token (will be cached)",
        verbose: bool = False
    ) -> Optional[str]:
        """Resolve token with fallback chain.
        
        Args:
            cli_token: Token passed via CLI option.
            env_vars: List of environment variable names to check.
            reset: Whether to remove cached token.
            prompt_text: Text to show when prompting for token.
            verbose: Whether to show verbose output.
        
        Returns:
            Resolved token or None if not found and no prompt.
        """
        env_vars = env_vars or ["TOKEN", "API_TOKEN"]
        
        # Handle reset
        if reset and self.cache_file.exists():
            self.cache_file.unlink()
            if verbose:
                click.echo(click.style("Cached token removed.", fg="yellow"))
        
        # Check environment variables
        for env_var in env_vars:
            value = os.environ.get(env_var)
            if value:
                if verbose:
                    click.echo(f"Using token from environment variable {env_var}.")
                return value
        
        # Check CLI token
        if cli_token:
            if verbose:
                click.echo("Using token provided via --token.")
            return cli_token
        
        # Check cached token
        cached = self._read_cached_token()
        if cached:
            if verbose:
                click.echo("Using cached token.")
            return cached
        
        # Interactive prompt
        if sys.stdin.isatty():
            token = click.prompt(
                prompt_text,
                hide_input=True,
                default="",
                show_default=False
            )
            if token:
                self._write_cached_token(token)
                click.echo(click.style("Token cached for future runs.", fg="green"))
                return token
        
        return None
    
    @property
    def cache_path(self) -> Path:
        """Get the cache file path."""
        return self.cache_file
    
    def _read_cached_token(self) -> Optional[str]:
        """Read token from cache file."""
        if not self.cache_file.exists():
            return None
        try:
            content = self.cache_file.read_text().strip()
            return content if content else None
        except (IOError, OSError):
            return None
    
    def _write_cached_token(self, token: str) -> None:
        """Write token to cache file with restricted permissions."""
        try:
            self.cache_file.write_text(token)
            self.cache_file.chmod(0o600)
        except (IOError, OSError):
            pass  # Silently fail cache writes
