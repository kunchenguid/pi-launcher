#!/usr/bin/env python3
"""
check-release-contract.py - CI gate that validates the release pipeline and
packaging contracts without needing any signing secret.

Checks, in order:
  1. packaging/pi-release.json schema, pinning, and URL hygiene
  2. upstream SHA256SUMS still matches the pinned checksum (supply chain)
  3. .github/workflows/release.yml has every required gate, in order
  4. release.yml references exactly the canonical secret names
  5. release.yml pins the canonical Team ID and bundle ID
  6. release-please is anchored at v1.1.0 and calls the signed release workflow
  7. .github/workflows/ci.yml runs the test suite on pull requests
  8. homebrew/pi-launcher.rb matches the release artifact contract
  9. no private key material anywhere in the tracked tree
"""

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


# ---- 1. pi-release.json ----
manifest = json.loads((ROOT / "packaging/pi-release.json").read_text())
required_keys = {
    "upstream",
    "tag",
    "version",
    "url",
    "sha256",
    "sumsUrl",
    "licenseUrl",
    "licenseSha256",
}
check(
    "pi-release.json has all required keys",
    required_keys <= set(manifest),
    f"missing: {required_keys - set(manifest)}",
)
check(
    "pi-release.json pins a 64-hex sha256",
    re.fullmatch(r"[0-9a-f]{64}", manifest.get("sha256", "")) is not None
    and re.fullmatch(r"[0-9a-f]{64}", manifest.get("licenseSha256", "")) is not None,
)
upstream = "https://github.com/earendil-works/pi"
check(
    "pi-release.json URLs are https and upstream-pinned",
    manifest.get("upstream") == upstream
    and manifest.get("url", "").startswith(upstream + "/releases/download/")
    and manifest.get("sumsUrl", "").startswith(upstream + "/releases/download/")
    and manifest.get("licenseUrl", "").startswith(
        "https://raw.githubusercontent.com/earendil-works/pi/"
    ),
)
check(
    "pi-release.json tag/version consistency",
    manifest.get("tag") == "v" + manifest.get("version", "\x00")
    and manifest.get("tag", "") in manifest.get("url", "")
    and manifest.get("tag", "") in manifest.get("licenseUrl", "")
    and manifest.get("tag", "") in manifest.get("sumsUrl", ""),
)

# ---- 2. upstream SHA256SUMS cross-check ----
try:
    with urllib.request.urlopen(manifest["sumsUrl"], timeout=30) as resp:
        sums = resp.read().decode()
    entry = next(
        (
            line.split()[0]
            for line in sums.splitlines()
            if line.split()[-1] == "pi-darwin-arm64.tar.gz"
        ),
        None,
    )
    check(
        "upstream SHA256SUMS matches the pinned checksum",
        entry == manifest["sha256"],
        f"upstream={entry} pinned={manifest['sha256']}",
    )
except Exception as exc:  # network failure must not silently pass
    check("upstream SHA256SUMS matches the pinned checksum", False, repr(exc))

# ---- 3. release.yml gates, in order ----
release_yml = (ROOT / ".github/workflows/release.yml").read_text()
required_sequence = [
    "scripts/fetch-pi.sh",
    "scripts/build-app.sh",
    "scripts/sign-app.sh",
    '--signed',
    "notarytool submit",
    "stapler staple",
    "--signed --gatekeeper",
    "gh release create",
    "gh release upload",
    "gh release download",
    "shasum -a 256 -c",
]
positions = []
for token in required_sequence:
    pos = release_yml.find(token)
    if pos < 0:
        break
    positions.append(pos)
check(
    "release.yml has every required gate in order",
    len(positions) == len(required_sequence)
    and positions == sorted(positions),
    f"found {len(positions)}/{len(required_sequence)} tokens, ordered={positions == sorted(positions)}",
)
for token in (
    "Verify the publication-ready artifact",
    "Verify the published artifact",
    "runs-on: macos-26",
):
    check(f"release.yml contains '{token}'", token in release_yml)

