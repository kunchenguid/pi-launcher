#!/usr/bin/env python3
"""
merge-release-pr.py - autonomous-mode merge of exactly one release-please
release PR.

This is the only automated merge in the repository and it is deliberately
narrow: the PR must be the release-please-generated release PR for the default
branch, opened by the Actions bot, still carrying the `autorelease: pending`
label, and touching nothing outside the release-please output set derived from
release-please-config.json. The merge is pinned to the exact head SHA that was
inspected, so a concurrent push to the release branch fails the merge instead
of shipping unreviewed content.

Nothing here enables repository-wide auto-merge, and it refuses to run unless
the caller passed the release-please PR payload from the same workflow run.

Environment:
  GITHUB_TOKEN                    token with contents+pull-requests write
  GITHUB_REPOSITORY               owner/name
  RELEASE_PLEASE_PR               the release-please action `pr` output (JSON)
  RELEASE_PLEASE_RELEASE_CREATED  the action `release_created` output
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_api import GitHubError, client_from_env, emit_outputs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "release-please-config.json"
MANIFEST_PATH = ".release-please-manifest.json"
RELEASE_BRANCH_PREFIX = "release-please--branches--"
EXPECTED_AUTHOR = "github-actions[bot]"
PENDING_LABEL = "autorelease: pending"
MERGE_METHOD = "squash"


class MergeRefused(RuntimeError):
    """The PR is not the exact release-please output PR, so it is not merged."""


def release_please_outputs(config):
    """Derive the complete set of files release-please may touch.

    Mirrors scripts/check-release-ci-exclusions.sh. Any config shape this
    cannot derive exactly is refused rather than approximated.
    """
    packages = config.get("packages") or {}
    if list(packages) != ["."]:
        raise MergeRefused(
            f"cannot derive release-please outputs for packages {list(packages)}"
        )
    package = packages["."]
    release_type = package.get("release-type") or config.get("release-type")
    if release_type != "simple":
        raise MergeRefused(f"unsupported release-please release-type {release_type!r}")

    outputs = {MANIFEST_PATH}
    outputs.add(package.get("changelog-path") or config.get("changelog-path") or "CHANGELOG.md")
    outputs.add(package.get("version-file") or config.get("version-file") or "version.txt")
    for entry in list(package.get("extra-files") or []) + list(config.get("extra-files") or []):
        path = entry.get("path") if isinstance(entry, dict) else entry
        if not path:
            raise MergeRefused(f"unusable extra-files entry {entry!r}")
        outputs.add(str(path))
    return outputs


def pr_number(payload):
    """Read the PR number out of the release-please action `pr` output."""
    if not payload or not payload.strip():
        raise MergeRefused(
            "release-please produced no release PR; autonomous mode has nothing "
            "to merge (the guarded pin commit is on main but unreleased)"
        )
    data = json.loads(payload)
    if isinstance(data, list):
        if len(data) != 1:
            raise MergeRefused(f"expected exactly one release PR, got {len(data)}")
        data = data[0]
    number = data.get("number")
    if not isinstance(number, int):
        raise MergeRefused(f"release PR payload has no numeric number: {payload[:200]}")
    return number


def validate_pr(pr, default_branch, allowed_files, changed_files):
    if pr.get("state") != "open":
        raise MergeRefused(f"PR #{pr.get('number')} is {pr.get('state')!r}, not open")
    if pr.get("draft"):
        raise MergeRefused(f"PR #{pr.get('number')} is a draft")
    base = (pr.get("base") or {}).get("ref")
    if base != default_branch:
        raise MergeRefused(f"PR targets {base!r}, not the default branch {default_branch!r}")
    head = (pr.get("head") or {}).get("ref") or ""
    if not head.startswith(RELEASE_BRANCH_PREFIX):
        raise MergeRefused(f"PR head branch {head!r} is not a release-please branch")
    author = (pr.get("user") or {}).get("login")
    if author != EXPECTED_AUTHOR:
        raise MergeRefused(f"PR author {author!r} is not {EXPECTED_AUTHOR!r}")
    labels = {label.get("name") for label in pr.get("labels") or []}
    if PENDING_LABEL not in labels:
        raise MergeRefused(f"PR labels {sorted(labels)} lack {PENDING_LABEL!r}")
    if not changed_files:
        raise MergeRefused("PR changes no files")
    unexpected = sorted(set(changed_files) - allowed_files)
    if unexpected:
        raise MergeRefused(
            f"PR changes files outside the release-please output set: {unexpected}"
        )
    head_sha = (pr.get("head") or {}).get("sha") or ""
    if len(head_sha) != 40:
        raise MergeRefused(f"PR head sha {head_sha!r} is unusable")
    return head_sha


def wait_for_default_branch(client, repo, default_branch, sha, sleep=time.sleep, attempts=12):
    """Block until the default branch actually points at the merge commit."""
    for attempt in range(attempts):
        ref = client.get_json(f"/repos/{repo}/git/ref/heads/{default_branch}")
        if (ref.get("object") or {}).get("sha") == sha:
            return
        sleep(min(2 ** attempt, 10))
    raise MergeRefused(
        f"{default_branch} did not advance to the merge commit {sha} in time"
    )


def main(client=None, env=None, sleep=time.sleep):
    env = os.environ if env is None else env
    client = client_from_env() if client is None else client

    repo = env.get("GITHUB_REPOSITORY") or ""
    if "/" not in repo:
        raise MergeRefused(f"GITHUB_REPOSITORY {repo!r} is not owner/name")
    if (env.get("RELEASE_PLEASE_RELEASE_CREATED") or "").lower() == "true":
        raise MergeRefused(
            "release-please created a release before the release PR was merged; "
            "the repository is in an unexpected state, so nothing is merged"
        )

    allowed_files = release_please_outputs(json.loads(CONFIG_PATH.read_text()))
    number = pr_number(env.get("RELEASE_PLEASE_PR"))

    default_branch = client.get_json(f"/repos/{repo}").get("default_branch")
    pr = client.get_json(f"/repos/{repo}/pulls/{number}")
    changed = [item.get("filename") for item in client.paginate(f"/repos/{repo}/pulls/{number}/files")]
    head_sha = validate_pr(pr, default_branch, allowed_files, changed)

    print(f"merge-release-pr: PR #{number} {pr.get('title')!r}")
    print(f"merge-release-pr: files {sorted(changed)} within {sorted(allowed_files)}")

    result = client.put_json(
        f"/repos/{repo}/pulls/{number}/merge",
        {
            "merge_method": MERGE_METHOD,
            "sha": head_sha,
            "commit_title": f"{pr.get('title')} (#{number})",
        },
    )
    if not result.get("merged"):
        raise MergeRefused(f"merge of PR #{number} was refused: {result.get('message')}")
    merge_sha = result.get("sha") or ""
    print(f"merge-release-pr: merged PR #{number} as {merge_sha}")

    wait_for_default_branch(client, repo, default_branch, merge_sha, sleep=sleep)
    emit_outputs({"merged_pr": str(number), "merge_sha": merge_sha})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MergeRefused, GitHubError) as exc:
        print(f"merge-release-pr: REFUSED: {exc}", file=sys.stderr)
        sys.exit(1)
