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
  6. Homebrew tap updates only after the published artifact is verified
  7. release-please is anchored at v1.2.1, tags releases as bare vX.Y.Z with
     no component prefix, keeps the human path, and gates the autonomous
     merge mode used by the upstream Pi updater
  8. .github/workflows/ci.yml runs the test suite on pull requests
  9. .github/workflows/upstream-pi-sync.yml gates every upstream Pi bump before
     it can reach main, and quarantines a failure instead of retrying
  10. homebrew/pi-launcher.rb matches the release artifact contract
  11. no private key material anywhere in the tracked tree
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

# A signature check proves structure, not launchability: the signed bundle,
# the publication-ready zip, and the downloaded published zip must each start
# the bundled Pi and report the pinned version.
launch_gates = [m.start() for m in re.finditer(r"scripts/check-app-version\.sh", release_yml)]
check(
    "release.yml launches the signed app, the publication zip, and the published zip",
    len(launch_gates) == 3
    and release_yml.find("- name: Verify the signed app")
    < launch_gates[0]
    < release_yml.find("- name: Package for notarization")
    and release_yml.find("- name: Verify the publication-ready artifact")
    < launch_gates[1]
    < release_yml.find("- name: Compute artifact checksum")
    and release_yml.find("- name: Verify the published artifact")
    < launch_gates[2]
    < release_yml.find("- name: Update Homebrew Cask"),
    f"gates at {launch_gates}",
)

# ---- 4. canonical secrets only ----
referenced = set(re.findall(r"secrets\.([A-Z0-9_]+)", release_yml))
canonical = {
    "APP_STORE_CONNECT_KEY_ID",
    "APP_STORE_CONNECT_ISSUER_ID",
    "APP_STORE_CONNECT_API_KEY",
    "MAC_DEVELOPER_ID_CERT_P12",
    "MAC_DEVELOPER_ID_CERT_PASSWORD",
    "GITHUB_TOKEN",
    "HOMEBREW_TAP_TOKEN",
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

# ---- 6. Homebrew tap handoff ----
publish_pos = release_yml.find("- name: Publish the GitHub release")
published_verify_pos = release_yml.find("- name: Verify the published artifact")
homebrew_pos = release_yml.find("- name: Update Homebrew Cask")
cleanup_pos = release_yml.find("- name: Remove the signing keychain")
check(
    "Homebrew update runs only after publication and published-artifact verification",
    -1 not in (publish_pos, published_verify_pos, homebrew_pos, cleanup_pos)
    and publish_pos < published_verify_pos < homebrew_pos < cleanup_pos,
)
check(
    "Homebrew update consumes the release version, URL, and checksum outputs",
    'VERSION: ${{ steps.version.outputs.release_version }}' in release_yml
    and 'SHA256: ${{ steps.checksum.outputs.sha256 }}' in release_yml
    and 'DOWNLOAD_URL: ${{ steps.version.outputs.cask_download_url }}' in release_yml
    and 'echo "sha256=$SHA256" >> "$GITHUB_OUTPUT"' in release_yml
    and 'CASK_TAG_NAME="${TAG_NAME/$RELEASE_VERSION/$VERSION_REF}"' in release_yml
    and 'CASK_ZIP_NAME="${ZIP_NAME/$RELEASE_VERSION/$VERSION_REF}"' in release_yml,
)
check(
    "Homebrew tap credentials are ephemeral and absent from the remote URL",
    'git_with_tap_auth clone "https://github.com/kunchenguid/homebrew-tap.git"'
    in release_yml
    and "password=$HOMEBREW_TAP_TOKEN" in release_yml
    and "x-access-token:${HOMEBREW_TAP_TOKEN}@" not in release_yml,
)
expected_generated_cask = '''cask "pi-launcher" do
  version "${VERSION}"
  sha256 "${SHA256}"

  url "${DOWNLOAD_URL}"
  name "Pi Launcher"
  desc "Run the bundled Pi CLI under a stable, signed app identity"
  homepage "https://github.com/kunchenguid/pi-launcher"

  depends_on arch: :arm64
  depends_on macos: :ventura

  app "Pi Launcher.app"
  binary "#{appdir}/Pi Launcher.app/Contents/MacOS/pi-launcher", target: "pi-signed"

  uninstall quit: "com.kunchenguid.pi-launcher"
end
'''
heredoc_match = re.search(
    r'cat > "\$RUNNER_TEMP/homebrew-tap/Casks/pi-launcher\.rb" << CASK_EOF\n'
    r'(?P<body>.*?)'
    r'^          CASK_EOF$',
    release_yml,
    re.MULTILINE | re.DOTALL,
)
generated_cask = ""
if heredoc_match:
    generated_cask = "\n".join(
        line.removeprefix("          ")
        for line in heredoc_match.group("body").splitlines()
    ) + "\n"
check(
    "Homebrew generator preserves the seeded pi-launcher cask structure",
    generated_cask == expected_generated_cask,
)

# ---- 7. release-please handoff ----
release_please_config = json.loads((ROOT / "release-please-config.json").read_text())
release_please_manifest = json.loads(
    (ROOT / ".release-please-manifest.json").read_text()
)
release_please_yml = (ROOT / ".github/workflows/release-please.yml").read_text()
check(
    "release-please is anchored at published v1.2.1",
    release_please_manifest == {".": "1.2.1"}
    and release_please_config.get("bootstrap-sha")
    == "431fb3ae841dbb46ac81105b72eb5c62c5b6f997"
    and release_please_config.get("packages", {}).get(".", {}).get("release-type")
    == "simple",
)
# Component-in-tag defaults to true upstream, which produced the empty
# `pi-launcher-v1.2.0` release: release.yml only accepts a bare `vX.Y.Z` tag.
# This must stay an explicit false, not merely absent, so package-name can
# never silently reintroduce a component prefix.
check(
    "release-please-config.json pins tags to vX.Y.Z with no component prefix",
    release_please_config.get("packages", {}).get(".", {}).get(
        "include-component-in-tag"
    )
    is False,
)
check(
    "release-please calls the signed release workflow after creating a release",
    "release_created: ${{ steps.outcome.outputs.release_created }}"
    in release_please_yml
    and "tag_name: ${{ steps.outcome.outputs.tag_name }}" in release_please_yml
    and "version: ${{ steps.outcome.outputs.version }}" in release_please_yml
    and "if: ${{ needs.release-please.outputs.release_created == 'true' }}"
    in release_please_yml
    and "uses: ./.github/workflows/release.yml" in release_please_yml
    and "tag_name: ${{ needs.release-please.outputs.tag_name }}"
    in release_please_yml
    and "secrets: inherit" in release_please_yml,
)
check(
    "release-please keeps the human push-to-main path",
    "push:\n    branches:\n      - main" in release_please_yml,
)
# Autonomous mode is opt-in per call, merges through the checked-in guard, and
# re-runs release-please in the same run because a GITHUB_TOKEN merge does not
# start a new workflow.
check(
    "release-please auto-merge is an opt-in, guarded, two-pass mode",
    "workflow_call:" in release_please_yml
    and "auto_merge_release_pr:" in release_please_yml
    and "default: false" in release_please_yml
    and release_please_yml.count("googleapis/release-please-action@v4") == 2
    and "python3 scripts/merge-release-pr.py" in release_please_yml
    and release_please_yml.count("if: ${{ inputs.auto_merge_release_pr }}") == 2,
)
check(
    "release.yml supports release-please while retaining tag and manual triggers",
    'tags:\n      - "v*"' in release_yml
    and "workflow_dispatch:" in release_yml
    and "workflow_call:" in release_yml
    and "description: Release tag created by release-please" in release_yml,
)

# ---- 8. ci.yml runs tests on PRs without secrets ----
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
    "ci.yml runs the release automation script tests",
    "make test-scripts" in ci_yml,
)
check(
    "ci.yml does not reference signing secrets",
    "MAC_DEVELOPER_ID" not in ci_yml and "APP_STORE_CONNECT" not in ci_yml,
)

