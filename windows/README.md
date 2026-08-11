# Windows app — Drobo Dashboard REBORN

Native Windows client. It shows the Drobo's health, capacity, and drive bays,
polling the local agent every 10 seconds.

**It knows nothing about the Drobo protocol** — it talks only to the agent's
JSON API (`/api/status`), exactly like the web dashboard. All Drobo logic stays
in one place (`agent/`).

## Requirements

- .NET 8 SDK (`winget install Microsoft.DotNet.SDK.8`) — only for building; an
  installed copy needs nothing but Python (see below)
- Python (only for the agent — see [agent/README.md](../agent/README.md))
- The agent running somewhere reachable (usually this same PC)

## Installing (the easy way)

If someone handed you a `DroboDashboardReborn-*-win-x64.msi`, that's the whole
install:

1. Double-click it. It installs to
   `%LOCALAPPDATA%\Programs\DroboDashboardReborn` — no admin rights needed —
   and adds Start Menu and Desktop shortcuts.
2. Launch it from either shortcut. That runs `Launch-DroboDashboardReborn.bat`,
   which starts the agent (needs Python — the shortcut will tell you if it's
   missing and where to get it) and then opens the app.
3. First run: the agent generates its access token and writes
   `agent\config.json` next to itself inside the install folder. The app
   reads that file automatically and fills in the token — you shouldn't have
   to type or paste anything.

To remove it: **Settings → Apps → Drobo Dashboard REBORN → Uninstall**, same
as any other Windows app. That is a real, registered uninstall — it removes
the install folder, the shortcuts, and the Add/Remove Programs entry.

