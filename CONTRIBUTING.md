# Contributing

Issues and PRs are welcome. A few ground rules that keep this project what it is:

- **The fixed-target boundary is not negotiable.** The launcher will never gain a target option, PATH resolution, an environment override, a config file, or a shell. PRs that add any way to run something other than the one bundled Pi will be closed. If you need a general launcher, fork - the code is tiny.
- **Terminal transparency is the product.** Any change to `src/pi-launcher.c` must keep the full test suite green (`make test`) and should come with a new test if it touches process, signal, or terminal behavior.
- **No new dependencies.** The launcher is one C file against libSystem. The build is `clang`, `codesign`, `ditto`, and POSIX shell. Tests are C + Python 3 stdlib. Keep it that way.
- **Signing is release-only.** Local development builds are ad-hoc signed and need no Apple credentials. Do not commit certificates, keychains, provisioning profiles, or notarization responses; CI scans for key material.

## Workflow

1. Fork and branch.
2. `make test` must pass on macOS arm64 (this builds the unsigned dev app and runs the functional + PTY + smoke suites).
3. `python3 scripts/check-release-contract.py` must pass if you touch packaging, workflows, or the cask template.
4. Open the PR. CI runs the same suite on macos-26 without any signing secret.

To bump the bundled Pi version: update `packaging/pi-release.json` (version, tag, URLs, both checksums) in its own PR, and say why the new upstream version is worth shipping.
