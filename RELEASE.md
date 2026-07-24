# pi-launcher release pipeline

Releases are tag-based. Pushing a tag like `v1.2.3` (or dispatching `.github/workflows/release.yml` with a tag name) builds, signs, notarizes, staples, verifies, and publishes `Pi-Launcher-1.2.3.zip` plus `Pi-Launcher-1.2.3.zip.sha256` to the GitHub release for that tag.

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

1. Merge everything that should ship. `main` must be green.
2. Tag: `git tag v1.2.3 && git push origin v1.2.3` (or dispatch the workflow with the tag name).
3. The workflow resolves the version from the tag, stamps it into the bundle, fetches and re-verifies the pinned upstream Pi, builds, signs inside-out, notarizes, staples, verifies the publication-ready zip, publishes, then downloads the published asset and verifies it again.
4. If any step fails, do not retry blindly: the failure is the gate working. Fix the cause, re-tag.

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
