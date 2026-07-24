# AGENTS.md

pi-launcher: a minimal signed macOS app whose executable spawns the one bundled official Pi CLI and stays alive as its parent in the caller's existing terminal. The whole product is a security boundary plus terminal transparency; both are enforced by tests, not by convention.

## Invariants (do not weaken)

- **Fixed target**: the launcher may only exec `Contents/Resources/pi/pi` inside its own bundle. Never add a target option, PATH/env resolution, a config file, or a shell. Negative tests in `tests/functional.py` guard this.
- **Transparency**: fork+exec (never bare `exec`), same session/process group/controlling TTY, no PTY, no daemonization. Keyboard/job-control signals reach Pi via the group and are never forwarded (no duplicates); only signals directed at the launcher pid (HUP/TERM/USR1/USR2) are relayed, once. Pi exit codes pass through; Pi signal deaths are re-raised so the caller sees the identical wait status.
- **Why same-group works for Ctrl-Z**: Pi's suspend is `process.kill(0, "SIGTSTP")` (see upstream `interactive-mode.js`), a group-wide stop, and the shell's `fg` continues the group. The launcher keeps default stop dispositions and does not use `waitpid(WUNTRACED)`; adding stop-relay machinery here reintroduces a stop/continue race - don't.
- **Entitlements**: launcher has none; bundled Pi has exactly `allow-jit` (JavaScriptCore). `scripts/verify-app.sh` enforces both.
- **Provenance**: bundled Pi is pinned in `packaging/pi-release.json` (version, URLs, sha256, upstream license sha256) and cross-checked against upstream `SHA256SUMS` on every fetch. Bump it in its own PR.

## Layout and commands

- `src/pi-launcher.c` - the entire launcher. Header comment documents the signal contract.
- `tests/probe.c` + `tests/functional.py` + `tests/pty_tests.py` - probe-payload test double driven through real PTYs; `make test` runs everything including the bundled-Pi smoke.
- `scripts/` - fetch/build/sign/verify pipeline; `check-release-contract.py` is the secret-free CI gate for workflow/packaging changes.
- `packaging/icon/AppIcon.svg` - source for the original app icon; `scripts/build-icon.sh` renders the iconset and icns under ignored `build/`. Do not substitute Pi brand assets without explicit third-party app-branding permission.
- `.github/workflows/` - `ci.yml` (PR tests, no secrets), `release.yml` (tag-based signed/notarized release; canonical secret names documented in `RELEASE.md`).
- `homebrew/pi-launcher.rb` - cask contract for the separate tap task; never publish from this repo.
- Team ID `9T2J7MNUP9`, bundle id `com.kunchenguid.pi-launcher`, app `Pi Launcher.app`, asset `Pi-Launcher-<version>.zip`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
