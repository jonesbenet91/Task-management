"""GitHub Tech Stack Analysis Command - analyzes repositories for framework, language, and pipeline info."""

import os
import re
import csv
import json
import time
import base64
import click
import requests
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from datascripts.services.token_manager import TokenManager
from datascripts.services.csv_helper import CsvHelper
from datascripts.services import GITHUB_ORG

console = Console()

class GitHubTechStackAnalyzer:
    """Analyzes GitHub repositories for tech stack information."""
    
    ORG = GITHUB_ORG
    MAX_RETRIES = 3
    RETRY_DELAY = 2  # seconds
    
    def __init__(self, token: str, verbose: bool = False):
        self.token = token
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        })
    
    def _api_call(self, url: str, retry_count: int = 0) -> Optional[requests.Response]:
        """Make GitHub API call with retry logic."""
        try:
            response = self.session.get(url)
            
            # Handle rate limiting
            if response.status_code == 403 and 'rate limit' in response.text.lower():
                if retry_count < self.MAX_RETRIES:
                    wait_time = self.RETRY_DELAY * (2 ** retry_count)
                    if self.verbose:
                        console.print(f"[yellow]Rate limited, waiting {wait_time}s...[/yellow]")
                    time.sleep(wait_time)
                    return self._api_call(url, retry_count + 1)
                return None
            
            # Handle other errors with retry
            if response.status_code >= 500:
                if retry_count < self.MAX_RETRIES:
                    wait_time = self.RETRY_DELAY * (2 ** retry_count)
                    time.sleep(wait_time)
                    return self._api_call(url, retry_count + 1)
                return None
            
            return response
            
        except requests.RequestException as e:
            if retry_count < self.MAX_RETRIES:
                wait_time = self.RETRY_DELAY * (2 ** retry_count)
                time.sleep(wait_time)
                return self._api_call(url, retry_count + 1)
            if self.verbose:
                console.print(f"[red]API call failed: {e}[/red]")
            return None
    
    def _get_file_content(self, repo: str, path: str, sha: str = None) -> Optional[str]:
        """Get content of a file from GitHub."""
        if sha:
            # Get blob directly
            url = f"https://api.github.com/repos/{self.ORG}/{repo}/git/blobs/{sha}"
            response = self._api_call(url)
            if response and response.status_code == 200:
                data = response.json()
                if data.get('encoding') == 'base64':
                    return base64.b64decode(data['content']).decode('utf-8', errors='ignore')
        else:
            # Get via contents API
            url = f"https://api.github.com/repos/{self.ORG}/{repo}/contents/{path}"
            response = self._api_call(url)
            if response and response.status_code == 200:
                data = response.json()
                if isinstance(data, dict) and data.get('encoding') == 'base64':
                    return base64.b64decode(data['content']).decode('utf-8', errors='ignore')
        return None
    
    def _strip_xml_comments(self, content: str) -> str:
        """Remove XML comments from content."""
        return re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)
    
    def _strip_properties_comments(self, content: str) -> str:
        """Remove # and // comments from properties/gradle files."""
        lines = []
        for line in content.split('\n'):
            # Remove inline comments
            line = re.sub(r'^\s*#.*$', '', line)
            line = re.sub(r'^\s*//.*$', '', line)
            if line.strip():
                lines.append(line)
        return '\n'.join(lines)
    
    def _strip_json_comments(self, content: str) -> str:
        """Remove // and /* */ comments from JSON content."""
        # Remove single-line comments
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        # Remove multi-line comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        return content
    
    def _normalize_version(self, version: str) -> str:
        """Normalize version string (strip ^, ~, ranges, etc.)."""
        if not version:
            return "N/A"
        # Strip common prefixes
        version = re.sub(r'^[\^~>=<]+', '', version.strip())
        # Handle ranges - take first version
        version = version.split('-')[0].split(' ')[0]
        # Map Java 1.8 -> 8
        if version == "1.8":
            return "8"
        return version
    
    def analyze_repo(self, repo_name: str) -> Dict[str, str]:
        """Analyze a single repository and return tech stack info."""
        result = {
            'Repo': repo_name,
            'RepoStatus': 'N/A',
            'Framework': 'N/A',
            'Language': 'N/A',
            'Pipeline': 'N/A',
            'DockerImage': 'N/A',
            'RepoType': 'N/A',
            'HasCatalog': 'N/A'
        }
        
        # Rule 1: RepoStatus
        repo_url = f"https://api.github.com/repos/{self.ORG}/{repo_name}"
        repo_response = self._api_call(repo_url)
        
        if not repo_response:
            result['RepoStatus'] = 'NotFound'
            return result
        
        if repo_response.status_code == 404:
            result['RepoStatus'] = 'NotFound'
            return result
        
        if repo_response.status_code != 200:
            result['RepoStatus'] = 'NotFound'
            return result
        
        repo_data = repo_response.json()
        result['RepoStatus'] = 'Archived' if repo_data.get('archived', False) else 'Active'
        
        # Get default branch tree
        default_branch = repo_data.get('default_branch', 'main')
        tree_url = f"https://api.github.com/repos/{self.ORG}/{repo_name}/git/trees/{default_branch}?recursive=1"
        tree_response = self._api_call(tree_url)
        
        if not tree_response or tree_response.status_code != 200:
            return result
        
        tree_data = tree_response.json()
        files = {item['path']: item for item in tree_data.get('tree', [])}
        
        # Rule 2: Framework
        framework_info = self._detect_framework(repo_name, files)
        result['Framework'] = framework_info
        
        # Rule 3: Language
        language_info = self._detect_language(repo_name, files)
        result['Language'] = language_info
        
        # Rule 4: Pipeline
        pipeline_info = self._detect_pipeline(files)
        result['Pipeline'] = pipeline_info
        
        # Rule 5: Docker Image
        docker_image = self._detect_docker_image(repo_name, files)
        result['DockerImage'] = docker_image
        
        # Rule 6: RepoType
        repo_type = self._detect_repo_type(files)
        result['RepoType'] = repo_type
        
        # Rule 7: HasCatalog
        has_catalog = self._detect_catalog(files)
        result['HasCatalog'] = has_catalog
        
        return result
    
    def _detect_framework(self, repo: str, files: Dict) -> str:
        """Detect framework and version (Springboot > Micronaut > Angular)."""
        frameworks = {}
        
        # Check Springboot (any pom.xml)
        pom_files = [path for path in files if path.endswith('pom.xml')]
        if pom_files:
            version = self._get_springboot_version(repo, pom_files, files)
            if version:
                frameworks['Springboot'] = f"Springboot-{version}"
        
        # Check Micronaut (pom.xml, micronaut-cli.yml, OR gradle.properties)
        if pom_files and 'Springboot' not in frameworks:  # Only check if not already Springboot
            version = self._get_micronaut_version(repo, files, pom_files)
            if version:
                frameworks['Micronaut'] = f"Micronaut-{version}"
        elif not pom_files:  # No pom.xml, check gradle-based Micronaut
            micronaut_files = [path for path in files if path.endswith('micronaut-cli.yml')]
            has_gradle_props = 'gradle.properties' in files
            
            if micronaut_files or has_gradle_props:
                version = self._get_micronaut_version(repo, files, [])
                if version:
                    frameworks['Micronaut'] = f"Micronaut-{version}"
        
        # Check Angular (any package.json)
        package_files = [path for path in files if path.endswith('package.json')]
        if package_files:
            version = self._get_angular_version(repo, package_files, files)
            if version:
                frameworks['Angular'] = f"Angular-{version}"
        
        # Priority: Springboot > Micronaut > Angular
        if 'Springboot' in frameworks:
            return frameworks['Springboot']
        if 'Micronaut' in frameworks:
            return frameworks['Micronaut']
        if 'Angular' in frameworks:
            return frameworks['Angular']
        
        return "N/A"
    
    def _get_springboot_version(self, repo: str, pom_files: List[str], files: Dict) -> Optional[str]:
        """Extract Spring Boot version from pom.xml files."""
        for pom_path in pom_files[:3]:  # Check first 3 pom files
            content = self._get_file_content(repo, pom_path, files[pom_path].get('sha'))
            if not content:
                continue
            
            content = self._strip_xml_comments(content)
            
            # Check <parent><artifactId>spring-boot-starter-parent</artifactId><version>X</version>
            match = re.search(
                r'<parent>.*?<artifactId>spring-boot-starter-parent</artifactId>.*?<version>(.*?)</version>.*?</parent>',
                content, re.DOTALL
            )
            if match:
                return self._normalize_version(match.group(1))
            
            # Check <dependencyManagement>...<artifactId>spring-boot-dependencies</artifactId><version>X</version>
            match = re.search(
                r'<dependencyManagement>.*?<artifactId>spring-boot-dependencies</artifactId>.*?<version>(.*?)</version>',
                content, re.DOTALL
            )
            if match:
                return self._normalize_version(match.group(1))
            
            # Check <plugin><artifactId>spring-boot-maven-plugin</artifactId><version>X</version>
            match = re.search(
                r'<artifactId>spring-boot-maven-plugin</artifactId>.*?<version>(.*?)</version>',
                content, re.DOTALL
            )
            if match:
                return self._normalize_version(match.group(1))
        
        return None
    
    def _get_micronaut_version(self, repo: str, files: Dict, pom_files: List[str] = None) -> Optional[str]:
        """Extract Micronaut version from pom.xml, gradle.properties, or build.gradle."""
        # Check pom.xml for Micronaut parent first
        if pom_files:
            for pom_path in pom_files[:3]:  # Check first 3 pom files
                content = self._get_file_content(repo, pom_path, files[pom_path].get('sha'))
                if not content:
                    continue
                
                content = self._strip_xml_comments(content)
                
                # Check <parent><groupId>io.micronaut.platform</groupId><artifactId>micronaut-parent</artifactId><version>X</version>
                match = re.search(
                    r'<parent>.*?<groupId>io\.micronaut\.platform</groupId>.*?<artifactId>micronaut-parent</artifactId>.*?<version>(.*?)</version>.*?</parent>',
                    content, re.DOTALL
                )
                if match:
                    return self._normalize_version(match.group(1))
        
        # Check gradle.properties
        if 'gradle.properties' in files:
            content = self._get_file_content(repo, 'gradle.properties', files['gradle.properties'].get('sha'))
            if content:
                content = self._strip_properties_comments(content)
                # Check for various Micronaut version property patterns
                patterns = [
                    r'micronautVersion\s*=\s*([^\s\r\n]+)',
                    r'micronaut\.version\s*=\s*([^\s\r\n]+)',
                    r'micronautFrameworkVersion\s*=\s*([^\s\r\n]+)',
                    r'micronaut-version\s*=\s*([^\s\r\n]+)'
                ]
                for pattern in patterns:
                    match = re.search(pattern, content, re.IGNORECASE)
                    if match:
                        version = match.group(1).strip()
                        # Remove any trailing quotes or special characters
                        version = version.strip('\'"')
                        if version:
                            return self._normalize_version(version)
        
        # Check all gradle files in the repository
        gradle_files = [path for path in files if path.endswith(('build.gradle', 'build.gradle.kts'))]
        for gradle_file in gradle_files:
            content = self._get_file_content(repo, gradle_file, files[gradle_file].get('sha'))
            if content:
                content = self._strip_properties_comments(content)
                
                # Pattern 1: id("io.micronaut.application") version "X.X.X"
                match = re.search(r'id\s*\(\s*["\']io\.micronaut\.[^"\']+["\']\s*\)\s+version\s+["\'](\d+\.\d+\.\d+)["\']', content)
                if match:
                    return self._normalize_version(match.group(1))
                
                # Pattern 2: io.micronaut:micronaut-bom:X.X.X
                match = re.search(r'io\.micronaut:micronaut-(?:bom|platform)[:\'](\d+\.\d+\.\d+)', content)
                if match:
                    return self._normalize_version(match.group(1))
                
                # Pattern 3: micronaut { version "X.X.X" }
                match = re.search(r'micronaut\s*\{[^}]*version\s+["\'](\d+\.\d+\.\d+)["\']', content, re.DOTALL)
                if match:
                    return self._normalize_version(match.group(1))
                
                # Pattern 4: General micronaut version reference
                match = re.search(r'micronaut[^"\']*["\'](\d+\.\d+\.\d+)["\']', content, re.IGNORECASE)
                if match:
                    return self._normalize_version(match.group(1))
        
        return None
    
    def _get_angular_version(self, repo: str, package_files: List[str], files: Dict) -> Optional[str]:
        """Extract Angular version from package.json files."""
        for pkg_path in package_files[:3]:  # Check first 3 package.json files
            content = self._get_file_content(repo, pkg_path, files[pkg_path].get('sha'))
            if not content:
                continue
            
            content = self._strip_json_comments(content)
            
            try:
                pkg_data = json.loads(content)
                
                # Check dependencies["@angular/core"]
                if '@angular/core' in pkg_data.get('dependencies', {}):
                    return self._normalize_version(pkg_data['dependencies']['@angular/core'])
                
                # Check devDependencies["@angular/core"]
                if '@angular/core' in pkg_data.get('devDependencies', {}):
                    return self._normalize_version(pkg_data['devDependencies']['@angular/core'])
                
                # Fallback to @angular/cli
                if '@angular/cli' in pkg_data.get('devDependencies', {}):
                    return self._normalize_version(pkg_data['devDependencies']['@angular/cli'])
            except json.JSONDecodeError:
                continue
        
        return None
    
    def _detect_language(self, repo: str, files: Dict) -> str:
        """Detect language and version (Java > Typescript)."""
        languages = {}
        
        # Check Java
        pom_files = [path for path in files if path.endswith('pom.xml')]
        gradle_files = [path for path in files if path in ['build.gradle', 'build.gradle.kts']]
        
        if pom_files or gradle_files:
            version = self._get_java_version(repo, pom_files, gradle_files, files)
            languages['Java'] = f"Java-{version}" if version else "Java-N/A"
        
        # Check Typescript
        package_files = [path for path in files if path.endswith('package.json')]
        if package_files:
            version = self._get_typescript_version(repo, package_files, files)
            if version:
                languages['Typescript'] = f"Typescript-{version}"
        
        # Priority: Java > Typescript
        if 'Java' in languages:
            return languages['Java']
        if 'Typescript' in languages:
            return languages['Typescript']
        
        return "N/A"
    
    def _get_java_version(self, repo: str, pom_files: List[str], gradle_files: List[str], files: Dict) -> Optional[str]:
        """Extract Java version from Maven or Gradle files."""
        # Check Maven (pom.xml)
        for pom_path in pom_files[:3]:
            content = self._get_file_content(repo, pom_path, files[pom_path].get('sha'))
            if not content:
                continue
            
            content = self._strip_xml_comments(content)
            
            # Order of precedence for Maven
            patterns = [
                r'<maven\.compiler\.release>(.*?)</maven\.compiler\.release>',
                r'<java\.version>(.*?)</java\.version>',
                r'<jdk\.version>(.*?)</jdk\.version>',
                r'<maven\.compiler\.target>(.*?)</maven\.compiler\.target>',
                r'<target>(.*?)</target>',
                r'<release>(.*?)</release>'
            ]
            
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    return self._normalize_version(match.group(1))
        
        # Check Gradle
        for gradle_file in gradle_files:
            content = self._get_file_content(repo, gradle_file, files[gradle_file].get('sha'))
            if not content:
                continue
            
            content = self._strip_properties_comments(content)
            
            # Check toolchain
            match = re.search(r'languageVersion\s*=\s*JavaLanguageVersion\.of\((\d+)\)', content)
            if match:
                return self._normalize_version(match.group(1))
            
            # Check sourceCompatibility with JavaVersion.toVersion()
            match = re.search(r'sourceCompatibility\s*=\s*JavaVersion\.toVersion\(["\']?(\d+(?:\.\d+)?)["\']?\)', content)
            if match:
                return self._normalize_version(match.group(1))
            
            # Check sourceCompatibility (standard)
            match = re.search(r'sourceCompatibility\s*=\s*["\']?(\d+(?:\.\d+)?)["\']?', content)
            if match:
                return self._normalize_version(match.group(1))
            
            # Check targetCompatibility with JavaVersion.toVersion()
            match = re.search(r'targetCompatibility\s*=\s*JavaVersion\.toVersion\(["\']?(\d+(?:\.\d+)?)["\']?\)', content)
            if match:
                return self._normalize_version(match.group(1))
            
            # Check targetCompatibility (standard)
            match = re.search(r'targetCompatibility\s*=\s*["\']?(\d+(?:\.\d+)?)["\']?', content)
            if match:
                return self._normalize_version(match.group(1))
        
        return None
    
    def _get_typescript_version(self, repo: str, package_files: List[str], files: Dict) -> Optional[str]:
        """Extract Typescript version from package.json files."""
        for pkg_path in package_files[:3]:
            content = self._get_file_content(repo, pkg_path, files[pkg_path].get('sha'))
            if not content:
                continue
            
            content = self._strip_json_comments(content)
            
            try:
                pkg_data = json.loads(content)
                
                # Check devDependencies["typescript"]
                if 'typescript' in pkg_data.get('devDependencies', {}):
                    return self._normalize_version(pkg_data['devDependencies']['typescript'])
                
                # Check dependencies["typescript"]
                if 'typescript' in pkg_data.get('dependencies', {}):
                    return self._normalize_version(pkg_data['dependencies']['typescript'])
            except json.JSONDecodeError:
                continue
        
        return None
    
    def _detect_pipeline(self, files: Dict) -> str:
        """Detect pipeline type (AzureDevOps, GitHubActions, Mixed, N/A)."""
        # Check for Azure DevOps pipeline files (with or without 's')
        has_azure = any(path in ['azure-pipelines.yml', 'azure-pipelines.yaml', 
                                  'azure-pipeline.yml', 'azure-pipeline.yaml'] 
                       for path in files)
        has_github = any(path.startswith('.github/workflows/') and path.endswith(('.yml', '.yaml')) 
                        for path in files)
        
        if has_azure and has_github:
            return 'Mixed'
        elif has_azure:
            return 'AzureDevOps'
        elif has_github:
            return 'GitHubActions'
        else:
            return 'N/A'
    
    def _detect_docker_image(self, repo: str, files: Dict) -> str:
        """Detect Docker base image from Dockerfile."""
        # Find Dockerfile (case insensitive)
        dockerfile_path = None
        for path in files:
            if path.lower() == 'dockerfile' or path.lower().endswith('/dockerfile'):
                dockerfile_path = path
                break
        
        if not dockerfile_path:
            return 'N/A'
        
        # Get Dockerfile content
        content = self._get_file_content(repo, dockerfile_path, files[dockerfile_path].get('sha'))
        if not content:
            return 'N/A'
        
        # Parse FROM statement (avoid commented lines)
        for line in content.split('\n'):
            stripped_line = line.strip()
            # Skip empty lines and comments
            if not stripped_line or stripped_line.startswith('#'):
                continue
            
            # Check for FROM statement (case insensitive)
            if stripped_line.upper().startswith('FROM '):
                # Extract image name (everything after FROM)
                image = stripped_line[5:].strip()
                # Remove AS alias if present
                if ' AS ' in image.upper():
                    image = image.split(' AS ')[0].split(' as ')[0].strip()
                # Remove inline comments
                if '#' in image:
                    image = image.split('#')[0].strip()
                return image if image else 'N/A'
        
        return 'N/A'
    
    def _detect_repo_type(self, files: Dict) -> str:
        """Detect if repository is Monorepo or Polyrepo."""
        # Check for monorepo indicators
        monorepo_indicators = 0
        
        # 1. Check for monorepo config files
        monorepo_configs = [
            'lerna.json',
            'nx.json',
            'pnpm-workspace.yaml',
            'rush.json',
            'turbo.json'
        ]
        if any(config in files for config in monorepo_configs):
            return 'Monorepo'
        
        # 2. Count package.json files (multiple in different directories suggests monorepo)
        package_json_files = [path for path in files if path.endswith('package.json')]
        if len(package_json_files) > 1:
            # Check if they're in different directories (not just nested node_modules)
            unique_dirs = set()
            for path in package_json_files:
                if 'node_modules' not in path:
                    dir_path = '/'.join(path.split('/')[:-1]) if '/' in path else ''
                    unique_dirs.add(dir_path)
            if len(unique_dirs) > 1:
                monorepo_indicators += 1
        
        # 3. Count pom.xml files (multiple in different directories suggests monorepo)
        pom_files = [path for path in files if path.endswith('pom.xml')]
        if len(pom_files) > 1:
            # Check if they're in different directories
            unique_dirs = set()
            for path in pom_files:
                dir_path = '/'.join(path.split('/')[:-1]) if '/' in path else ''
                unique_dirs.add(dir_path)
            if len(unique_dirs) > 1:
                monorepo_indicators += 1
        
        # 4. Check for typical monorepo directory structures
        monorepo_patterns = ['packages/', 'apps/', 'services/', 'modules/', 'libs/']
        dirs_with_pattern = 0
        for pattern in monorepo_patterns:
            if any(path.startswith(pattern) for path in files):
                dirs_with_pattern += 1
        
        if dirs_with_pattern >= 2:  # Multiple typical monorepo directories
            monorepo_indicators += 1
        
        # Decision: If 2+ indicators, it's likely a monorepo
        if monorepo_indicators >= 2:
            return 'Monorepo'
        
        # Default to Polyrepo
        return 'Polyrepo'
    
    def _detect_catalog(self, files: Dict) -> str:
        """Detect if catalog-info.yaml or catalog-info.yml exists."""
        has_catalog = any(path in ['catalog-info.yaml', 'catalog-info.yml'] for path in files)
        return 'Yes' if has_catalog else 'No'


