# pi-launcher release pipeline

Releases are managed by release-please. Conventional commits merged to `main` produce or update a release PR containing the next version and changelog. Merging that PR creates the `v<version>` tag and GitHub release, then the same workflow calls `.github/workflows/release.yml` to build, sign, notarize, staple, verify, and upload `Pi-Launcher-<version>.zip` plus its checksum. The signed release workflow also retains its tag-push and manual dispatch entry points for recovery.

The pipeline stops at any failed gate: pinned-checksum or upstream `SHA256SUMS` mismatch, missing nested signature, wrong identity, wrong Team ID, wrong bundle ID, missing hardened runtime, missing secure timestamp, unexpected entitlements, notarization failure, stapling failure, Gatekeeper rejection, wrong architecture, a signed or published bundle that no longer launches the pinned Pi, or a mismatch between the verified zip and the published download. The last two verification steps run against the exact zip that users and Homebrew download, not an intermediate app, and each of them launches that app and checks it reports the pinned Pi version.

## Automatic upstream Pi updates

`.github/workflows/upstream-pi-sync.yml` keeps the bundled Pi current without a human in the loop. It runs daily and, on a normal day, costs one short Ubuntu job:

1. **Discover** (Ubuntu). `scripts/update-pi-pin.py --check` resolves `earendil-works/pi`'s latest release. It is stable-only: a published, non-draft, non-prerelease, exactly-`vX.Y.Z` release with exactly one `pi-darwin-arm64.tar.gz` and one `SHA256SUMS` asset. The checksum comes from the official `SHA256SUMS`, cross-checked against the asset digest GitHub reports; the license SHA-256 is computed from the tag-pinned `LICENSE`. Nothing about the target repository, asset names, tag, URL, or checksum can come from a workflow input. If the pin is already current, the run stops here.
2. **Quarantine check.** If an open `upstream-pi-blocked` issue is keyed to that exact Pi tag, the run stops with a visible no-op. Close the issue to re-enable attempts for that tag. A newer stable Pi is a separate candidate and is unaffected.
3. **Gate** (macOS). The candidate pin is written into the workspace but *not* committed. The run then does everything CI does - lint, strict compile, provenance fetch, icon, functional, real-PTY, bundled-Pi smoke, and `check-release-contract.py` - against the candidate. Only if every gate passes does it commit `fix: bundle Pi X.Y.Z` and fast-forward push to `main`, and only if `main` is still the exact commit those gates ran against.
4. **Release.** The updater calls `release-please.yml` with `auto_merge_release_pr: true`. That autonomous mode merges exactly one PR - the release-please-generated release PR, verified by `scripts/merge-release-pr.py` to be open, bot-authored, on a `release-please--branches--` head, labelled `autorelease: pending`, and touching nothing outside the release-please output set - with a squash merge pinned to the inspected head SHA. Because a `GITHUB_TOKEN` merge does not start a new workflow run, release-please is invoked a second time in the same run to create the tag and release, which then hands off to the normal signed release workflow below. Repository-wide auto-merge stays off, and nothing else in this repository is ever auto-merged.
5. **Quarantine on failure.** Any failure - discovery, provenance, gate, merge, signing, notarization, publication, Homebrew - fails the run and files (or refreshes) one labelled `upstream-pi-blocked` issue keyed by the exact Pi tag, carrying the stage, the tested `main` SHA, and the run URL. That tag is not attempted again until the issue is closed. There is no blind daily retry.

`workflow_dispatch` offers a `dry_run` option that runs discovery and the full gate but never touches `main`. If a failure happens *after* the pin commit lands, the issue says so: the manifest is already current, so daily discovery will report no update, and recovery is the manual release dispatch below, after diagnosis.

The updater reads no signing or notarization secret. It maintains the supply-chain pin only; the app itself still has no self-update path.

## One-time setup

Use Apple Team `9T2J7MNUP9`. Reuse the existing **Developer ID Application** certificate (do not create a new one; Apple caps them per team) and the existing App Store Connect API key. Set exactly these repository secrets on `kunchenguid/pi-launcher`:

