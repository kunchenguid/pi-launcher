<h1 align="center">pi-launcher</h1>
<p align="center">
  <a href="https://github.com/kunchenguid/pi-launcher/actions/workflows/ci.yml"
    ><img
      alt="CI"
      src="https://img.shields.io/github/actions/workflow/status/kunchenguid/pi-launcher/ci.yml?style=flat-square&label=ci"
  /></a>
  <a href="https://github.com/kunchenguid/pi-launcher/actions/workflows/release.yml"
    ><img
      alt="Release"
      src="https://img.shields.io/github/actions/workflow/status/kunchenguid/pi-launcher/release.yml?style=flat-square&label=release"
  /></a>
  <a
    href="https://img.shields.io/badge/platform-macOS%20arm64-blue?style=flat-square"
    ><img
      alt="Platform"
      src="https://img.shields.io/badge/platform-macOS%20arm64-blue?style=flat-square"
  /></a>
  <a href="https://x.com/kunchenguid"
    ><img
      alt="X"
      src="https://img.shields.io/badge/X-@kunchenguid-black?style=flat-square"
  /></a>
  <a href="https://discord.gg/Wsy2NpnZDu"
    ><img
      alt="Discord"
      src="https://img.shields.io/discord/1439901831038763092?style=flat-square&label=discord"
  /></a>
</p>

<h3 align="center">Run Pi in your terminal under a stable, signed macOS app identity.</h3>

**Independent project:** pi-launcher is an independent, unofficial launcher for Pi. It is not affiliated with or endorsed by the Pi maintainers.