def _update_repos_csv(csv_path: Path, results: List[Dict], verbose: bool, console: Console):
    """Update the repos CSV file with analysis results."""
    import csv as csv_module
    from datetime import datetime
    
    try:
        # Read existing CSV
        with open(csv_path, 'r', encoding='utf-8') as f:
            csv_reader = csv_module.reader(f)
            rows = list(csv_reader)
        
        if not rows:
            console.print("[red]CSV file is empty, cannot update[/red]")
            return
        
        # Find column indices
        header = rows[0]
        repo_name_idx = None
        repo_status_idx = None
        framework_idx = None
        language_idx = None
        pipeline_idx = None
        docker_image_idx = None
        repo_type_idx = None
        has_catalog_idx = None
        
        # Find existing columns (case-insensitive)
        for idx, col in enumerate(header):
            col_lower = col.lower().strip()
            if col_lower in ['reponame', 'repo name', 'repository']:
                repo_name_idx = idx
            elif col_lower in ['repostatus', 'repo status', 'status']:
                repo_status_idx = idx
            elif col_lower == 'framework':
                framework_idx = idx
            elif col_lower == 'language':
                language_idx = idx
            elif col_lower == 'pipeline':
                pipeline_idx = idx
            elif col_lower in ['docker image', 'dockerimage']:
                docker_image_idx = idx
            elif col_lower in ['repotype', 'repo type']:
                repo_type_idx = idx
            elif col_lower in ['hascatalog', 'has catalog']:
                has_catalog_idx = idx
        
        # Add missing columns to header
        if repo_status_idx is None:
            repo_status_idx = len(header)
            header.append('RepoStatus')
        if framework_idx is None:
            framework_idx = len(header)
            header.append('Framework')
        if language_idx is None:
            language_idx = len(header)
            header.append('Language')
        if pipeline_idx is None:
            pipeline_idx = len(header)
            header.append('Pipeline')
        if docker_image_idx is None:
            docker_image_idx = len(header)
            header.append('Docker Image')
        if repo_type_idx is None:
            repo_type_idx = len(header)
            header.append('RepoType')
        if has_catalog_idx is None:
            has_catalog_idx = len(header)
            header.append('HasCatalog')
        
        # Ensure all rows have enough columns
        max_cols = len(header)
        for i in range(len(rows)):
            while len(rows[i]) < max_cols:
                rows[i].append('')
        
        # Create lookup dict for results
        results_dict = {result['Repo']: result for result in results}
        
        # Update data rows
        updated_count = 0
        for i in range(1, len(rows)):
            if repo_name_idx is not None and len(rows[i]) > repo_name_idx:
                repo_name = rows[i][repo_name_idx].strip().strip('"')
                # Remove org prefix if present
                if '/' in repo_name:
                    repo_name = repo_name.split('/')[-1]
                
                if repo_name in results_dict:
                    result = results_dict[repo_name]
                    rows[i][repo_status_idx] = result['RepoStatus']
                    rows[i][framework_idx] = result['Framework']
                    rows[i][language_idx] = result['Language']
                    rows[i][pipeline_idx] = result['Pipeline']
                    rows[i][docker_image_idx] = result['DockerImage']
                    rows[i][repo_type_idx] = result['RepoType']
                    rows[i][has_catalog_idx] = result['HasCatalog']
                    updated_count += 1
        
        # Write updated CSV
        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            csv_writer = csv_module.writer(f)
            csv_writer.writerows(rows)
        
        console.print(f"\n[green]✓ Updated {updated_count} repositories in {csv_path}[/green]")
        
    except Exception as e:
        console.print(f"[red]Error updating CSV: {e}[/red]")


