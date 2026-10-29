#!/usr/bin/env python3
"""Prepare a Jira issue for testing and create its SIT testing task.

Usage:
  Set JIRA_PAT, edit the configuration below, then run:
    python done.py PROJECT-123 [file1 file2 ...]
    python done.py --dry-run PROJECT-123 [file1 file2 ...]

JIRA_CA_BUNDLE may point to a corporate CA bundle. Dry-run still reads the
original issue, but does not call any write endpoints.
"""

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests


# ------------------------------ Configuration ------------------------------
BASE_URL = "https://alm.swe.la.gov/jira"
LABEL = "SIT_COMPLETE"
COMMENT_TEXT = "DUR has been tested on SIT and is working as expected. Please consider testing on higher environment.Moving DUR to UAT."
DESCRIPTION_APPEND_TEXT = ""  # Temporarily disabled; set text here to enable.
ISSUE_TYPE_NAME = "Task"
LINK_TYPE_NAME = "Tests"
SWAP_LINK_DIRECTION = False

# Jira has a single summary/title field. This template includes the source
# issue key, SIT Testing designation, and the original issue summary.
TEST_TASK_SUMMARY_TEMPLATE = "{issue_key} | SIT Testing - {summary}"
TEST_TASK_DESCRIPTION_TEMPLATE = "SIT testing for {issue_key}: {summary}"
EXTRA_REQUIRED_FIELDS = {
    # Example: "customfield_12345": {"value": "Platform Team"},
}

REQUEST_TIMEOUT_SECONDS = 30
# ----------------------------------------------------------------------------


class JiraError(Exception):
    """Raised when a Jira response is invalid or a request cannot be made."""


def api_request(session, method, path, **kwargs):
    """Send an API request and exit with Jira's full error response on failure."""
    url = f"{BASE_URL.rstrip('/')}/rest/api/2/{path.lstrip('/')}"
    try:
        response = session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
    except requests.RequestException as exc:
        print(f"Jira request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not 200 <= response.status_code < 300:
        print(
            f"Jira API error: HTTP {response.status_code}\n{response.text}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return response


def response_json(response, context):
    try:
        return response.json()
    except ValueError as exc:
        raise JiraError(f"Jira returned invalid JSON while {context}.") from exc


def issue_path(issue_key):
    return f"issue/{quote(issue_key, safe='')}"


def main():
    parser = argparse.ArgumentParser(
        description="Prepare a Jira issue for SIT testing."
    )
    parser.add_argument("--dry-run", action="store_true", help="skip all writes")
    parser.add_argument("issue_key", help="key of the Jira issue to prepare")
    parser.add_argument("files", nargs="*", help="files to attach to the testing task")
    args = parser.parse_args()

    token = os.environ.get("JIRA_PAT")
    if not token:
        parser.error("JIRA_PAT must be set in the environment.")

    ca_bundle = os.environ.get("JIRA_CA_BUNDLE")
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
    )
    session.verify = r"C:\Certs\alm.swe.la.gov.crt"

    original = response_json(
        api_request(
            session,
            "GET",
            issue_path(args.issue_key),
            params={"fields": "description,project,summary"},
        ),
        "loading the original issue",
    )
    fields = original.get("fields", {})
    project = fields.get("project", {}).get("key")
    summary = fields.get("summary")
    description = fields.get("description")
    if not project or not summary:
        raise JiraError("Original issue response is missing project key or summary.")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise JiraError("Original issue description is not plain text.")

    task_summary = TEST_TASK_SUMMARY_TEMPLATE.format(
        issue_key=args.issue_key,
        summary=summary,
    )
    task_description = TEST_TASK_DESCRIPTION_TEMPLATE.format(
        issue_key=args.issue_key,
        summary=summary,
    )
    reserved_fields = {"project", "issuetype", "summary", "description"}
    conflicts = reserved_fields.intersection(EXTRA_REQUIRED_FIELDS)
    if conflicts:
        raise JiraError(
            "EXTRA_REQUIRED_FIELDS cannot override: "
            + ", ".join(sorted(conflicts))
        )

    available_files = []
    for file_name in args.files:
        path = Path(file_name)
        if not path.is_file():
            print(f"Warning: skipping missing file: {file_name}", file=sys.stderr)
        else:
            available_files.append(path)

    if args.dry_run:
        print(f"Would add label '{LABEL}' to {args.issue_key}.")
        print(f"Would add a comment to {args.issue_key}.")
        if DESCRIPTION_APPEND_TEXT:
            print(
                f"Would append configured text to the description of "
                f"{args.issue_key}."
            )
        else:
            print(f"Would leave the description of {args.issue_key} unchanged.")
        print(
            f"Would create {ISSUE_TYPE_NAME} in {project} with summary "
            f"'{task_summary}'."
        )
        print(f"Testing task description: '{task_description}'.")
        print(
            f"Would link the new task and {args.issue_key} using "
            f"'{LINK_TYPE_NAME}' (swap directions: {SWAP_LINK_DIRECTION})."
        )
        print(f"Would attach {len(available_files)} file(s) to the new task.")
        print("Issue statuses would remain unchanged.")
        print("New testing task key: (not created — dry-run)")
        return

    original_path = issue_path(args.issue_key)
    api_request(
        session,
        "PUT",
        original_path,
        json={"update": {"labels": [{"add": LABEL}]}},
    )

    api_request(
        session,
        "POST",
        f"{original_path}/comment",
        json={"body": COMMENT_TEXT},
    )

    if DESCRIPTION_APPEND_TEXT:
        separator = "\n\n" if description else ""
        api_request(
            session,
            "PUT",
            original_path,
            json={
                "fields": {
                    "description": (
                        f"{description}{separator}{DESCRIPTION_APPEND_TEXT}"
                    )
                }
            },
        )

    create_fields = {
        "project": {"key": project},
        "issuetype": {"name": ISSUE_TYPE_NAME},
        "summary": task_summary,
        "description": task_description,
        **EXTRA_REQUIRED_FIELDS,
    }
    created = response_json(
        api_request(
            session,
            "POST",
            "issue",
            json={"fields": create_fields},
        ),
        "creating the testing task",
    )
    task_key = created.get("key")
    if not task_key:
        raise JiraError("Jira created the testing task without returning its key.")

    inward_key, outward_key = args.issue_key, task_key
    if SWAP_LINK_DIRECTION:
        inward_key, outward_key = outward_key, inward_key
    api_request(
        session,
        "POST",
        "issueLink",
        json={
            "type": {"name": LINK_TYPE_NAME},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        },
    )

    for path in available_files:
        with path.open("rb") as attachment:
            api_request(
                session,
                "POST",
                f"{issue_path(task_key)}/attachments",
                headers={"X-Atlassian-Token": "no-check"},
                files={"file": (path.name, attachment)},
            )

    print(f"New testing task key: {task_key}")


if __name__ == "__main__":
    try:
        main()
    except JiraError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