The official [Pi](https://github.com/earendil-works/pi) standalone macOS binary ships ad-hoc signed: Gatekeeper rejects it, and any tool that binds trust to a code-signed app identity has nothing stable to attach to. In my setup that tool is [Automic Vault](https://www.automicvault.com), which approves credential access by walking the process ancestry and matching designated requirements. An ad-hoc `a.out` signature gives it a different cdhash on every build.

pi-launcher is the smallest fix: a signed, notarized `Pi Launcher.app` whose only executable does one thing - spawn the one official Pi binary bundled inside the same app, then stay alive as its parent in your existing terminal. No GUI, no PTY, no new window. Your terminal session stays exactly as it was; the process ancestry gains a stable signed identity.

- **One fixed target** - the bundled Pi is pinned by version, upstream URL, and SHA-256. No PATH lookup, no target flag, no shell, no environment override.
- **Transparent terminal semantics** - same TTY, same process group, same session. Exit codes, signal deaths, Ctrl-C, Ctrl-Z/`fg`, and resize all behave like a direct `pi` run, verified by a real pseudo-terminal test suite.
- **Auditable by design** - the launcher is one C file, ~250 lines, zero dependencies beyond libSystem. The app has no entitlements; the bundled Pi gets exactly one (`allow-jit`, because JavaScriptCore).

## Quick Start

```sh
$ brew install --cask kunchenguid/tap/pi-launcher   # tap PR lands after first release
$ pi-signed "review the diff on main"                # exactly like `pi`
```

Until the tap is published, download `Pi-Launcher-<version>.zip` from the [latest release](https://github.com/kunchenguid/pi-launcher/releases/latest), unzip, and move `Pi Launcher.app` to `/Applications`. Then run it:

```sh
$ /Applications/Pi\ Launcher.app/Contents/MacOS/pi-launcher -p "summarize this repo"
```

Add a shim if you want a short command (this never touches an existing `pi`):

```sh
$ ln -s "/Applications/Pi Launcher.app/Contents/MacOS/pi-launcher" ~/.local/bin/pi-signed
```

## How It Works

```
your shell (zsh)
  └─ pi-launcher          <- signed app identity, stays alive as parent
       └─ pi              <- the exact official binary bundled in the app
            └─ bash, gh, ...   <- Pi's own children, unchanged
```

- **Fork + exec, not exec** - the launcher stays in the process chain as Pi's direct parent, so an ancestry-walking tool sees the signed app on every Pi child.
- **Same process group** - the launcher does not create a new session, process group, or PTY. Keyboard signals go to the foreground group exactly like a direct run: Pi handles Ctrl-C, Pi's own Ctrl-Z stops the whole group (it literally calls `kill(0, SIGTSTP)`), and `fg` resumes both.
- **No duplicate signals** - terminal-generated signals already reach Pi through the group, so the launcher never forwards those. It only forwards signals aimed at the launcher process itself (`SIGHUP`, `SIGTERM`, `SIGUSR1`, `SIGUSR2`), each exactly once, so killing the launcher can't orphan a live Pi.
- **Exit status reproduced** - Pi's exit code passes through unchanged; if Pi dies by a signal, the launcher re-raises the same signal on itself, so `$?` and job notifications match a direct run.

## CLI Reference

There are no launcher flags. Every argument is passed to Pi byte-for-byte, including things that look like launcher options (`--target`, `--help`, whatever). See `pi --help` for Pi's own flags.

| Invocation | What happens |
| ---------- | ------------ |
| `pi-signed [pi args...]` | Runs the bundled Pi with those args |
| Exit code `0`-`255` | Pi's own exit code |
| Exit code `126` | The bundled Pi inside the app is not executable (reinstall) |
| Exit code `127` | The bundled Pi inside the app is missing (reinstall) |

## Security Boundary

- The launcher can execute exactly one file: `Contents/Resources/pi/pi` inside its own app bundle. There is no code path that resolves a command from `PATH`, from an environment variable, or from an argument. The test suite includes negative tests that try all three.
- The bundled Pi is the official `pi-darwin-arm64.tar.gz` from [earendil-works/pi releases](https://github.com/earendil-works/pi/releases), pinned in `packaging/pi-release.json` and verified against both the pinned checksum and upstream's `SHA256SUMS` at build time. Its MIT license ships in the bundle.
- The pin follows upstream automatically: a daily workflow picks up new *stable* Pi releases only, and a candidate is only committed and released after it passes the whole suite - provenance, functional, real-PTY, and a bundled-Pi launch smoke test - on a build made from it. A Pi release that fails any gate is held back and reported, not shipped. See [RELEASE.md](RELEASE.md).
- Signing: Developer ID Application (Team `9T2J7MNUP9`), hardened runtime, secure timestamps, nested code signed inside-out, notarized, stapled. The launcher itself has zero entitlements.
- This is a process wrapper, not a sandbox. Pi still runs with your full user permissions, same as always.

## Limitations

- macOS arm64 only (that's what upstream's standalone binary targets).
- One pinned Pi version per pi-launcher release. Upgrading Pi means a new pi-launcher release; the app itself does not self-update - `brew upgrade` (or a fresh download) is how you get the newer bundle.
- Pi remains Mario Zechner's project. Report Pi behavior issues upstream, launcher issues here.
- A signed parent is necessary but not sufficient for Automic Vault recognition: whether Automic binds policy to this app identity is still unproven until the attended live test runs. The identity exists; don't assume the policy match.

## Verify a Release

Every release publishes `Pi-Launcher-<version>.zip` and `Pi-Launcher-<version>.zip.sha256`. The release workflow verifies the exact published artifact after upload (signature, notarization, Gatekeeper); you can repeat all of it locally:

```sh
shasum -a 256 -c Pi-Launcher-<version>.zip.sha256
unzip Pi-Launcher-<version>.zip
spctl --assess --type execute -v "Pi Launcher.app"     # accepted, Notarized Developer ID
xcrun stapler validate "Pi Launcher.app"               # ticket stapled
codesign -d -r- "Pi Launcher.app"                      # DR: identifier + apple generic + Team 9T2J7MNUP9
codesign -d --entitlements - "Pi Launcher.app/Contents/MacOS/pi-launcher"   # no entitlements
```

## Development

```sh
make fetch      # download + verify the pinned upstream Pi
make app        # build the unsigned dev app
make test       # functional + PTY + bundled-Pi smoke suites
make clean
```

The test suite swaps a probe binary into the same bundle layout and drives the launcher through real pseudo-terminals: argv/env/cwd byte-exactness, stdio transparency, exit and signal reproduction, foreground process group, resize, Ctrl-C, Ctrl-Z/`fg`, and Pi-style group suspend, plus negative tests for the fixed-target boundary. Releases are cut by merging the release-please PR, not by pushing a tag manually; see [RELEASE.md](RELEASE.md).
