""" Script to find stale branches and notify if delete if they are older than 150 days 
"""
import argparse
import logging
import os
import sys
import datetime
import re

import requests

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

KEEP_ALIVE_PREFIX = "keep-alive-"
GITHUB_GRAPHQL_URL = "https://github.boschdevcloud.com/api/graphql"
GITHUB_API_URL = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 10


def add_branch_slack_reminders(branch, slack_reminder):
    """Add branch to list of user email"""
    if branch['target']['author']['email'] not in slack_reminder:
        slack_reminder[branch['target']['author']['email']] = []
    slack_reminder[branch['target']['author']['email']].append(branch['name'])

def delete_branches(args,branches):
    """Delete branch"""
    for branch in branches:
        if args.dry_run == "true":
            logging.info(f"DRY RUN: Would delete branch {branch['name']}")
            continue
        logging.info(f"Deleting branch {branch['name']}")
        url = GITHUB_API_URL+"repos/"+args.gh_repo+"git/refs/"+branch['name']
        headers = {
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': f'Bearer {args.gh_token}',
        }
        try:
            response = requests.delete(url, headers=headers)
            response.raise_for_status()
        except requests.exceptions.HTTPError as err:
            logging.error(err)

def grab_all_branches(args, page = "", branches = []) -> str:
    """Grab all branches from github"""
    repo_owner, repo_name = args.gh_repo.split('/')
    query = """{repository(owner: \"%s\", name: \"%s\") {
        refs(first: 100, refPrefix: \"refs/heads/\"%s) {
        nodes {
            name
            associatedPullRequests(first:1){
                nodes {
                    state
                }
            }
            target {
            ... on Commit {
                oid
                committedDate
                author {
                    name
                    email
                }
                
            }
            }
        }
        pageInfo {
            endCursor
            hasNextPage
            hasPreviousPage
        }
        }
    }
    }""" % (repo_owner, repo_name, page)
    headers = {
    'Accept': 'application/vnd.github.v3+json',
    'Authorization': f'Bearer {args.gh_token}',
    }
    try:
        response = requests.post(GITHUB_GRAPHQL_URL, json={'query': query}, headers=headers)
        response.raise_for_status()
        data = response.json()['data']
        branches.extend(data['repository']['refs']['nodes'])
        if data['repository']['refs']['pageInfo']['hasNextPage']:
            page = ", after: \"%s\"" % data['repository']['refs']['pageInfo']['endCursor']
            return grab_all_branches(args, page, branches)
        else:
            return branches
    except requests.exceptions.HTTPError as err:
        logging.error(err)
        sys.exit(1)
        
def triage_branches(args, branches):
    """Triage the branches"""
    branches_to_delete = []
    slack_reminder = {}

    for branch in branches:
        # ignore branches with keep-alive prefix
        if branch['name'].startswith(KEEP_ALIVE_PREFIX): 
            continue
        # ignore branches with open PRs
        if branch['associatedPullRequests']['nodes']:
            if branch['associatedPullRequests']['nodes'][0]['state'] == 'OPEN':
                continue
        # ignore default branch
        if re.match(args.branches_to_be_ignored, branch['name']):
            continue
        # ignore branches that not match the filter regex
        if not re.match(args.branches_filter_regex, branch['name']):
            continue
        lastBranchCommit = datetime.datetime.strptime(branch['target']['committedDate'], '%Y-%m-%dT%H:%M:%SZ')
        if lastBranchCommit < datetime.datetime.today() - datetime.timedelta(days=args.days_delete):
            branches_to_delete.append(branch)
        elif lastBranchCommit < datetime.datetime.today() - datetime.timedelta(days=args.days_notify):
            email = branch['target']['author']['email']
            if email not in slack_reminder:
                slack_reminder[email] = []
            slack_reminder[email].append(branch)

    if branches_to_delete:
        delete_branches(args, branches_to_delete)
    if slack_reminder:
        send_slack_message(args, slack_reminder)

