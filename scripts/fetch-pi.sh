#!/bin/bash
# fetch-pi.sh - download and verify the pinned upstream Pi release.
#
# Provenance gate: the tarball must match BOTH the checksum pinned in
# packaging/pi-release.json AND the SHA256SUMS entry published on the
# upstream release. Any mismatch stops the build.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$REPO_ROOT/packaging/pi-release.json"
VENDOR_DIR="$REPO_ROOT/build/vendor"
TREE_DIR="$VENDOR_DIR/pi-tree"

read_manifest() {
  python3 - "$MANIFEST" "$1" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    print(json.load(f)[sys.argv[2]])
PY
}

URL="$(read_manifest url)"
EXPECTED_SHA256="$(read_manifest sha256)"
SUMS_URL="$(read_manifest sumsUrl)"
LICENSE_URL="$(read_manifest licenseUrl)"
EXPECTED_LICENSE_SHA256="$(read_manifest licenseSha256)"
VERSION="$(read_manifest version)"

TARBALL="$VENDOR_DIR/pi-darwin-arm64.tar.gz"
SUMS_FILE="$VENDOR_DIR/SHA256SUMS"
LICENSE_FILE="$VENDOR_DIR/LICENSE.upstream"

mkdir -p "$VENDOR_DIR"

sha256_of() {
  shasum -a 256 "$1" | awk '{print $1}'
}

download() {
  local url="$1" out="$2"
  echo "fetch-pi: downloading $url"
  curl -fsSL --retry 3 -o "$out" "$url"
}

if [[ ! -f "$TARBALL" ]] || [[ "$(sha256_of "$TARBALL")" != "$EXPECTED_SHA256" ]]; then
  download "$URL" "$TARBALL"
fi
if [[ ! -f "$SUMS_FILE" ]]; then
  download "$SUMS_URL" "$SUMS_FILE"
fi
if [[ ! -f "$LICENSE_FILE" ]] || [[ "$(sha256_of "$LICENSE_FILE")" != "$EXPECTED_LICENSE_SHA256" ]]; then
  download "$LICENSE_URL" "$LICENSE_FILE"
fi

ACTUAL_SHA256="$(sha256_of "$TARBALL")"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "fetch-pi: FAIL: tarball sha256 $ACTUAL_SHA256 does not match pinned $EXPECTED_SHA256" >&2
  exit 1
fi

SUMS_ENTRY="$(awk -v f="pi-darwin-arm64.tar.gz" '$2 == f {print $1}' "$SUMS_FILE")"
if [[ -z "$SUMS_ENTRY" ]]; then
  echo "fetch-pi: FAIL: upstream SHA256SUMS has no pi-darwin-arm64.tar.gz entry" >&2
  exit 1
fi
if [[ "$SUMS_ENTRY" != "$EXPECTED_SHA256" ]]; then
  echo "fetch-pi: FAIL: upstream SHA256SUMS entry $SUMS_ENTRY does not match pinned $EXPECTED_SHA256" >&2
  exit 1
fi

ACTUAL_LICENSE_SHA256="$(sha256_of "$LICENSE_FILE")"
if [[ "$ACTUAL_LICENSE_SHA256" != "$EXPECTED_LICENSE_SHA256" ]]; then
  echo "fetch-pi: FAIL: upstream LICENSE sha256 mismatch" >&2
  exit 1
fi

rm -rf "$TREE_DIR"
mkdir -p "$TREE_DIR"
tar xzf "$TARBALL" -C "$TREE_DIR"
# The tarball contains a top-level pi/ directory holding the standalone
# binary and its runtime resources; the whole tree ships inside the app.
if [[ ! -x "$TREE_DIR/pi/pi" ]]; then
  echo "fetch-pi: FAIL: extracted tree has no executable pi/pi" >&2
  exit 1
fi
cp "$LICENSE_FILE" "$TREE_DIR/pi/LICENSE"

echo "fetch-pi: verified pi $VERSION ($ACTUAL_SHA256)"
