# Stentor installer for Windows.  Usage (PowerShell): irm <site>/install.ps1 | iex
# Running it again updates to the latest release.
$ErrorActionPreference = "Stop"
$Repo = "Can-0f-Tuna/stentor"

# Read the old installer variables for upgrades from before 0.10.
function Get-StentorEnv($Name) {
    $Value = [Environment]::GetEnvironmentVariable("STENTOR_${Name}")
    if ($null -eq $Value) { $Value = [Environment]::GetEnvironmentVariable("HOLD_DICTATE_${Name}") }
    $Value
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> Installing uv (Python tool installer)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
}

# Python 3.13 is the newest one the offline engine's libraries have wheels for everywhere (see updater.py).
$Python = Get-StentorEnv "PYTHON"
if (-not $Python) { $Python = "3.13" }
$Source = Get-StentorEnv "SOURCE"
$Tag = ""
if (-not $Source) {
    # The newest stable Stentor release, or the main branch if there is none yet; an archive, so no git is needed.
    # Not GitHub's "latest": that stays on hold-dictate 0.9.4 for the copies installed before the rename.
    try {
        $Release = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases?per_page=100") |
            Where-Object { -not ($_.draft -or $_.prerelease) -and $_.tag_name -match '^v\d+\.\d+\.\d+$' } |
            Sort-Object { [version]$_.tag_name.Substring(1) } -Descending | Select-Object -First 1
        if ($Release -and [version]$Release.tag_name.Substring(1) -ge [version]"0.10.0") { $Tag = $Release.tag_name }
    } catch { }
    $Ref = if ($Tag) { "tags/$Tag" } else { "heads/main" }
}

$Tools = & uv tool dir
if ($LASTEXITCODE -ne 0) { throw "Could not find uv's tool directory" }
$Extras = if (Get-StentorEnv "OFFLINE") { "[local]" } else { "" }
# Running it again keeps the offline engine if this computer has it (STENTOR_OFFLINE=1 adds it).
# An install from when this was called hold-dictate counts too: its offline engine comes along.
$OldTool = Join-Path $Tools "hold-dictate"
foreach ($Tool in (Join-Path $Tools "stentor"), $OldTool) {
    $InstalledPython = Join-Path $Tool "Scripts\python.exe"
    if (-not $Extras -and (Test-Path $InstalledPython)) {
        & $InstalledPython -c "from importlib.util import find_spec; raise SystemExit(find_spec('faster_whisper') is None)"
        if ($LASTEXITCODE -eq 0) { $Extras = "[local]" }
    }
}

$Packages = @()
if (-not $Source) {
    $Source = "stentor$Extras @ https://github.com/$Repo/archive/refs/$Ref.tar.gz"
} elseif ($Extras) {
    $Packages += "--with", "faster-whisper>=1.0"
}

# A running copy locks its files. Match the daemon of either generation and stop it before reinstalling.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match '(?:^|\s)-m\s+(?:stentor|hold_dictate)\s+run(?:\s|$)' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "==> Installing Stentor $Tag"
uv tool install --force --python $Python @Packages $Source
if ($LASTEXITCODE -ne 0) { throw "Install failed" }

Write-Host "==> Starting Stentor (it also starts at every login; look for its icon next to the clock)"
stentor service install
# An install from when this was called hold-dictate. `service install` has just stopped it and taken its
# start-at-login entry away; its tool goes too, so the old command is not left behind. Its settings were copied.
# Only now, once Stentor runs: an install that fails half way leaves the old copy working.
try {  # with "Stop", Windows PowerShell turns a native command's error output into an exception
    if (Test-Path $OldTool) { uv tool uninstall hold-dictate *> $null }
} catch { }
if (-not (Get-StentorEnv "NO_SETUP")) {
    Write-Host "==> Opening the settings in your browser. Add your API key there."
    stentor settings --wait 10
}
