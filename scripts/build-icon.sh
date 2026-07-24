#!/bin/bash
# build-icon.sh - render the original SVG app icon into a macOS icns.
#
# The SVG is the source of truth. All PNG iconset intermediates and the icns
# are generated under build/ and remain untracked.
#
# Usage: build-icon.sh [output.icns]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$REPO_ROOT/packaging/icon/AppIcon.svg"
OUTPUT="${1:-$REPO_ROOT/build/AppIcon.icns}"

if [[ "$OUTPUT" != *.icns ]]; then
  echo "build-icon: FAIL: output must end in .icns" >&2
  exit 1
fi
if [[ ! -f "$SOURCE" ]]; then
  echo "build-icon: FAIL: source icon missing at $SOURCE" >&2
  exit 1
fi

ICONSET="${OUTPUT%.icns}.iconset"
rm -rf "$ICONSET" "$OUTPUT"
mkdir -p "$ICONSET"

render() {
  local name="$1" pixels="$2"
  /usr/bin/sips -s format png -z "$pixels" "$pixels" \
    "$SOURCE" --out "$ICONSET/$name" >/dev/null
}

render icon_16x16.png 16
render icon_16x16@2x.png 32
cp "$ICONSET/icon_16x16@2x.png" "$ICONSET/icon_32x32.png"
render icon_32x32@2x.png 64
render icon_128x128.png 128
render icon_128x128@2x.png 256
cp "$ICONSET/icon_128x128@2x.png" "$ICONSET/icon_256x256.png"
render icon_256x256@2x.png 512
cp "$ICONSET/icon_256x256@2x.png" "$ICONSET/icon_512x512.png"
render icon_512x512@2x.png 1024

/usr/bin/iconutil -c icns "$ICONSET" -o "$OUTPUT"
echo "build-icon: generated $OUTPUT"
