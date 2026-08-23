<#
.SYNOPSIS
  Install KAME API Rotation into the real Hermes and restart it.

.DESCRIPTION
  This exists because `tools/deploy.py` cannot always be run from where the
  plugin is developed. An assistant session, or any process launched by an MSIX
  packaged app, inherits that package's identity, and Windows then redirects
  every AppData\Local path it touches into

      %LOCALAPPDATA%\Packages\<package>\LocalCache\Local\...

  Reads fall through, so the redirected view looks exactly like the real
  install -- same bytes, same timestamps -- while every *write* lands in a
  private copy Hermes never opens. That is not a theory: it is how v1.0.8 was
  "deployed" on 20 August and never ran for a single call.

  So this script does two things `deploy.py` does, plus one it cannot:

    1. proves it is running outside any app container, and refuses otherwise;
    2. copies the plugin and reads the manifest back out of the destination;
    3. restarts Hermes, because a copied plugin is not a loaded plugin.

  Run it from a normal PowerShell window -- one you opened yourself from the
  Start menu or the taskbar, not one opened by another application.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\deploy_from_host.ps1
#>

[CmdletBinding()]
param(
    # Skip the restart. For a machine where Hermes is being started by hand.
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'

$Source      = Join-Path $PSScriptRoot '..\hermes-kame-api-rotation' | Resolve-Path
$HermesHome  = Join-Path $env:LOCALAPPDATA 'hermes'
$Target      = Join-Path $HermesHome 'plugins\hermes-kame-api-rotation'
$HermesExe   = Join-Path $HermesHome 'hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe'

# Directories that are build output or local state, never part of the plugin.
$Ignore = @('__pycache__', '.pytest_cache', '.mypy_cache')

function Fail($message) {
    Write-Host ''
    Write-Host 'REFUSING TO DEPLOY' -ForegroundColor Red
    Write-Host $message
    exit 3
}

# --- 1. prove we are not inside a container ---------------------------------
# The check is on the *resolved* path, because the redirect is invisible in the
# path you type: "$env:LOCALAPPDATA\hermes" is a real string that quietly
# resolves somewhere else. Resolution is what exposes it.

Write-Host "KAME API Rotation - deploy" -ForegroundColor Cyan
Write-Host ''

New-Item -ItemType Directory -Force -Path $HermesHome | Out-Null

# Ask the filesystem where the path really goes, rather than trusting the name.
$real = [System.IO.Path]::GetFullPath($HermesHome)
$marker = @('localcache', 'windowsapps', 'packages\') | Where-Object { $real.ToLower().Contains($_) }
if (-not $marker) {
    # GetFullPath does not follow the MSIX redirect, so ask a child process to
    # report where it actually landed. Creating and reading back one file is
    # the only reliable probe.
    $token = [guid]::NewGuid().ToString('N')
    $stamp = Join-Path $HermesHome ".kame-deploy-probe-$token"
    Set-Content -Path $stamp -Value $token -Encoding ascii
    try {
        $shadow = Join-Path $env:LOCALAPPDATA "Packages"
        $hit = Get-ChildItem $shadow -Directory -ErrorAction SilentlyContinue |
               ForEach-Object { Join-Path $_.FullName "LocalCache\Local\hermes\.kame-deploy-probe-$token" } |
               Where-Object { Test-Path $_ } |
               Select-Object -First 1
        if ($hit) {
            Remove-Item $stamp -Force -ErrorAction SilentlyContinue
            Fail @"
this PowerShell is running inside an app container.
The write landed in:
  $hit
Hermes never reads that path, so the copy would be invisible to it -- exactly
how v1.0.8 was lost.

Open PowerShell yourself from the Start menu (not from inside another app) and
run this script again.
"@
        }
    } finally {
        Remove-Item $stamp -Force -ErrorAction SilentlyContinue
    }
} else {
    Fail "LOCALAPPDATA already points inside a package container ($marker). Open a normal PowerShell and retry."
}

Write-Host "host      : not containerized" -ForegroundColor Green
Write-Host "source    : $Source"
Write-Host "target    : $Target"

# --- 2. copy, then read the manifest back out of the destination ------------

$expected = (Select-String -Path (Join-Path $Source 'plugin.yaml') -Pattern '^version:\s*"?([0-9.]+)"?').Matches[0].Groups[1].Value
Write-Host "version   : $expected"
Write-Host ''

if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Target | Out-Null

Get-ChildItem $Source -Recurse -File | Where-Object {
    $path = $_.FullName
    -not ($Ignore | Where-Object { $path -like "*\$_\*" })
} | ForEach-Object {
    $relative = $_.FullName.Substring($Source.Path.Length + 1)
    $destination = Join-Path $Target $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
    Copy-Item $_.FullName $destination -Force
}

# The whole point of the exercise: not "did the copy run" but "does the place
# Hermes reads now say 1.0.9".
$landed = (Select-String -Path (Join-Path $Target 'plugin.yaml') -Pattern '^version:\s*"?([0-9.]+)"?').Matches[0].Groups[1].Value
if ($landed -ne $expected) {
    Fail "the destination reads $landed, not $expected. The copy did not land where it was aimed."
}
Write-Host "copied and verified: destination manifest reads $landed" -ForegroundColor Green

# --- 2b. seed the Desktop half ----------------------------------------------
# The Python half installs this itself when it registers (desktop_ui.py), so
# this copy is only a head start: it puts the chip in place before the backend
# has finished starting, on the first launch after an upgrade. Same bytes, same
# destination, so the two can never disagree.
#
# It goes to desktop-plugins\ and NOT to plugins\<name>\desktop\ -- both are
# loaded by the same renderer pipeline, but the second is capped to
# defaultEnabled:false, and a status chip that waits for someone to find a
# toggle is not a status chip.

$DesktopSource = Join-Path $Source 'desktop-ui\plugin.js'
$DesktopTarget = Join-Path $HermesHome 'desktop-plugins\hermes-kame-api-rotation\plugin.js'
if (Test-Path $DesktopSource) {
    New-Item -ItemType Directory -Force -Path (Split-Path $DesktopTarget) | Out-Null
    Copy-Item $DesktopSource $DesktopTarget -Force
    Write-Host "desktop   : panel installed at $DesktopTarget" -ForegroundColor Green
} else {
    Write-Host "desktop   : desktop-ui\plugin.js missing - no status chip, no /kame page" -ForegroundColor Yellow
}

# --- 3. restart, because a copied plugin is not a loaded plugin -------------

if ($NoRestart) {
    Write-Host ''
    Write-Host "Restart Hermes yourself -- the running process still has the old code." -ForegroundColor Yellow
    exit 0
}

Write-Host ''
$running = Get-Process -Name 'Hermes' -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "stopping Hermes (pid $($running.Id -join ', '))"
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}
if (Test-Path $HermesExe) {
    Start-Process $HermesExe
    Write-Host "Hermes restarted. Type /kame in a new chat to see the panel." -ForegroundColor Green
} else {
    Write-Host "Hermes.exe not found at $HermesExe - start it from the Start menu." -ForegroundColor Yellow
}
