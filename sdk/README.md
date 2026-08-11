# drobo-nasd

**Talk to a Drobo NAS from Python.** The protocol, recovered from the official
firmware and confirmed against a live Drobo 5N.

Drobo's parent went into liquidation in 2023. `drobo.com` no longer resolves,
the apps were pulled, and there was never a published protocol. The hardware
still works. This is what it speaks.

Standard library only — nothing to install, nothing to rot.

```python
from drobo_nasd import esatm

snap = esatm.read_snapshot("10.0.0.5")
print(snap.device.name, snap.device.firmware)
for d in snap.drives:
    print(f"bay {d.bay}: {d.state} {d.model} {d.capacity_bytes/1e12:.1f} TB")
```

## What works

| | |
|---|---|
| **Status** | Identity, firmware, per-bay drive health, capacity, volumes, pack health |
| **Discovery** | Find a Drobo by mDNS and confirm it by hardware serial, not IP |
| **Configuration** | Network settings, shares and their permissions, admin account |
| **Sensors** | Chassis temperature, uptime, throughput and IOPS |
| **Software** | DroboApps installed on the device |
| **Identify** | Flash the front lights |

Two channels, both solved. **TCP 5000** pushes a complete status document the
instant you connect, no login. **TCP 5001** takes commands after a 220-byte
login carrying the device serial twice.

## What it refuses to do

This talks to hardware holding somebody's only copy of their data, on a device
that will never get another firmware update and has no vendor recovery tool.

`commands.DO_NOT_SEND` refuses **54 of the 103 known commands** — six
arbitrary-execution escape hatches and forty-eight destructive ones: format,
firmware flash, reset, repair, every `eCmdSet*`. Enforced in code.

`commands.SAFE_WRITES` is the one narrow exception, and holds exactly one
entry: `eCmdIdentify`. A command may only join it if it cannot touch the disk
pack, cannot affect availability, is reversible, and costs nothing worse than
mild confusion when it goes wrong.

**Two names in the table lie**, and both are refused:
`eCmdForceSingleInitiator` reads like a query but writes, and
`eCmdGetNextUniqueLUNID` starts with `Get` but *allocates*.

Parameters are always sent **empty**. The firmware gives ids and names but
never documents `<Params>`, and a guessed parameter is an unreviewed
instruction to somebody's storage.

## What is not known

Some things are measured, some are guessed, and the docstrings say which.

- **Share access codes.** Stored as bare integers. What `1` versus `2` means is
  computed at runtime and is not in the firmware — only Samba's own libraries
  mention read/write lists. So `shareedit` refuses to invent a code, and
  `shares` reports raw numbers rather than claiming a share is "read-only".
- **Pack status codes.** `mStatus`, `DNASStatus` and friends are reported as
  observed values with no invented meaning.
- **The command port tires.** After roughly ninety connections in an hour,
  `eCmdGetSysInfo` stopped answering entirely while the array stayed healthy,
  and recovered on its own in about half an hour. Ask rarely.

## Requirements

Python 3.10 or newer. Nothing else.

## Unaffiliated

Not connected with Drobo, StorCentric, or their successors. No Drobo software
is redistributed here. Reimplementing a protocol to keep hardware you own
working is ordinary interoperability.
