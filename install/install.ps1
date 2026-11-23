# Stentor installer for Windows.  Usage (PowerShell): irm <site>/install.ps1 | iex
# Running it again updates to the latest release.
$ErrorActionPreference = "Stop"
$Repo = "Can-0f-Tuna/stentor"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> Installing uv (Python tool installer)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
}

# The latest release, or the main branch if there is none yet. An archive, so no git is needed.
# Python 3.13 is the newest one the offline engine's libraries have wheels for everywhere (see updater.py).
$Tag = ""
if ($env:STENTOR_SOURCE) {
    $Source = $env:STENTOR_SOURCE
} else {
    try { $Tag = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest").tag_name } catch { }
    $Ref = if ($Tag) { "tags/$Tag" } else { "heads/main" }
    $Source = "stentor @ https://github.com/$Repo/archive/refs/$Ref.tar.gz"
}
$Python = if ($env:STENTOR_PYTHON) { $env:STENTOR_PYTHON } else { "3.13" }

# A running copy locks its files; stop it before reinstalling.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*stentor*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "==> Installing Stentor $Tag"
uv tool install --force --python $Python $Source
if ($LASTEXITCODE -ne 0) { throw "install failed" }

Write-Host "==> Starting Stentor (it also starts at every login; look for its icon next to the clock)"
stentor service install
if (-not $env:STENTOR_NO_SETUP) {
    Write-Host "==> Opening the settings in your browser. Add your API key there."
    stentor settings --wait 10
}