```sh
REPO=kunchenguid/pi-launcher
base64 -i DeveloperID_Application.p12 | tr -d '\n' | gh secret set MAC_DEVELOPER_ID_CERT_P12 --repo "$REPO"
printf '%s' 'THE_P12_PASSWORD' | gh secret set MAC_DEVELOPER_ID_CERT_PASSWORD --repo "$REPO"
printf '%s' 'KEY_ID' | gh secret set APP_STORE_CONNECT_KEY_ID --repo "$REPO"
printf '%s' 'ISSUER_ID' | gh secret set APP_STORE_CONNECT_ISSUER_ID --repo "$REPO"
base64 -i "AuthKey_KEY_ID.p8" | gh secret set APP_STORE_CONNECT_API_KEY --repo "$REPO"
```

On GNU/Linux, use `base64 -w0` instead of `base64 -i`. Never commit the `.p12`, the `.p8`, or either password. The workflow decodes the p12 into a throwaway keychain on the runner and validates that it actually contains `Developer ID Application: Kun Chen (9T2J7MNUP9)` before signing anything.

## Cut a release

1. Merge conventional commits for everything that should ship. `main` must be green.
2. Review the release-please PR. Confirm its proposed version and generated `CHANGELOG.md`, then merge it. Do not create or push the release tag by hand.
3. Release-please creates the tag and GitHub release. Its downstream `signed-release` job calls the existing release workflow with that tag, avoiding GitHub's suppression of separate workflows triggered by `GITHUB_TOKEN` events.
4. The called workflow stamps the tag version into the bundle, fetches and re-verifies the pinned upstream Pi, builds, signs inside-out, notarizes, staples, verifies the publication-ready zip, uploads it to the release, then downloads and verifies it again.
5. If any step fails, do not retry blindly: the failure is the gate working. Fix the cause. The release workflow's manual dispatch remains available for recovery against the existing tag.

The release-please manifest's last-released version must match the highest published bare `vX.Y.Z` tag. `scripts/check-release-contract.py` derives that version from git tags rather than hardcoding it; a new launcher release must not require a contract-script bump, or the daily Pi updater will fail the gate and quarantine the Pi tag (as `v0.84.2` was after launcher `1.2.1` shipped while the check still named `1.2.0`). Never move the manifest backward or reuse an existing version. The historical `bootstrap-sha` in `release-please-config.json` is the start commit and must not move.

Release-please tags are always bare `v<version>` (`release-please-config.json` sets `include-component-in-tag: false`), matching `release.yml`'s tag parsing, the Homebrew cask download URL, and `scripts/check-release-contract.py`. If `package-name` is ever needed for something else, that flag must stay explicit or a future release-please default change can silently reintroduce a component-prefixed tag (`<package>-v<version>`) that `release.yml` rejects and that ships an empty, asset-less release - exactly what happened with `pi-launcher-v1.2.0`.

## Artifact contract (consumed by the Homebrew tap)

- Asset: `Pi-Launcher-<version>.zip` containing exactly `Pi Launcher.app`.
- Checksum sidecar: `Pi-Launcher-<version>.zip.sha256` in `shasum -a 256` format.
- The app: bundle id `com.kunchenguid.pi-launcher`, executable `Contents/MacOS/pi-launcher`, LSUIElement, arm64, hardened runtime, notarized and stapled. Its designated requirement pins the identifier and Team ID.
- The cask shape lives at `homebrew/pi-launcher.rb`. The tap task copies it into the captain's tap, sets `version` and `sha256` from the release, and chooses the shim target name (template suggests `pi-signed`; it must never shadow an existing `pi`).

## Local validation without secrets

Everything except signing and notarization runs locally:

```sh
make test                                 # full suite on the unsigned dev app
python3 scripts/check-release-contract.py # release/config contracts
```

Signed and notarized builds require the certificate and API key above, which exist only as GitHub secrets.
