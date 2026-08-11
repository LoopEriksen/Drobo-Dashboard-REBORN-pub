<#
.SYNOPSIS
    Record ONE session with the original Drobo Dashboard that unblocks
    several stuck features at once.

.DESCRIPTION
    Almost everything left on Phase 1's list is stuck for the same reason: the
    firmware gave us all 103 command names and ids, but never what goes inside
    their <Params>. This project refuses to guess -- a guessed parameter is an
    unreviewed instruction to somebody's storage -- so those commands go out
    empty and the device either ignores them or answers empty back.

    The only thing that settles it is watching the original Dashboard do the
    same job and reading the bytes. capture-identify.ps1 does that for one
    command. This does it for the whole list, because the expensive part is not
    the recording -- it is you sitting down with the old software and the Drobo
    on the same network, and that should happen once rather than six times.

    Each step below is a feature currently blocked. Work through as many as you
    can; skipping some is fine and the rest still pay off. When it finishes it
    runs tools/extract_commands.py, which prints every command that went past
    with its parameters.

    STEP LIST REVISED 2026-08-06. The 2026-08-05 session answered five of the
    seven questions the old list asked -- LED brightness, rename, the event
    log, how a share permission write is framed, and DroboApps start/stop are
    all CONFIRMED in docs/protocol-map.md now. Re-recording them would cost
    time and settle nothing, so they are gone and the still-open questions have
    taken their place. Keep doing that: when a step's answer lands in the
    protocol map, delete the step.

.NOTES
    MUST BE RUN AS ADMINISTRATOR -- pktmon needs it.

        Right-click Windows Terminal or PowerShell -> "Run as administrator"
        cd "C:\Users\<you>\...\Drobo Dashboard REBORN"
        powershell -ExecutionPolicy Bypass -File tools\capture-session.ps1

    BEFORE YOU START:
      - this PC must be on the same network as the Drobo
      - Drobo Dashboard 3.5.0 must be CLOSED, so the capture catches it
        connecting from scratch rather than joining a conversation in progress
      - have paper next to you. Step 3 asks which permission words you picked
        and in what order, and that is the one thing the packets cannot tell us

    SAFETY: every step here is something the ORIGINAL Dashboard does in normal
    use. Nothing asks you to format, repair, or flash anything. If a screen
    offers one of those, do not click it -- and if you are not sure, stop.

    The capture is written to your TEMP directory, never into the repo.
    On an unencrypted protocol it contains whatever is typed while recording,
    including passwords -- see docs/captures/README.md. Do not commit it.
#>
param(
    [string]$DroboIP = "",
    # Seven minutes, up from five. The previous session got through six of
    # seven steps and the one it dropped was the last one on the list, which is
    # what running out of time looks like. Creating a share and adding a user
    # both involve dialogs, so this list is slower than the one before it.
    # Recording longer costs nothing but disk; running out costs another
    # evening.
    [int]$Seconds = 420
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host ""; Write-Host "  $msg" -ForegroundColor Red; exit 1 }

# One indented, coloured line. Everything this script prints is indented two
# spaces, so the indent lives here rather than in every caller.
function Say($msg, $color = 'Gray') { Write-Host "  $msg" -ForegroundColor $color }

<#
Find a working Python.

Not just "call py and hope". On this machine `py` resolves through a
WindowsApps app-execution alias that lives on the USER path only, and this
script runs ELEVATED -- a context where those aliases are not always
resolvable. If that failed we would record the capture perfectly and then die
at the decode step, which reads like the whole session was wasted when in fact
the valuable part is safely on disk.

So: try the launcher, then real interpreters at their known locations, and
verify each candidate actually executes rather than merely existing on disk (a
Store stub exists and does nothing useful). Returns "" if nothing works, and
the caller prints the capture path so the run is recoverable by hand.
#>
function Find-Python {
    # Memoised: this is asked three times (discovery, the unreachable
    # diagnosis, the decode) and each miss spawns a process per candidate.
    if ($null -ne $script:_python) { return $script:_python }

    $candidates = @('py', 'python')
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Python" -Directory -ErrorAction SilentlyContinue |
                   ForEach-Object { Join-Path $_.FullName 'python.exe' }
    $candidates += Get-ChildItem 'C:\Program Files\Python*' -Directory -ErrorAction SilentlyContinue |
                   ForEach-Object { Join-Path $_.FullName 'python.exe' }

    foreach ($c in $candidates) {
        try {
            $out = & $c -c "print('ok')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -eq 'ok') { $script:_python = $c; return $c }
        } catch { }
    }
    $script:_python = ""
    return ""
}
function Step($n, $title, $why, $lines) {
    Write-Host ""
    Write-Host "  [$n] $title" -ForegroundColor Yellow
    Write-Host "      unblocks: $why" -ForegroundColor DarkGray
    foreach ($l in $lines) { Write-Host "      $l" }
}