def get_slack_user_id(args, email):
    """Get slack user id"""
    try:
        response = requests.get("https://slack.com/api/users.lookupByEmail?email="+email, headers={'Authorization': f'Bearer {args.slack_token}'})
        response.raise_for_status()
        if response.json()['ok']:
            return response.json()['user']['id']
        return None
    except requests.exceptions.HTTPError as err:
        logging.error(err)

def send_internal_email(sender_email: str, password: str, receiver_email: str, subject: str, body: str):
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'html'))

        with smtplib.SMTP('rb-smtp-auth.rbesz01.com', 25) as server:
            server.starttls()
            server.login(sender_email, password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
            logging.info(f"Email sent to {receiver_email}")
    except Exception as e:
        logging.error(f"Error sending email to {receiver_email}: {e}")

def send_slack_message(args, slack_reminder):
    """Send slack message to remind users to delete their branches"""
    repo_owner, repo_name = args.gh_repo.split('/')
    for user_email in slack_reminder.keys():
        branch_url = f"https://github.boschdevcloud.com/{args.gh_repo}/compare/{args.default_branch}...{repo_owner}:{repo_name}:"
        branches = slack_reminder[user_email]
        delete_branch_msg = "git push origin --delete " + ' '.join([branch['name'] for branch in branches])

        html_message = f"""
        <html>
        <body>
            <p>Hi,</p>

            <p>The following branches in <strong>{args.gh_repo}</strong> are more than <strong>{args.days_notify} days</strong> old:</p>

            <ul>
            {''.join(f'<li><a href="{branch_url + branch["name"]}">{branch["name"]}</a></li>' for branch in branches)}
            </ul>

            <p>
            Please update these branches if you want to keep them.
            </p>

            <p>
            Otherwise, you can <strong>delete</strong> them using:
            </p>

            <pre><code>{delete_branch_msg}</code></pre>

            <p>
            If no action is taken, the branch{'es' if len(branches) > 1 else ''} will be automatically deleted in <strong>{args.days_delete - args.days_notify} days</strong>.
            </p>

            <p>Thanks,<br/>Automation Bot</p>
        </body>
        </html>
        """

        title = f"You have old branches in repo {args.gh_repo}"
        # if args.dry_run == "true":
        #     logging.info(f"DRY RUN: Would send email to {user_email}")
        #     continue
        logging.info(f"Sending email to {user_email}")
        send_internal_email(args.sender, args.password, user_email, title, html_message)


def parse_args():
    """Define and parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='Find old github branches', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--gh-repo', help='The owner and repository name', default=os.getenv('GITHUB_REPOSITORY'))
    parser.add_argument('--gh-token', help='Github API Token', default=os.getenv('GITHUB_TOKEN'))
    parser.add_argument('--sender', help='NTID of sender', default=os.getenv('SENDER'))
    parser.add_argument('--password', help='Log in password of sender', default=os.getenv('PASSWORD'))
    # parser.add_argument(
    #     '--slack-token', help='Slack token', default=os.getenv('SLACK_TOKEN'))
    parser.add_argument(
        '--days-delete', type=int, help='Number of days to delete')
    parser.add_argument(
        '--days-notify',type=int, help='Number of days to notify')
    parser.add_argument(
        '--default-branch', help='Default branch name.')
    parser.add_argument(
        '--branches-to-be-ignored', help='An optional Regex that will be used to ignore branches from this action.', default="^(release\/.+|develop|main)$")
    parser.add_argument(
        '--branches-filter-regex', help='An optional Regex that will be used to filter branches from this action')
    parser.add_argument(
        '--dry-run', help='If this is enabled, the action will not delete or tag any branches.')
    parser.add_argument('--verbose', help='Verbose output', action='store_true')

    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    # log_level = logging.DEBUG if args.verbose else logging.INFO
    # logging.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s', level=log_level)
    # if not args.gh_repo or not args.gh_token:
    #     logging.error("Missing required arguments")
    #     sys.exit(1)
    # all_branches = grab_all_branches(args)

    # if all_branches:
    #     triage_branches(args, all_branches)
    # else:
    #     logging.info("No branches found")
    print(vars(args))


if __name__ == "__main__":
    main()
