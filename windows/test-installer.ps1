<#
.SYNOPSIS
    Proves the MSI built by build-installer.ps1 actually installs, runs, and
    uninstalls cleanly -- an installer nobody has ever run is just a file.

.DESCRIPTION
    Runs the full lifecycle for real, against this machine:
      1. msiexec /i  (per-user, quiet, verbose log)
      2. Verify what landed: exe present and full size, agent source present,
         Start Menu + Desktop shortcuts resolve, an ARP entry exists, and --
         critically -- no config.json and no hardcoded access token shipped.
      3. Launch the installed exe, confirm the process is still alive after a
         few seconds (catches "dead on arrival" self-contained-exe failures),
         then terminate it.
      4. msiexec /x  (quiet, verbose log)
      5. Verify removal: install dir, shortcuts, and ARP entry are all gone.
      6. A second, short install/uninstall cycle with DESKTOPSHORTCUT=0 --
         the documented non-interactive opt-out for the installer's desktop-
         shortcut checkbox (see Package.wxs) -- proving the property is
         actually wired to the component's install condition, not just
         present in the source and assumed to work. Finishes clean, same as
         step 5.

    Every check uses the same PASS/FAIL style as the Python test suites under
    agent/, so a human skimming either one recognizes the pattern immediately.
    Exits 1 if anything failed, 0 if everything passed.

    Destructive by nature: it installs and then uninstalls the product on the
    machine it runs on. If Drobo Dashboard REBORN is already installed when
    this starts, it refuses to run rather than uninstall someone's real,
    in-use install out from under them.

.EXAMPLE
    powershell windows/test-installer.ps1
    powershell windows/test-installer.ps1 -MsiPath windows\dist\DroboDashboardReborn-0.1.0-win-x64.msi
#>
[CmdletBinding()]
param(
    [string]$MsiPath
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$WindowsDir = Join-Path $RepoRoot 'windows'
$DistDir = Join-Path $WindowsDir 'dist'
$LogDir = Join-Path $DistDir 'test-logs'

$script:failures = 0
$script:checks = 0

function Check([string]$Label, [bool]$Cond, [string]$Extra = '') {
    $script:checks++
    if ($Cond) {
        Write-Host "  PASS  $Label"
    } else {
        $script:failures++
        $suffix = if ($Extra) { " -- $Extra" } else { '' }
        Write-Host "  FAIL  $Label$suffix" -ForegroundColor Red
    }
}

function Section([string]$Title) {
    Write-Host ''
    Write-Host "-- $Title --" -ForegroundColor Cyan
}

# --- 0. Find the MSI, refuse to run if something's already installed -------

Section 'Preflight'

if (-not $MsiPath) {
    $candidate = Get-ChildItem -LiteralPath $DistDir -Filter 'DroboDashboardReborn-*-win-x64.msi' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $candidate) {
        Write-Host "FAIL  No MSI found under $DistDir. Run windows\build-installer.ps1 first." -ForegroundColor Red
        exit 1
    }
    $MsiPath = $candidate.FullName
}
if (-not (Test-Path -LiteralPath $MsiPath)) {
    Write-Host "FAIL  MSI not found: $MsiPath" -ForegroundColor Red
    exit 1
}
$MsiPath = (Resolve-Path -LiteralPath $MsiPath).Path
Write-Host "MSI: $MsiPath"

$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\DroboDashboardReborn'
$existingArp = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ItemProperty $_.PsPath -ErrorAction SilentlyContinue } |
    Where-Object { $_.DisplayName -eq 'Drobo Dashboard REBORN' }

