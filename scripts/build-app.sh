#!/bin/bash
# build-app.sh - compile the launcher and assemble "Pi Launcher.app".
#
# The result is an unsigned development app (ad-hoc signed so it runs on
# Apple Silicon). Release signing lives in sign-app.sh.
#
# Environment:
#   VERSION        - app version stamped into Info.plist (default 0.0.0-dev)
#   PAYLOAD_DIR    - directory whose contents become Contents/Resources/pi
#                    (default: build/vendor/pi-tree/pi from fetch-pi.sh)
#   APP_DIR        - output app path (default: build/Pi Launcher.app)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-0.0.0-dev}"
PAYLOAD_DIR="${PAYLOAD_DIR:-$REPO_ROOT/build/vendor/pi-tree/pi}"
APP_DIR="${APP_DIR:-$REPO_ROOT/build/Pi Launcher.app}"
LAUNCHER_BIN="$REPO_ROOT/build/pi-launcher"
ICON_FILE="$REPO_ROOT/build/AppIcon.icns"

if [[ ! -x "$PAYLOAD_DIR/pi" ]]; then
  echo "build-app: FAIL: payload $PAYLOAD_DIR has no executable pi (run scripts/fetch-pi.sh first)" >&2
  exit 1
fi

mkdir -p "$REPO_ROOT/build"

clang \
  -O2 -std=c11 -Wall -Wextra -Wpedantic -Werror \
  -arch arm64 -mmacosx-version-min=13.0 \
  -o "$LAUNCHER_BIN" \
  "$REPO_ROOT/src/pi-launcher.c"

# Generate the original app icon. The iconset and icns stay under the
# ignored build tree.
"$REPO_ROOT/scripts/build-icon.sh" "$ICON_FILE"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

sed "s/@VERSION@/$VERSION/g" "$REPO_ROOT/packaging/Info.plist" > "$APP_DIR/Contents/Info.plist"
plutil -lint "$APP_DIR/Contents/Info.plist" >/dev/null

cp "$LAUNCHER_BIN" "$APP_DIR/Contents/MacOS/pi-launcher"
chmod 755 "$APP_DIR/Contents/MacOS/pi-launcher"

# The payload is copied, never linked: the shipped app must be a
# self-contained bundle.
rm -rf "$APP_DIR/Contents/Resources/pi"
cp -R "$PAYLOAD_DIR" "$APP_DIR/Contents/Resources/pi"
cp "$REPO_ROOT/packaging/THIRD-PARTY-NOTICES.md" "$APP_DIR/Contents/Resources/THIRD-PARTY-NOTICES.md"
cp "$ICON_FILE" "$APP_DIR/Contents/Resources/AppIcon.icns"

# Ad-hoc sign nested code first, then the outer bundle, so the development
# app is coherent and runnable on Apple Silicon. Release builds re-sign
# everything with the Developer ID identity inside-out, same order.
find "$APP_DIR/Contents/Resources" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "^Mach-O"; then
    codesign --force --sign - "$f" >/dev/null
  fi
done
codesign --force --sign - "$APP_DIR" >/dev/null

echo "build-app: assembled $APP_DIR (version $VERSION)"
