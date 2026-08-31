#!/bin/sh
# skillery installer (Linux / macOS). ASCII-only by design: this file is piped into
# `sh`, and non-ASCII can corrupt under some locales.
#
# Usage:
#   curl -fsSL <RAW_URL>/install.sh | sh
#
# What it does: installs uv (Astral) if missing, then `uv tool install skillery-cli`.
set -eu

if ! command -v uv >/dev/null 2>&1; then
  echo "[skillery] uv not found - installing it (Astral)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # make uv visible in this shell
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "[skillery] installing skillery-cli via uv tool ..."
uv tool install --upgrade skillery-cli
uv tool update-shell >/dev/null 2>&1 || true

echo ""
echo "[skillery] Done. Verify:  skillery --version"
echo "[skillery] Update later:  skillery self-update"
