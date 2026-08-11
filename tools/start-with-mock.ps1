<#
.SYNOPSIS
    Start Drobo Dashboard REBORN against a SIMULATED Drobo, and shut the
    simulator down again when the app exits.

.DESCRIPTION
    For trying the app out, and for exercising states a healthy Drobo will
    never show you -- a failed drive, a dead cache battery, a full array. The
    simulator answers every endpoint the real driver does, so the whole app
    works against it.

    THE LIFETIME TIE IS THE POINT. The agent this starts belongs to this app
    instance and nothing else, so when the app exits -- including via Exit on
    the tray icon -- the simulated agent is stopped too. Without that you get
    an orphaned Python process holding port 7420, which then quietly serves a
    SIMULATED Drobo to the next launch that expects a real one. That is a
    genuinely confusing failure and the reason this script exists rather than
    a note in the README telling you to remember.

    Written in PowerShell rather than batch for exactly that: batch cannot get
    a child's PID reliably, so it cannot kill the right process afterwards.
    Here the agent is a real process object, waited on and stopped by identity.

.NOTES
    Double-click START WITH SIMULATED DROBO (testing).bat instead of running
    this by hand. No administrator rights are needed.
#>
param(
    [int]$Port = 7420,
    [string]$Token = 'live-demo-token'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Say($msg, $colour = 'Gray') { Write-Host "  $msg" -ForegroundColor $colour }

Write-Host ''
Write-Host '  Drobo Dashboard REBORN -- simulated Drobo' -ForegroundColor Cyan
Write-Host '  ------------------------------------------'
Write-Host ''

# --- python present? --------------------------------------------------------
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Say 'Python was not found. Install it from python.org and tick' 'Red'
    Say '"Add Python to PATH" during setup, then run this again.' 'Red'
    Read-Host "`n  Press Enter to close"
    exit 1
}

# --- is something already on the port? --------------------------------------
# Refuse rather than fight it. Two agents on one port is how you end up talking
# to the wrong one, and silently binding alongside an existing listener (which
# Windows will allow) makes which-one-answers a coin toss.
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Say "Something is already listening on port $Port." 'Yellow'
    Say 'That is probably an agent from an earlier run. Close it first,'
    Say 'or close any other Drobo Dashboard window and try again.'
    Read-Host "`n  Press Enter to close"
    exit 1
}

# --- find the app -----------------------------------------------------------
# Newest build wins, and the helper says which one it picked. This used to be
# a first-match-wins list that ran an eleven-day-old binary while the agent ran
# from source -- see tools\find-app-exe.ps1 for the whole story.
$app = & (Join-Path $PSScriptRoot 'find-app-exe.ps1') -Repo $repo

# --- start the simulated agent ---------------------------------------------
$cfg = Join-Path $repo 'agent\config.mock.json'
if (-not (Test-Path -LiteralPath $cfg)) {
    # Synthesize it. config.*.json is gitignored (local configs carry real
    # tokens), which had a consequence nobody noticed until a public-release
    # review: a fresh clone had NO mock config, so the one launcher every
    # hardware-less stranger tries died right here with "Missing ...". The
    # simulator's config is entirely non-secret -- the token below is the
    # documented demo value for a loopback-only fake device -- so writing it
    # on demand costs nothing and makes a clean checkout actually work.
    Say 'First run: creating agent\config.mock.json for the simulator...' 'DarkGray'
    @'
{
  "agent": {
    "bind": "127.0.0.1",
    "port": 7420,
    "token": "live-demo-token",
    "poll_seconds": 5
  },
  "drobo": {
    "driver": "mock",
    "simulate_config": true
  },
  "photos": {
    "enabled": false
  },
  "alerts": {
    "resend_seconds": 3600
  }
}
'@ | Set-Content -LiteralPath $cfg -Encoding ascii
}

Say 'Starting the simulated Drobo...' 'Green'
# Each path is quoted EXPLICITLY. Windows PowerShell 5.1 does not quote array
# arguments when handing them to a native executable, so this repository's own
# path -- "...\Drobo Dashboard REBORN\..." -- gets split at the first space and
# Python is asked to run a file called "...\GitHub\Drobo". Caught by testing
# this rather than by reading it.
$agentArgs = @("`"$(Join-Path $repo 'agent\run_agent.py')`"", '--config', "`"$cfg`"")
$agent = Start-Process -FilePath 'py' -ArgumentList $agentArgs `
                       -WorkingDirectory $repo -PassThru -WindowStyle Hidden

# Give it a moment to bind, then confirm it actually did. Launching the app
# against an agent that failed to start produces "cannot reach the agent",
# which sends you looking in the wrong place entirely.
$ready = $false
foreach ($i in 1..20) {
    Start-Sleep -Milliseconds 400
    if ($agent.HasExited) { break }
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/ping" -TimeoutSec 2 -UseBasicParsing | Out-Null
        $ready = $true; break
    } catch { }
}

if (-not $ready) {
    Say 'The simulated agent did not start.' 'Red'
    if (-not $agent.HasExited) { Stop-Process -Id $agent.Id -Force -ErrorAction SilentlyContinue }
    Read-Host "`n  Press Enter to close"
    exit 1
}

Say "Simulated Drobo is up on http://127.0.0.1:$Port" 'Green'
Write-Host ''
Say "Browser dashboard:  http://127.0.0.1:$Port/?token=$Token" 'Cyan'
Say "Agent token:        $Token" 'Cyan'
Write-Host ''
Say 'Break things on purpose while it runs, from another terminal:'
Say "  curl -X POST -H `"X-Agent-Token: $Token`" http://127.0.0.1:$Port/api/mock/fail/3" 'DarkGray'
Say "  curl -X POST -H `"X-Agent-Token: $Token`" http://127.0.0.1:$Port/api/mock/reset" 'DarkGray'
Write-Host ''

try {
    if ($app) {
        Say 'Starting the app...' 'Green'
        # -Wait blocks until the app process really ends. Closing to the tray
        # does NOT end it, which is correct: the simulator should stay up for
        # as long as the app is still running, tray or not. Only Exit ends it.
        Start-Process -FilePath $app -Wait
        Write-Host ''
        Say 'The app has closed.'
    }
    else {
        Say 'The Windows app is not built, so only the browser dashboard is'  'Yellow'
        Say 'available. To build it:'                                          'Yellow'
        Say '  dotnet build windows\DroboDashboardReborn -c Release'           'DarkGray'
        Write-Host ''
        Say 'Open the browser link above. Press Enter here when you are done.'
        Read-Host
    }
}
finally {
    # Runs whatever happened above, including Ctrl+C: the simulator must not
    # outlive the thing it was started for.
    if ($agent -and -not $agent.HasExited) {
        Say 'Stopping the simulated Drobo...'
        Stop-Process -Id $agent.Id -Force -ErrorAction SilentlyContinue
    }
    Say 'Done.' 'Green'
    Write-Host ''
}
