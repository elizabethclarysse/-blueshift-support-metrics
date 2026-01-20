#!/usr/bin/env python3
"""
Zendesk Support Metrics Dashboard
Auto-updating web dashboard for Zendesk support metrics
"""

from flask import Flask, render_template, jsonify
import requests
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import statistics
import os
from functools import lru_cache
import time

app = Flask(__name__)

# Zendesk API credentials
ZENDESK_SUBDOMAIN = "blueshiftsuccess"
ZENDESK_EMAIL = "elizabeth@getblueshift.com"
ZENDESK_API_TOKEN = "sUTVfNmGxrk0S5ZbkqRJ0Uk4NoScEgTYZyIf3zaW"

# Custom field IDs
CUSTOM_PRIORITY_FIELD_ID = 12111292502931
BLUESHIFT_FEATURE_FIELD_ID = 77333748
RESOLUTION_TYPE_FIELD_ID = 13566867224979

# Elizabeth's user ID to exclude
ELIZABETH_USER_ID = 24222567847

# Blueshift org IDs to exclude from top clients
BLUESHIFT_ORG_IDS = [362903768, 21165487894291]  # Add known Blueshift org IDs

# Cache settings (5 minutes)
CACHE_TIMEOUT = 300
cache = {}

class ZendeskDashboard:
    def __init__(self):
        self.base_url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
        self.auth = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)
        self.agent_cache = {}

    def make_request(self, endpoint, params=None):
        """Make API request to Zendesk"""
        url = f"{self.base_url}/{endpoint}"
        try:
            response = requests.get(url, auth=self.auth, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error making request: {e}")
            return None

    def get_cached_data(self, cache_key, fetch_function, *args):
        """Get data from cache or fetch if expired"""
        now = time.time()
        if cache_key in cache:
            data, timestamp = cache[cache_key]
            if now - timestamp < CACHE_TIMEOUT:
                return data

        # Fetch fresh data
        data = fetch_function(*args)
        cache[cache_key] = (data, now)
        return data

    def fetch_tickets(self, start_date, end_date):
        """Fetch tickets for date range"""
        query = f"type:ticket created>={start_date} created<={end_date}"

        all_tickets = []
        url = f"{self.base_url}/search.json"
        params = {
            'query': query,
            'sort_by': 'created_at',
            'sort_order': 'desc'
        }

        page = 1
        while url and page <= 10:  # Limit to 10 pages for performance
            if page == 1:
                response = requests.get(url, auth=self.auth, params=params, timeout=30)
            else:
                response = requests.get(url, auth=self.auth, timeout=30)

            if response.status_code != 200:
                break

            data = response.json()
            results = data.get('results', [])

            if not results:
                break

            all_tickets.extend(results)
            url = data.get('next_page')
            page += 1

        return all_tickets

    def get_ticket_metrics(self, ticket_id):
        """Get metrics for a single ticket"""
        metrics_data = self.make_request(f"tickets/{ticket_id}/metrics.json")

        if not metrics_data or 'ticket_metric' not in metrics_data:
            return None

        metrics = metrics_data['ticket_metric']
        return {
            'reply_time_in_minutes': metrics.get('reply_time_in_minutes', {}).get('calendar'),
            'full_resolution_time_in_minutes': metrics.get('full_resolution_time_in_minutes', {}).get('calendar'),
            'reopens': metrics.get('reopens', 0),
        }

    def get_custom_field_value(self, ticket, field_id):
        """Extract custom field value from ticket"""
        custom_fields = ticket.get('custom_fields', [])
        for field in custom_fields:
            if field.get('id') == field_id:
                return field.get('value')
        return None

    def get_agent_name(self, agent_id):
        """Fetch agent name by ID with caching"""
        if agent_id in self.agent_cache:
            return self.agent_cache[agent_id]

        data = self.make_request(f"users/{agent_id}.json")
        if data and 'user' in data:
            name = data['user'].get('name', f'Agent {agent_id}')
            self.agent_cache[agent_id] = name
            return name
        return f'Agent {agent_id}'

    def get_satisfaction_ratings(self, start_date):
        """Fetch CSAT ratings - filters by date and excludes 'offered' (unanswered)"""
        all_ratings = []
        url = f"{self.base_url}/satisfaction_ratings.json"
        params = {'sort_order': 'desc'}

        # Fetch ratings (paginated)
        page = 1
        while url and page <= 5:  # Limit to 5 pages (500 ratings) for performance
            if page == 1:
                response = requests.get(url, auth=self.auth, params=params, timeout=30)
            else:
                response = requests.get(url, auth=self.auth, timeout=30)

            if response.status_code != 200:
                break

            data = response.json()
            ratings = data.get('satisfaction_ratings', [])

            if not ratings:
                break

            all_ratings.extend(ratings)
            url = data.get('next_page')
            page += 1

        # Filter by date and exclude "offered" (unanswered surveys)
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        filtered_ratings = []

        for rating in all_ratings:
            # Skip "offered" - these are surveys sent but not answered
            if rating.get('score') == 'offered':
                continue

            # Check date
            created_at = rating.get('created_at', '')
            if created_at:
                try:
                    rating_date = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    if rating_date.replace(tzinfo=None) >= start_dt:
                        filtered_ratings.append(rating)
                except:
                    pass

        return filtered_ratings

    def calculate_metrics(self, start_date, end_date):
        """Calculate all dashboard metrics"""
        tickets = self.fetch_tickets(start_date, end_date)

        if not tickets:
            return None

        # Initialize metrics
        metrics = {
            'total_tickets': len(tickets),
            'tickets_by_status': defaultdict(int),
            'tickets_by_priority': defaultdict(int),
            'tickets_by_resolution_type': defaultdict(int),
            'top_clients': Counter(),
            'top_clients_chat': Counter(),
            'response_times': [],
            'resolution_times': [],
            'escalations': 0,
            'bugs': 0,
            'issues': 0,  # data_issue + infrastructure_issue
            'feature_requests': 0,
            'by_category': defaultdict(int),
            'monthly_trend': defaultdict(lambda: {'total': 0, 'bugs': 0, 'issues': 0, 'escalations': 0}),
            'agent_stats': defaultdict(lambda: {'count': 0, 'response_times': []}),
            'feature_bugs': defaultdict(int),
            'engineering_issues': defaultdict(list),
        }

        # Process tickets (sample up to 200 for performance)
        sample_tickets = tickets[:200]

        for ticket in tickets:
            # Status distribution
            status = ticket.get('status', 'unknown')
            metrics['tickets_by_status'][status] += 1

            # Priority distribution - use CUSTOM priority field
            custom_priority = self.get_custom_field_value(ticket, CUSTOM_PRIORITY_FIELD_ID)
            priority = custom_priority if custom_priority else ticket.get('priority', 'unknown')
            if priority:
                metrics['tickets_by_priority'][priority] += 1

            # Top clients (by organization) - EXCLUDE Blueshift
            org_id = ticket.get('organization_id')
            if org_id and org_id not in BLUESHIFT_ORG_IDS:
                metrics['top_clients'][org_id] += 1

                # Track chat volume separately (via channel)
                via = ticket.get('via', {})
                channel = via.get('channel', '')
                if channel == 'chat':
                    metrics['top_clients_chat'][org_id] += 1

            # Get custom fields
            blueshift_feature = self.get_custom_field_value(ticket, BLUESHIFT_FEATURE_FIELD_ID)
            resolution_type = self.get_custom_field_value(ticket, RESOLUTION_TYPE_FIELD_ID)

            # Track resolution type distribution (exclude Not Set)
            if resolution_type:
                metrics['tickets_by_resolution_type'][resolution_type] += 1

            # Tags for categorization
            tags = ticket.get('tags', [])
            is_escalation = 'jira_escalated' in tags

            if is_escalation:
                metrics['escalations'] += 1
                # Track engineering escalations by issue
                if blueshift_feature:
                    issue_key = f"{blueshift_feature}"
                    if len(metrics['engineering_issues'][issue_key]) < 5:  # Track up to 5 tickets per issue
                        metrics['engineering_issues'][issue_key].append({
                            'id': ticket.get('id'),
                            'subject': ticket.get('subject', 'No subject')[:60],
                            'resolution_type': resolution_type or 'Not set'
                        })

            # Track issues separately from bugs
            issue_resolution_types = ['data_issue', 'infrastructure_issue']
            bug_resolution_types = ['platform_bug', 'bug', 'product_issue']

            # Track issues (data_issue + infrastructure_issue)
            if resolution_type and any(issue_type in str(resolution_type).lower() for issue_type in issue_resolution_types):
                metrics['issues'] += 1

            # Track bugs (platform_bug, bug, product_issue, or bug tags)
            if resolution_type and any(bug_type in str(resolution_type).lower() for bug_type in bug_resolution_types):
                metrics['bugs'] += 1
                if blueshift_feature:
                    metrics['feature_bugs'][blueshift_feature] += 1

            if 'bug' in tags or 'bugs' in tags:
                metrics['bugs'] += 1
                if blueshift_feature and blueshift_feature not in metrics['feature_bugs']:
                    metrics['feature_bugs'][blueshift_feature] += 1

            if 'feature_request' in tags or 'feature-request' in tags:
                metrics['feature_requests'] += 1

            # Category tags
            for tag in ['deliv', 'decs', 'ui-decs', 'cd', 'ui-cd', 'imp', 'mobile', 'plt', 'ds']:
                if tag in tags:
                    metrics['by_category'][tag] += 1

            # Monthly trend with detailed breakdown
            created_at = ticket.get('created_at', '')
            if created_at:
                try:
                    month = created_at[:7]  # YYYY-MM
                    metrics['monthly_trend'][month]['total'] += 1

                    # Track issues per month (data_issue + infrastructure_issue)
                    if resolution_type and any(issue_type in str(resolution_type).lower() for issue_type in issue_resolution_types):
                        metrics['monthly_trend'][month]['issues'] += 1

                    # Track bugs per month (platform_bug, bug, product_issue, or bug tags)
                    if resolution_type and any(bug_type in str(resolution_type).lower() for bug_type in bug_resolution_types):
                        metrics['monthly_trend'][month]['bugs'] += 1
                    elif 'bug' in tags or 'bugs' in tags:
                        metrics['monthly_trend'][month]['bugs'] += 1

                    # Track escalations per month
                    if is_escalation:
                        metrics['monthly_trend'][month]['escalations'] += 1
                except:
                    pass

            # Agent stats - EXCLUDE Elizabeth
            assignee_id = ticket.get('assignee_id')
            if assignee_id and assignee_id != ELIZABETH_USER_ID:
                metrics['agent_stats'][assignee_id]['count'] += 1

        # Get detailed metrics for sample
        for ticket in sample_tickets[:50]:  # Limit to 50 for API performance
            ticket_id = ticket.get('id')
            assignee_id = ticket.get('assignee_id')

            if not assignee_id or assignee_id == ELIZABETH_USER_ID:
                continue

            ticket_metrics = self.get_ticket_metrics(ticket_id)

            if not ticket_metrics:
                continue

            # First Response Time
            if ticket_metrics['reply_time_in_minutes']:
                frt_hours = ticket_metrics['reply_time_in_minutes'] / 60
                metrics['response_times'].append(frt_hours)
                metrics['agent_stats'][assignee_id]['response_times'].append(frt_hours)

            # Resolution Time
            if ticket_metrics['full_resolution_time_in_minutes']:
                resolution_hours = ticket_metrics['full_resolution_time_in_minutes'] / 60
                metrics['resolution_times'].append(resolution_hours)

        # Get CSAT ratings (excludes "offered" which are unanswered surveys)
        csat_ratings = self.get_satisfaction_ratings(start_date)
        # Count positive ratings: good, unoffered_good (good without survey), great
        good_ratings = sum(1 for r in csat_ratings if r.get('score') in ['good', 'unoffered_good', 'great'])
        # Count negative ratings: bad, unoffered_bad (bad without survey)
        bad_ratings = sum(1 for r in csat_ratings if r.get('score') in ['bad', 'unoffered_bad'])
        total_ratings = len(csat_ratings)

        metrics['csat'] = {
            'score': (good_ratings / total_ratings * 100) if total_ratings > 0 else 0,
            'total_ratings': total_ratings,
            'good_ratings': good_ratings,
            'bad_ratings': bad_ratings
        }

        # Calculate averages
        metrics['avg_response_time'] = statistics.mean(metrics['response_times']) if metrics['response_times'] else 0
        metrics['median_response_time'] = statistics.median(metrics['response_times']) if metrics['response_times'] else 0
        metrics['avg_resolution_time'] = statistics.mean(metrics['resolution_times']) if metrics['resolution_times'] else 0
        metrics['median_resolution_time'] = statistics.median(metrics['resolution_times']) if metrics['resolution_times'] else 0

        # Top clients with names
        metrics['top_clients_list'] = []
        for org_id, count in metrics['top_clients'].most_common(10):
            org_data = self.make_request(f"organizations/{org_id}.json")
            org_name = org_data['organization']['name'] if org_data and 'organization' in org_data else f"Org {org_id}"
            metrics['top_clients_list'].append({'name': org_name, 'count': count})

        # Top clients by chat volume
        metrics['top_clients_chat_list'] = []
        for org_id, count in metrics['top_clients_chat'].most_common(10):
            org_data = self.make_request(f"organizations/{org_id}.json")
            org_name = org_data['organization']['name'] if org_data and 'organization' in org_data else f"Org {org_id}"
            metrics['top_clients_chat_list'].append({'name': org_name, 'count': count})

        # Agent stats with names
        metrics['agent_stats_list'] = []
        for agent_id, stats in metrics['agent_stats'].items():
            avg_frt = statistics.mean(stats['response_times']) if stats['response_times'] else 0
            agent_name = self.get_agent_name(agent_id)
            metrics['agent_stats_list'].append({
                'name': agent_name,
                'count': stats['count'],
                'avg_frt': round(avg_frt, 2)
            })

        metrics['agent_stats_list'].sort(key=lambda x: x['count'], reverse=True)

        return metrics


# Initialize dashboard
dashboard = ZendeskDashboard()


@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html')


@app.route('/api/metrics')
def get_metrics():
    """API endpoint for metrics data"""
    # Current month: from 1st of the month to today
    today = datetime.now()
    start_date = today.replace(day=1)  # First day of current month
    end_date = today

    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    cache_key = f"metrics_{start_str}_{end_str}"

    metrics = dashboard.get_cached_data(
        cache_key,
        dashboard.calculate_metrics,
        start_str,
        end_str
    )

    if not metrics:
        return jsonify({'error': 'Failed to fetch metrics'}), 500

    # For monthly trend, we need historical data to show a meaningful trend
    # Fetch last 2 months of data separately (with caching) for faster loading
    months_back = 2
    trend_start = (today.replace(day=1) - timedelta(days=months_back * 31)).replace(day=1)
    trend_start_str = trend_start.strftime('%Y-%m-%d')

    trend_cache_key = f"trend_{trend_start_str}_{end_str}"

    # Use a simple in-memory cache check
    trend_data = dashboard.get_cached_data(
        trend_cache_key,
        lambda s, e: dashboard.fetch_tickets(s, e),  # Just get tickets
        trend_start_str,
        end_str
    )

    # Calculate monthly breakdown from trend tickets
    monthly_trend_converted = {}
    if trend_data:
        issue_resolution_types = ['data_issue', 'infrastructure_issue']
        bug_resolution_types = ['platform_bug', 'bug', 'product_issue']
        month_stats = defaultdict(lambda: {'total': 0, 'bugs': 0, 'issues': 0, 'escalations': 0})

        for ticket in trend_data:
            created_at = ticket.get('created_at', '')
            if created_at:
                month = created_at[:7]  # YYYY-MM
                month_stats[month]['total'] += 1

                # Check for issues and bugs
                resolution_type = dashboard.get_custom_field_value(ticket, RESOLUTION_TYPE_FIELD_ID)
                tags = ticket.get('tags', [])

                # Track issues (data_issue + infrastructure_issue)
                if resolution_type and any(issue_type in str(resolution_type).lower() for issue_type in issue_resolution_types):
                    month_stats[month]['issues'] += 1

                # Track bugs (platform_bug, bug, product_issue, or bug tags)
                if resolution_type and any(bug_type in str(resolution_type).lower() for bug_type in bug_resolution_types):
                    month_stats[month]['bugs'] += 1
                elif 'bug' in tags or 'bugs' in tags:
                    month_stats[month]['bugs'] += 1

                # Check for escalations
                if 'jira_escalated' in tags:
                    month_stats[month]['escalations'] += 1

        # Convert to simple dict
        for month in sorted(month_stats.keys()):
            monthly_trend_converted[month] = dict(month_stats[month])
    else:
        # Fallback to current month data
        for month, data in sorted(metrics['monthly_trend'].items()):
            monthly_trend_converted[month] = {
                'total': data['total'],
                'bugs': data['bugs'],
                'issues': data['issues'],
                'escalations': data['escalations']
            }

    # Top feature bugs
    feature_bugs_list = [
        {'feature': feature, 'count': count}
        for feature, count in sorted(metrics['feature_bugs'].items(), key=lambda x: x[1], reverse=True)[:10]
    ]

    # Engineering issues summary
    engineering_issues_list = [
        {
            'feature': feature,
            'count': len(tickets),
            'sample_tickets': tickets
        }
        for feature, tickets in sorted(metrics['engineering_issues'].items(), key=lambda x: len(x[1]), reverse=True)[:10]
    ]

    response_data = {
        'total_tickets': metrics['total_tickets'],
        'tickets_by_status': dict(metrics['tickets_by_status']),
        'tickets_by_priority': dict(metrics['tickets_by_priority']),
        'tickets_by_resolution_type': dict(metrics['tickets_by_resolution_type']),
        'top_clients': metrics['top_clients_list'],
        'top_clients_chat': metrics['top_clients_chat_list'],
        'avg_response_time': round(metrics['avg_response_time'], 2),
        'median_response_time': round(metrics['median_response_time'], 2),
        'avg_resolution_time': round(metrics['avg_resolution_time'], 2),
        'median_resolution_time': round(metrics['median_resolution_time'], 2),
        'escalations': metrics['escalations'],
        'bugs': metrics['bugs'],
        'issues': metrics['issues'],
        'feature_requests': metrics['feature_requests'],
        'csat': metrics['csat'],
        'by_category': dict(metrics['by_category']),
        'monthly_trend': monthly_trend_converted,
        'agent_stats': metrics['agent_stats_list'][:10],
        'feature_bugs': feature_bugs_list,
        'engineering_issues': engineering_issues_list,
        'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    return jsonify(response_data)


@app.route('/api/monthly/<int:year>/<int:month>')
def get_monthly_metrics(year, month):
    """Get metrics for a specific month"""
    start_date = f"{year}-{month:02d}-01"

    # Calculate last day of month
    if month == 12:
        end_date = f"{year}-12-31"
    else:
        next_month = datetime(year, month + 1, 1)
        last_day = next_month - timedelta(days=1)
        end_date = last_day.strftime('%Y-%m-%d')

    cache_key = f"metrics_{start_date}_{end_date}"

    metrics = dashboard.get_cached_data(
        cache_key,
        dashboard.calculate_metrics,
        start_date,
        end_date
    )

    if not metrics:
        return jsonify({'error': 'Failed to fetch metrics'}), 500

    return jsonify({
        'total_tickets': metrics['total_tickets'],
        'avg_response_time': round(metrics['avg_response_time'], 2),
        'escalations': metrics['escalations'],
        'csat': metrics['csat']
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
