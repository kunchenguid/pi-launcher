#!/bin/bash
# verify-app.sh - release gate: verify structure, signatures, identity,
# hardening, and (optionally) Gatekeeper acceptance of "Pi Launcher.app".
#
# Modes:
#   verify-app.sh <app>                 structural + ad-hoc-safe checks (CI on PRs)
#   verify-app.sh <app> --signed        additionally require Developer ID identity,
#                                       hardened runtime, Team ID, entitlements
#   verify-app.sh <app> --signed --gatekeeper
#                                       additionally require a stapled notarization
#                                       ticket and spctl acceptance
#
# Every failed check stops the release. There is no warn-only path.
set -euo pipefail

APP_DIR="${1:-}"
MODE_SIGNED=0
MODE_GATEKEEPER=0
for arg in "${@:2}"; do
  case "$arg" in
    --signed) MODE_SIGNED=1 ;;
    --gatekeeper) MODE_GATEKEEPER=1 ;;
    *) echo "verify-app: FAIL: unknown argument $arg" >&2; exit 1 ;;
  esac
done
if [[ "$MODE_GATEKEEPER" -eq 1 ]]; then
  MODE_SIGNED=1
fi

EXPECTED_BUNDLE_ID="com.kunchenguid.pi-launcher"
EXPECTED_TEAM_ID="9T2J7MNUP9"
EXPECTED_EXECUTABLE="pi-launcher"
EXPECTED_ICON_FILE="AppIcon"
EXPECTED_DISCLAIMER="It is not affiliated with or endorsed by the Pi maintainers."

fail() {
  echo "verify-app: FAIL: $1" >&2
  exit 1
}

[[ -n "$APP_DIR" ]] || fail "usage: verify-app.sh <app> [--signed] [--gatekeeper]"
[[ -d "$APP_DIR" ]] || fail "no app at $APP_DIR"

plist_get() {
  /usr/libexec/PlistBuddy -c "Print :$1" "$APP_DIR/Contents/Info.plist" 2>/dev/null || true
}

# ---- Structure ----
[[ "$(plist_get CFBundleIdentifier)" == "$EXPECTED_BUNDLE_ID" ]] \
  || fail "CFBundleIdentifier is '$(plist_get CFBundleIdentifier)', expected $EXPECTED_BUNDLE_ID"
[[ "$(plist_get CFBundleExecutable)" == "$EXPECTED_EXECUTABLE" ]] \
  || fail "CFBundleExecutable mismatch"
[[ "$(plist_get CFBundlePackageType)" == "APPL" ]] \
  || fail "CFBundlePackageType is not APPL"
[[ "$(plist_get CFBundleIconFile)" == "$EXPECTED_ICON_FILE" ]] \
  || fail "CFBundleIconFile must be $EXPECTED_ICON_FILE"
[[ "$(plist_get CFBundleGetInfoString)" == *"independent, unofficial launcher for Pi"* ]] \
  || fail "CFBundleGetInfoString must identify the independent, unofficial launcher"
[[ "$(plist_get CFBundleGetInfoString)" == *"$EXPECTED_DISCLAIMER"* ]] \
  || fail "CFBundleGetInfoString is missing the non-affiliation disclaimer"
[[ "$(plist_get NSHumanReadableCopyright)" == *"$EXPECTED_DISCLAIMER"* ]] \
  || fail "NSHumanReadableCopyright is missing the non-affiliation disclaimer"
[[ "$(plist_get LSUIElement)" == "true" ]] \
  || fail "LSUIElement must be true (agent app, no Dock presence)"
[[ -x "$APP_DIR/Contents/MacOS/pi-launcher" ]] \
  || fail "main executable missing or not executable"
[[ -x "$APP_DIR/Contents/Resources/pi/pi" ]] \
  || fail "bundled Pi missing or not executable"
[[ -f "$APP_DIR/Contents/Resources/pi/LICENSE" ]] \
  || fail "upstream MIT LICENSE missing from bundle"
[[ -f "$APP_DIR/Contents/Resources/THIRD-PARTY-NOTICES.md" ]] \
  || fail "THIRD-PARTY-NOTICES.md missing from bundle"
ICON_PATH="$APP_DIR/Contents/Resources/$EXPECTED_ICON_FILE.icns"
[[ -f "$ICON_PATH" ]] \
  || fail "$EXPECTED_ICON_FILE.icns missing from bundle"
file -b "$ICON_PATH" | grep -q "^Mac OS X icon" \
  || fail "$EXPECTED_ICON_FILE.icns is not a valid macOS icon"

# ---- Architecture: every Mach-O in the bundle must be arm64 ----
MACH_O_COUNT=0
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "^Mach-O"; then
    MACH_O_COUNT=$((MACH_O_COUNT + 1))
    file -b "$f" | grep -q "arm64" \
      || fail "non-arm64 Mach-O in bundle: $f ($(file -b "$f"))"
  fi
done < <(find "$APP_DIR" -type f -print0)
[[ "$MACH_O_COUNT" -ge 2 ]] \
  || fail "expected at least 2 Mach-O files (launcher, payload), found $MACH_O_COUNT"

