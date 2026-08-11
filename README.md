# Drobo Dashboard REBORN

Software that keeps a Drobo NAS working now that the company behind it is
gone. Drobo's parent, StorCentric, was liquidated in 2023; the last Drobo
Dashboard release was December 2019 and will never be updated. The hardware
still works. This project rebuilds the software around it.

**This project is unaffiliated with Drobo, StorCentric, or their successors.**
"Drobo" is used only to identify the hardware this software talks to.

## What's here

| | |
|---|---|
| `sdk/` | The Drobo network protocol, reverse-engineered: discovery, status, health, shares |
| `agent/` | A background service exposing the Drobo as a local JSON API, with a web dashboard |
| `windows/` | A WPF desktop app: status, alerts, shares, settings, tools, MSI installer build |
| `tools/` | Packet-capture and firmware-analysis tooling used to map the protocol |

## Running it

Windows 10/11 with Python 3 installed. Double-click `START DROBO DASHBOARD.bat`
— it starts the agent, finds the Drobo on your network, and opens the app.
`START WITH SIMULATED DROBO (testing).bat` runs everything against a built-in
simulator instead, no hardware needed.

The installer build is unsigned, so Windows SmartScreen will warn on first
run, and Smart App Control machines will refuse it. Known limitation.

## Safety posture

This software is read-only toward your data. It never formats, repairs,
flashes firmware, or touches the disk pack; the command layer refuses those
operations at every level. The few write commands it supports (identify
lights, and nothing destructive) are individually allow-listed.

Code comments occasionally reference internal research notes
(`docs/protocol-map.md` and similar) that are not part of this repository.

## License

No license is granted yet — all rights reserved. If you want to use this
beyond reading it, open an issue.

Credits for reverse-engineering groundwork this project built on are in
[CREDITS.md](CREDITS.md).
