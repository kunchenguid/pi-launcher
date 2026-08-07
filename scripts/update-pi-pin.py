#!/usr/bin/env python3
"""
update-pi-pin.py - deterministic, stable-only upstream Pi discovery and pin
generator for packaging/pi-release.json.

The target repository, asset names, and license path are compiled in. Nothing
about the resolved pin comes from a workflow input: there is no way to point
this at another repository, tag, URL, or checksum. Every candidate must be a
published, non-draft, non-prerelease `vX.Y.Z` release whose arm64 archive
checksum comes from the upstream SHA256SUMS asset, cross-checked against the
release asset digest GitHub reports.

Usage:
  update-pi-pin.py --check    # resolve and report; never touches the manifest
  update-pi-pin.py --write    # resolve, report, and write a strictly newer pin

Exit status is 0 for both "already current" and "update available"; any
provenance, shape, or validation failure exits non-zero so the caller halts.
"""

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_api import GitHubError, client_from_env, emit_outputs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "packaging/pi-release.json"

UPSTREAM_OWNER = "earendil-works"
UPSTREAM_REPO = "pi"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_OWNER}/{UPSTREAM_REPO}"
RAW_ROOT = f"https://raw.githubusercontent.com/{UPSTREAM_OWNER}/{UPSTREAM_REPO}"
ARCHIVE_ASSET = "pi-darwin-arm64.tar.gz"
SUMS_ASSET = "SHA256SUMS"
LICENSE_PATH = "LICENSE"

STABLE_TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The manifest key order is part of the file's contract; keep it stable so a
# generated pin is byte-identical to a hand-written one.
MANIFEST_KEYS = (
    "upstream",
    "tag",
    "version",
    "url",
    "sha256",
    "sumsUrl",
    "licenseUrl",
    "licenseSha256",
)


class UpstreamError(RuntimeError):
    """The upstream release does not satisfy the stable-only pin contract."""


def semver(version):
    match = STABLE_TAG_RE.match("v" + version)
    if not match:
        raise UpstreamError(f"not a canonical stable version: {version!r}")
    return tuple(int(part) for part in match.groups())


def sole_asset(assets, name):
    matches = [asset for asset in assets if asset.get("name") == name]
    if len(matches) != 1:
        raise UpstreamError(
            f"expected exactly one {name!r} asset, found {len(matches)}"
        )
    asset = matches[0]
    if asset.get("state") != "uploaded":
        raise UpstreamError(f"{name!r} asset state is {asset.get('state')!r}")
    if not isinstance(asset.get("id"), int):
        raise UpstreamError(f"{name!r} asset has no numeric id")
    return asset


def sums_entry(sums_text, filename):
    """Return the single 64-hex checksum SHA256SUMS records for `filename`."""
    entries = []
    for line in sums_text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        digest, name = fields
        if name.lstrip("*") == filename:
            entries.append(digest)
    if len(entries) != 1:
        raise UpstreamError(
            f"SHA256SUMS has {len(entries)} entries for {filename!r}, expected 1"
        )
    if not SHA256_RE.fullmatch(entries[0]):
        raise UpstreamError(f"SHA256SUMS entry for {filename!r} is not 64 lowercase hex")
    return entries[0]


def asset_digest(asset):
    """Return the sha256 GitHub reports for an asset, when it reports one."""
    digest = asset.get("digest")
    if not digest:
        return None
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or not SHA256_RE.fullmatch(value):
        raise UpstreamError(f"unusable asset digest {digest!r}")
    return value


def build_manifest(tag, archive_sha256, license_sha256):
    version = tag[1:]
    return {
        "upstream": UPSTREAM_URL,
        "tag": tag,
        "version": version,
        "url": f"{UPSTREAM_URL}/releases/download/{tag}/{ARCHIVE_ASSET}",
        "sha256": archive_sha256,
        "sumsUrl": f"{UPSTREAM_URL}/releases/download/{tag}/{SUMS_ASSET}",
        "licenseUrl": f"{RAW_ROOT}/{tag}/{LICENSE_PATH}",
        "licenseSha256": license_sha256,
    }


