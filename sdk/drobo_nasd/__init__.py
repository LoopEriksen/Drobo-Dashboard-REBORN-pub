"""
drobo_nasd -- speak to a Drobo NAS.

Drobo's parent went into liquidation in 2023 and `drobo.com` no longer
resolves. The hardware still works; the software that talked to it does not,
and there was never a published protocol. This package is that protocol,
recovered from the official firmware and confirmed against a live Drobo 5N.

Standard library only. No dependencies to rot.

    from drobo_nasd import esatm

    snap = esatm.read_snapshot("10.0.0.5")
    print(snap.device.name, snap.capacity.used_bytes, len(snap.drives))

Two channels, both solved:

  TCP 5000 -- status. The device pushes a complete <ESATMUpdate> document the
              instant you connect, with no login. Identity, per-bay health,
              capacity and volumes all come from this. See `esatm`.

  TCP 5001 -- commands. A DRINETTM frame and a 220-byte login carrying the
              device serial twice. See `config_cmd`, `sysinfo`, `droboapps`.

SAFETY, WHICH IS NOT OPTIONAL HERE
----------------------------------
This talks to hardware holding somebody's only copy of their data, on a device
that will never receive another firmware update and has no vendor recovery
tool left.

`commands.DO_NOT_SEND` refuses 54 of the 103 known commands -- six
arbitrary-execution escape hatches and forty-eight destructive ones (format,
firmware flash, reset, repair, every eCmdSet*). The guard is enforced in code,
not by convention.

`commands.SAFE_WRITES` is the narrow, explicitly-approved exception: commands
that change something harmless. It currently holds exactly one entry,
`eCmdIdentify`, which blinks the front lights and stops on its own. A command
may only join it if it cannot touch the disk pack, cannot affect availability,
is reversible, and costs nothing worse than mild confusion when it goes wrong.

Two names in the table lie about what they do, and both are refused:
`eCmdForceSingleInitiator` reads like a query but writes, and
`eCmdGetNextUniqueLUNID` starts with "Get" but allocates.

Parameters are always sent EMPTY. The firmware gives command ids and names but
never documents their <Params>, and a guessed parameter is an unreviewed
instruction to somebody's storage.

WHAT IS KNOWN AND WHAT IS NOT
-----------------------------
Docstrings throughout distinguish what was read out of the firmware, what was
measured on the wire, and what remains a guess -- including the corrections,
where a confident earlier conclusion turned out to be wrong. That record is
deliberate: the reasoning is worth as much as the code.

Unaffiliated with Drobo, StorCentric, or their successors.
"""

__version__ = "0.1.0"

__all__ = [
    "commands",
    "config_cmd",
    "discovery",
    "droboapps",
    "drobosync",
    "esatm",
    "firmware",
    "identify",
    "models",
    "netcheck",
    "shareedit",
    "shares",
    "sysinfo",
]
