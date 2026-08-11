<#
.SYNOPSIS
    Builds a distributable installer for Drobo Dashboard REBORN.

.DESCRIPTION
    One command, idempotent, safe to re-run:
      1. Publishes the WPF app self-contained, single-file, win-x64.
      2. Assembles it with the Python agent's source (no config.json --
         the access token is generated on first run, never shipped) and a
         couple of small helper scripts into a versioned staging folder
         under windows/dist/.
      3. Packages that folder as a real .msi with WiX, if the `wix` CLI is
         available (installed once via `dotnet tool install --global wix`,
         plus the UI extension -- see the WixToolset.UI.wixext step below,
         needed for the desktop-shortcut checkbox dialog in Package.wxs).
         If WiX isn't available, the extension can't be resolved, or the
         build fails for any other reason, falls back to a self-contained
         PowerShell installer (windows/installer/install.ps1) zipped up with
         the payload -- still a genuine install/uninstall, just not an MSI.

    windows/dist/ is gitignored; nothing this script produces should ever be
    committed.

.EXAMPLE
    pwsh windows/build-installer.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Host ''
    Write-Host "BUILD FAILED: $Message" -ForegroundColor Red
    exit 1
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$WindowsDir = Join-Path $RepoRoot 'windows'
$ProjectDir = Join-Path $WindowsDir 'DroboDashboardReborn'
$CsprojPath = Join-Path $ProjectDir 'DroboDashboardReborn.csproj'
$InstallerDir = Join-Path $WindowsDir 'installer'
$AgentSrc = Join-Path $RepoRoot 'agent'
$DistDir = Join-Path $WindowsDir 'dist'

Write-Host '== Drobo Dashboard REBORN -- installer build ==' -ForegroundColor Cyan

# --- 1. Preconditions ----------------------------------------------------

if (-not (Test-Path $CsprojPath)) {
    Fail "Can't find the WPF project at $CsprojPath. Run this from a full checkout."
}

$dotnetCmd = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnetCmd) {
    Fail (
        ".NET SDK not found on PATH. Install the .NET 8 SDK first:`n" +
        "    winget install Microsoft.DotNet.SDK.8`n" +
        "then open a new terminal and re-run this script."
    )
}

try {
    $sdks = & dotnet --list-sdks 2>$null
} catch {
    $sdks = $null
}
if (-not ($sdks -match '^8\.')) {
    Fail (
        ".NET 8 SDK not found (`dotnet --list-sdks` didn't report an 8.x`n" +
        "entry). Install it with: winget install Microsoft.DotNet.SDK.8"
    )
}

# --- 2. Read the one version number everything else follows -------------

$csprojText = Get-Content -LiteralPath $CsprojPath -Raw
$versionMatch = [regex]::Match($csprojText, '<Version>([^<]+)</Version>')
if (-not $versionMatch.Success) {
    Fail "Couldn't find a <Version> element in $CsprojPath."
}
$Version = $versionMatch.Groups[1].Value
Write-Host "Version: $Version"

$StagingName = "DroboDashboardReborn-$Version-win-x64"
$StagingDir = Join-Path $DistDir $StagingName

# --- 3. Clean slate for this version -------------------------------------
# Safe to re-run: always start this version's staging folder fresh rather
# than layering a publish on top of a previous one and hoping nothing stale
# is left behind.

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
if (Test-Path $StagingDir) {
    Remove-Item -Recurse -Force $StagingDir
}
New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null

# --- 4. Publish the WPF app -----------------------------------------------
# Self-contained + single-file: the ~155 MB result carries its own .NET
# runtime, so end users need nothing preinstalled. Don't fight the size.

Write-Host ''
Write-Host '-- Publishing WPF app (self-contained, win-x64, single-file)...'

& dotnet publish $CsprojPath `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -p:PublishSingleFile=true `
    -o $StagingDir
if ($LASTEXITCODE -ne 0) {
    Fail 'dotnet publish failed -- see the output above.'
}

$exePath = Join-Path $StagingDir 'DroboDashboardReborn.exe'
if (-not (Test-Path $exePath)) {
    Fail "Publish reported success but $exePath doesn't exist."
}

# --- 5. Copy the Python agent (no config.json -- ever) --------------------
# The app is useless without the agent, so the agent's source ships too and
# runs with the system's own Python (this project is stdlib-only by design,
# so nothing needs to be installed beyond Python itself). config.json holds
# the access token; agent/drobo_agent/config.py generates one the first
# time run_agent.py actually starts, so shipping a baked-in one would mean
# every install sharing the same token -- a real security bug, not a
# convenience. __pycache__ and the test suite aren't useful to an installed
# copy, so both are left behind.

Write-Host ''
Write-Host '-- Copying the Python agent...'

$agentDst = Join-Path $StagingDir 'agent'
New-Item -ItemType Directory -Force -Path $agentDst | Out-Null

