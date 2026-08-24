#!/usr/bin/env python3
"""
automation_tests.py - hermetic tests for the release automation scripts.

Drives scripts/update-pi-pin.py, scripts/merge-release-pr.py,
scripts/pi-quarantine.py, and the release-please last-released-version helper
in scripts/check-release-contract.py against a fake GitHub API / in-process
fixtures, so the daily autonomous updater's decisions - what it accepts as a
stable upstream release, what it is willing to merge, when it refuses to
retry, and that a new launcher release cannot fail the Pi gate - are
exercised rather than assumed. No network, no repository state.

Usage: automation_tests.py
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from gh_api import GitHubError  # noqa: E402

FAILURES = []
PASSES = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def check_raises(name, exception, callable_, needle=""):
    try:
        callable_()
    except exception as exc:
        check(name, needle in str(exc), f"message={str(exc)!r}")
    except Exception as exc:  # noqa: BLE001 - wrong exception type is a failure
        check(name, False, f"raised {type(exc).__name__}: {exc}")
    else:
        check(name, False, "no exception raised")


def load_script(filename):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


update_pin = load_script("update-pi-pin.py")
merge_pr = load_script("merge-release-pr.py")
quarantine = load_script("pi-quarantine.py")


class FakeGitHub:
    """Canned GitHub API responses plus a record of every mutating call."""

    def __init__(self, json_routes=None, byte_routes=None, list_routes=None):
        self.json_routes = dict(json_routes or {})
        self.byte_routes = dict(byte_routes or {})
        self.list_routes = dict(list_routes or {})
        self.writes = []

    def get_json(self, path):
        if path not in self.json_routes:
            raise GitHubError(f"GET {path}: HTTP 404")
        return json.loads(json.dumps(self.json_routes[path]))

    def get_bytes(self, path, accept=None):
        if path not in self.byte_routes:
            raise GitHubError(f"GET {path}: HTTP 404")
        return self.byte_routes[path]

    def paginate(self, path, per_page=100, max_pages=20):
        if path not in self.list_routes:
            raise GitHubError(f"GET {path}: HTTP 404")
        return iter(json.loads(json.dumps(self.list_routes[path])))

    def post_json(self, path, payload):
        self.writes.append(("POST", path, payload))
        return dict(self.json_routes.get(("POST", path), {}))

    def patch_json(self, path, payload):
        self.writes.append(("PATCH", path, payload))
        return dict(self.json_routes.get(("PATCH", path), {}))

    def put_json(self, path, payload):
        self.writes.append(("PUT", path, payload))
        return dict(self.json_routes.get(("PUT", path), {}))


def outputs_from(path):
    """Parse the GITHUB_OUTPUT file a script wrote."""
    values = {}
    for line in Path(path).read_text().splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return values


# =====================================================================
# update-pi-pin.py
# =====================================================================

REPO_PATH = "/repos/earendil-works/pi"
TAG = "v9.9.9"
ARCHIVE_SHA = "3f" * 32
LICENSE_TEXT = b"MIT License\n\nCopyright (c) upstream\n"
LICENSE_SHA = hashlib.sha256(LICENSE_TEXT).hexdigest()
DOWNLOAD_ROOT = f"https://github.com/earendil-works/pi/releases/download/{TAG}"


def asset(name, asset_id, **overrides):
    data = {
        "id": asset_id,
        "name": name,
        "state": "uploaded",
        "browser_download_url": f"{DOWNLOAD_ROOT}/{name}",
    }
    data.update(overrides)
    return data


def sums_text(archive_sha=ARCHIVE_SHA):
    return (
        f"{archive_sha}  pi-darwin-arm64.tar.gz\n"
        f"{'ab' * 32}  pi-linux-x64.tar.gz\n"
    )


def upstream_client(release_overrides=None, sums=None, license_bytes=LICENSE_TEXT,
                    tag_ref=None, assets=None):
    release = {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-06T12:00:00Z",
        "assets": assets if assets is not None else [
            asset("pi-darwin-arm64.tar.gz", 111, digest=f"sha256:{ARCHIVE_SHA}"),
            asset("SHA256SUMS", 222),
        ],
    }
    release.update(release_overrides or {})
    import base64

    return FakeGitHub(
        json_routes={
            f"{REPO_PATH}/releases/latest": release,
            f"{REPO_PATH}/git/ref/tags/{TAG}": tag_ref
            if tag_ref is not None
            else {"ref": f"refs/tags/{TAG}", "object": {"sha": "0" * 39 + "a", "type": "commit"}},
            f"{REPO_PATH}/contents/LICENSE?ref={TAG}": {
                "encoding": "base64",
                "content": base64.b64encode(license_bytes).decode(),
            },
        },
        byte_routes={
            f"{REPO_PATH}/releases/assets/222": (
                sums if sums is not None else sums_text()
            ).encode(),
        },
    )


manifest, meta = update_pin.resolve_latest_stable(upstream_client())
check(
    "resolves the stable release into the pinned manifest shape",
    manifest
    == {
        "upstream": "https://github.com/earendil-works/pi",
        "tag": TAG,
        "version": "9.9.9",
        "url": f"{DOWNLOAD_ROOT}/pi-darwin-arm64.tar.gz",
        "sha256": ARCHIVE_SHA,
        "sumsUrl": f"{DOWNLOAD_ROOT}/SHA256SUMS",
        "licenseUrl": f"https://raw.githubusercontent.com/earendil-works/pi/{TAG}/LICENSE",
        "licenseSha256": LICENSE_SHA,
    },
    f"manifest={manifest}",
)
check("records the upstream commit", meta["commit"].endswith("a"))

# The generator must reproduce the checked-in pin byte for byte, or a bump
# would silently rewrite a file the release contract validates.
current = update_pin.load_current()
regenerated = update_pin.render_manifest(
    update_pin.build_manifest(current["tag"], current["sha256"], current["licenseSha256"])
)
check(
    "regenerates the checked-in pin byte for byte",
    regenerated == (ROOT / "packaging/pi-release.json").read_text(),
)

check_raises(
    "refuses a prerelease",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client({"prerelease": True})
    ),
    "prerelease",
)
check_raises(
    "refuses a draft release",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(upstream_client({"draft": True})),
    "draft",
)
check_raises(
    "refuses an unpublished release",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(upstream_client({"published_at": None})),
    "published_at",
)
for bad_tag in ("v9.9.9-rc.1", "9.9.9", "v9.9", "nightly", "v09.9.9"):
    check_raises(
        f"refuses the non-stable tag {bad_tag!r}",
        update_pin.UpstreamError,
        lambda tag=bad_tag: update_pin.resolve_latest_stable(
            upstream_client({"tag_name": tag})
        ),
        "stable",
    )
check_raises(
    "refuses duplicate archive assets",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(
            assets=[
                asset("pi-darwin-arm64.tar.gz", 111),
                asset("pi-darwin-arm64.tar.gz", 112),
                asset("SHA256SUMS", 222),
            ]
        )
    ),
    "exactly one",
)
check_raises(
    "refuses a missing SHA256SUMS asset",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(assets=[asset("pi-darwin-arm64.tar.gz", 111)])
    ),
    "exactly one",
)
check_raises(
    "refuses an asset that is still uploading",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(
            assets=[
                asset("pi-darwin-arm64.tar.gz", 111, state="starter"),
                asset("SHA256SUMS", 222),
            ]
        )
    ),
    "state",
)
check_raises(
    "refuses an asset URL that is not the canonical download URL",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(
            assets=[
                asset(
                    "pi-darwin-arm64.tar.gz",
                    111,
                    browser_download_url="https://evil.example/pi.tar.gz",
                ),
                asset("SHA256SUMS", 222),
            ]
        )
    ),
    "download URL",
)
check_raises(
    "refuses SHA256SUMS without an arm64 entry",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(sums=f"{'ab' * 32}  pi-linux-x64.tar.gz\n")
    ),
    "0 entries",
)
check_raises(
    "refuses duplicate SHA256SUMS entries",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(sums=sums_text() + f"{'cd' * 32}  pi-darwin-arm64.tar.gz\n")
    ),
    "2 entries",
)
check_raises(
    "refuses a non-hex SHA256SUMS entry",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(sums="not-a-checksum  pi-darwin-arm64.tar.gz\n")
    ),
    "64 lowercase hex",
)
check_raises(
    "refuses a release whose asset digest contradicts SHA256SUMS",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(
            assets=[
                asset("pi-darwin-arm64.tar.gz", 111, digest=f"sha256:{'cd' * 32}"),
                asset("SHA256SUMS", 222),
            ]
        )
    ),
    "does not match",
)
check_raises(
    "refuses a tag that does not resolve",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(
        upstream_client(tag_ref={"ref": "refs/tags/v1.0.0", "object": {"sha": "0" * 40}})
    ),
    "does not resolve",
)
check_raises(
    "refuses an empty upstream LICENSE",
    update_pin.UpstreamError,
    lambda: update_pin.resolve_latest_stable(upstream_client(license_bytes=b"")),
    "empty",
)

check(
    "compares stable versions numerically, not lexically",
    update_pin.semver("0.9.0") < update_pin.semver("0.10.0") < update_pin.semver("1.0.0"),
)


def run_update_pin(mode, pinned_version, target_tag=TAG):
    """Run main() against a temporary manifest and return (outputs, manifest text)."""
    with tempfile.TemporaryDirectory() as tmp:
        pin_path = Path(tmp) / "pi-release.json"
        pin_path.write_text(
            update_pin.render_manifest(
                update_pin.build_manifest("v" + pinned_version, "ff" * 32, "ee" * 32)
            )
        )
        output_path = Path(tmp) / "outputs"
        output_path.touch()
        saved_manifest, saved_client = update_pin.MANIFEST_PATH, update_pin.client_from_env
        saved_output = os.environ.get("GITHUB_OUTPUT")
        update_pin.MANIFEST_PATH = pin_path
        update_pin.client_from_env = lambda: upstream_client(
            {"tag_name": target_tag} if target_tag != TAG else None
        )
        os.environ["GITHUB_OUTPUT"] = str(output_path)
        try:
            update_pin.main([mode])
            return outputs_from(output_path), pin_path.read_text()
        finally:
            update_pin.MANIFEST_PATH = saved_manifest
            update_pin.client_from_env = saved_client
            if saved_output is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = saved_output


outputs, written = run_update_pin("--write", "9.9.8")
check(
    "--write pins a newer stable release and reports it",
    outputs["update_available"] == "true"
    and outputs["updated"] == "true"
    and outputs["target_tag"] == TAG
    and outputs["target_version"] == "9.9.9"
    and outputs["current_version"] == "9.9.8"
    and json.loads(written)["sha256"] == ARCHIVE_SHA,
    f"outputs={outputs}",
)

outputs, written = run_update_pin("--check", "9.9.8")
check(
    "--check never writes the manifest",
    outputs["update_available"] == "true"
    and outputs["updated"] == "false"
    and json.loads(written)["version"] == "9.9.8",
    f"outputs={outputs}",
)

outputs, written = run_update_pin("--write", "9.9.9")
check(
    "an already-current pin is an idempotent no-op",
    outputs["update_available"] == "false"
    and outputs["updated"] == "false"
    and json.loads(written)["sha256"] == "ff" * 32,
    f"outputs={outputs}",
)

outputs, written = run_update_pin("--write", "10.0.0")
check(
    "never downgrades to an older upstream release",
    outputs["update_available"] == "false" and json.loads(written)["version"] == "10.0.0",
    f"outputs={outputs}",
)


# =====================================================================
# merge-release-pr.py
# =====================================================================

REPO = "kunchenguid/pi-launcher"
HEAD_SHA = "1" * 40
MERGE_SHA = "2" * 40

allowed = merge_pr.release_please_outputs(
    json.loads((ROOT / "release-please-config.json").read_text())
)
check(
    "derives the release-please output set from the real config",
    allowed == {".release-please-manifest.json", "CHANGELOG.md", "version.txt"},
    f"allowed={sorted(allowed)}",
)
check_raises(
    "refuses to guess outputs for an unsupported release type",
    merge_pr.MergeRefused,
    lambda: merge_pr.release_please_outputs({"packages": {".": {"release-type": "node"}}}),
    "release-type",
)
check_raises(
    "refuses to guess outputs for a multi-package config",
    merge_pr.MergeRefused,
    lambda: merge_pr.release_please_outputs(
        {"packages": {".": {"release-type": "simple"}, "sub": {}}}
    ),
    "cannot derive",
)


def release_pr(**overrides):
    pr = {
        "number": 6,
        "state": "open",
        "draft": False,
        "title": "chore(main): release pi-launcher 1.2.0",
        "base": {"ref": "main"},
        "head": {"ref": "release-please--branches--main", "sha": HEAD_SHA},
        "user": {"login": "github-actions[bot]"},
        "labels": [{"name": "autorelease: pending"}],
    }
    pr.update(overrides)
    return pr


DEFAULT_FILES = [".release-please-manifest.json", "CHANGELOG.md", "version.txt"]

check(
    "accepts the exact release-please output PR",
    merge_pr.validate_pr(release_pr(), "main", allowed, DEFAULT_FILES) == HEAD_SHA,
)
check_raises(
    "refuses a PR carrying any non-release-please file",
    merge_pr.MergeRefused,
    lambda: merge_pr.validate_pr(
        release_pr(), "main", allowed, DEFAULT_FILES + ["src/pi-launcher.c"]
    ),
    "outside the release-please output set",
)
check_raises(
    "refuses a PR from a human branch",
    merge_pr.MergeRefused,
    lambda: merge_pr.validate_pr(
        release_pr(head={"ref": "feature/x", "sha": HEAD_SHA}), "main", allowed, DEFAULT_FILES
    ),
    "not a release-please branch",
)
check_raises(
    "refuses a PR opened by anyone but the Actions bot",
    merge_pr.MergeRefused,
    lambda: merge_pr.validate_pr(
        release_pr(user={"login": "someone"}), "main", allowed, DEFAULT_FILES
    ),
    "author",
)
check_raises(
    "refuses a PR that lost the autorelease label",
    merge_pr.MergeRefused,
    lambda: merge_pr.validate_pr(release_pr(labels=[]), "main", allowed, DEFAULT_FILES),
    "autorelease: pending",
)
check_raises(
    "refuses a PR that does not target the default branch",
    merge_pr.MergeRefused,
    lambda: merge_pr.validate_pr(
        release_pr(base={"ref": "next"}), "main", allowed, DEFAULT_FILES
    ),
    "default branch",
)
check_raises(
    "refuses a closed PR",
    merge_pr.MergeRefused,
    lambda: merge_pr.validate_pr(release_pr(state="closed"), "main", allowed, DEFAULT_FILES),
    "not open",
)
check_raises(
    "refuses an empty PR",
    merge_pr.MergeRefused,
    lambda: merge_pr.validate_pr(release_pr(), "main", allowed, []),
    "changes no files",
)


def merge_client(files=None, pr=None):
    return FakeGitHub(
        json_routes={
            f"/repos/{REPO}": {"default_branch": "main"},
            f"/repos/{REPO}/pulls/6": pr or release_pr(),
            f"/repos/{REPO}/git/ref/heads/main": {"object": {"sha": MERGE_SHA}},
            ("PUT", f"/repos/{REPO}/pulls/6/merge"): {"merged": True, "sha": MERGE_SHA},
        },
        list_routes={
            f"/repos/{REPO}/pulls/6/files": [
                {"filename": name} for name in (files or DEFAULT_FILES)
            ]
        },
    )


def run_merge(client, env_overrides=None):
    with tempfile.TemporaryDirectory() as tmp:
        output_path = Path(tmp) / "outputs"
        output_path.touch()
        env = {
            "GITHUB_REPOSITORY": REPO,
            "RELEASE_PLEASE_PR": json.dumps({"number": 6, "title": "release"}),
            "RELEASE_PLEASE_RELEASE_CREATED": "false",
        }
        env.update(env_overrides or {})
        saved_output = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = str(output_path)
        try:
            merge_pr.main(client=client, env=env, sleep=lambda _seconds: None)
            return outputs_from(output_path)
        finally:
            if saved_output is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = saved_output


client = merge_client()
merged_outputs = run_merge(client)
merge_calls = [call for call in client.writes if call[0] == "PUT"]
check(
    "merges the release PR as a squash pinned to the inspected head sha",
    len(merge_calls) == 1
    and merge_calls[0][1] == f"/repos/{REPO}/pulls/6/merge"
    and merge_calls[0][2]["merge_method"] == "squash"
    and merge_calls[0][2]["sha"] == HEAD_SHA
    and merged_outputs["merge_sha"] == MERGE_SHA,
    f"writes={client.writes}",
)

client = merge_client(files=DEFAULT_FILES + ["scripts/sign-app.sh"])
check_raises(
    "merges nothing when the PR touches a non-release-please file",
    merge_pr.MergeRefused,
    lambda: run_merge(client),
    "outside the release-please output set",
)
check("no merge attempt after a refusal", client.writes == [], f"writes={client.writes}")

check_raises(
    "refuses to merge when release-please produced no release PR",
    merge_pr.MergeRefused,
    lambda: run_merge(merge_client(), {"RELEASE_PLEASE_PR": ""}),
    "no release PR",
)
check_raises(
    "refuses to merge when a release already exists for this run",
    merge_pr.MergeRefused,
    lambda: run_merge(merge_client(), {"RELEASE_PLEASE_RELEASE_CREATED": "true"}),
    "unexpected state",
)

stuck = merge_client()
stuck.json_routes[f"/repos/{REPO}/git/ref/heads/main"] = {"object": {"sha": "9" * 40}}
check_raises(
    "fails when main never advances to the merge commit",
    merge_pr.MergeRefused,
    lambda: run_merge(stuck),
    "did not advance",
)


# =====================================================================
# pi-quarantine.py
# =====================================================================

QUARANTINE_LIST = f"/repos/{REPO}/issues?state=open&labels=upstream-pi-blocked"


def quarantine_client(issues):
    return FakeGitHub(
        json_routes={
            f"/repos/{REPO}/labels/upstream-pi-blocked": {"name": "upstream-pi-blocked"},
            ("POST", f"/repos/{REPO}/issues"): {
                "html_url": "https://example.invalid/issues/1",
                "number": 1,
            },
            ("PATCH", f"/repos/{REPO}/issues/1"): {
                "html_url": "https://example.invalid/issues/1",
                "number": 1,
            },
        },
        list_routes={QUARANTINE_LIST: issues},
    )


def run_quarantine(argv, client):
    with tempfile.TemporaryDirectory() as tmp:
        output_path = Path(tmp) / "outputs"
        output_path.touch()
        saved_output = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = str(output_path)
        try:
            code = quarantine.main(argv, client=client, env={"GITHUB_REPOSITORY": REPO})
            return code, outputs_from(output_path)
        finally:
            if saved_output is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = saved_output


blocked_issue = {
    "number": 1,
    "html_url": "https://example.invalid/issues/1",
    "body": "context\n<!-- pi-upstream-blocked: v9.9.9 -->\n",
}

_, status = run_quarantine(["status", "--key", "v9.9.9"], quarantine_client([blocked_issue]))
check(
    "a blocked tag reports as quarantined with its issue",
    status["quarantined"] == "true" and status["issue_url"].endswith("/issues/1"),
    f"status={status}",
)

_, status = run_quarantine(["status", "--key", "v9.9.10"], quarantine_client([blocked_issue]))
check(
    "a different upstream release is evaluated on its own merits",
    status["quarantined"] == "false",
    f"status={status}",
)

_, status = run_quarantine(
    ["status", "--key", "v9.9.9"],
    quarantine_client([{"number": 2, "html_url": "x", "body": "unrelated", "pull_request": {}}]),
)
check("pull requests are not mistaken for quarantine issues", status["quarantined"] == "false")

client = quarantine_client([])
run_quarantine(
    [
        "open",
        "--key",
        "v9.9.9",
        "--stage",
        "the pre-mutation build and launcher gate",
        "--run-url",
        "https://example.invalid/run/1",
        "--base-sha",
        "abc123",
        "--pin-pushed",
        "false",
    ],
    client,
)
created = [call for call in client.writes if call[0] == "POST"]
check(
    "a failed gate files one labelled, keyed issue",
    len(created) == 1
    and created[0][1] == f"/repos/{REPO}/issues"
    and created[0][2]["labels"] == ["upstream-pi-blocked"]
    and "<!-- pi-upstream-blocked: v9.9.9 -->" in created[0][2]["body"]
    and "the pre-mutation build and launcher gate" in created[0][2]["body"]
    and "https://example.invalid/run/1" in created[0][2]["body"]
    and "abc123" in created[0][2]["body"],
    f"writes={client.writes}",
)

client = quarantine_client([blocked_issue])
run_quarantine(
    ["open", "--key", "v9.9.9", "--stage", "the signed release pipeline", "--pin-pushed", "true"],
    client,
)
check(
    "a repeat failure refreshes the same issue instead of alerting again",
    [call[0] for call in client.writes] == ["PATCH"]
    and "manual release dispatch" in client.writes[0][2]["body"],
    f"writes={client.writes}",
)

client = FakeGitHub(
    json_routes={("POST", f"/repos/{REPO}/issues"): {"html_url": "u", "number": 1}},
    list_routes={QUARANTINE_LIST: []},
)
run_quarantine(["open", "--key", "discovery", "--stage", "upstream discovery"], client)
check(
    "the quarantine label is created when the repository lacks it",
    [call[1] for call in client.writes] == [f"/repos/{REPO}/labels", f"/repos/{REPO}/issues"],
    f"writes={client.writes}",
)

# =====================================================================
# check-release-contract.py: release-please last-released version
# =====================================================================
#
# Reproduced 2026-08-15: gate run 31865351430 launched Pi 0.84.2, then
# check-release-contract.py failed `release-please is anchored at published
# v1.2.0` because launcher 1.2.1 had already updated the manifest. Quarantine
# then blocked v0.84.2. The last-released version must come from published
# tags, not a hardcoded launcher version.

contract = load_script("check-release-contract.py")
SIMPLE_CONFIG = {
    "bootstrap-sha": contract.RELEASE_PLEASE_BOOTSTRAP_SHA,
    "packages": {".": {"release-type": "simple"}},
}
# Tags as they existed after launcher 1.2.1: the tree the failed Pi gate saw.
TAGS_AFTER_1_2_1 = [
    "v1.0.0",
    "v1.1.0",
    "v1.2.0",
    "pi-launcher-v1.2.0",
    "v1.2.1",
]


def anchor(manifest_version, config=None, tags=None):
    return contract.release_please_anchor_status(
        {".": manifest_version},
        config if config is not None else SIMPLE_CONFIG,
        tags if tags is not None else TAGS_AFTER_1_2_1,
    )


ok, detail = anchor("1.2.1")
check(
    "after launcher 1.2.1 the Pi-updater contract still passes",
    ok,
    detail,
)
ok, detail = anchor("1.3.0", tags=TAGS_AFTER_1_2_1 + ["v1.3.0"])
check(
    "a later launcher release does not require a contract-script version bump",
    ok,
    detail,
)
ok, detail = anchor("1.2.0")
check(
    "a manifest left behind the latest published tag is refused",
    not ok and "manifest=1.2.0" in detail and "1.2.1" in detail,
    detail,
)
ok, detail = anchor(
    "1.2.1",
    config={
        "bootstrap-sha": "0" * 40,
        "packages": {".": {"release-type": "simple"}},
    },
)
check(
    "moving the historical bootstrap-sha is refused",
    not ok and "bootstrap-sha" in detail,
    detail,
)
check(
    "component-prefixed tags are not treated as published launcher versions",
    contract.latest_published_launcher_version(
        ["pi-launcher-v1.2.0", "v1.2.0", "v1.1.0"]
    )
    == "1.2.0",
)
ok, detail = anchor("1.2.1", tags=["pi-launcher-v1.2.1"])
check(
    "a tree with only a component-prefixed tag has no published version",
    not ok and "no published" in detail,
    detail,
)

print()
print(f"automation: {len(PASSES)} passed, {len(FAILURES)} failed")
sys.exit(1 if FAILURES else 0)