# ---- 9. the autonomous upstream Pi updater ----
sync_yml = (ROOT / ".github/workflows/upstream-pi-sync.yml").read_text()
check(
    "upstream-pi-sync runs on a schedule and never cancels itself mid-release",
    "schedule:" in sync_yml
    and re.search(r"- cron: \"\d+ \d+ \* \* \*\"", sync_yml) is not None
    and "group: pi-upstream-sync" in sync_yml
    and "cancel-in-progress: false" in sync_yml,
)
check(
    "upstream-pi-sync discovers on Ubuntu and gates on macOS",
    "runs-on: ubuntu-latest" in sync_yml and "runs-on: macos-26" in sync_yml,
)
check(
    "upstream-pi-sync never reads a signing or notarization secret",
    "MAC_DEVELOPER_ID" not in sync_yml
    and "APP_STORE_CONNECT" not in sync_yml
    and "HOMEBREW_TAP_TOKEN" not in sync_yml,
)
# The whole point of the updater: every gate runs against the candidate pin
# before a single byte of it reaches main.
sync_sequence = [
    "scripts/update-pi-pin.py --check",
    "scripts/pi-quarantine.py status",
    "scripts/update-pi-pin.py --write",
    "scripts/fetch-pi.sh",
    "make test-icon",
    "make test-functional",
    "make test-pty",
    "make test-smoke",
    "python3 scripts/check-release-contract.py",
    "git push origin",
]
sync_positions = []
for token in sync_sequence:
    pos = sync_yml.find(token)
    if pos < 0:
        break
    sync_positions.append(pos)
check(
    "upstream-pi-sync gates the candidate pin before it can reach main",
    len(sync_positions) == len(sync_sequence)
    and sync_positions == sorted(sync_positions),
    f"found {len(sync_positions)}/{len(sync_sequence)} tokens, "
    f"ordered={sync_positions == sorted(sync_positions)}",
)
check(
    "upstream-pi-sync pushes only onto the exact commit it tested",
    'if [ "$REMOTE" != "$BASE_SHA" ]' in sync_yml
    and "--force" not in sync_yml,
)
check(
    "upstream-pi-sync releases only through release-please autonomous mode",
    "uses: ./.github/workflows/release-please.yml" in sync_yml
    and "auto_merge_release_pr: true" in sync_yml
    and "secrets: inherit" in sync_yml
    and "if: ${{ needs.gate.outputs.pushed == 'true' }}" in sync_yml,
)
check(
    "upstream-pi-sync quarantines a failure instead of retrying it",
    "scripts/pi-quarantine.py open" in sync_yml
    and "needs.discover.result == 'failure'" in sync_yml
    and "needs.gate.result == 'failure'" in sync_yml
    and "needs.release.result == 'failure'" in sync_yml
    and "needs.discover.outputs.quarantined != 'true'" in sync_yml,
)

# ---- 10. Homebrew cask template matches the release contract ----
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

# ---- 11. no private key material in the tree ----
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