Copy-Item -LiteralPath (Join-Path $AgentSrc 'run_agent.py') -Destination $agentDst
Copy-Item -LiteralPath (Join-Path $AgentSrc 'config.example.json') -Destination $agentDst
Copy-Item -LiteralPath (Join-Path $AgentSrc 'README.md') -Destination $agentDst

$droboAgentSrc = Join-Path $AgentSrc 'drobo_agent'
$droboAgentDst = Join-Path $agentDst 'drobo_agent'
Get-ChildItem -LiteralPath $droboAgentSrc -Recurse -Force |
    Where-Object { $_.FullName -notmatch '__pycache__' } |
    ForEach-Object {
        $relative = $_.FullName.Substring($droboAgentSrc.Length).TrimStart('\')
        $destPath = Join-Path $droboAgentDst $relative
        if ($_.PSIsContainer) {
            New-Item -ItemType Directory -Force -Path $destPath | Out-Null
        } else {
            New-Item -ItemType Directory -Force -Path (Split-Path $destPath -Parent) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $destPath -Force
        }
    }

# The Drobo protocol lives in its own package (sdk/drobo_nasd), a sibling of
# agent/ rather than a subpackage, so the agent cannot start without it.
#
# It is staged to <install>\sdk\drobo_nasd, which is exactly where
# drobo_agent/__init__.py's fallback looks (two levels up from the package,
# then into sdk/). Get this path wrong and the failure appears at INSTALL time
# with a ModuleNotFoundError, not at build time -- so test-installer.ps1 checks
# for it explicitly rather than assuming this copy worked.
$sdkSrc = Join-Path $RepoRoot 'sdk\drobo_nasd'
$sdkDst = Join-Path $StagingDir 'sdk\drobo_nasd'
if (-not (Test-Path -LiteralPath $sdkSrc)) {
    Fail "the SDK is missing from the repo at $sdkSrc -- the agent cannot run without it."
}
Get-ChildItem -LiteralPath $sdkSrc -Recurse -Force |
    Where-Object { $_.FullName -notmatch '__pycache__' } |
    ForEach-Object {
        $relative = $_.FullName.Substring($sdkSrc.Length).TrimStart('\')
        $destPath = Join-Path $sdkDst $relative
        if ($_.PSIsContainer) {
            New-Item -ItemType Directory -Force -Path $destPath | Out-Null
        } else {
            New-Item -ItemType Directory -Force -Path (Split-Path $destPath -Parent) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $destPath -Force
        }
    }
if (-not (Test-Path -LiteralPath (Join-Path $sdkDst 'esatm.py'))) {
    Fail 'the SDK did not stage correctly -- refusing to ship an agent that cannot start.'
}

if (Test-Path (Join-Path $agentDst 'config.json')) {
    # Should be impossible given the copy list above, but a shipped token
    # would be bad enough that this is worth guarding explicitly rather
    # than trusting the file list never changes underneath this script.
    Fail 'config.json ended up in the staged payload -- refusing to ship a real access token.'
}

# --- 5b. No personal identifiers in the payload ---------------------------
# The staged tree ships verbatim inside the MSI, so a stale checkout builds
# whatever identifiers its sources still carry. That happened: installers
# built before the 2026-08-10 privacy scrub shipped the owner's real device
# hostname in three staged files, and nothing noticed until a full audit.
# Sources get scrubbed; artifacts built FROM them must be checked at the
# moment of building, because a payload is exactly a snapshot of a checkout.
#
# Two pattern sets. The GENERIC ones live here: they describe identifier
# SHAPES (any real Drobo serial, any real Drobo MAC, any real user-profile
# path), so the tracked script never has to name an actual identifier. The
# first version of this check listed the owner's real username, device name
# and share names right here -- re-leaking, in a tracked file, exactly what
# that day's scrub had just removed. The specific strings belong in
# windows/installer-identifiers.local.txt (gitignored, one regex per line),
# which each developer seeds with their own; missing is a warning, not a
# failure, so a fresh checkout still gets the generic protection.
Write-Host '-- Checking the staged payload for personal identifiers...'
$identifierPatterns = @(
    'drb\d{6}[a-z]\d{5}',        # the shape of every real Drobo serial
    '00[:-]1[Aa][:-]62',         # Drobo Inc's public MAC OUI -- catches any real unit's MAC
    'C:\\+Users\\+(?!you\b|<you>|Public\b)[a-z]' # a real user-profile path (placeholders allowed)
)
$localList = Join-Path $WindowsDir 'installer-identifiers.local.txt'
if (Test-Path -LiteralPath $localList) {
    $identifierPatterns += Get-Content -LiteralPath $localList |
        Where-Object { $_.Trim() -and -not $_.StartsWith('#') }
} else {
    Write-Host ('   note: {0} not found -- only generic identifier patterns applied' -f $localList)
}
$staged = Get-ChildItem $StagingDir -Recurse -File |
          Where-Object { $_.Extension -notin '.exe', '.dll', '.pdb', '.ico' }
foreach ($f in $staged) {
    $text = [System.IO.File]::ReadAllText($f.FullName)
    foreach ($pat in $identifierPatterns) {
        if ($text -imatch $pat) {
            Fail ("staged file {0} matches identifier pattern '{1}' -- refusing to ship it. " -f `
                  $f.FullName.Substring($StagingDir.Length + 1), $pat) `
                + 'Scrub the source it was staged from, then rebuild.'
        }
    }
}

# --- 6. Installer helper scripts ------------------------------------------

Write-Host '-- Adding launcher and updater...'

Copy-Item -LiteralPath (Join-Path $InstallerDir 'Launch-DroboDashboardReborn.bat') -Destination $StagingDir
Copy-Item -LiteralPath (Join-Path $InstallerDir 'Check-ForUpdates.bat') -Destination $StagingDir

Set-Content -LiteralPath (Join-Path $StagingDir 'version.txt') -Value $Version -NoNewline -Encoding ascii

# --- 7. Package it ----------------------------------------------------------

$wixCmd = Get-Command wix -ErrorAction SilentlyContinue
$msiPath = Join-Path $DistDir "$StagingName.msi"
$usedWix = $false

if ($wixCmd) {
    Write-Host ''
    Write-Host '-- Checking for the WixToolset.UI.wixext extension...'
    # Needed for Package.wxs's desktop-shortcut checkbox dialog. Pinned to
    # 5.x to match this WiX v5 toolset -- an unpinned `wix extension add`
    # resolves the latest major (v7 at time of writing) instead, which wix
    # build then refuses to load against a v5 toolset (WIX6101). Idempotent:
    # a no-op if it's already present (e.g. from windows/dist's own prior
    # run, or added manually). Failure here (offline, no matching version
    # cached) is not fatal -- the wix build below will simply fail to
    # resolve the extension and this script falls back to the PowerShell
    # installer exactly as it would for any other WiX failure.
    $existingExt = & wix extension list 2>$null
    if (-not ($existingExt -match 'WixToolset\.UI\.wixext')) {
        & wix extension add WixToolset.UI.wixext/5.0.2 2>$null | Out-Null
    }

    Write-Host ''
    Write-Host '-- Building MSI with WiX...'
    $wxsPath = Join-Path $InstallerDir 'Package.wxs'
    $wixArgs = @(
        'build', $wxsPath,
        '-arch', 'x64',
        '-ext', 'WixToolset.UI.wixext',
        '-d', "StagingDir=$StagingDir",
        '-d', "Version=$Version",
        '-d', "ProjectDir=$ProjectDir",
        '-o', $msiPath
    )
    & wix @wixArgs
    if ($LASTEXITCODE -eq 0 -and (Test-Path $msiPath)) {
        $usedWix = $true
    } else {
        Write-Host "WiX build failed (exit $LASTEXITCODE) -- falling back to the PowerShell installer." -ForegroundColor Yellow
        if (Test-Path $msiPath) { Remove-Item -Force $msiPath }
    }
} else {
    Write-Host ''
    Write-Host "WiX CLI not found on PATH (install with: dotnet tool install --global wix) -- using the PowerShell installer fallback." -ForegroundColor Yellow
}

if (-not $usedWix) {
    Copy-Item -LiteralPath (Join-Path $InstallerDir 'install.ps1') -Destination $StagingDir
    Copy-Item -LiteralPath (Join-Path $InstallerDir 'uninstall.ps1') -Destination $StagingDir

    $zipPath = Join-Path $DistDir "$StagingName-installer.zip"
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    Compress-Archive -Path (Join-Path $StagingDir '*') -DestinationPath $zipPath
}

# --- 8. Report --------------------------------------------------------------

Write-Host ''
Write-Host '== Build complete ==' -ForegroundColor Green
Write-Host "Version:        $Version"
Write-Host "Staged payload: $StagingDir"

if ($usedWix) {
    $msiSize = (Get-Item $msiPath).Length
    Write-Host "Installer:      $msiPath ($([math]::Round($msiSize/1MB, 1)) MB, .msi via WiX)"
    Write-Host ''
    Write-Host 'Install:   msiexec /i "' -NoNewline; Write-Host "$msiPath`""
    Write-Host 'Uninstall: from Settings > Apps, or msiexec /x "' -NoNewline; Write-Host "$msiPath`""
} else {
    $zipSize = (Get-Item $zipPath).Length
    Write-Host "Installer:      $zipPath ($([math]::Round($zipSize/1MB, 1)) MB, PowerShell fallback -- WiX unavailable)"
    Write-Host ''
    Write-Host 'Install:   extract the zip, then run install.ps1 from inside it'
    Write-Host 'Uninstall: from Settings > Apps, or the Start Menu "Uninstall" shortcut'
}
