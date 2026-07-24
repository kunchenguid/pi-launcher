# Homebrew cask for the captain's public tap (homebrew-tap).
#
# This file is the release metadata contract consumed by the later tap
# task: copy it into the tap as Casks/pi-launcher.rb, set `version` to the
# release tag without the "v", and set `sha256` from the release's
# Pi-Launcher-<version>.zip.sha256 asset. This repo's release workflow
# guarantees the artifact name, the .sha256 sidecar, and the app layout
# below. Do not publish from this repository.
cask "pi-launcher" do
  version "0.0.0" # replace with the release version, e.g. "1.0.0"
  sha256 "REPLACE_WITH_RELEASE_ZIP_SHA256"

  url "https://github.com/kunchenguid/pi-launcher/releases/download/v#{version}/Pi-Launcher-#{version}.zip"
  name "Pi Launcher"
  desc "Signed, notarized macOS app that launches one bundled Pi CLI transparently in your terminal"
  homepage "https://github.com/kunchenguid/pi-launcher"

  # The app is agent-only (LSUIElement); the binary shim puts the launcher
  # on PATH without touching any existing `pi` command.
  app "Pi Launcher.app"
  binary "#{appdir}/Pi Launcher.app/Contents/MacOS/pi-launcher", target: "pi-signed"

  # No zap: the launcher writes nothing of its own, and Pi's own data in
  # ~/.pi belongs to Pi, not to this cask.
end
