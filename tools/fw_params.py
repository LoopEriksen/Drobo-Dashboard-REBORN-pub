#!/usr/bin/env python3
"""
fw_params.py -- recover candidate command PARAMETERS from the nasd binary.

THE PROBLEM
-----------
The firmware gave us all 103 command names and their numeric ids. What it never
gave us -- and what has blocked half of Phase 1 -- is what goes INSIDE each
command's <Params> block. This project refuses to guess one, because a guessed
parameter is an unreviewed instruction to somebody's storage. So those commands
go out empty, and the device ignores them or answers empty back.

The assumed route to fixing that was a packet capture of the original Dashboard.
It turns out there is a second route, and it costs nothing: **nasd is compiled
with its XML tag names as ordinary strings, and they sit next to the handler
code that uses them.** `IdentifyInterval` was found this way by hand. This tool
does it systematically.

    py tools/fw_params.py vendor/extracted/rootfs/sbin/nasd
    py tools/fw_params.py <binary> --command eCmdSetDimming
    py tools/fw_params.py <binary> --validate

WHAT THIS PROVES, AND WHAT IT DOES NOT
---------------------------------------
It proves a string exists in the binary near code that mentions a command. That
is real evidence and it is NOT a guess -- but it is weaker than a capture:

  - It does not prove the XML NESTING. Knowing `Dimming` exists does not tell
    you whether it is <Params><Dimming>59</Dimming></Params> or one level
    deeper inside some container.
  - It does not prove WHICH command uses it. Proximity in .rodata usually
    reflects proximity in the source, but "usually" is not "always".
  - It cannot distinguish a parameter the command READS from a field it WRITES
    into its reply. Both are just tag names to the compiler.

So output here is FIRMWARE-level evidence in protocol-map.md's sense: better
than a guess, short of CONFIRMED. Nothing found here should be SENT to a device
until it is confirmed, and `commands.DO_NOT_SEND` still refuses every write
regardless of what this prints.

WHY YOU CAN TRUST THE METHOD AT ALL
------------------------------------
Because it reproduces answers we already got independently, off the wire.
`--validate` checks exactly that: eCmdGetPerformance's reply fields
(Iops/ReadThroughout/WriteThroughout/TierIOps) and the network config's
(NasName/NasWorkgroup/IPConfig) were CONFIRMED by measurement long before this
tool existed, and this tool must rediscover them from the binary alone. If it
cannot reproduce what we know, nothing else it says is worth reading -- the same
discipline that caught the pcapng dissector returning "0 frames" while its own
fixtures agreed with it.

Read-only. Nothing is executed; the binary is ARM and this only reads bytes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk")))

try:
    from drobo_nasd.commands import COMMANDS
except ImportError:  # pragma: no cover
    COMMANDS = {}

#: How far either side of a handler marker to look. Chosen empirically: the
#: Performance field cluster sits within ~200 bytes of its handler string, and
#: widening much past this starts pulling in the next function's strings.
WINDOW = 400

_STRING_RE = re.compile(rb"[\x20-\x7e]{3,}")

#: An XML tag name as this firmware writes them: CamelCase, no spaces, no
#: punctuation. Deliberately strict -- the cost of a loose filter is a list of
#: plausible-looking noise that someone then tries to send to their NAS.
_TAG_RE = re.compile(r"^[A-Z][A-Za-z0-9_]{2,31}$")

#: Strings that pass the shape test but are obviously not XML tags. C++ symbol
#: fragments, log verbs, and type names that appear all over the binary.
_NOISE = {
    "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "TRACE", "FATAL",
    "TMCmd", "ESADevice", "DNASConfig", "Unable", "Failed", "Error",
    "Got", "Request", "Output", "Input", "Invalid", "Cannot", "Could",
    "NULL", "None", "True", "False", "Yes", "No", "Version",
    "Command", "Params", "Result", "ResultDetails", "ESAID", "DRINETTM",
    "String", "Integer", "Boolean", "Buffer", "Length", "Size", "Count",
    "Success", "Failure", "Status", "Type", "Name", "Value", "Data",
}


def extract_strings(blob: bytes) -> list[tuple[int, str]]:
    """Every printable run in the binary, with its file offset."""
    return [(m.start(), m.group().decode("ascii", "replace"))
            for m in _STRING_RE.finditer(blob)]


def find_markers(strings: list[tuple[int, str]], command: str) -> list[tuple[int, str]]:
    """
    Offsets of strings that anchor `command`'s handler.

    TWO kinds of anchor, and the difference matters -- getting it wrong is the
    first mistake this tool made:

      1. The bare command name, e.g. "eCmdSetDimming". These are nearly useless.
         They live in one contiguous NAME TABLE (that is how this project
         recovered all 103 commands in the first place), so a name's neighbours
         are just other names -- no parameters, ever.

      2. The HANDLER FUNCTION's own debug string, e.g.
         "ESABlockDevice::SetDimming()" or "ESADevice::GetPerformance". These
         sit in .rodata beside the tag names that function actually uses, which
         is what makes the Performance field cluster recoverable.

    So the command name is stripped of its "eCmd" prefix and matched as a
    substring too, which is what finds the second kind. Both are returned;
    candidates_near does the filtering.
    """
    markers = [(off, s) for off, s in strings if command in s]

    bare = command[4:] if command.startswith("eCmd") else command
    if len(bare) >= 4:
        for off, s in strings:
            # "::" restricts this to C++ symbol/debug strings, so a command
            # like eCmdGetConfig doesn't match every log line mentioning
            # "GetConfig" in passing.
            if bare in s and "::" in s and (off, s) not in markers:
                markers.append((off, s))
    return markers


def candidates_near(strings: list[tuple[int, str]], offset: int,
                    window: int = WINDOW) -> list[str]:
    """Tag-shaped strings within `window` bytes of `offset`."""
    out: list[str] = []
    for off, s in strings:
        if abs(off - offset) > window:
            continue
        if _TAG_RE.match(s) and s not in _NOISE and not s.startswith("eCmd"):
            out.append(s)
    return out


def analyse(blob: bytes, command: str, window: int = WINDOW) -> dict:
    """Candidate tag names associated with one command."""
    strings = extract_strings(blob)
    markers = find_markers(strings, command)
    seen: dict[str, int] = {}
    for off, _ in markers:
        for tag in candidates_near(strings, off, window):
            seen[tag] = seen.get(tag, 0) + 1
    return {
        "command": command,
        "id": COMMANDS.get(command),
        "markers": len(markers),
        # Most-repeated first: a tag near several of a command's markers is a
        # better bet than one seen beside a single log line.
        "candidates": sorted(seen, key=lambda t: (-seen[t], t)),
    }


# ---------------------------------------------------------------------------
# Self-validation. If this cannot rediscover what we already measured on the
# wire, nothing else it prints is worth reading.
# ---------------------------------------------------------------------------

#: (anchor string, tags we CONFIRMED independently by measurement)
_KNOWN_GOOD = [
    # From the live 5N's eCmdGetPerformance reply -- see docs/command-surface.md.
    ("ESADevice::GetPerformance",
     {"Iops", "ReadThroughout", "WriteThroughout", "TierIOps"}),
    # From the network section of eCmdGetConfig, parsed by config_cmd.py.
    ("DNASGetNetworkConfig",
     {"NasName", "NasWorkgroup", "IPConfig"}),
]


def validate(blob: bytes, window: int = WINDOW) -> list[tuple[str, set, set]]:
    """Returns [(anchor, expected, found)] for each known-good case."""
    strings = extract_strings(blob)
    results = []
    for anchor, expected in _KNOWN_GOOD:
        found: set[str] = set()
        for off, s in strings:
            if anchor in s:
                found.update(candidates_near(strings, off, window))
        results.append((anchor, expected, found))
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Recover candidate command parameters from the nasd binary.")
    ap.add_argument("binary", help="path to sbin/nasd from the extracted firmware")
    ap.add_argument("--command", default="", help="analyse one command only")
    ap.add_argument("--window", type=int, default=WINDOW,
                    help=f"bytes either side of a marker to search (default {WINDOW})")
    ap.add_argument("--validate", action="store_true",
                    help="check the method reproduces what we already measured")
    ap.add_argument("--min", type=int, default=1,
                    help="only show commands with at least this many candidates")
    args = ap.parse_args(argv)

    if not os.path.exists(args.binary):
        print(f"No such file: {args.binary}")
        return 2
    blob = open(args.binary, "rb").read()

    if args.validate:
        print("\nDoes this method rediscover what we already measured on the wire?\n")
        ok = True
        for anchor, expected, found in validate(blob, args.window):
            missing = expected - found
            mark = "PASS" if not missing else "FAIL"
            if missing:
                ok = False
            print(f"  {mark}  {anchor}")
            print(f"        expected: {', '.join(sorted(expected))}")
            if missing:
                print(f"        MISSING:  {', '.join(sorted(missing))}")
        print("\n" + ("The method reproduces known-good results.\n"
                      if ok else
                      "The method FAILED to reproduce known results -- do not trust its "
                      "other output.\n"))
        return 0 if ok else 1

    wanted = [args.command] if args.command else sorted(COMMANDS)
    if args.command and args.command not in COMMANDS:
        print(f"{args.command!r} is not in the firmware command table.")
        return 2

    print(f"\n{os.path.basename(args.binary)} -- candidate parameters per command")
    print("FIRMWARE-level evidence: these strings exist near each command's handler.")
    print("That is stronger than a guess and weaker than a capture -- it does not")
    print("prove the XML nesting, nor that a tag is a parameter rather than a")
    print("reply field. Confirm before sending anything.\n")

    shown = 0
    for command in wanted:
        result = analyse(blob, command, args.window)
        if len(result["candidates"]) < args.min:
            continue
        shown += 1
        print(f"  {command}  (id {result['id']}, {result['markers']} marker(s))")
        for tag in result["candidates"][:12]:
            print(f"      {tag}")
        print()

    print(f"{shown} command(s) with candidates. Run with --validate to check the "
          f"method against results we already confirmed by measurement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
