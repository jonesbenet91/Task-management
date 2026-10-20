"""CSV utilities for reading and writing data files."""

import csv
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from rich.console import Console

_console = Console()


def write_csv(
    rows: Sequence[dict],
    fieldnames: Sequence[str],
    output_path: str,
    *,
    label: str = "rows",
) -> Path:
    """Write a list of dicts to a CSV file.

    Creates parent directories if needed, writes a UTF-8 CSV with CRLF
    line endings (Excel-friendly), and prints a summary line.

    Args:
        rows: Data rows to write.
        fieldnames: Ordered column names for the CSV header.
        output_path: Destination file path.
        label: Human-readable name for the rows (used in the summary).

    Returns:
        The resolved Path of the written file.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, lineterminator="\r\n", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    _console.print(f"[green]Wrote {len(rows)} {label} to {path}[/green]")
    return path

class CsvHelper:
    """Helper class for parsing CSV files with flexible header matching."""
    
    @staticmethod
    def parse_projects_csv(path: str) -> Dict[str, str]:
        """Parse a projects CSV file and return map projectKey => teamName.
        
        Accepts headers like "JIRA Board Name/Key", "JIRA Project Name", "Team Name".
        
        Args:
            path: Path to the CSV file.
        
        Returns:
            Dictionary mapping project keys to team names.
        """
        result: Dict[str, str] = {}
        file_path = Path(path)
        
        if not file_path.exists() or not file_path.is_file():
            return result
        
        try:
            with open(file_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return result
                
                headers = [h.strip() for h in reader.fieldnames]
                lower_headers = {h.lower(): h for h in headers}
                
                for row in reader:
                    # Skip empty rows
                    if not any(v.strip() for v in row.values() if v):
                        continue
                    
                    # Clean row values
                    row = {k: (v.strip() if v else '') for k, v in row.items()}
                    
                    # Find team name
                    team = (
                        row.get('Team Name') or
                        row.get('team name') or
                        row.get('Team') or
                        'Unknown'
                    )
                    
                    # Find project key
                    proj = (
                        row.get('JIRA Board Name/Key') or
                        row.get('JIRA Project Name')
                    )
                    
                    if not proj:
                        # Try case-insensitive header lookup
                        for header, original in lower_headers.items():
                            if header in ['jira board name/key', 'jira board name', 'board key', 'project key']:
                                proj = row.get(original)
                                if proj:
                                    break
                            elif header in ['jira project name', 'project name'] and not proj:
                                proj = row.get(original)
                    
                    if proj:
                        result[proj.strip()] = team or 'Unknown'
        
        except (IOError, csv.Error):
            pass
        
        return result
    
    @staticmethod
    def parse_teams_csv(path: str) -> List[str]:
        """Parse teams CSV to extract team ids or names.
        
        Used by PagerDuty flows. Accepts headers like id, team_id, name.
        
        Args:
            path: Path to the CSV file.
        
        Returns:
            List of unique team IDs or names.
        """
        result: List[str] = []
        file_path = Path(path)
        
        if not file_path.exists() or not file_path.is_file():
            return result
        
        try:
            with open(file_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return result
                
                headers = [h.strip() for h in reader.fieldnames]
                lower_headers = {h.lower(): h for h in headers}
                
                for row in reader:
                    # Skip empty rows
                    if not any(v.strip() for v in row.values() if v):
                        continue
                    
                    # Clean row values
                    row = {k: (v.strip() if v else '') for k, v in row.items()}
                    
                    # Find team ID
                    team_id = row.get('id') or row.get('team_id')
                    
                    if not team_id:
                        # Try case-insensitive header lookup
                        for header, original in lower_headers.items():
                            if header in ['id', 'team_id', 'teamid']:
                                team_id = row.get(original)
                                if team_id:
                                    break
                            elif header in ['name', 'team', 'team_name']:
                                team_id = row.get(original)
                    
                    if team_id:
                        result.append(team_id)
        
        except (IOError, csv.Error):
            pass
        
        # Return unique values while preserving order
        seen = set()
        unique_result = []
        for item in result:
            if item not in seen:
                seen.add(item)
                unique_result.append(item)
        
        return unique_result
    
    @staticmethod
    def parse_teams_csv_with_names(path: str) -> List[Dict[str, str]]:
        """Parse teams CSV to extract team id and name pairs.
        
        Args:
            path: Path to the CSV file.
        
        Returns:
            List of dictionaries with 'id' and 'name' keys.
        """
        result: List[Dict[str, str]] = []
        file_path = Path(path)
        
        if not file_path.exists() or not file_path.is_file():
            return result
        
        try:
            with open(file_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return result
                
                headers = [h.strip() for h in reader.fieldnames]
                lower_headers = {h.lower(): h for h in headers}
                
                for row in reader:
                    # Skip empty rows
                    if not any(v.strip() for v in row.values() if v):
                        continue
                    
                    # Clean row values
                    row = {k: (v.strip() if v else '') for k, v in row.items()}
                    
                    # Find team ID
                    team_id = row.get('id') or row.get('team_id')
                    if not team_id:
                        for header, original in lower_headers.items():
                            if header in ['id', 'team_id', 'teamid']:
                                team_id = row.get(original)
                                if team_id:
                                    break
                    
                    # Find team name
                    team_name = row.get('name') or row.get('team_name') or row.get('Team Name')
                    if not team_name:
                        for header, original in lower_headers.items():
                            if header in ['name', 'team', 'team_name', 'team name']:
                                team_name = row.get(original)
                                if team_name:
                                    break
                    
                    if team_id:
                        result.append({
                            'id': team_id,
                            'name': team_name or team_id
                        })
        
        except (IOError, csv.Error):
            pass
        
        return result
    
    @staticmethod
    def parse_repos_csv(path: str, repo_col: str = "Repository URL", team_col: str = "Product Name") -> List[Dict[str, str]]:
        """Parse a repositories CSV file.
        
        Args:
            path: Path to the CSV file.
            repo_col: Column name for repository URL.
            team_col: Column name for team/product name.
        
        Returns:
            List of dictionaries with repository info.
        """
        result: List[Dict[str, str]] = []
        file_path = Path(path)
        
        if not file_path.exists() or not file_path.is_file():
            return result
        
        try:
            with open(file_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return result
                
                for row in reader:
                    # Skip empty rows
                    if not any(v.strip() for v in row.values() if v):
                        continue
                    
                    # Clean row values
                    row = {k: (v.strip() if v else '') for k, v in row.items()}
                    
                    if row.get(repo_col):
                        result.append(row)
        
        except (IOError, csv.Error):
            pass
        
        return result