# ---- Version fields must agree ----
SHORT_VERSION="$(plist_get CFBundleShortVersionString)"
BUNDLE_VERSION="$(plist_get CFBundleVersion)"
[[ -n "$SHORT_VERSION" && "$SHORT_VERSION" == "$BUNDLE_VERSION" ]] \
  || fail "CFBundleShortVersionString ($SHORT_VERSION) != CFBundleVersion ($BUNDLE_VERSION)"

echo "verify-app: structure OK ($MACH_O_COUNT Mach-O files, version $SHORT_VERSION)"

if [[ "$MODE_SIGNED" -eq 0 ]]; then
  exit 0
fi

# ---- Signature validity (all nested code + outer seal) ----
codesign --verify --deep --strict --verbose=2 "$APP_DIR" 2>&1 \
  || fail "codesign --verify --deep --strict failed"

# ---- Identity, Team ID, hardened runtime, entitlements on every Mach-O ----
LAUNCHER_ENTITLEMENTS_XML="$(mktemp)"
PI_ENTITLEMENTS_XML="$(mktemp)"
trap 'rm -f "$LAUNCHER_ENTITLEMENTS_XML" "$PI_ENTITLEMENTS_XML"' EXIT

while IFS= read -r -d '' f; do
  file -b "$f" | grep -q "^Mach-O" || continue
  INFO="$(codesign -dvvv "$f" 2>&1)" \
    || fail "codesign -dvvv failed on $f"
  echo "$INFO" | grep -q "^Authority=Developer ID Application: Kun Chen ($EXPECTED_TEAM_ID)$" \
    || fail "$f not signed by the expected Developer ID identity"
  echo "$INFO" | grep -q "^TeamIdentifier=$EXPECTED_TEAM_ID$" \
    || fail "$f has wrong Team ID"
  echo "$INFO" | grep -q "flags=0x10000(runtime)" \
    || fail "$f missing hardened runtime"
  echo "$INFO" | grep -q "^Timestamp=" \
    || fail "$f missing secure timestamp"
done < <(find "$APP_DIR" -type f -print0)

# ---- Entitlement boundary: launcher none, bundled Pi exactly allow-jit ----
codesign -d --entitlements :- --xml "$APP_DIR/Contents/MacOS/pi-launcher" > "$LAUNCHER_ENTITLEMENTS_XML" 2>/dev/null || true
if [[ -s "$LAUNCHER_ENTITLEMENTS_XML" ]]; then
  fail "launcher executable must have no entitlements"
fi
codesign -d --entitlements :- --xml "$APP_DIR/Contents/Resources/pi/pi" > "$PI_ENTITLEMENTS_XML" 2>/dev/null \
  || fail "cannot read bundled Pi entitlements"
[[ -s "$PI_ENTITLEMENTS_XML" ]] \
  || fail "bundled Pi missing com.apple.security.cs.allow-jit entitlement"
plutil -lint "$PI_ENTITLEMENTS_XML" >/dev/null \
  || fail "bundled Pi entitlements plist is malformed"
PI_JIT="$(/usr/libexec/PlistBuddy -c "Print :com.apple.security.cs.allow-jit" "$PI_ENTITLEMENTS_XML" 2>/dev/null || true)"
[[ "$PI_JIT" == "true" ]] \
  || fail "bundled Pi missing com.apple.security.cs.allow-jit entitlement"
PI_ENTITLEMENT_KEYS="$(plutil -convert json -o - "$PI_ENTITLEMENTS_XML" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")"
[[ "$PI_ENTITLEMENT_KEYS" == "1" ]] \
  || fail "bundled Pi must have exactly one entitlement (allow-jit), found $PI_ENTITLEMENT_KEYS"

# ---- Outer bundle identity and designated requirement ----
APP_INFO="$(codesign -dvvv "$APP_DIR" 2>&1)"
echo "$APP_INFO" | grep -q "^Identifier=$EXPECTED_BUNDLE_ID$" \
  || fail "app signing identifier is not $EXPECTED_BUNDLE_ID"
DR="$(codesign -d -r- "$APP_DIR" 2>&1)"
echo "$DR" | grep -q "identifier \"$EXPECTED_BUNDLE_ID\"" \
  || fail "designated requirement does not pin $EXPECTED_BUNDLE_ID"
echo "$DR" | grep -q "anchor apple generic" \
  || fail "designated requirement missing anchor apple generic"
echo "$DR" | grep -Eq "certificate leaf\[subject\.OU\] = \"?$EXPECTED_TEAM_ID\"?$" \
  || fail "designated requirement missing Team ID $EXPECTED_TEAM_ID"

echo "verify-app: signatures OK (Developer ID, hardened runtime, secure timestamps)"

if [[ "$MODE_GATEKEEPER" -eq 0 ]]; then
  exit 0
fi

# ---- Notarization + Gatekeeper (final-artifact gate) ----
xcrun stapler validate "$APP_DIR" >/dev/null \
  || fail "no stapled notarization ticket"
spctl --assess --type execute --verbose=4 "$APP_DIR" 2>&1 \
  || fail "Gatekeeper assessment failed"
SPCTL_OUT="$(spctl --assess --type execute --verbose=4 "$APP_DIR" 2>&1)"
echo "$SPCTL_OUT" | grep -q "source=Notarized Developer ID" \
  || fail "Gatekeeper source is not 'Notarized Developer ID'"

echo "verify-app: Gatekeeper OK (notarized, stapled, accepted)"
