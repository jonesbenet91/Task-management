"""PagerDuty standards compliance command."""

import click
from datetime import datetime, timedelta
from typing import Any, Optional
from rich.console import Console
from rich.table import Table
from rich.progress import Progress
from tabulate import tabulate

from .pagerduty_base import PagerDutyBaseCommand, PagerDutyAuthError
from ..services import TokenManager, CsvHelper


# Standards definitions with weights
STANDARDS = {
    # Basic Service Configuration
    'escalation_policy': {
        'name': 'Has Escalation Policy',
        'description': 'Service must have an escalation policy configured',
        'weight': 1.0,
    },
    'integrations': {
        'name': 'Has Integrations',
        'description': 'Service should have at least one integration configured',
        'weight': 0.8,
    },
    'description': {
        'name': 'Has Description',
        'description': 'Service should have a meaningful description',
        'weight': 0.6,
    },
    'alert_grouping': {
        'name': 'Alert Grouping Configured',
        'description': 'Service should have alert grouping configured for noise reduction',
        'weight': 0.7,
    },
    'status_page': {
        'name': 'Status Page Integration',
        'description': 'Service should be linked to a status page component',
        'weight': 0.5,
    },
    
    # On-Call and Escalation Standards
    'primary_oncall': {
        'name': 'Primary On-Call Setup',
        'description': 'Escalation policy must have a clear primary on-call responder (not whole team)',
        'weight': 1.0,
    },
    'secondary_oncall': {
        'name': 'Secondary On-Call Setup',
        'description': 'Escalation policy should have a secondary on-call backup responder',
        'weight': 0.9,
    },
    'escalation_levels': {
        'name': 'Multi-Level Escalation',
        'description': 'Escalation policy must have more than 2 levels (minimum 3: primary, secondary, management)',
        'weight': 1.0,
    },
    'response_time': {
        'name': 'Response Time Configured',
        'description': 'Escalation policy should have appropriate response times (3-5 minutes)',
        'weight': 0.7,
    },
    
    # Schedule and Team Management
    'schedule_coverage': {
        'name': 'Schedule Coverage',
        'description': 'Service should have proper schedule coverage without gaps',
        'weight': 0.8,
    },
    'no_duplicate_oncall': {
        'name': 'No Duplicate On-Call',
        'description': 'Same person should not be on both primary and secondary at same time',
        'weight': 0.9,
    },
    'active_users_only': {
        'name': 'No Inactive Users',
        'description': 'All users in schedules and escalation policies should be active',
        'weight': 1.0,
    },
    'no_permanent_oncall': {
        'name': 'No Permanent On-Call',
        'description': 'Primary escalation level should use schedules for rotation, not permanent user assignments',
        'weight': 0.9,
    },
    
    # Notification and Response Standards
    'notification_rules': {
        'name': 'Notification Rules Configured',
        'description': 'On-call users should have proper notification rules (app, SMS, phone)',
        'weight': 0.8,
    },
    'immediate_assignment': {
        'name': 'Auto-Assignment Configured',
        'description': 'Incidents should be automatically assigned to on-call responder',
        'weight': 0.6,
    },
    
    # Escalation Efficiency Standards
    'max_escalation_levels': {
        'name': 'Maximum 5 Escalation Levels',
        'description': 'An incident that has more levels of escalation will cause delays in resolution',
        'weight': 0.8,
    },
    'unique_escalation_users': {
        'name': 'Unique Users Per Escalation Level',
        'description': 'Escalation must be handled by a different person at each level',
        'weight': 0.9,
    },
    'sam_clement_top_level': {
        'name': 'Sam Clement at Top Escalation Level',
        'description': 'Sam Clement must be included in the final escalation level',
        'weight': 1.0,
    },
}


