#!/bin/bash
# check-app-version.sh - prove an app bundle actually launches its bundled Pi.
#
# A code-signature check proves structure; this proves behavior. Run it against
# the signed bundle and against every extraction of the distributed zip, so a
# signing, stapling, packaging, or distribution regression that breaks launch
# cannot reach users.
set -euo pipefail

APP="${1:?usage: check-app-version.sh <path-to-app-bundle> <expected-pi-version>}"
EXPECTED="${2:?usage: check-app-version.sh <path-to-app-bundle> <expected-pi-version>}"
EXE="$APP/Contents/MacOS/pi-launcher"

if [[ ! -x "$EXE" ]]; then
  echo "check-app-version: FAIL: $EXE is not executable" >&2
  exit 1
fi

ACTUAL="$("$EXE" --version)"
if [[ "$ACTUAL" != "$EXPECTED" ]]; then
  echo "check-app-version: FAIL: $EXE reported Pi '$ACTUAL', expected '$EXPECTED'" >&2
  exit 1
fi

echo "check-app-version: $APP launches the bundled Pi $ACTUAL"
