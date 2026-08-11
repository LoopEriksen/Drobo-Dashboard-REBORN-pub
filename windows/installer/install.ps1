<#
.SYNOPSIS
    Fallback installer for Drobo Dashboard REBORN (used only if the WiX MSI
    build isn't available -- see windows/build-installer.ps1).

.DESCRIPTION
    Copies the already-published payload (this script's own folder) to
    %LOCALAPPDATA%\Programs\DroboDashboardReborn, creates Start Menu and
    Desktop shortcuts, and registers a real "Add/Remove Programs" entry
    under HKCU so it can be found and removed the normal Windows way, no
    admin rights required (everything here is per-user).

    Run this from inside the extracted installer package, i.e. the folder
    that already contains DroboDashboardReborn.exe, agent\, and the other
    payload files alongside this script.
#>
$ErrorActionPreference = 'Stop'

$PayloadDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\DroboDashboardReborn'
$StartMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Drobo Dashboard REBORN'
$DesktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Drobo Dashboard REBORN.lnk'
$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DroboDashboardReborn'

$versionPath = Join-Path $PayloadDir 'version.txt'
$version = if (Test-Path $versionPath) { (Get-Content $versionPath -Raw).Trim() } else { '0.0.0' }

Write-Host "Installing Drobo Dashboard REBORN $version to $InstallDir ..."

# --- Copy payload -----------------------------------------------------
# Re-runnable: wipe and re-copy rather than trying to diff, so upgrades and
# re-installs behave the same as a fresh install.
if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Get-ChildItem -LiteralPath $PayloadDir -Force |
    Where-Object { $_.Name -notin @('install.ps1') } |
    ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force
    }

# uninstall.ps1 ships in the payload too, but copy it explicitly in case the
# Where-Object filter above ever changes -- the uninstall entry depends on
# it existing inside InstallDir, not the (temporary) payload dir.
Copy-Item -LiteralPath (Join-Path $PayloadDir 'uninstall.ps1') -Destination $InstallDir -Force

# --- Shortcuts ----------------------------------------------------------
New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null
$shell = New-Object -ComObject WScript.Shell

$targetBat = Join-Path $InstallDir 'Launch-DroboDashboardReborn.bat'
$iconPath = Join-Path $InstallDir 'DroboDashboardReborn.exe'

foreach ($linkPath in @(
    (Join-Path $StartMenuDir 'Drobo Dashboard REBORN.lnk'),
    $DesktopLink
)) {
    $shortcut = $shell.CreateShortcut($linkPath)
    $shortcut.TargetPath = $targetBat
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.IconLocation = $iconPath
    $shortcut.Description = 'Start the Drobo agent and the dashboard'
    $shortcut.Save()
}

$updateShortcut = $shell.CreateShortcut((Join-Path $StartMenuDir 'Check for Updates.lnk'))
$updateShortcut.TargetPath = Join-Path $InstallDir 'Check-ForUpdates.bat'
$updateShortcut.WorkingDirectory = $InstallDir
$updateShortcut.Description = 'Check whether a newer Drobo Dashboard REBORN is available'
$updateShortcut.Save()

$uninstallShortcut = $shell.CreateShortcut((Join-Path $StartMenuDir 'Uninstall Drobo Dashboard REBORN.lnk'))
$uninstallShortcut.TargetPath = 'powershell.exe'
$uninstallShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\uninstall.ps1`""
$uninstallShortcut.WorkingDirectory = $InstallDir
$uninstallShortcut.Description = 'Remove Drobo Dashboard REBORN'
$uninstallShortcut.Save()

# --- Add/Remove Programs entry ------------------------------------------
New-Item -Path $UninstallKey -Force | Out-Null
Set-ItemProperty -Path $UninstallKey -Name 'DisplayName' -Value 'Drobo Dashboard REBORN'
Set-ItemProperty -Path $UninstallKey -Name 'DisplayVersion' -Value $version
Set-ItemProperty -Path $UninstallKey -Name 'Publisher' -Value 'Drobo Dashboard REBORN Project'
Set-ItemProperty -Path $UninstallKey -Name 'InstallLocation' -Value $InstallDir
Set-ItemProperty -Path $UninstallKey -Name 'DisplayIcon' -Value $iconPath
Set-ItemProperty -Path $UninstallKey -Name 'UninstallString' `
    -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\uninstall.ps1`""
Set-ItemProperty -Path $UninstallKey -Name 'QuietUninstallString' `
    -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\uninstall.ps1`" -Quiet"
Set-ItemProperty -Path $UninstallKey -Name 'NoModify' -Value 1 -Type DWord
Set-ItemProperty -Path $UninstallKey -Name 'NoRepair' -Value 1 -Type DWord
try {
    $sizeKb = [math]::Round(((Get-ChildItem $InstallDir -Recurse -File | Measure-Object Length -Sum).Sum) / 1KB)
    Set-ItemProperty -Path $UninstallKey -Name 'EstimatedSize' -Value $sizeKb -Type DWord
} catch {
    # Cosmetic field only -- if it can't be computed, Windows just shows no size.
}

Write-Host ''
Write-Host "Done. Drobo Dashboard REBORN $version is installed."
Write-Host "Launch it from the Start Menu or Desktop shortcut."
Write-Host "To remove it later: Settings > Apps, or the Start Menu 'Uninstall' shortcut."
