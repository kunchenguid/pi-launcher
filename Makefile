SHELL := /bin/bash

APP := build/Pi Launcher.app
TEST_APP := build/TestPiLauncher.app
LAUNCHER_EXE := $(APP)/Contents/MacOS/pi-launcher
TEST_LAUNCHER_EXE := $(TEST_APP)/Contents/MacOS/pi-launcher
PI_VERSION := $(shell python3 -c "import json; print(json.load(open('packaging/pi-release.json'))['version'])")

.PHONY: all fetch app test-app test test-icon test-functional test-pty test-smoke verify clean

all: app

fetch:
	scripts/fetch-pi.sh

app: fetch
	scripts/build-app.sh

# Test app: same bundle shape, but Contents/Resources/pi/pi is the probe
# binary instead of Pi. The launcher under test cannot tell the
# difference - that is the point.
test-app:
	mkdir -p build
	clang -O2 -std=c11 -Wall -Wextra -Wpedantic -Werror \
	  -arch arm64 -mmacosx-version-min=13.0 \
	  -o build/probe tests/probe.c
	mkdir -p build/test-payload
	cp build/probe build/test-payload/pi
	chmod 755 build/test-payload/pi
	printf 'Test payload placeholder license (real bundles ship the upstream Pi MIT license).\n' > build/test-payload/LICENSE
	VERSION=0.0.0-test \
	  PAYLOAD_DIR="$(CURDIR)/build/test-payload" \
	  APP_DIR="$(CURDIR)/$(TEST_APP)" \
	  scripts/build-app.sh

test: test-icon test-functional test-pty test-smoke

test-icon: app
	python3 tests/icon_contract.py "$(APP)"

test-functional: test-app
	python3 tests/functional.py "$(TEST_LAUNCHER_EXE)"

test-pty: test-app
	python3 tests/pty_tests.py "$(TEST_LAUNCHER_EXE)"

# The real bundled Pi, driven through the real launcher: proves the
# packaged app actually boots Pi, loads its themes, and passes argv.
test-smoke: app
	[[ "$$("$(LAUNCHER_EXE)" --version)" == "$(PI_VERSION)" ]]
	"$(LAUNCHER_EXE)" -p --model invalid/model "say hi" 2>&1 | \
	  grep -q 'Model "invalid/model" not found'
	scripts/verify-app.sh "$(APP)"
	@echo "test-smoke: bundled Pi works through the launcher"

verify: app
	scripts/verify-app.sh "$(APP)"

clean:
	rm -rf build