# ---- 4. canonical secrets only ----
referenced = set(re.findall(r"secrets\.([A-Z0-9_]+)", release_yml))
canonical = {
    "APP_STORE_CONNECT_KEY_ID",
    "APP_STORE_CONNECT_ISSUER_ID",
    "APP_STORE_CONNECT_API_KEY",
    "MAC_DEVELOPER_ID_CERT_P12",
    "MAC_DEVELOPER_ID_CERT_PASSWORD",
    "GITHUB_TOKEN",
}
check(
    "release.yml references exactly the canonical secrets",
    referenced == canonical,
    f"referenced={sorted(referenced)}",
)

# ---- 5. pinned identity ----
check(
    "release.yml pins Team ID, bundle ID, and Developer ID identity",
    "TEAM_ID: 9T2J7MNUP9" in release_yml
    and "BUNDLE_ID: com.kunchenguid.pi-launcher" in release_yml
    and "Developer ID Application: Kun Chen (9T2J7MNUP9)" in release_yml,
)

# ---- 6. release-please handoff ----
release_please_config = json.loads((ROOT / "release-please-config.json").read_text())
release_please_manifest = json.loads(
    (ROOT / ".release-please-manifest.json").read_text()
)
release_please_yml = (ROOT / ".github/workflows/release-please.yml").read_text()
check(
    "release-please is anchored at published v1.1.0",
    release_please_manifest == {".": "1.1.0"}
    and release_please_config.get("bootstrap-sha")
    == "431fb3ae841dbb46ac81105b72eb5c62c5b6f997"
    and release_please_config.get("packages", {}).get(".", {}).get("release-type")
    == "simple",
)
check(
    "release-please calls the signed release workflow after creating a release",
    "release_created: ${{ steps.release.outputs.release_created }}"
    in release_please_yml
    and "tag_name: ${{ steps.release.outputs.tag_name }}" in release_please_yml
    and "version: ${{ steps.release.outputs.version }}" in release_please_yml
    and "if: ${{ needs.release-please.outputs.release_created == 'true' }}"
    in release_please_yml
    and "uses: ./.github/workflows/release.yml" in release_please_yml
    and "tag_name: ${{ needs.release-please.outputs.tag_name }}"
    in release_please_yml
    and "secrets: inherit" in release_please_yml,
)
check(
    "release.yml supports release-please while retaining tag and manual triggers",
    'tags:\n      - "v*"' in release_yml
    and "workflow_dispatch:" in release_yml
    and "workflow_call:" in release_yml
    and "description: Release tag created by release-please" in release_yml,
)

# ---- 7. ci.yml runs tests on PRs without secrets ----
ci_yml = (ROOT / ".github/workflows/ci.yml").read_text()
check(
    "ci.yml runs the full test suite on pull_request",
    "pull_request:" in ci_yml
    and "make test-icon" in ci_yml
    and "make test-functional" in ci_yml
    and "make test-pty" in ci_yml
    and "make test-smoke" in ci_yml,
)
check(
    "ci.yml does not reference signing secrets",
    "MAC_DEVELOPER_ID" not in ci_yml and "APP_STORE_CONNECT" not in ci_yml,
)

# ---- 8. Homebrew cask template matches the release contract ----
cask = (ROOT / "homebrew/pi-launcher.rb").read_text()
check(
    "cask url matches the release asset naming",
    "https://github.com/kunchenguid/pi-launcher/releases/download/v#{version}/Pi-Launcher-#{version}.zip"
    in cask,
)
check(
    "cask installs the app and a binary shim",
    'app "Pi Launcher.app"' in cask
    and 'binary "#{appdir}/Pi Launcher.app/Contents/MacOS/pi-launcher"' in cask,
)
check(
    "cask sha256 is a placeholder for the tap task",
    'sha256 "REPLACE_WITH_RELEASE_ZIP_SHA256"' in cask,
)

# ---- 9. no private key material in the tree ----
tracked = subprocess.run(
    ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
).stdout.split()
leaks = []
for name in tracked:
    path = ROOT / name
    if not path.is_file() or path.stat().st_size > 5_000_000:
        continue
    try:
        text = path.read_text(errors="strict")
    except (UnicodeDecodeError, ValueError):
        continue
    needle = "PRIVATE KEY" + "-----"
    if needle in text:
        leaks.append(name)
check("no private key material tracked", not leaks, f"leaks={leaks}")

print()
if FAILURES:
    print(f"contract: {len(FAILURES)} FAILED")
    sys.exit(1)
print("contract: all checks passed")
