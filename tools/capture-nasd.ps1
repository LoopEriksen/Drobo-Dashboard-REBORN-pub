<#
.SYNOPSIS
    Capture the conversation between Drobo Dashboard and the Drobo, so the
    client->device command framing can be recovered.

.DESCRIPTION
    This is the last unknown in Phase 1. We already understand what the Drobo
    *says* -- it pushes a DRINASD status greeting the moment anything connects.
    What we cannot yet do is *ask it questions*: the request header and login
    exchange exist only in compiled ARM code, so the only way to learn them is
    to watch the original Dashboard do it.

    IMPORTANT: use Dashboard NORMALLY against the Drobo's real address. Do not
    try to proxy it. Dashboard's discovery runs over UDP from a Windows service
    (DDService.exe, not the GUI), so a TCP-only redirect silently fails before
    any connection is made. Capturing the real traffic avoids all of that.

    Two backends, tried in order:
      tshark  - from Wireshark, if Npcap is installed. Writes .pcapng directly.
      pktmon  - built into Windows 10/11. No install needed, but REQUIRES an
                elevated (Administrator) terminal.

    Captures ALL traffic to and from the Drobo, not just TCP 5000, so the UDP
    discovery exchange is recorded too -- that is how Dashboard finds the device
    in the first place, and it is worth having.

.PARAMETER DroboIp
    The Drobo's real address. Find it with:  py tools/drobo_probe.py mdns

.PARAMETER Seconds
    How long to capture. Default 180. Start this, then drive Dashboard.

.EXAMPLE
    # In an ADMINISTRATOR PowerShell:
    pwsh tools/capture-nasd.ps1 -DroboIp 10.0.0.5 -Seconds 180

.NOTES
    PRIVACY: this protocol is unencrypted. A Drobo password typed into Dashboard
    while capturing will be in the file in plain text. Use a disposable password
    or scrub before sharing. See docs/captures/README.md.

    This only observes. It sends nothing to the Drobo.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DroboIp,
    [int]$Seconds = 180,
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$captureDir = Join-Path $repoRoot 'docs\captures'
if (-not (Test-Path $captureDir)) { New-Item -ItemType Directory -Path $captureDir | Out-Null }

if (-not $OutFile) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutFile = Join-Path $captureDir "nasd-dashboard-$stamp.pcapng"
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-Tshark {
    $cmd = Get-Command tshark -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @("$env:ProgramFiles\Wireshark\tshark.exe",
                     "${env:ProgramFiles(x86)}\Wireshark\tshark.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Test-NpcapPresent {
    Test-Path "$env:SystemRoot\System32\Npcap\wpcap.dll"
}

Write-Host ''
Write-Host '  Drobo protocol capture' -ForegroundColor Cyan
Write-Host "  target : $DroboIp (all ports -- TCP 5000 and UDP discovery)"
Write-Host "  output : $OutFile"
Write-Host "  window : $Seconds seconds"
Write-Host ''

try {
    $probe = New-Object System.Net.Sockets.TcpClient
    $probe.Connect($DroboIp, 5000)
    $probe.Close()
    Write-Host "  reachable: yes -- $DroboIp`:5000 accepted a connection" -ForegroundColor Green
} catch {
    Write-Warning "Could not reach $DroboIp`:5000. Check the address before capturing."
}

$tshark = Find-Tshark
$useTshark = $tshark -and (Test-NpcapPresent)

if (-not $useTshark -and -not (Test-Elevated)) {
    Write-Host ''
    Write-Error @"
No capture backend is usable from this terminal.

  tshark : $(if ($tshark) { 'installed, but Npcap is missing' } else { 'not installed' })
  pktmon : available, but needs an ADMINISTRATOR terminal (this one is not)

Pick either:
  1. Re-run this script from an Administrator PowerShell (uses pktmon, no install), or
  2. Install Npcap from https://npcap.com/ and re-run (uses tshark).
"@
    exit 1
}

Write-Host ''
Write-Host '  >>> Use Drobo Dashboard NORMALLY now, against the real Drobo. <<<' -ForegroundColor Yellow
Write-Host '      Click: the status page, the drive/bay detail, and the tools pages.'
Write-Host '      Those are the screens that fetch battery, fan, PSU and performance --'
Write-Host '      the fields we cannot read yet.'
Write-Host '      Avoid anything offering to format, rename, or update firmware.'
Write-Host ''

if ($useTshark) {
    Write-Host "  backend: tshark" -ForegroundColor Green
    & $tshark -f "host $DroboIp" -a "duration:$Seconds" -w $OutFile
    if ($LASTEXITCODE -ne 0) { Write-Error "tshark exited $LASTEXITCODE"; exit $LASTEXITCODE }
}
else {
    Write-Host '  backend: pktmon (elevated)' -ForegroundColor Green
    $etl = [System.IO.Path]::ChangeExtension($OutFile, '.etl')

    # Filter by IP only, so UDP discovery is captured alongside TCP 5000.
    pktmon filter remove | Out-Null
    pktmon filter add DroboAll -i $DroboIp | Out-Null
    pktmon start --capture --pkt-size 0 --file-name $etl | Out-Null

    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $left = [int]($deadline - (Get-Date)).TotalSeconds
        Write-Host -NoNewline "`r  capturing... $left s remaining   "
        Start-Sleep -Seconds 1
    }
    Write-Host ''

    pktmon stop | Out-Null
    pktmon filter remove | Out-Null

    pktmon etl2pcap $etl --out $OutFile | Out-Null
    if (-not (Test-Path $OutFile)) {
        Write-Warning "etl2pcap produced nothing. Raw ETL is at $etl"
        Write-Warning 'Convert manually:  pktmon etl2pcap <file.etl> --out <file.pcapng>'
        exit 1
    }
}

if (-not (Test-Path $OutFile)) { Write-Error 'No capture file was produced.'; exit 1 }

$size = (Get-Item $OutFile).Length
Write-Host ''
Write-Host "  wrote $OutFile ($size bytes)" -ForegroundColor Green

if ($size -lt 5000) {
    Write-Warning 'Very small file -- Dashboard may not have talked to the Drobo.'
    Write-Warning 'Make sure Dashboard was showing the Drobo, then capture again.'
}

Write-Host ''
Write-Host '  Next:' -ForegroundColor Cyan
Write-Host "    py tools/nasd_dissect.py `"$OutFile`""
Write-Host ''
Write-Host '  Before sharing: the protocol is unencrypted -- see docs/captures/README.md' -ForegroundColor Yellow
Write-Host ''
