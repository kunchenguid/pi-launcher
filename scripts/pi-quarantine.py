#!/usr/bin/env python3
"""
pi-quarantine.py - one durable, deduplicated alert per failed upstream Pi
update, and the check that stops the updater retrying a known-bad version.

A failed update gate is not a transient condition to retry every day: it means
that exact upstream Pi release does not survive this repository's launcher
gates. So the updater files (or refreshes) a single `upstream-pi-blocked`
issue keyed by the exact Pi tag and refuses to attempt that tag again until a
human closes the issue. A newer stable Pi is a different key and is evaluated
on its own merits.

Usage:
  pi-quarantine.py status --key v0.84.0
  pi-quarantine.py open --key v0.84.0 --stage "build gate" --run-url URL \
      [--base-sha SHA] [--pin-pushed true|false]
"""

import argparse
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_api import GitHubError, client_from_env, emit_outputs  # noqa: E402

LABEL = "upstream-pi-blocked"
LABEL_COLOR = "b60205"
LABEL_DESCRIPTION = "An upstream Pi release failed the automated update gate"
MARKER_PREFIX = "pi-upstream-blocked"
DISCOVERY_KEY = "discovery"


def marker(key):
    return f"<!-- {MARKER_PREFIX}: {key} -->"


def find_issue(client, repo, key):
    """Return the open quarantine issue for `key`, or None."""
    needle = marker(key)
    for issue in client.paginate(f"/repos/{repo}/issues?state=open&labels={LABEL}"):
        if "pull_request" in issue:
            continue
        if needle in (issue.get("body") or ""):
            return issue
    return None


def issue_title(key):
    if key == DISCOVERY_KEY:
        return "Upstream Pi discovery is failing"
    return f"Upstream Pi {key} is blocked from release"


def issue_body(key, args, now):
    pin_pushed = (args.pin_pushed or "false").lower() == "true"
    lines = [
        marker(key),
        "",
        f"The automated upstream Pi updater halted at **{args.stage}**.",
        "",
        f"- Target: `{key}`",
        f"- Tested `main`: `{args.base_sha or 'n/a'}`",
        f"- Workflow run: {args.run_url or 'n/a'}",
        f"- Pin commit already on `main`: **{'yes' if pin_pushed else 'no'}**",
        f"- Last attempt: {now}",
        "",
    ]
    if pin_pushed:
        lines += [
            "The pin commit is already on `main`, so daily discovery will report",
            "no update. Do not re-tag or re-notarize automatically: diagnose first,",
            "then use the manual release dispatch against the existing tag as",
            "described in `RELEASE.md`.",
            "",
        ]
    lines += [
        f"While this issue is open the updater will not attempt `{key}` again.",
        "Close it once the cause is fixed to re-enable the next scheduled attempt.",
        "A newer stable Pi release is a separate candidate and is unaffected.",
    ]
    return "\n".join(lines) + "\n"


def ensure_label(client, repo):
    try:
        client.get_json(f"/repos/{repo}/labels/{LABEL}")
    except GitHubError:
        client.post_json(
            f"/repos/{repo}/labels",
            {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
        )


def cmd_status(client, repo, args):
    issue = find_issue(client, repo, args.key)
    quarantined = issue is not None
    if quarantined:
        print(f"pi-quarantine: {args.key} is quarantined by {issue['html_url']}")
    else:
        print(f"pi-quarantine: {args.key} is not quarantined")
    emit_outputs(
        {
            "quarantined": "true" if quarantined else "false",
            "issue_url": issue["html_url"] if quarantined else "",
        }
    )
    return 0


def cmd_open(client, repo, args, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    body = issue_body(args.key, args, now)
    issue = find_issue(client, repo, args.key)
    if issue is None:
        ensure_label(client, repo)
        issue = client.post_json(
            f"/repos/{repo}/issues",
            {"title": issue_title(args.key), "body": body, "labels": [LABEL]},
        )
        print(f"pi-quarantine: opened {issue['html_url']}")
    else:
        # One alert per key: refresh the existing issue instead of piling on
        # comments or opening a duplicate.
        issue = client.patch_json(
            f"/repos/{repo}/issues/{issue['number']}", {"body": body}
        )
        print(f"pi-quarantine: refreshed {issue['html_url']}")
    emit_outputs({"issue_url": issue["html_url"]})
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="report whether a key is quarantined")
    status.add_argument("--key", required=True)

    opener = sub.add_parser("open", help="create or refresh the quarantine issue")
    opener.add_argument("--key", required=True)
    opener.add_argument("--stage", required=True)
    opener.add_argument("--run-url", default="")
    opener.add_argument("--base-sha", default="")
    opener.add_argument("--pin-pushed", default="false")
    return parser


def main(argv=None, client=None, env=None):
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    repo = env.get("GITHUB_REPOSITORY") or ""
    if "/" not in repo:
        print(f"pi-quarantine: GITHUB_REPOSITORY {repo!r} is not owner/name", file=sys.stderr)
        return 2
    client = client_from_env() if client is None else client
    if args.command == "status":
        return cmd_status(client, repo, args)
    return cmd_open(client, repo, args)


if __name__ == "__main__":
    sys.exit(main())