class PagerDutyStandardsCommand(PagerDutyBaseCommand):
    """Analyze PagerDuty services for standards compliance."""
    
    def __init__(self):
        super().__init__()
        self.schedule_cache: dict[str, Any] = {}
        self.escalation_policy_cache: dict[str, Any] = {}
        self.user_cache: dict[str, Any] = {}
    
    def get_escalation_policy(self, policy_id: str) -> Optional[dict]:
        """Get escalation policy details with caching."""
        if policy_id in self.escalation_policy_cache:
            return self.escalation_policy_cache[policy_id]
        
        try:
            response = self.make_request(
                'GET',
                f'https://api.pagerduty.com/escalation_policies/{policy_id}'
            )
            
            if response.status_code == 200:
                data = response.json()
                policy = data.get('escalation_policy')
                if policy:
                    self.escalation_policy_cache[policy_id] = policy
                    return policy
        except PagerDutyAuthError:
            raise  # Re-raise auth errors to stop execution
        except Exception:
            pass
        
        return None
    
    def get_schedule(self, schedule_id: str) -> Optional[dict]:
        """Get schedule details with coverage analysis."""
        if schedule_id in self.schedule_cache:
            return self.schedule_cache[schedule_id]
        
        try:
            # Get schedule with entries for the next 30 days
            # Note: The /schedules/{id}/entries endpoint is deprecated
            # Using /schedules/{id} with since/until params instead
            since = datetime.utcnow().isoformat() + 'Z'
            until = (datetime.utcnow() + timedelta(days=30)).isoformat() + 'Z'
            
            response = self.make_request(
                'GET',
                f'https://api.pagerduty.com/schedules/{schedule_id}',
                params={
                    'time_zone': 'UTC',
                    'since': since,
                    'until': until
                }
            )
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            schedule = data.get('schedule')
            
            if not schedule:
                return None
            
            # Extract entries from the schedule response
            # The API returns final_schedule.rendered_schedule_entries
            final_schedule = schedule.get('final_schedule', {})
            entries = final_schedule.get('rendered_schedule_entries', [])
            
            # Store entries directly in schedule for backward compatibility
            schedule['entries'] = entries
            
            # Analyze coverage gaps
            schedule['coverage_analysis'] = self._analyze_schedule_coverage(
                schedule['entries'], since, until
            )
            
            self.schedule_cache[schedule_id] = schedule
            return schedule
            
        except PagerDutyAuthError:
            raise  # Re-raise auth errors to stop execution
        except Exception:
            return None
    
    def _analyze_schedule_coverage(self, entries: list, since: str, until: str) -> dict:
        """Analyze schedule entries for coverage gaps."""
        if not entries:
            return {
                'has_coverage': False,
                'coverage_percentage': 0.0,
                'gaps_count': 1,
                'gaps': [{'start': since, 'end': until}],
            }
        
        # Sort entries by start time
        sorted_entries = sorted(entries, key=lambda x: x.get('start', ''))
        
        from dateutil import parser
        
        start_time = parser.isoparse(since.replace('Z', '+00:00'))
        end_time = parser.isoparse(until.replace('Z', '+00:00'))
        total_period = (end_time - start_time).total_seconds()
        
        gaps = []
        covered_time = 0
        current_time = start_time
        
        for entry in sorted_entries:
            entry_start = parser.isoparse(entry['start'])
            entry_end = parser.isoparse(entry['end'])
            
            # Check for gap before this entry
            if entry_start > current_time:
                gap_duration = (entry_start - current_time).total_seconds() / 3600
                gaps.append({
                    'start': current_time.isoformat(),
                    'end': entry_start.isoformat(),
                    'duration_hours': gap_duration,
                })
            
            # Calculate covered time
            coverage_start = max(current_time, entry_start)
            coverage_end = min(end_time, entry_end)
            
            if coverage_end > coverage_start:
                covered_time += (coverage_end - coverage_start).total_seconds()
            
            current_time = max(current_time, entry_end)
        
        # Check for gap after last entry
        if current_time < end_time:
            gap_duration = (end_time - current_time).total_seconds() / 3600
            gaps.append({
                'start': current_time.isoformat(),
                'end': end_time.isoformat(),
                'duration_hours': gap_duration,
            })
        
        coverage_percentage = (covered_time / total_period * 100) if total_period > 0 else 0
        
        return {
            'has_coverage': coverage_percentage > 95,
            'coverage_percentage': coverage_percentage,
            'gaps_count': len(gaps),
            'gaps': gaps,
            'entries_count': len(entries),
        }
    
    def get_service_schedules(self, service: dict) -> dict[str, Any]:
        """Get schedules for a service from its escalation policy."""
        ep_id = service.get('escalation_policy', {}).get('id')
        if not ep_id:
            return {}
        
        policy = self.get_escalation_policy(ep_id)
        if not policy:
            return {}
        
        schedules = {}
        rules = policy.get('escalation_rules', [])
        
        for rule in rules:
            targets = rule.get('targets', [])
            for target in targets:
                if target.get('type') == 'schedule_reference':
                    schedule_id = target.get('id')
                    schedule = self.get_schedule(schedule_id)
                    if schedule:
                        schedules[schedule_id] = schedule
        
        return schedules
    
    def get_user_details(self, user_id: str) -> Optional[dict]:
        """Get user details with activity check."""
        if user_id in self.user_cache:
            return self.user_cache[user_id]
        
        try:
            response = self.make_request(
                'GET',
                f'https://api.pagerduty.com/users/{user_id}'
            )
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            user = data.get('user')
            
            if not user:
                return None
            
            # Try to get user sessions (requires admin permissions)
            # This is optional - if we can't access it, we'll skip session checks
            try:
                sessions_response = self.make_request(
                    'GET',
                    f'https://api.pagerduty.com/users/{user_id}/sessions',
                    params={'limit': 10},
                    silent_auth_errors=True  # Don't show error panel for this optional endpoint
                )
                
                if sessions_response.status_code == 200:
                    sessions_data = sessions_response.json()
                    sessions = sessions_data.get('user_sessions', [])
                    
                    # Find most recent session
                    last_session = None
                    for session in sessions:
                        if not last_session:
                            last_session = session
                        elif session.get('created_at', '') > last_session.get('created_at', ''):
                            last_session = session
                    
                    user['last_session'] = last_session
                    user['is_recently_active'] = self._is_user_recently_active(last_session)
                else:
                    user['last_session'] = None
                    user['is_recently_active'] = None
            except PagerDutyAuthError:
                # Sessions endpoint requires admin permissions that user tokens may not have
                # This is non-critical, so we'll skip it and continue
                user['last_session'] = None
                user['is_recently_active'] = None
            except Exception:
                user['last_session'] = None
                user['is_recently_active'] = None
            
            self.user_cache[user_id] = user
            return user
            
        except PagerDutyAuthError:
            raise  # Re-raise auth errors to stop execution
        except Exception:
            return None
    
    def _is_user_recently_active(self, last_session: Optional[dict]) -> Optional[bool]:
        """Check if user is recently active based on last session."""
        if not last_session or 'created_at' not in last_session:
            return None
        
        from dateutil import parser
        
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        last_session_time = parser.isoparse(last_session['created_at'])
        
        # Make timezone-aware comparison
        if last_session_time.tzinfo:
            thirty_days_ago = thirty_days_ago.replace(tzinfo=last_session_time.tzinfo)
        
        return last_session_time > thirty_days_ago
    
    def extract_users_from_escalation_policy(self, policy: dict) -> list[dict]:
        """Extract all users from escalation policy rules."""
        all_users = []
        rules = policy.get('escalation_rules', [])
        
        for rule_index, rule in enumerate(rules):
            targets = rule.get('targets', [])
            for target in targets:
                if target.get('type') == 'user_reference':
                    all_users.append({
                        'user_id': target.get('id'),
                        'rule_index': rule_index,
                        'escalation_level': rule_index + 1,
                    })
                elif target.get('type') == 'schedule_reference':
                    schedule_id = target.get('id')
                    schedule = self.get_schedule(schedule_id)
                    if schedule:
                        schedule_users = self._extract_users_from_schedule(schedule, rule_index)
                        all_users.extend(schedule_users)
        
        return all_users
    
    def _extract_users_from_schedule(self, schedule: dict, rule_index: int) -> list[dict]:
        """Extract users from schedule entries."""
        users = []
        entries = schedule.get('entries', [])
        seen_keys = set()
        
        for entry in entries:
            user = entry.get('user')
            if user and user.get('id'):
                key = f"{user['id']}_{rule_index}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    users.append({
                        'user_id': user['id'],
                        'rule_index': rule_index,
                        'escalation_level': rule_index + 1,
                        'schedule_id': schedule.get('id'),
                    })
        
        return users
    
    def get_user_notification_rules(self, user_id: str) -> list:
        """Get user notification rules."""
        try:
            response = self.make_request(
                'GET',
                f'https://api.pagerduty.com/users/{user_id}/notification_rules',
                silent_auth_errors=True  # Don't show error panel - this is checked per-user
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get('notification_rules', [])
        except PagerDutyAuthError:
            # Notification rules may not be accessible with user token permissions
            # This is non-critical for some standards checks
            pass
        except Exception:
            pass
        
        return []
    
    def has_proper_notification_setup(self, notification_rules: list) -> bool:
        """Check if notification rules provide proper coverage."""
        if not notification_rules:
            return False
        
        notification_types: dict[str, list] = {}
        active_rules_count = 0
        
        for rule in notification_rules:
            contact_method = rule.get('contact_method')
            if not contact_method:
                continue
            
            # Skip disabled contact methods
            if contact_method.get('enabled') is False:
                continue
            
            method_type = contact_method.get('type')
            delay = rule.get('start_delay_in_minutes', 0)
            
            if method_type:
                active_rules_count += 1
                if method_type not in notification_types:
                    notification_types[method_type] = []
                notification_types[method_type].append({
                    'delay': delay,
                    'rule': rule,
                    'contact_method': contact_method,
                })
        
        if active_rules_count == 0:
            return False
        
        # Check for reasonable notification timing (within 5 minutes)
        has_reasonable = False
        for rules in notification_types.values():
            for rule_info in rules:
                if rule_info['delay'] <= 5:
                    has_reasonable = True
                    break
            if has_reasonable:
                break
        
        if not has_reasonable:
            return False
        
        # Check for reliable notification types
        reliable_types = [
            'email_contact_method',
            'phone_contact_method',
            'sms_contact_method',
            'push_notification_contact_method'
        ]
        
        has_reliable = any(t in notification_types for t in reliable_types)
        if not has_reliable:
            return False
        
        # Prefer multiple types or multiple rules for redundancy
        if len(notification_types) < 2:
            total_rules = sum(len(rules) for rules in notification_types.values())
            if total_rules < 2:
                return False
        
        return True
    
    def schedule_has_rotation(self, schedule: Optional[dict]) -> bool:
        """Check if a schedule provides actual rotation."""
        if not schedule:
            return False
        
        entries = schedule.get('entries', [])
        if not entries:
            return False
        
        # Check for multiple different users
        unique_user_ids = set()
        for entry in entries:
            user = entry.get('user')
            if user and user.get('id'):
                unique_user_ids.add(user['id'])
        
        if len(unique_user_ids) <= 1:
            return False
        
        # Check for reasonable rotation durations
        from dateutil import parser
        
        for entry in entries:
            if 'start' in entry and 'end' in entry:
                start = parser.isoparse(entry['start'])
                end = parser.isoparse(entry['end'])
                duration_hours = (end - start).total_seconds() / 3600
                
                # Consider 7 days or less as reasonable
                if duration_hours <= (7 * 24):
                    return True
        
        return False
    
    def check_standard(self, service: dict, standard_key: str) -> bool:
        """Check if a service meets a specific standard."""
        try:
            return self._check_standard_impl(service, standard_key)
        except PagerDutyAuthError:
            raise SystemExit(1)
    
    def _check_standard_impl(self, service: dict, standard_key: str) -> bool:
        """Internal implementation of standard checking."""
        
        if standard_key == 'escalation_policy':
            ep = service.get('escalation_policy', {})
            return bool(ep.get('id') or ep.get('summary'))
        
        elif standard_key == 'integrations':
            integrations = service.get('integrations', [])
            if isinstance(integrations, list) and len(integrations) > 0:
                return True
            return bool(service.get('integration_email'))
        
        elif standard_key == 'description':
            desc = (service.get('description') or '').strip()
            return len(desc) > 10
        
        elif standard_key == 'alert_grouping':
            return bool(service.get('alert_grouping_parameters'))
        
        elif standard_key == 'status_page':
            status = service.get('status')
            return bool(status) and status != 'disabled'
        
        elif standard_key == 'primary_oncall':
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            rules = policy.get('escalation_rules', [])
            if not rules:
                return False
            
            first_rule = rules[0]
            targets = first_rule.get('targets', [])
            return len(targets) > 0
        
        elif standard_key == 'secondary_oncall':
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            rules = policy.get('escalation_rules', [])
            return len(rules) >= 2
        
        elif standard_key == 'escalation_levels':
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            rules = policy.get('escalation_rules', [])
            return len(rules) >= 3
        
        elif standard_key == 'response_time':
            # First check if schedule coverage passes - fail if schedule coverage fails
            if not self.check_standard(service, 'schedule_coverage'):
                return False
            
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            rules = policy.get('escalation_rules', [])
            if not rules:
                return False
            
            first_rule = rules[0]
            delay = first_rule.get('escalation_delay_in_minutes', 0)
            return delay <= 5
        
        elif standard_key == 'schedule_coverage':
            schedules = self.get_service_schedules(service)
            if not schedules:
                return False
            
            for schedule in schedules.values():
                coverage = schedule.get('coverage_analysis', {})
                if not coverage.get('has_coverage'):
                    return False
            
            return True
        
        elif standard_key == 'no_duplicate_oncall':
            # First check if schedule coverage passes - fail if schedule coverage fails
            if not self.check_standard(service, 'schedule_coverage'):
                return False
            
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            all_users = self.extract_users_from_escalation_policy(policy)
            if not all_users:
                return True
            
            # Group by level
            users_by_level: dict[int, set] = {}
            for user_info in all_users:
                level = user_info['escalation_level']
                if level not in users_by_level:
                    users_by_level[level] = set()
                users_by_level[level].add(user_info['user_id'])
            
            primary_users = users_by_level.get(1, set())
            secondary_users = users_by_level.get(2, set())
            
            if not secondary_users:
                return True
            
            duplicate_users = primary_users & secondary_users
            return len(duplicate_users) == 0
        
        elif standard_key == 'active_users_only':
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            all_users = self.extract_users_from_escalation_policy(policy)
            if not all_users:
                return True
            
            unique_user_ids = set(u['user_id'] for u in all_users)
            
            for user_id in unique_user_ids:
                user_details = self.get_user_details(user_id)
                
                if not user_details:
                    return False
                
                if not user_details.get('role'):
                    return False
                
                if user_details.get('type') != 'user':
                    return False
                
                # Check activity
                is_active = user_details.get('is_recently_active')
                if is_active is False:
                    last_session = user_details.get('last_session')
                    if last_session and 'created_at' in last_session:
                        from dateutil import parser
                        ninety_days_ago = datetime.utcnow() - timedelta(days=90)
                        session_time = parser.isoparse(last_session['created_at'])
                        if session_time.tzinfo:
                            ninety_days_ago = ninety_days_ago.replace(tzinfo=session_time.tzinfo)
                        if session_time < ninety_days_ago:
                            return False
            
            return True
        
        elif standard_key == 'no_permanent_oncall':
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            rules = policy.get('escalation_rules', [])
            if not rules:
                return False
            
            primary_rule = rules[0]
            targets = primary_rule.get('targets', [])
            if not targets:
                return False
            
            has_direct_users = False
            has_schedules = False
            
            for target in targets:
                target_type = target.get('type')
                
                if target_type == 'user_reference':
                    has_direct_users = True
                elif target_type == 'schedule_reference':
                    has_schedules = True
                    schedule_id = target.get('id')
                    if schedule_id:
                        schedule = self.get_schedule(schedule_id)
                        if not self.schedule_has_rotation(schedule):
                            has_direct_users = True
            
            if has_direct_users and not has_schedules:
                return False
            
            return True
        
        elif standard_key == 'notification_rules':
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            all_users = self.extract_users_from_escalation_policy(policy)
            if not all_users:
                return True
            
            unique_user_ids = set(u['user_id'] for u in all_users)
            
            for user_id in unique_user_ids:
                rules = self.get_user_notification_rules(user_id)
                if not self.has_proper_notification_setup(rules):
                    return False
            
            return True
        
        elif standard_key == 'immediate_assignment':
            # Placeholder - would need incident assignment configuration
            return True
        
        elif standard_key == 'max_escalation_levels':
            # Check that there are no more than 5 escalation levels
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            rules = policy.get('escalation_rules', [])
            # Pass if 5 or fewer levels, fail if more than 5
            return len(rules) <= 5
        
        elif standard_key == 'unique_escalation_users':
            # Check that no user is on-call at multiple escalation levels at the same time
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            # Build a list of user time windows per escalation level
            # Each entry: {user_id, level, start, end}
            user_time_windows: list[dict] = []
            rules = policy.get('escalation_rules', [])
            
            for rule_index, rule in enumerate(rules):
                level = rule_index + 1
                targets = rule.get('targets', [])
                
                for target in targets:
                    if target.get('type') == 'user_reference':
                        # Direct user assignment - they're always on-call at this level
                        user_time_windows.append({
                            'user_id': target.get('id'),
                            'level': level,
                            'start': None,  # None means always
                            'end': None,
                        })
                    elif target.get('type') == 'schedule_reference':
                        schedule_id = target.get('id')
                        schedule = self.get_schedule(schedule_id)
                        if schedule:
                            entries = schedule.get('entries', [])
                            for entry in entries:
                                user = entry.get('user')
                                if user and user.get('id'):
                                    user_time_windows.append({
                                        'user_id': user['id'],
                                        'level': level,
                                        'start': entry.get('start'),
                                        'end': entry.get('end'),
                                    })
            
            if not user_time_windows:
                return True  # No users to check
            
            # Group by user_id
            from collections import defaultdict
            user_windows: dict[str, list[dict]] = defaultdict(list)
            for window in user_time_windows:
                user_windows[window['user_id']].append(window)
            
            # Check each user for overlapping time windows at different levels
            from dateutil import parser
            
            for _user_id, windows in user_windows.items():
                # Get unique levels for this user
                levels = set(w['level'] for w in windows)
                if len(levels) <= 1:
                    continue  # User only at one level, no conflict possible
                
                # Check for overlapping time windows at different levels
                for i, w1 in enumerate(windows):
                    for w2 in windows[i + 1:]:
                        if w1['level'] == w2['level']:
                            continue  # Same level, not a conflict
                        
                        # Check for time overlap
                        # If either has None (always on-call), it's a conflict
                        if w1['start'] is None or w2['start'] is None:
                            return False  # Permanent assignment at multiple levels
                        
                        # Parse times and check overlap
                        w1_start = parser.isoparse(w1['start'])
                        w1_end = parser.isoparse(w1['end'])
                        w2_start = parser.isoparse(w2['start'])
                        w2_end = parser.isoparse(w2['end'])
                        
                        # Check if windows overlap
                        if w1_start < w2_end and w2_start < w1_end:
                            return False  # Overlapping time at different levels
            
            return True
        
        elif standard_key == 'sam_clement_top_level':
            # Check that Sam Clement is in the top (final) escalation level
            ep_id = service.get('escalation_policy', {}).get('id')
            if not ep_id:
                return False
            
            policy = self.get_escalation_policy(ep_id)
            if not policy:
                return False
            
            rules = policy.get('escalation_rules', [])
            if not rules:
                return False
            
            # Get the top (final) escalation level
            top_level_rule = rules[-1]
            targets = top_level_rule.get('targets', [])
            
            if not targets:
                return False
            
            # Check all targets in the top level
            sam_clement_found = False
            
            for target in targets:
                if target.get('type') == 'user_reference':
                    user_id = target.get('id')
                    if user_id:
                        user = self.get_user_details(user_id)
                        if user:
                            user_name = user.get('name', '').lower()
                            if 'sam clement' in user_name or 'sam.clement' in user_name.replace(' ', '.'):
                                sam_clement_found = True
                                break
                
                elif target.get('type') == 'schedule_reference':
                    # Check if Sam Clement is in the schedule
                    schedule_id = target.get('id')
                    schedule = self.get_schedule(schedule_id)
                    if schedule:
                        entries = schedule.get('entries', [])
                        for entry in entries:
                            user = entry.get('user')
                            if user:
                                user_name = user.get('summary', '').lower()
                                if 'sam clement' in user_name or 'sam.clement' in user_name.replace(' ', '.'):
                                    sam_clement_found = True
                                    break
                                # Also check by fetching full user details
                                user_id = user.get('id')
                                if user_id:
                                    user_details = self.get_user_details(user_id)
                                    if user_details:
                                        user_name = user_details.get('name', '').lower()
                                        if 'sam clement' in user_name or 'sam.clement' in user_name.replace(' ', '.'):
                                            sam_clement_found = True
                                            break
                        
                        if sam_clement_found:
                            break
            
            return sam_clement_found
        
        return False
    
    def analyze_standards(
        self,
        services: list[dict],
        standards_to_check: dict[str, dict],
        console: Console
    ) -> list[dict]:
        """Analyze all services against the specified standards."""
        results = []
        
        with Progress(console=console) as progress:
            task = progress.add_task("Analyzing standards...", total=len(services))
            
            for service in services:
                service_id = service.get('id', 'unknown')
                service_name = service.get('name') or service.get('summary', 'Unknown Service')
                
                compliance = {}
                total_weight = 0
                earned_score = 0
                
                for key, standard in standards_to_check.items():
                    passed = self.check_standard(service, key)
                    compliance[key] = {
                        'passed': passed,
                        'name': standard['name'],
                        'description': standard['description'],
                        'weight': standard['weight'],
                    }
                    
                    total_weight += standard['weight']
                    if passed:
                        earned_score += standard['weight']
                
                overall_score = earned_score / total_weight if total_weight > 0 else 0
                
                results.append({
                    'id': service_id,
                    'name': service_name,
                    'url': f"https://{self.subdomain}.pagerduty.com/services/{service_id}" if self.subdomain else f"https://app.pagerduty.com/services/{service_id}",
                    'overall_score': overall_score,
                    'compliance': compliance,
                    'passed_count': sum(1 for c in compliance.values() if c['passed']),
                    'total_count': len(compliance),
                })
                
                progress.advance(task)
        
        # Sort by score (lowest first)
        results.sort(key=lambda x: x['overall_score'])
        
        return results
    
    def get_compliance_status(self, score: float) -> str:
        """Get status emoji and text for a compliance score."""
        if score >= 0.9:
            return '🟢 Excellent'
        elif score >= 0.7:
            return '🟡 Good'
        elif score >= 0.5:
            return '🟠 Needs Work'
        return '🔴 Poor'
    
    def display_summary_stats(self, analysis: list[dict], console: Console) -> None:
        """Display overall summary statistics."""
        total = len(analysis)
        if total == 0:
            return
        
        scores = [r['overall_score'] for r in analysis]
        avg_score = sum(scores) / len(scores)
        
        excellent = sum(1 for s in scores if s >= 0.9)
        good = sum(1 for s in scores if 0.7 <= s < 0.9)
        needs_work = sum(1 for s in scores if 0.5 <= s < 0.7)
        poor = sum(1 for s in scores if s < 0.5)
        
        table = Table(title="Standards Compliance Overview")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        
        table.add_row("Total Services", str(total))
        table.add_row("Average Score", f"{avg_score * 100:.1f}%")
        table.add_row("Excellent (≥90%)", f"{excellent} ({excellent / total * 100:.1f}%)")
        table.add_row("Good (70-89%)", f"{good} ({good / total * 100:.1f}%)")
        table.add_row("Needs Work (50-69%)", f"{needs_work} ({needs_work / total * 100:.1f}%)")
        table.add_row("Poor (<50%)", f"{poor} ({poor / total * 100:.1f}%)")
        
        console.print(table)
    
    def display_summary_results(self, results: list[dict], console: Console) -> None:
        """Display summary table of results."""
        table = Table(title="Service Standards Compliance")
        table.add_column("Service Name", style="cyan")
        table.add_column("Score", justify="right")
        table.add_column("Status")
        table.add_column("Passed", justify="right")
        
        for result in results:
            score_pct = f"{result['overall_score'] * 100:.1f}%"
            status = self.get_compliance_status(result['overall_score'])
            passed_ratio = f"{result['passed_count']}/{result['total_count']}"
            
            table.add_row(
                result['name'],
                score_pct,
                status,
                passed_ratio
            )
        
        console.print(table)
    
    def display_detailed_results(self, results: list[dict], console: Console) -> None:
        """Display detailed breakdown per service."""
        for result in results:
            score_pct = f"{result['overall_score'] * 100:.1f}%"
            status = self.get_compliance_status(result['overall_score'])
            
            console.print(f"\n[bold cyan]{result['name']}[/bold cyan] - {score_pct} ({status})")
            console.print(f"URL: {result['url']}")
            
            table = Table()
            table.add_column("Standard")
            table.add_column("Status")
            table.add_column("Description")
            
            for _key, standard in result['compliance'].items():
                status_text = "✅ Pass" if standard['passed'] else "❌ Fail"
                table.add_row(
                    standard['name'],
                    status_text,
                    standard['description']
                )
            
            console.print(table)


@click.command('standards')
@click.option('--token', help='PagerDuty API token')
@click.option('--reset-token', is_flag=True, help='Reset cached token')
@click.option('--name', help='Filter services by name')
@click.option('--team', multiple=True, help='Filter by team ID (can be used multiple times)')
@click.option('--teams-csv', help='Path to CSV file with team IDs and names')
@click.option('--limit', default=25, help='Number of results per page (0 for unlimited)')
@click.option('--all', 'fetch_all', is_flag=True, help='Fetch all results (pagination)')
@click.option('--skip-cache', is_flag=True, help='Skip API cache')
@click.option('--min-score', default=0.0, type=float, help='Minimum compliance score to display (0.0-1.0)')
@click.option('--failing-only', is_flag=True, help='Show only services that fail standards (score < 0.7)')
@click.option('--standard', multiple=True, help='Specific standards to check')
@click.option('--detailed', is_flag=True, help='Show detailed compliance breakdown per service')
@click.option('--summary-only', is_flag=True, help='Show only overall summary statistics')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.option('--csv', 'output_csv', is_flag=True, help='Output as CSV to outputs/csv/pagerduty_standard.csv')
@click.option('--csv-path', default=None, help='Path for CSV output file')
def standards_command(
    token: Optional[str],
    reset_token: bool,
    name: Optional[str],
    team: tuple,
    teams_csv: Optional[str],
    limit: int,
    fetch_all: bool,
    skip_cache: bool,
    min_score: float,
    failing_only: bool,
    standard: tuple,
    detailed: bool,
    summary_only: bool,
    output_json: bool,
    output_csv: bool,
    csv_path: Optional[str],
) -> None:
    """Analyze PagerDuty services for standards compliance and best practices."""
    console = Console()
    
    # Get token
    token_manager = TokenManager('pagerduty_token.cache')
    api_token = token_manager.get_token(
        token,
        ['PAGERDUTY_API_TOKEN', 'PAGERDUTY_TOKEN'],
        reset_token,
        console
    )
    
    if not api_token:
        console.print("[red]Error: No API token provided[/red]")
        raise SystemExit(1)
    
    cmd = PagerDutyStandardsCommand()
    cmd.set_token(api_token)
    cmd.skip_cache = skip_cache
    
    # Validate token before proceeding
    console.print("[cyan]Validating API token...[/cyan]")
    try:
        cmd.validate_token(console)
        console.print("[green]Token validated successfully[/green]\n")
    except Exception:
        raise SystemExit(1)
    
    # Parse teams from CSV if provided
    teams_data = []
    team_ids = list(team)
    
    if teams_csv:
        console.print(f"[cyan]Loading teams from {teams_csv}...[/cyan]")
        teams_data = CsvHelper.parse_teams_csv_with_names(teams_csv)
        team_ids.extend([t['id'] for t in teams_data])
        console.print(f"[green]Loaded {len(teams_data)} teams from CSV[/green]")
    
    # Create team ID to name mapping
    _team_id_to_name = {t['id']: t['name'] for t in teams_data}  # noqa: F841
    
    # Determine which standards to check
    standards_to_check = STANDARDS.copy()
    if standard:
        standards_to_check = {k: v for k, v in STANDARDS.items() if k in standard}
        if not standards_to_check:
            console.print("[red]No valid standards specified[/red]")
            raise SystemExit(1)
    
    # Process each team or all services
    all_analysis = []
    
    if teams_csv and teams_data:
        # Process each team separately
        console.print(f"[cyan]Processing {len(teams_data)} teams...[/cyan]\n")
        
        for team_info in teams_data:
            team_id = team_info['id']
            team_name = team_info['name']
            
            console.print(f"[cyan]Fetching services for team: {team_name} ({team_id})[/cyan]")
            
            # Build query for services - use server-side team filtering
            query_params = {
                'limit': limit if limit > 0 else 100,
                'team_ids[]': [team_id],
                'include[]': 'teams'
            }
            if name:
                query_params['query'] = name
            
            try:
                services = cmd.paginated_request('/services', query_params, fetch_all)
                
                # No need to filter - API already filtered by team
                if not services:
                    console.print(f"[yellow]  No services found for team {team_name}[/yellow]")
                    continue
                
                console.print(f"[green]  Found {len(services)} services[/green]")
                
                # Analyze standards for this team's services
                analysis = cmd.analyze_standards(services, standards_to_check, console)
                
                # Add team info to each result
                for result in analysis:
                    result['team_id'] = team_id
                    result['team_name'] = team_name
                
                all_analysis.extend(analysis)
                
            except PagerDutyAuthError:
                # Permission errors should stop execution
                raise SystemExit(1)
            except Exception as e:
                console.print(f"[red]  Error processing team {team_name}: {e}[/red]")
                continue
    else:
        # Process all services (original behavior)
        query_params = {'limit': limit if limit > 0 else 25}
        if name:
            query_params['query'] = name
        
        # Use server-side team filtering if team IDs provided
        if team_ids:
            query_params['team_ids[]'] = team_ids
            query_params['include[]'] = 'teams'
        
        console.print("[cyan]Fetching services for analysis...[/cyan]")
        
        try:
            services = cmd.paginated_request('/services', query_params, fetch_all)
        except PagerDutyAuthError:
            # Permission errors should stop execution
            raise SystemExit(1)
        except Exception as e:
            console.print(f"[red]Error fetching services: {e}[/red]")
            raise SystemExit(1)
        
        # No need for client-side filtering when using API's team_ids[] parameter
        if not services:
            console.print("[yellow]No services found.[/yellow]")
            return
        
        console.print(f"[green]Found {len(services)} services to analyze[/green]")
        
        # Analyze standards
        all_analysis = cmd.analyze_standards(services, standards_to_check, console)
        
        # Add team info to results - extract from service teams data if available
        for result in all_analysis:
            # Find the corresponding service to get team data
            service = next((s for s in services if s.get('id') == result.get('id')), None)
            if service and service.get('teams'):
                # Get the first team name (services can have multiple teams)
                teams = service.get('teams', [])
                if teams and len(teams) > 0:
                    result['team_name'] = teams[0].get('summary', '') or teams[0].get('name', '')
                else:
                    result['team_name'] = ''
            else:
                result['team_name'] = ''
            result['team_id'] = ''
    
    if not all_analysis:
        console.print("[yellow]No services to analyze.[/yellow]")
        return
    if not all_analysis:
        console.print("[yellow]No services to analyze.[/yellow]")
        return
    
    # Filter results
    if failing_only:
        all_analysis = [r for r in all_analysis if r['overall_score'] < 0.7]
    elif min_score > 0:
        all_analysis = [r for r in all_analysis if r['overall_score'] >= min_score]
    
    if not all_analysis:
        console.print("[green]No services match the specified criteria.[/green]")
        return
    
    # Handle output formats
    if output_json:
        import json
        print(json.dumps(all_analysis, indent=2))
        return
    
    if output_csv or csv_path:
        import csv as csv_module
        
        # Build CSV data with team_name as first column
        headers = [
            'team_name', 'service_name', 'overall_score', 'status',
            'passed_count', 'total_count', 'url'
        ]
        
        # Add standard columns in the order defined in STANDARDS
        if all_analysis:
            # Use STANDARDS order to ensure consistent column order
            for key in STANDARDS.keys():
                if key in all_analysis[0]['compliance']:
                    headers.append(f"{all_analysis[0]['compliance'][key]['name']} (Pass/Fail)")
        
        rows = []
        for result in all_analysis:
            row = [
                result.get('team_name', ''),
                result['name'],
                f"{result['overall_score']:.3f}",
                cmd.get_compliance_status(result['overall_score']),
                result['passed_count'],
                result['total_count'],
                result['url'],
            ]
            
            # Add standards in same order as headers
            for key in STANDARDS.keys():
                if key in result['compliance']:
                    row.append('Pass' if result['compliance'][key]['passed'] else 'Fail')
            
            rows.append(row)
        
        # Determine output path: explicit csv-path, or default for --csv flag
        output_path = csv_path if csv_path else 'outputs/csv/pagerduty_standard.csv'
        
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv_module.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        
        console.print(f"[green]CSV exported to {output_path}[/green]")
        
        # Don't return if not using --csv flag, show summary too
        if output_csv:
            return
    
    # Display results
    cmd.display_summary_stats(all_analysis, console)
    
    if summary_only:
        return
    
    if detailed:
        cmd.display_detailed_results(all_analysis, console)
    else:
        cmd.display_summary_results(all_analysis, console)