There's also a **Check for Updates** shortcut in the Start Menu folder. See
[Updating](#updating) below — right now, until a release feed exists, it will
truthfully tell you it has nothing to check.

### Install-tested, for real

This isn't a claim taken on faith — `windows/test-installer.ps1` runs the
full lifecycle against a real machine (`msiexec /i`, verify, launch, `msiexec
/x`, verify removal) and is meant to be re-run after any change to the
installer. Latest run, from an **unelevated** shell:

```
powershell windows/test-installer.ps1
...
ALL PASS (24 checks)
```

What that run actually confirmed, observed directly (not assumed):

- **No elevation needed.** `msiexec /i ... /qn` from a non-admin, non-elevated
  shell returned exit code 0 with no UAC prompt. `Scope="perUser"` in
  `installer/Package.wxs` does what it says — a NAS dashboard does not make
  you an administrator to install it.
- **Files land where the package claims.** App exe present at
  `%LOCALAPPDATA%\Programs\DroboDashboardReborn\DroboDashboardReborn.exe`,
  full self-contained size (~150 MB, matching the staged payload byte for
  byte). The agent's Python source is complete underneath `agent\` —
  `run_agent.py`, the `drobo_agent` package, and its `nasd` subpackage all
  present.
- **No shipped secrets.** No `config.json` anywhere in the installed tree,
  and no hardcoded token literal in any installed `.py` file — grepped
  directly, not inferred from the build script's intent. The access token is
  generated fresh on first run, as designed.
- **Shortcuts are real.** Both the Start Menu entry and the Desktop shortcut
  exist and resolve to `Launch-DroboDashboardReborn.bat` inside the install
  folder (Desktop here means wherever Windows actually points the Desktop
  special folder — if Windows redirects it (cloud-sync "Known Folder Move" setups do), not necessarily
  `%USERPROFILE%\Desktop`).
- **A real Add/Remove Programs entry exists** — `DisplayName` "Drobo
  Dashboard REBORN", `DisplayVersion` "0.1.0". One detail worth knowing if
  you go looking for it yourself: WiX v4's `Scope="perUser"` packages
  register this under `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\
  Uninstall`, not `HKCU`, even though the install itself is fully per-user
  and needs no admin rights — that's Windows Installer's own per-user-managed
  install bookkeeping, not a sign the install touched machine-wide state.
- **The installed app is not dead on arrival.** Launched directly
  (`DroboDashboardReborn.exe`, not through the launcher `.bat`), it was still
  running 3 seconds later with a real window title ("Drobo Dashboard
  REBORN"), then was terminated cleanly.
- **Uninstall leaves nothing behind.** `msiexec /x ... /qn` returned exit
  code 0, and afterward: install folder gone, Start Menu folder gone, Desktop
  shortcut gone, the Add/Remove Programs entry gone, and the `HKCU\Software\
  DroboDashboardReborn` key (written by the shortcut components) gone too.

One rough edge, not a defect: `InstallLocation` in the registered ARP entry
is blank (WiX doesn't set `ARPINSTALLLOCATION` here), so some third-party
"where is this installed" tools may not find the folder from Add/Remove
Programs alone. Windows' own Settings → Apps page doesn't need it and works
fine.

## Building the installer

```powershell
powershell windows/build-installer.ps1
```

One command: publishes the app self-contained (win-x64, single-file, no .NET
runtime required on the target machine — that's the ~155 MB), stages it
together with the agent's Python source, and packages the result as an `.msi`
with [WiX](https://wixtoolset.org/) (`dotnet tool install --global wix`, plus
the UI extension) if `wix` is on PATH. It prints the exact output path when
done. Safe to re-run — each run replaces that version's output under
`windows/dist/` (gitignored, never commit anything from it).

If WiX isn't installed, the script falls back automatically to
`windows/installer/install.ps1`, a self-contained PowerShell installer zipped
up with the same payload — still a genuine install (Start Menu/Desktop
shortcuts, a real Add/Remove Programs entry under `HKCU`) and a genuine
uninstall (`windows/installer/uninstall.ps1`, or the Start Menu "Uninstall"
shortcut it creates), just not an `.msi`. The script tells you plainly which
path it took.

`windows/installer/` holds the WiX source (`Package.wxs`) and the fallback
scripts; `windows/installer/*.bat` ship inside the installed app too (they're
how the installed copy launches itself and checks for updates).

After building, prove the MSI actually works rather than trusting it on
faith:

```powershell
powershell windows/test-installer.ps1
```

Installs it for real (`msiexec /i`), checks what landed (files, shortcuts,
ARP entry, no shipped secrets), launches the installed exe and confirms it's
still alive a few seconds later, then uninstalls it (`msiexec /x`) and checks
that removal was actually clean. Prints `PASS`/`FAIL` per check and exits 1 on
any failure — see [Install-tested, for real](#install-tested-for-real) above
for the most recent results. It refuses to run if Drobo Dashboard REBORN is
already installed on the machine, since it uninstalls at the end.

## Updating

Update checks ship **off by default** and there is one implementation of them,
shared: `agent/drobo_agent/updates.py`, configured by the `updates` block in
`agent/config.json`.

Two entry points, one engine:

- **In the app and the web dashboard**, via the agent's `/api/update`.
- **The Check for Updates shortcut**, which runs
  `py -m drobo_agent.updates` — so it still works when the agent isn't running,
  which is the reason to keep it.

To turn checking on, set `"enabled": true` in that block and point
`manifest_url` at either a GitHub Releases API URL:

```json
{ "enabled": true, "manifest_url": "https://api.github.com/repos/<owner>/<repo>/releases/latest" }
```

…or your own JSON manifest carrying `version`, `download_url` and `sha256`. The
engine detects which it got from the content, because a URL can't tell you what
it will return. A GitHub release is announced but flagged as unverifiable —
GitHub publishes no checksum — while a manifest with a valid SHA-256 says so.
Neither is ever downloaded or installed automatically.

> Until 2026-07-28 there were **two** update checkers: this one, and a separate
> `check-update.ps1` with its own `update-config.json`. Two mechanisms that each
> had to be switched on independently is how an updater quietly stops working —
> you enable one and assume you're covered. The PowerShell one is gone; the
> shortcut it served now calls the shared engine.

What it does **not** do yet: check automatically, notify you unprompted, or
download/install anything itself. It's on-demand only, and a failed or
disabled check is always silent — never a popup, never a nag. It also isn't
wired into the app's own UI yet; that is the natural next step.

## Versioning

One number, `<Version>` in `DroboDashboardReborn.csproj`, drives everything:
the app's assembly metadata, the installer's file name and MSI version, and
what `version.txt` (and so the update checker) reports. Bump it there before
building a release.

## Build and run (development)

```bash
dotnet run -c Release --project windows/DroboDashboardReborn
```

Or build the `.exe` and launch it:

```bash
dotnet build -c Release --project windows/DroboDashboardReborn
```

The executable lands at
`windows/DroboDashboardReborn/bin/Release/net8.0-windows/DroboDashboardReborn.exe`.

## Using it (dev build)

1. Start the agent: `py agent/run_agent.py` (it prints its URL and token).
2. In the app, the **Agent** field defaults to `http://127.0.0.1:7420`.
3. Paste the **token** from `agent/config.json` and press **Connect**.

It then shows the live device: name, firmware, overall state, capacity, and a
row per drive bay coloured by health. It refreshes every 10 seconds.

## Why WPF and not WinUI 3 (yet)

WinUI 3 would give the exact Windows 11 look. This first build is
WPF because it produces a real native `.exe` with no packaging workload, so it
builds and launches with just the .NET SDK — which made it verifiable end to
end. The API boundary is identical, so moving the same views to WinUI 3 later is
a UI-layer change, not a rearchitecture.

## Not committed

`bin/` and `obj/` are build output and are gitignored, as is all of
`windows/dist/` (everything `build-installer.ps1` produces: the staged
payload, the `.msi`, the `.wixpdb`). The project sources (`.csproj`, `.xaml`,
`.cs`) and the installer sources (`windows/installer/**`,
`windows/build-installer.ps1`) are tracked.
