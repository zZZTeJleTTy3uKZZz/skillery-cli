# skillery installer (Windows). ASCII-only by design: `irm ... | iex` decodes as
# Latin-1, so non-ASCII characters here would break the script.
#
# Usage:
#   irm <RAW_URL>/install.ps1 | iex
#
# What it does: installs uv (Astral) if missing, then `uv tool install skillery-cli`.
$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "[skillery] uv not found - installing it (Astral)..."
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  # make uv visible in this session
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "[skillery] installing skillery-cli via uv tool ..."
uv tool install --upgrade skillery-cli
try { uv tool update-shell | Out-Null } catch { }

Write-Host ""
Write-Host "[skillery] Done. Verify:  skillery --version"
Write-Host "[skillery] Update later:  skillery self-update"