# --- must be elevated -------------------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Fail @"
This needs an ADMINISTRATOR terminal -- pktmon will not capture without it.

  Right-click PowerShell or Windows Terminal, choose "Run as administrator",
  then run this script again from the repository folder.
"@
}

# --- where is the Drobo -----------------------------------------------------
$repo = Split-Path -Parent $PSScriptRoot
if (-not $DroboIP) {
    $cfg = Join-Path $repo 'agent\config.json'
    if (Test-Path $cfg) {
        try { $DroboIP = (Get-Content $cfg -Raw | ConvertFrom-Json).drobo.host } catch { }
    }
}
# --- does the configured address still answer? ------------------------------
# DHCP moves this device. The address in config.json was correct when it was
# written and silently goes stale the next time the lease changes -- which has
# already cost one capture attempt. So the configured address is a HINT: if it
# does not answer, fall back to the same mDNS browse the agent uses, rather
# than failing with "not reachable" at somebody who is standing right next to a
# working Drobo.
function Test-DroboAt([string]$addr) {
    if (-not $addr -or $addr -eq 'auto') { return $false }
    try {
        $probe = New-Object System.Net.Sockets.TcpClient
        $iar = $probe.BeginConnect($addr, 5000, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(2500, $false) -and $probe.Connected
        $probe.Close()
        return $ok
    } catch { return $false }
}

if (-not (Test-DroboAt $DroboIP)) {
    if ($DroboIP -and $DroboIP -ne 'auto') {
        Write-Host ""
        Say "$DroboIP did not answer -- looking for the Drobo on the network..." 'Yellow'
    } else {
        Write-Host ""
        Say "Looking for the Drobo on the network..." 'Cyan'
    }

    # Find-Python, not a bare `py`. Its own comment explains why -- under
    # elevation the WindowsApps alias may not resolve -- and this call site was
    # the one place that ignored it, so the search died with
    # CommandNotFoundException on exactly the machines it was written for.
    $found = ""
    $pyFind = Find-Python
    if (-not $pyFind) {
        Say "No working Python here, so the network search is unavailable." 'Yellow'
        Say "Give the address directly instead:  -DroboIP 10.0.0.5" 'Yellow'
    } else {
        Push-Location (Join-Path $repo 'sdk')
        try {
            # discover_mdns returns every candidate; verify_candidate proves
            # which is a real Drobo. Link-local (169.254.x) answers are skipped
            # -- a device advertising one alongside a routable address should
            # always be reached on the routable one.
            $found = & $pyFind -c @"
from drobo_nasd import discovery
for c in discovery.discover_mdns(timeout=4.0):
    if c.host.startswith('169.254.'):
        continue
    try:
        discovery.verify_candidate(c, timeout=6)
        print(c.host)
        break
    except Exception:
        pass
"@ 2>$null
        } catch {
            # A search that fails is not a reason to abandon the session -- the
            # caller can still pass -DroboIP by hand.
            $found = ""
        } finally { Pop-Location }
    }

    $found = ($found | Select-Object -First 1)
    if ($found) {
        $DroboIP = $found.Trim()
        Say "Found it at $DroboIP" 'Green'
    } elseif ($pyFind) {
        # A search that RAN and found nothing has to say so. Testing this found
        # a Python that could not import the SDK's dependencies: its errors
        # went to the suppressed stderr, the search "succeeded" with no result,
        # and the run carried on against the stale address as though the search
        # had never been attempted.
        Say "The network search did not find a Drobo." 'Yellow'
    }
}

if (-not $DroboIP -or $DroboIP -eq 'auto') {
    Fail "No Drobo found, and no address given. Pass one:  -DroboIP 10.0.0.5"
}

# --- is it actually reachable ----------------------------------------------
# Checked BEFORE recording rather than after. Discovering at the end that five
# minutes of clicking produced an empty capture is a miserable way to find out
# you were on the wrong Wi-Fi -- which has happened three times on this project.
Write-Host ""
Write-Host "  Checking $DroboIP answers before we start recording..." -ForegroundColor Cyan
$reachable = $false
try {
    $probe = New-Object System.Net.Sockets.TcpClient
    $probe.Connect($DroboIP, 5000)
    $reachable = $probe.Connected
    $probe.Close()
} catch { }
if (-not $reachable) {
    Write-Host ""
    Write-Host "  $DroboIP did not answer on port 5000." -ForegroundColor Red
    Write-Host ""
    $pyEarly = Find-Python
    if ($pyEarly) {
        Push-Location (Join-Path $repo 'sdk')
        try { & $pyEarly -m drobo_nasd.netcheck $DroboIP } catch { } finally { Pop-Location }
    }
    Fail @"
Nothing to capture until the Drobo answers. Check:
  - this PC is on the same network as the Drobo (the app's Network banner
    will say so if it is not)
  - the Drobo is powered on and its lights are steady
"@
}
Write-Host "  It answered. Good." -ForegroundColor Green

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dir   = Join-Path $env:TEMP "drobo-session-$stamp"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$etl    = Join-Path $dir 'session.etl'
$pcapng = Join-Path $dir 'session.pcapng'

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "   Read the whole list FIRST, then press Enter to start." -ForegroundColor Cyan
Write-Host "   Recording runs for $Seconds seconds and cannot be paused." -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan

Step 1 "Open Drobo Dashboard and let it connect" `
     "nothing on its own -- but every later step needs a live session" `
     @("Just open it and wait until it shows your Drobo.",
       "Do not click anything else yet.")

# Still first, and now with the button's REAL NAME. The previous two sessions
# both missed this step because they said "click Identify" and there is no
# button called Identify. It is "Blink Lights", on the Tools page, next to
# Rename -- established 2026-08-06 by reading Drobo Dashboard.exe. Naming a
# control by its internal name and hoping the operator finds it is how a step
# goes unrecorded twice.
Step 2 "Tools -> BLINK LIGHTS. Press it. This is the important one." `
     "eCmdIdentify -- ours is accepted by the device and blinks nothing" `
     @("It is NOT called Identify. On the Tools page, next to Rename, there is",
       "a button labelled 'Blink Lights' with a small sun/rays icon.",
       "Press it once. Then press it again to turn it off.",
       "This is the whole reason for the session. Our own Identify has now",
       "been tested on the hardware and does nothing -- the device accepts it",
       "and no light moves. So the question is no longer 'what parameter',",
       "it is 'what does Dashboard actually SEND', and nobody has ever seen",
       "that. One press answers it.",
       "If the lights do not move for the ORIGINAL Dashboard either, say so --",
       "that is equally useful, and it means we stop chasing this.")

Step 3 "Cycle ONE share through every permission level" `
     "what the access codes 0 / 1 / 2 actually MEAN" `
     @("Open Shares -> one share -> its user permissions.",
       "Set it to EVERY named level in turn, applying after each one:",
       "  e.g. Read only  -> apply,  Read-Write -> apply,  No access -> apply",
       "WRITE DOWN THE ORDER YOU PICKED THEM IN. That is what turns the",
       "numbers in the capture into words.",
       "FINISH ON THE LEVEL YOU ACTUALLY WANT -- whatever you set last is",
       "what it stays on.",
       "If one of your shares is the one whose access we could not confirm,",
       "use THAT share: cycling it both settles the code and fixes the share.")

Step 4 "Turn Time Machine on for a share, then off again" `
     "TimeMachineEnabled -- we can read the flag, never seen it written" `
     @("Same share settings screen. Toggle it, apply, toggle it back.",
       "If it asks for a size limit, set any value -- we want to see whether",
       "the size rides along in the same write.")

Step 5 "Create a share, then delete it" `
     "creating and deleting shares -- never observed at all" `
     @("Make a new empty share called ZZTESTZZ, then delete it.",
       "Safe: a share you just made and never wrote to holds no files, so",
       "deleting it cannot lose anything.",
       "Skip if the delete confirmation mentions anything beyond that share.")

Step 6 "Add a user, then remove it" `
     "eCmdGetUserGroupList -- your UserList is EMPTY, so we have never seen one" `
     @("Users / Accounts -> add one called ZZTESTZZ, then remove it.",
       "This is the only way to see the shape of a user record.",
       "You will be asked for a password. Pick a THROWAWAY one and change",
       "nothing else -- nasd is unencrypted, so whatever you type here lands",
       "in the capture file in clear text.")

Step 7 "Spin the drives down on demand, if the option exists" `
     "whether a 'spin down now' action exists separately from the delay setting" `
     @("Look in Tools / Settings for something like 'Spin down drives now'.",
       "If there is no such button, that is the answer -- say so and move on.",
       "Harmless: the drives spin straight back up on the next access.")

Write-Host ""
Write-Host "  Do NOT: format, repair, update firmware, or change the disk pack." -ForegroundColor Red
Write-Host ""
Read-Host "  Press Enter when you have read all of that and are ready"

# --- record -----------------------------------------------------------------
pktmon filter remove | Out-Null
pktmon filter add DroboSession -i $DroboIP | Out-Null
pktmon start --capture --pkt-size 0 --file-name $etl --file-size 512 | Out-Null

Write-Host ""
Write-Host "  RECORDING -- go and work through the steps above." -ForegroundColor Green
Write-Host ""

for ($i = $Seconds; $i -gt 0; $i--) {
    $m = [math]::Floor($i / 60); $s = $i % 60
    Write-Host -NoNewline ("`r  {0}:{1:D2} remaining...   " -f $m, $s)
    Start-Sleep -Seconds 1
}
Write-Host "`r  Stopping.                      "

pktmon stop | Out-Null
pktmon filter remove | Out-Null

Push-Location $dir
pktmon etl2pcap $etl --out $pcapng | Out-Null
Pop-Location

if (-not (Test-Path $pcapng)) { Fail "Conversion produced no pcapng. Capture kept at $etl" }
$size = (Get-Item $pcapng).Length
Write-Host ""
Write-Host "  Captured: $pcapng  ($([math]::Round($size/1KB)) KB)" -ForegroundColor Green

# --- read it ----------------------------------------------------------------
Write-Host ""
# Built with Join-Path, and NOT spelled out inside the message string below.
# It was, once, and the backslash-t of "\tools\" survived a round trip through
# an editor as a literal TAB -- so the one line printed to somebody with no
# Python, whose entire purpose is to be copied and pasted, told them to run a
# path that does not exist.
$extractor = Join-Path $repo 'tools\extract_commands.py'
$py = Find-Python
if (-not $py) {
    # The capture is safe on disk; only the convenience decode is unavailable.
    # Say exactly how to finish it by hand rather than implying a lost session.
    Write-Host "  Recorded successfully, but no working Python was found to decode it." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  THE CAPTURE IS SAFE. Decode it yourself with:" -ForegroundColor Green
    Write-Host "      py `"$extractor`" `"$pcapng`""
    Write-Host ""
    Write-Host "  Or feed the path above to tools/extract_commands.py later."
} else {
    Write-Host "  --- every command Dashboard sent, with its parameters ---" -ForegroundColor Cyan
    & $py $extractor $pcapng
}

Write-Host ""
Write-Host "  Capture kept at: $dir" -ForegroundColor Green
Write-Host "  It is outside the repository. DO NOT COMMIT IT -- captures carry"
Write-Host "  device serials and anything typed while recording, including"
Write-Host "  passwords, because nasd has no encryption."
Write-Host ""
Write-Host "  Save the output above, along with:" -ForegroundColor Yellow
Write-Host "    - which steps you managed and which you skipped"
Write-Host "    - what the lights did on Identify, if anything (step 2)"
Write-Host "    - the exact permission WORDS you picked, IN THE ORDER you"
Write-Host "      applied them (step 3). Without the order the capture gives"
Write-Host "      us three numbers and no way to tell which word is which."
Write-Host "    - whether a 'spin down now' button exists at all (step 7)"
