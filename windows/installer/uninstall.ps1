<#
.SYNOPSIS
    Removes a Drobo Dashboard REBORN install made by install.ps1 (the
    fallback, non-MSI installer).

.DESCRIPTION
    Mirrors install.ps1 exactly in reverse: deletes the install directory,
    the Start Menu folder, the Desktop shortcut, and the Add/Remove Programs
    registry key. This file is copied into the install directory at install
    time and is what the "Uninstall" shortcut and the ARP entry both call --
    it does not run from the original download location, so it works even
    if that was a temp folder that's since been cleaned up.
#>
param(
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\DroboDashboardReborn'
$StartMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Drobo Dashboard REBORN'
$DesktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Drobo Dashboard REBORN.lnk'
$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DroboDashboardReborn'

if (-not $Quiet) { Write-Host 'Removing Drobo Dashboard REBORN...' }

# Ask any running agent/app to close first isn't attempted here -- deleting
# files out from under a running process just fails the delete, which is a
# safe, obvious failure mode (rerun after closing the app), not a silent one.

if (Test-Path $StartMenuDir) { Remove-Item -Recurse -Force $StartMenuDir }
if (Test-Path $DesktopLink) { Remove-Item -Force $DesktopLink }
if (Test-Path $UninstallKey) { Remove-Item -Recurse -Force $UninstallKey }

# Delete the install dir last, and from a copy of this logic running out of
# it -- self-deleting the running script's own folder on Windows fails on
# the last file (this .ps1 itself) while PowerShell still holds it open, so
# schedule that one removal to happen a moment after this process exits.
if (Test-Path $InstallDir) {
    $selfPath = $MyInvocation.MyCommand.Path
    $rest = Get-ChildItem -LiteralPath $InstallDir -Force |
        Where-Object { $_.FullName -ne $selfPath }
    foreach ($item in $rest) {
        Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }

    $cleanupCmd = "Start-Sleep -Milliseconds 500; " +
        "Remove-Item -LiteralPath '$InstallDir' -Recurse -Force -ErrorAction SilentlyContinue"
    Start-Process -WindowStyle Hidden -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-Command', $cleanupCmd) | Out-Null
}

if (-not $Quiet) {
    Write-Host 'Done. Drobo Dashboard REBORN has been removed.'
}