if ($existingArp -or (Test-Path $InstallRoot)) {
    Write-Host 'FAIL  Drobo Dashboard REBORN already appears to be installed on this machine.' -ForegroundColor Red
    Write-Host '      Refusing to run -- this script uninstalls at the end and would remove a real install.' -ForegroundColor Red
    Write-Host '      Uninstall it manually first, then re-run this script.' -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$installLog = Join-Path $LogDir 'install.log'
$uninstallLog = Join-Path $LogDir 'uninstall.log'

# --- 1. Install --------------------------------------------------------------

Section 'Install (msiexec /i, per-user, quiet)'

$installProc = Start-Process msiexec.exe -ArgumentList @(
    '/i', "`"$MsiPath`"",
    '/qn',
    '/l*v', "`"$installLog`""
) -Wait -PassThru -NoNewWindow

Check "msiexec /i exit code is 0 (got $($installProc.ExitCode))" ($installProc.ExitCode -eq 0)

if ($installProc.ExitCode -ne 0) {
    Write-Host ''
    Write-Host "msiexec failed -- relevant log lines from $installLog :" -ForegroundColor Yellow
    Get-Content -LiteralPath $installLog -ErrorAction SilentlyContinue |
        Select-String -Pattern 'Error|Return Value 3' |
        Select-Object -Last 20 |
        ForEach-Object { Write-Host "    $_" }
    Write-Host ''
    Write-Host "$script:failures FAILURES" -ForegroundColor Red
    exit 1
}

# --- 2. Verify what landed ----------------------------------------------------

Section 'Verify install contents'

Check "install dir exists ($InstallRoot)" (Test-Path $InstallRoot)

$exePath = Join-Path $InstallRoot 'DroboDashboardReborn.exe'
$exeOk = Test-Path $exePath
Check "app exe present" $exeOk
if ($exeOk) {
    $exeSizeMb = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    # Self-contained single-file WPF publish is reliably >100 MB (carries its
    # own .NET runtime); anything much smaller means publish was broken.
    Check "app exe is a real self-contained publish ($exeSizeMb MB)" ($exeSizeMb -gt 100)
}

$agentDir = Join-Path $InstallRoot 'agent'
Check "agent source dir present" (Test-Path $agentDir)
Check "run_agent.py present" (Test-Path (Join-Path $agentDir 'run_agent.py'))
Check "drobo_agent package present" (Test-Path (Join-Path $agentDir 'drobo_agent'))
# The agent imports drobo_nasd; without it the installed copy dies at
# startup with ModuleNotFoundError. Checked here because that failure
# happens at INSTALL time, not build time.
Check 'SDK package present (agent cannot start without it)' (Test-Path (Join-Path $InstallRoot 'sdk\drobo_nasd\esatm.py'))
Check 'SDK models present' (Test-Path (Join-Path $InstallRoot 'sdk\drobo_nasd\models.py'))
# Imported by both monitor.py and api.py at module load, so a staging miss here
# is a dead agent rather than a missing feature -- same class of failure as the
# two above, which is why it is checked the same way.
Check 'SDK netcheck present (agent imports it at startup)' (Test-Path (Join-Path $InstallRoot 'sdk\drobo_nasd\netcheck.py'))
Check "drobo_agent\nasd subpackage present" (Test-Path (Join-Path $agentDir 'drobo_agent\nasd'))

# The one non-negotiable: never ship a real device token or a config.json
# that would make every install share the same access token.
$shippedConfig = Get-ChildItem -LiteralPath $InstallRoot -Recurse -Filter 'config.json' -ErrorAction SilentlyContinue
Check "no config.json shipped in installed tree" ($shippedConfig.Count -eq 0) "found: $($shippedConfig.FullName -join ', ')"

$tokenHits = Get-ChildItem -LiteralPath $agentDir -Recurse -Filter '*.py' -ErrorAction SilentlyContinue |
    Select-String -Pattern '(?i)token[''"]?\s*[:=]\s*[''"][a-zA-Z0-9_-]{8,}[''"]' -ErrorAction SilentlyContinue
Check "no hardcoded token literal in installed .py files" ($null -eq $tokenHits) "found in: $($tokenHits.Path -join ', ')"

# Shortcuts
$startMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Drobo Dashboard REBORN'
$startMenuLnk = Join-Path $startMenuDir 'Drobo Dashboard REBORN.lnk'
Check "Start Menu shortcut exists" (Test-Path $startMenuLnk)

$shell = New-Object -ComObject WScript.Shell
$desktopFolder = $shell.SpecialFolders('Desktop')
$desktopLnk = Join-Path $desktopFolder 'Drobo Dashboard REBORN.lnk'
Check "Desktop shortcut exists" (Test-Path $desktopLnk)

if (Test-Path $startMenuLnk) {
    $target = $shell.CreateShortcut($startMenuLnk).TargetPath
    Check "Start Menu shortcut target exists ($target)" (Test-Path $target)
}
if (Test-Path $desktopLnk) {
    $target = $shell.CreateShortcut($desktopLnk).TargetPath
    Check "Desktop shortcut target exists ($target)" (Test-Path $target)
}

# ARP entry. WiX v4 perUser packages register this under HKLM's Uninstall
# key (Windows Installer's own per-user-managed-install mechanism), not
# HKCU -- check both since which one is used isn't this script's business,
# only that an entry exists somewhere.
$arpEntry = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ItemProperty $_.PsPath -ErrorAction SilentlyContinue } |
    Where-Object { $_.DisplayName -eq 'Drobo Dashboard REBORN' } |
    Select-Object -First 1
Check "Add/Remove Programs entry exists" ($null -ne $arpEntry)
if ($arpEntry) {
    Check "ARP entry reports version 0.1.0" ($arpEntry.DisplayVersion -eq '0.1.0') "got: $($arpEntry.DisplayVersion)"
    $productCode = $arpEntry.PSChildName
    Write-Host "ProductCode: $productCode"
} else {
    # Can't uninstall by ProductCode if it was never registered; fall back to
    # the MSI file itself so the rest of the script can still attempt cleanup.
    $productCode = $MsiPath
}

# --- 3. Run it well enough to prove it's not dead on arrival -----------------

Section 'Launch and liveness check'