def resolve_latest_stable(client):
    """Resolve the latest stable upstream release into a candidate manifest.

    Returns (manifest, meta). Raises UpstreamError for anything that is not an
    unambiguous, fully published stable release.
    """
    repo_path = f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}"
    release = client.get_json(f"{repo_path}/releases/latest")

    tag = release.get("tag_name") or ""
    if not STABLE_TAG_RE.fullmatch(tag):
        raise UpstreamError(f"latest release tag {tag!r} is not a stable vX.Y.Z tag")
    if release.get("draft"):
        raise UpstreamError(f"{tag} is a draft release")
    if release.get("prerelease"):
        raise UpstreamError(f"{tag} is a prerelease")
    if not release.get("published_at"):
        raise UpstreamError(f"{tag} has no published_at timestamp")

    assets = release.get("assets") or []
    archive = sole_asset(assets, ARCHIVE_ASSET)
    sums = sole_asset(assets, SUMS_ASSET)

    manifest = build_manifest(tag, "0" * 64, "0" * 64)
    for asset, expected_url in ((archive, manifest["url"]), (sums, manifest["sumsUrl"])):
        if asset.get("browser_download_url") != expected_url:
            raise UpstreamError(
                f"asset {asset.get('name')!r} download URL "
                f"{asset.get('browser_download_url')!r} is not {expected_url!r}"
            )

    sums_text = client.get_bytes(
        f"{repo_path}/releases/assets/{sums['id']}"
    ).decode("utf-8", "strict")
    archive_sha256 = sums_entry(sums_text, ARCHIVE_ASSET)

    reported = asset_digest(archive)
    if reported is not None and reported != archive_sha256:
        raise UpstreamError(
            f"release asset digest {reported} does not match the SHA256SUMS entry "
            f"{archive_sha256}"
        )

    tag_ref = client.get_json(f"{repo_path}/git/ref/tags/{tag}")
    if tag_ref.get("ref") != f"refs/tags/{tag}":
        raise UpstreamError(f"tag {tag} does not resolve to refs/tags/{tag}")
    commit = (tag_ref.get("object") or {}).get("sha") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise UpstreamError(f"tag {tag} resolves to an unusable object {commit!r}")

    license_blob = client.get_json(f"{repo_path}/contents/{LICENSE_PATH}?ref={tag}")
    if license_blob.get("encoding") != "base64":
        raise UpstreamError(
            f"{LICENSE_PATH} at {tag} is {license_blob.get('encoding')!r}-encoded"
        )
    license_bytes = base64.b64decode(license_blob.get("content") or "")
    if not license_bytes:
        raise UpstreamError(f"{LICENSE_PATH} at {tag} is empty")
    license_sha256 = hashlib.sha256(license_bytes).hexdigest()

    manifest = build_manifest(tag, archive_sha256, license_sha256)
    if list(manifest) != list(MANIFEST_KEYS):
        raise UpstreamError("generated manifest key order drifted from the contract")
    return manifest, {"commit": commit, "published_at": release["published_at"]}


def render_manifest(manifest):
    return json.dumps(manifest, indent=2) + "\n"


def load_current(path=None):
    return json.loads(Path(path or MANIFEST_PATH).read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_const",
        const="check",
        dest="mode",
        help="resolve and report only",
    )
    mode.add_argument(
        "--write",
        action="store_const",
        const="write",
        dest="mode",
        help="also write a strictly newer pin into packaging/pi-release.json",
    )
    args = parser.parse_args(argv)

    current = load_current()
    current_version = current["version"]
    manifest, meta = resolve_latest_stable(client_from_env())
    target_version = manifest["version"]

    newer = semver(target_version) > semver(current_version)
    print(f"pinned:  pi {current_version}")
    print(f"latest:  pi {target_version} ({manifest['tag']} @ {meta['commit'][:12]})")
    print(f"sha256:  {manifest['sha256']}")

    updated = False
    if not newer:
        print("update-pi-pin: already current, no change")
    elif args.mode == "write":
        MANIFEST_PATH.write_text(render_manifest(manifest))
        updated = True
        print(f"update-pi-pin: wrote {MANIFEST_PATH} for {manifest['tag']}")
    else:
        print(f"update-pi-pin: {target_version} is newer than the pinned {current_version}")

    emit_outputs(
        {
            "update_available": "true" if newer else "false",
            "updated": "true" if updated else "false",
            "current_version": current_version,
            "target_version": target_version,
            "target_tag": manifest["tag"],
            "target_sha256": manifest["sha256"],
            "upstream_commit": meta["commit"],
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (UpstreamError, GitHubError) as exc:
        print(f"update-pi-pin: FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