@click.command()
@click.option('--repos-csv', 
              default='data/repos.csv',
              help='Path to repos CSV file (default: data/repos.csv)')
@click.option('--token', 
              envvar='GITHUB_TOKEN',
              help='GitHub API token (reads from GITHUB_TOKEN env var)')
@click.option('--reset-token', 
              is_flag=True, 
              help='Clear cached token and prompt for new one')
@click.option('--update-csv', 
              is_flag=True, 
              help='Update the repos CSV file with analysis results')
@click.option('--verbose', 
              is_flag=True, 
              help='Show detailed progress information')
def techstack(repos_csv, token, reset_token, update_csv, verbose):
    """Analyze GitHub repositories for tech stack information (framework, language, pipeline)."""
    
    # Validate CSV file
    csv_path = Path(repos_csv)
    if not csv_path.exists():
        raise click.ClickException(f"CSV file not found: {repos_csv}")
    
    # Get GitHub token
    token_manager = TokenManager('github_token.cache')
    github_token = token_manager.get_token(
        cli_token=token,
        env_vars=['GITHUB_TOKEN'],
        reset=reset_token,
        prompt_text="GitHub API token (will be cached)",
        verbose=verbose
    )
    
    if not github_token:
        raise click.ClickException("GitHub token is required")
    
    # Parse CSV to get repo names
    repos = []
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        csv_reader = csv.reader(f)
        rows = list(csv_reader)
    
    # Skip header if present (check first row)
    start_idx = 0
    if rows and len(rows[0]) >= 2:
        # Check if second column contains header keywords
        second_col = rows[0][1].lower()
        if any(header in second_col for header in ['repository', 'repo', 'reponame']):
            start_idx = 1
    
    for row in rows[start_idx:]:
        if len(row) >= 2 and row[1]:  # 2nd column (index 1)
            repo_name = row[1].strip().strip('"')
            # Remove org prefix if present
            if '/' in repo_name:
                repo_name = repo_name.split('/')[-1]
            if repo_name:  # Only add non-empty names
                repos.append(repo_name)
    
    if not repos:
        raise click.ClickException("No repositories found in CSV")
    
    if verbose:
        console.print(f"[cyan]Found {len(repos)} repositories to analyze[/cyan]")
    
    # Analyze repositories
    analyzer = GitHubTechStackAnalyzer(github_token, verbose=verbose)
    results = []
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Analyzing repositories...", total=len(repos))
        
        for repo in repos:
            progress.update(task, description=f"Analyzing {repo}...")
            result = analyzer.analyze_repo(repo)
            results.append(result)
            progress.advance(task)
    
    # Output results as table
    table = Table(show_header=True, header_style="bold cyan", show_lines=False)
    table.add_column("Repo", style="white", width=20)
    table.add_column("RepoStatus", width=10)
    table.add_column("Framework", style="green", width=16)
    table.add_column("Language", style="blue", width=10)
    table.add_column("Pipeline", style="magenta", width=12)
    table.add_column("Docker Image", style="cyan", width=28)
    table.add_column("RepoType", width=10)
    table.add_column("HasCatalog", width=11)
    
    for result in results:
        # Get status value - use plain text without markup
        status = result.get('RepoStatus', 'N/A')
        if not status:  # Handle empty string
            status = 'N/A'
        
        table.add_row(
            result.get('Repo', 'N/A'),
            status,  # Plain text, no markup
            result.get('Framework', 'N/A'),
            result.get('Language', 'N/A'),
            result.get('Pipeline', 'N/A'),
            result.get('DockerImage', 'N/A'),
            result.get('RepoType', 'N/A'),
            result.get('HasCatalog', 'N/A')
        )
    
    console.print(table)
    
    if verbose:
        console.print(f"\n[cyan]Analyzed {len(results)} repositories[/cyan]")
    
    # Update CSV if requested
    if update_csv:
        _update_repos_csv(csv_path, results, verbose, console)
