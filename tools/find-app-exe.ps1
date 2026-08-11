<#
.SYNOPSIS
    Print the path of the NEWEST Drobo Dashboard REBORN executable, or
    nothing if none has been built.

.DESCRIPTION
    Both launchers used to keep their own ordered list of places the app might
    be and take the FIRST that existed. That silently ran stale builds. On
    2026-08-07 it ran an executable eleven days old: `dotnet build` had written
    to bin\Release\net8.0-windows\, the list preferred
    bin\Release\net8.0-windows\win-x64\, and the older one won because it came
    first. Half the running system was current -- the Python agent runs from
    source -- and half was eleven days old, with nothing on screen saying so.

    Newest wins here instead, by file timestamp. A rebuild is picked up
    wherever it lands, and adding a new output layout costs one line rather
    than a bug.

    The two launchers now share this rather than each keeping a list, because
    the lists had already drifted: the batch file never listed the win-x64
    path at all, so it could not see the build the PowerShell one preferred.

.PARAMETER Repo
    Repository root. Defaults to this script's parent, which is right when run
    from tools\.

.PARAMETER Quiet
    Print only the path. Without it, a human-readable note about which build
    was chosen and how old it is goes to the INFORMATION stream, where a
    caller capturing stdout will not pick it up.

.OUTPUTS
    One line: the full path. Nothing at all if the app has never been built --
    callers must treat empty as "not built" rather than an error, since the
    browser dashboard is a perfectly good fallback.
#>
[CmdletBinding()]
param(
    [string]$Repo = '',
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

# Worked out HERE rather than as a param() default, which is where it was
# first written and where it silently failed. Under Windows PowerShell 5.1,
# invoked as `powershell -File tools\find-app-exe.ps1`, $PSScriptRoot is empty
# while parameter defaults are being bound -- so Split-Path threw, the script
# wrote an error instead of a path, and the batch file's `for /f` captured
# nothing and reported "Windows app not built yet" on a machine with three
# builds on it. Exit code was still 0, so nothing looked wrong.
if (-not $Repo) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Repo = Split-Path -Parent $here
}

# Every place a build can land. Order is irrelevant now -- it is the timestamp
# that decides -- so this is just "everywhere to look".
$candidates = @(
    # An installed copy, from the MSI or install.ps1.
    (Join-Path $env:LOCALAPPDATA 'Programs\DroboDashboardReborn\DroboDashboardReborn.exe')
    # Developer builds. The win-x64 variants appear when built with
    # -r win-x64; the plain ones when built without. Both are common, which is
    # exactly how the stale-build problem arose.
    (Join-Path $Repo 'windows\DroboDashboardReborn\bin\Release\net8.0-windows\win-x64\DroboDashboardReborn.exe')
    (Join-Path $Repo 'windows\DroboDashboardReborn\bin\Release\net8.0-windows\DroboDashboardReborn.exe')
    (Join-Path $Repo 'windows\DroboDashboardReborn\bin\Debug\net8.0-windows\win-x64\DroboDashboardReborn.exe')
    (Join-Path $Repo 'windows\DroboDashboardReborn\bin\Debug\net8.0-windows\DroboDashboardReborn.exe')
)

# Packaged builds live in versioned folders that build-installer.ps1 creates,
# so they are discovered rather than listed -- a new version number must not
# require editing this file.
$dist = Join-Path $Repo 'windows\dist'
if (Test-Path -LiteralPath $dist) {
    $candidates += Get-ChildItem $dist -Directory -Filter 'DroboDashboardReborn-*-win-x64' -ErrorAction SilentlyContinue |
                   ForEach-Object { Join-Path $_.FullName 'DroboDashboardReborn.exe' }
}

$builds = foreach ($c in $candidates) {
    # -LiteralPath: the repository path contains spaces, and one of these is
    # under "Drobo Dashboard REBORN".
    if (Test-Path -LiteralPath $c -PathType Leaf) { Get-Item -LiteralPath $c }
}

if (-not $builds) { return }

$newest = $builds | Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $Quiet) {
    $age = (Get-Date) - $newest.LastWriteTime
    $when = if ($age.TotalHours -lt 1) { "{0:n0} minutes old" -f $age.TotalMinutes }
            elseif ($age.TotalDays -lt 1) { "{0:n0} hours old" -f $age.TotalHours }
            else { "{0:n0} days old" -f $age.TotalDays }
    # INFORMATION, not stdout: `for /f` in the batch file and $(...) in
    # PowerShell both capture stdout, and a chatty helper would poison the
    # path with its own commentary.
    Write-Information "  Using $($newest.FullName) ($when)" -InformationAction Continue
}

# The path, and nothing else, on stdout.
$newest.FullName