if ($exeOk) {
    $launchError = $null
    try {
        $appProc = Start-Process -FilePath $exePath -PassThru
    } catch {
        $launchError = $_
    }
    Check "app process started" ($null -ne $appProc) "$launchError"

    if ($appProc) {
        Start-Sleep -Seconds 3
        $stillRunning = Get-Process -Id $appProc.Id -ErrorAction SilentlyContinue
        Check "app process still alive after 3s (not dead on arrival)" ($null -ne $stillRunning) "exit code: $($appProc.ExitCode)"

        if ($stillRunning) {
            Write-Host "  window title: '$($stillRunning.MainWindowTitle)'"
            Stop-Process -Id $appProc.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
        }
    }
} else {
    Check "app process started" $false "skipped -- exe missing"
    Check "app process still alive after 3s (not dead on arrival)" $false "skipped -- exe missing"
}

# Belt and suspenders: make sure nothing from this test is left running
# before uninstall, since msiexec can't replace files that are in use.
Get-Process -Name 'DroboDashboardReborn' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# --- 4. Uninstall -------------------------------------------------------------

Section 'Uninstall (msiexec /x, quiet)'

$uninstallArg = if ($productCode -like '{*}') { $productCode } else { "`"$productCode`"" }
$uninstallProc = Start-Process msiexec.exe -ArgumentList @(
    '/x', $uninstallArg,
    '/qn',
    '/l*v', "`"$uninstallLog`""
) -Wait -PassThru -NoNewWindow

Check "msiexec /x exit code is 0 (got $($uninstallProc.ExitCode))" ($uninstallProc.ExitCode -eq 0)

# --- 5. Verify removal --------------------------------------------------------

Section 'Verify removal'

Check "install dir removed" (-not (Test-Path $InstallRoot))
Check "Start Menu shortcut folder removed" (-not (Test-Path $startMenuDir))
Check "Desktop shortcut removed" (-not (Test-Path $desktopLnk))

$arpAfter = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ItemProperty $_.PsPath -ErrorAction SilentlyContinue } |
    Where-Object { $_.DisplayName -eq 'Drobo Dashboard REBORN' }
Check "Add/Remove Programs entry removed" ($null -eq $arpAfter) "still present: $($arpAfter.PSChildName -join ', ')"

Check "HKCU\Software\DroboDashboardReborn removed" (-not (Test-Path 'HKCU:\Software\DroboDashboardReborn'))

# --- 6. DESKTOPSHORTCUT=0 opt-out, for real ------------------------------------
# The main lifecycle above already proves the default (ticked, no override) --
# this proves the documented non-interactive opt-out property actually does
# something, rather than just existing in Package.wxs and being assumed to
# work. A second short install/uninstall cycle, same machine, same rule as
# above: must finish clean.

Section 'DESKTOPSHORTCUT=0 opt-out (msiexec /i, quiet)'

$optOutLog = Join-Path $LogDir 'install-desktopshortcut0.log'
$optOutInstallProc = Start-Process msiexec.exe -ArgumentList @(
    '/i', "`"$MsiPath`"",
    '/qn',
    'DESKTOPSHORTCUT=0',
    '/l*v', "`"$optOutLog`""
) -Wait -PassThru -NoNewWindow

Check "msiexec /i DESKTOPSHORTCUT=0 exit code is 0 (got $($optOutInstallProc.ExitCode))" ($optOutInstallProc.ExitCode -eq 0)

Check "install dir exists with the override" (Test-Path $InstallRoot)
Check "Desktop shortcut NOT created with DESKTOPSHORTCUT=0" (-not (Test-Path $desktopLnk))
Check "Start Menu shortcut still created (unconditional, unaffected by DESKTOPSHORTCUT)" (Test-Path $startMenuLnk)

$optOutArpEntry = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ItemProperty $_.PsPath -ErrorAction SilentlyContinue } |
    Where-Object { $_.DisplayName -eq 'Drobo Dashboard REBORN' } |
    Select-Object -First 1
$optOutProductCode = if ($optOutArpEntry) { $optOutArpEntry.PSChildName } else { $MsiPath }

Section 'DESKTOPSHORTCUT=0 opt-out (msiexec /x, quiet)'

Get-Process -Name 'DroboDashboardReborn' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

$optOutUninstallLog = Join-Path $LogDir 'uninstall-desktopshortcut0.log'
$optOutUninstallArg = if ($optOutProductCode -like '{*}') { $optOutProductCode } else { "`"$optOutProductCode`"" }
$optOutUninstallProc = Start-Process msiexec.exe -ArgumentList @(
    '/x', $optOutUninstallArg,
    '/qn',
    '/l*v', "`"$optOutUninstallLog`""
) -Wait -PassThru -NoNewWindow

Check "msiexec /x exit code is 0 (got $($optOutUninstallProc.ExitCode))" ($optOutUninstallProc.ExitCode -eq 0)
Check "install dir removed again" (-not (Test-Path $InstallRoot))
Check "Start Menu shortcut folder removed again" (-not (Test-Path $startMenuDir))

# --- Summary -------------------------------------------------------------------

Write-Host ''
if ($script:failures -eq 0) {
    Write-Host "ALL PASS ($($script:checks) checks)" -ForegroundColor Green
    exit 0
} else {
    Write-Host "$script:failures FAILURES (of $script:checks checks)" -ForegroundColor Red
    Write-Host "Install log:   $installLog"
    Write-Host "Uninstall log: $uninstallLog"
    exit 1
}
