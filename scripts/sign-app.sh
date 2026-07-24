#!/bin/bash
# sign-app.sh - sign "Pi Launcher.app" with the Developer ID identity.
#
# Inside-out order, required by both Apple and this repo's release gate:
#   1. every nested Mach-O under Contents/Resources (dylibs, .node, pi)
#   2. the launcher main executable
#   3. the outer app bundle
#
# All code gets the hardened runtime and a secure timestamp. The bundled
# Pi executable is the only file that carries an entitlement (allow-jit;
# see packaging/pi-entitlements.plist). The launcher itself ships with
# zero entitlements.
#
# Environment:
#   APP_DIR       - app to sign (default: build/Pi Launcher.app)
#   SIGN_IDENTITY - codesign identity (default: the canonical Developer ID)
#   KEYCHAIN      - optional keychain file to pass to codesign --keychain
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$REPO_ROOT/build/Pi Launcher.app}"
SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application: Kun Chen (9T2J7MNUP9)}"
PI_ENTITLEMENTS="$REPO_ROOT/packaging/pi-entitlements.plist"

KEYCHAIN_ARGS=()
if [[ -n "${KEYCHAIN:-}" ]]; then
  KEYCHAIN_ARGS=(--keychain "$KEYCHAIN")
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "sign-app: FAIL: no app at $APP_DIR" >&2
  exit 1
fi

sign_one() {
  local path="$1"
  shift
  echo "sign-app: signing $path"
  codesign --force --sign "$SIGN_IDENTITY" \
    --options runtime --timestamp \
    "${KEYCHAIN_ARGS[@]}" \
    "$@" "$path" >/dev/null
}

# 1. Nested code, deepest paths first. The bundled Pi executable gets the
#    JIT entitlement; every other nested binary gets none.
PI_BINARY="$APP_DIR/Contents/Resources/pi/pi"
NESTED_LIST="$(mktemp)"
trap 'rm -f "$NESTED_LIST"' EXIT
find "$APP_DIR/Contents/Resources" -type f -print0 |
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q "^Mach-O"; then
      printf '%s\n' "$f"
    fi
  done | awk '{ print length($0), $0 }' | sort -rn | cut -d' ' -f2- > "$NESTED_LIST"
if [[ ! -s "$NESTED_LIST" ]]; then
  echo "sign-app: FAIL: no nested Mach-O found under $APP_DIR/Contents/Resources" >&2
  exit 1
fi
FOUND_PI=0
while IFS= read -r f; do
  if [[ "$f" == "$PI_BINARY" ]]; then
    FOUND_PI=1
    sign_one "$f" --entitlements "$PI_ENTITLEMENTS"
  else
    sign_one "$f"
  fi
done < "$NESTED_LIST"
if [[ "$FOUND_PI" -ne 1 ]]; then
  echo "sign-app: FAIL: bundled Pi binary missing at $PI_BINARY" >&2
  exit 1
fi

# 2. Main executable, no entitlements.
sign_one "$APP_DIR/Contents/MacOS/pi-launcher"

# 3. Outer bundle (seals everything signed above).
sign_one "$APP_DIR"

echo "sign-app: signed $APP_DIR with '$SIGN_IDENTITY'"
