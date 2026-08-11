<#
.SYNOPSIS
    Capture what the ORIGINAL Drobo Dashboard sends when you click Identify.

.DESCRIPTION
    Our own Identify is accepted by the Drobo -- it returns the same success
    code as commands that demonstrably work -- but nothing visibly happens, and
    the firmware contains no code path linking eCmdIdentify to any LED. So we
    do not know whether it works, what it is supposed to light up, or what
    parameters the real Dashboard sends with it.

    The firmware does name a parameter: IdentifyInterval, sitting right beside
    ESADevice::processCommand and a %llu format. Sending it changed nothing
    about the reply, which is exactly the kind of question a capture settles
    and guessing does not.

    This records the traffic while you click Identify in Drobo Dashboard 3.5.0,
    then runs it through tools/nasd_dissect.py to show the bytes.

.NOTES
    MUST BE RUN AS ADMINISTRATOR. pktmon needs it, and Npcap is not installed
    so tshark is not an option on this machine.

    Right-click Windows Terminal or PowerShell -> "Run as administrator", then:

        cd "C:\Users\<you>\...\Drobo Dashboard REBORN"
        powershell -ExecutionPolicy Bypass -File tools\capture-identify.ps1

    The capture is written to your TEMP directory, never into the repo.
    Captures can contain device serials and, on an unencrypted protocol, any
    password typed while recording -- see docs/captures/README.md.
#>
param(
    [string]$DroboIP = "",
    [int]$Seconds = 45
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host ""; Write-Host "  $msg" -ForegroundColor Red; exit 1 }

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
if (-not $DroboIP -or $DroboIP -eq 'auto') {
    Fail "No Drobo address. Pass one:  -DroboIP 10.0.0.5"
}

Write-Host ""
Write-Host "  Capturing traffic to and from $DroboIP" -ForegroundColor Cyan
Write-Host ""

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dir   = Join-Path $env:TEMP "drobo-identify-$stamp"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$etl    = Join-Path $dir 'identify.etl'
$pcapng = Join-Path $dir 'identify.pcapng'

# --- set up the filter ------------------------------------------------------
pktmon filter remove | Out-Null
pktmon filter add DroboIdentify -i $DroboIP | Out-Null

pktmon start --capture --pkt-size 0 --file-name $etl --file-size 64 | Out-Null
Write-Host "  Recording." -ForegroundColor Green
Write-Host ""
Write-Host "  NOW, in Drobo Dashboard 3.5.0:" -ForegroundColor Yellow
Write-Host "    1. Select your Drobo."
Write-Host "    2. Find Identify -- it is usually on the Tools or Status page,"
Write-Host "       sometimes labelled 'Identify Drobo' or shown as a lights icon."
Write-Host "    3. Click it, and WATCH THE UNIT. Note which lights change, and"
Write-Host "       whether you need the faceplate off to see them."
Write-Host ""
Write-Host "  Do nothing else in Dashboard -- the quieter the capture, the"
Write-Host "  easier it is to find the one command that matters."
Write-Host ""

for ($i = $Seconds; $i -gt 0; $i--) {
    Write-Host -NoNewline "`r  $i seconds left...   "
    Start-Sleep -Seconds 1
}
Write-Host "`r  Stopping.                 "

pktmon stop | Out-Null
pktmon filter remove | Out-Null

# --- convert ----------------------------------------------------------------
Push-Location $dir
pktmon etl2pcap $etl --out $pcapng | Out-Null
Pop-Location

if (-not (Test-Path $pcapng)) { Fail "Conversion produced no pcapng. Capture kept at $etl" }
$size = (Get-Item $pcapng).Length
Write-Host ""
Write-Host "  Captured: $pcapng  ($([math]::Round($size/1KB)) KB)" -ForegroundColor Green

# --- dissect ----------------------------------------------------------------
Write-Host ""
Write-Host "  --- what Dashboard sent on the command port (5001) ---" -ForegroundColor Cyan
Write-Host ""
& py (Join-Path $repo 'tools\nasd_dissect.py') $pcapng --port 5001
Write-Host ""
Write-Host "  Capture kept at: $dir"
Write-Host "  It is outside the repository. Do not commit it -- captures can"
Write-Host "  carry device serials and anything typed while recording."
Write-Host ""
Write-Host "  Note down what you saw on the unit, and keep anything above that"
Write-Host "  looks like a command frame."
