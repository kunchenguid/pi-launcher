# pi-launcher release pipeline

Releases are managed by release-please. Conventional commits merged to `main` produce or update a release PR containing the next version and changelog. Merging that PR creates the `v<version>` tag and GitHub release, then the same workflow calls `.github/workflows/release.yml` to build, sign, notarize, staple, verify, and upload `Pi-Launcher-<version>.zip` plus its checksum. The signed release workflow also retains its tag-push and manual dispatch entry points for recovery.

The pipeline stops at any failed gate: pinned-checksum or upstream `SHA256SUMS` mismatch, missing nested signature, wrong identity, wrong Team ID, wrong bundle ID, missing hardened runtime, missing secure timestamp, unexpected entitlements, notarization failure, stapling failure, Gatekeeper rejection, wrong architecture, or a mismatch between the verified zip and the published download. The last two verification steps run against the exact zip that users and Homebrew download, not an intermediate app.

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

The release-please manifest is anchored at the published `v1.1.0` release. Never move it backward or manually reuse an existing version.

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
