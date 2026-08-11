#!/usr/bin/env python3
"""
sweep_remainder.py -- test the safe commands the first sweep never tried.

WHY A SECOND SWEEP
------------------
Of the 103 commands in the firmware table:

    51  refused by commands.DO_NOT_SEND -- format, firmware flash, reset,
        repair, every eCmdSet*. These are not "untested", they are permanently
        excluded, and building them would be actively wrong.
    39  covered by tools/nasd_sweep.py
    13  safe by the guard's reckoning, but never actually sent

This handles that last 13 -- or rather, the seven of them that can be tested
without either risking the device or needing the owner standing in front of it.

WHAT IS TESTED HERE, AND WHY EACH IS SAFE
-----------------------------------------
  eCmdDeviceLogin / eCmdDeviceLogout
      We already log in on every command, but via the LOGIN FRAME TYPE, which
      is a different mechanism to these table commands. Whether they do
      anything at all is genuinely unknown, and finding out costs nothing --
      worst case is an extra session that the socket close ends anyway.

  eCmdGetSavedFileContent, eCmdDownloadConfigFile,
  eCmdDownloadAllConfigFiles, eCmdGetAppDataFile
      File reads. They need a filename we do not have, so they will almost
      certainly refuse -- but HOW they refuse is the information wanted. An
      error naming a missing parameter would tell us the parameter format,
      which is the thing blocking a configuration backup. Reads only; none of
      them writes.

  eCmdGetNASUsernamePassword
      Included deliberately, and this one needs justifying. The name says it
      hands back a credential, which is exactly why it matters: if this answers
      on a plain login, then anyone on the LAN can read the share passwords off
      this device, and the owner should know that. Every response goes through
      the same redaction as everything else BEFORE it is printed or stored, and
      no raw bytes touch disk.

DELIBERATELY NOT TESTED, WITH REASONS
-------------------------------------
  eCmdIdentify        A write. Built and ready, but it flashes lights -- there
                      is no point sending it unless someone is looking at the
                      unit. The owner's call, not something to slip into a sweep.
  eCmdGetUpdate       Would make the Drobo dial a vendor server that no longer
                      exists. A hang on a device that already tires under load
                      is a bad trade for knowing it times out.
  eCmdModeSense       Raw SCSI passthrough to a storage controller. No.
  eCmdDoPoll          Makes the device go and do work on command, with unknown
                      scope. The status greeting already refreshes on its own.
  eCmdCacheBattery    The name is not unambiguously a read -- it could as
                      easily start a battery self-test as report one. A 5N has
                      no cache battery anyway, so there is nothing to learn.
  eCmdAuthenticateUser
                      Needs a real username and password, and would send them
                      over an unencrypted protocol. Not ours to try.

Same discipline as the first sweep: names resolved to ids at runtime, never
hardcoded; an empty <Params> because we never guess parameters; a fresh
connection per command with a pause between; and a hard stop after three
consecutive timeouts, because the command port is known to tire.

    py tools/sweep_remainder.py 10.0.0.5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "sdk")))
sys.path.insert(0, _HERE)

from drobo_nasd import commands as C  # noqa: E402
from drobo_nasd import esatm  # noqa: E402
import nasd_sweep as sweep  # noqa: E402  (reuse its send/redact/report machinery)

#: The seven. See the module docstring for why each one is safe, and why the
#: other six of the thirteen are absent.
REMAINDER: list[str] = [
    "eCmdDeviceLogin",
    "eCmdDeviceLogout",
    "eCmdGetSavedFileContent",
    "eCmdDownloadConfigFile",
    "eCmdDownloadAllConfigFiles",
    "eCmdGetAppDataFile",
    "eCmdGetNASUsernamePassword",
]

#: Excluded from this sweep on purpose, with the reason, so the list of what
#: remains untested is explicit rather than something you work out by
#: subtracting two other lists.
NOT_TESTED: dict[str, str] = {
    "eCmdIdentify": "a write; pointless unless someone is watching the unit",
    "eCmdGetUpdate": "would dial a dead vendor server and likely hang",
    "eCmdModeSense": "raw SCSI passthrough to a storage controller",
    "eCmdDoPoll": "makes the device do work of unknown scope on command",
    "eCmdCacheBattery": "name is not unambiguously a read; may be a self-test",
    "eCmdAuthenticateUser": "needs real credentials over an unencrypted protocol",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="?", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    host = args.host
    if not host:
        try:
            cfg = json.load(open(os.path.join(_HERE, "..", "agent", "config.json"),
                                 encoding="utf-8"))
            host = cfg["drobo"].get("host", "")
        except (OSError, KeyError, ValueError):
            pass
    if not host or host == "auto":
        print("No Drobo address. Pass one, e.g.  py tools/sweep_remainder.py 10.0.0.5")
        return 1

    # Same guard the first sweep uses: nothing refused may ever be in the list.
    sweep.assert_allowlist_safe(REMAINDER)
    print(f"Safety check passed: {len(REMAINDER)} commands, none of them refused.\n")

    try:
        ident = esatm.parse_identity(esatm.fetch_greeting(host, 5000, timeout=8))
    except OSError as exc:
        print(f"Could not reach the Drobo at {host}: {exc}")
        return 1
    esa = ident.get("esa_id") or ""
    if not esa:
        print("The Drobo did not report a serial; cannot log in.")
        return 1
    print(f"Talking to {ident.get('name')} ({ident.get('model')}), "
          f"firmware {ident.get('firmware')}\n")

    results = []
    consecutive_timeouts = 0
    for i, name in enumerate(REMAINDER, 1):
        cmd_id = C.COMMANDS[name]
        print(f"[{i}/{len(REMAINDER)}] {name} (id {cmd_id}) ...", flush=True)
        rec = sweep.send_one_command(host, sweep.DEFAULT_COMMAND_PORT, esa, cmd_id, 6.0)
        rec["name"], rec["cmd_id"] = name, cmd_id
        results.append(rec)

        status = rec["status"]
        extra = ""
        if status == "answered":
            extra = f", {len(rec.get('tags', []))} fields"
            if rec.get("escaped_document"):
                extra += "  [escaped inner document]"
        if rec.get("detail"):
            extra += f"  ({str(rec['detail'])[:60]})"
        print(f"    -> {status}{extra}", flush=True)

        if status == "timeout":
            consecutive_timeouts += 1
            if consecutive_timeouts >= 3:
                print("\n!! three consecutive timeouts -- stopping. The device may "
                      "be tiring; not sending the rest.")
                break
        else:
            consecutive_timeouts = 0
        time.sleep(0.5)

    print("\nVerifying the device is still healthy ...")
    try:
        esatm.parse_identity(esatm.fetch_greeting(host, 5000, timeout=8))
        print("  status port OK -- greeting still arriving.")
    except OSError as exc:
        print(f"  !! status port did not answer: {exc}")

    print("\n== summary ==")
    for st in ("answered", "empty", "refused", "timeout"):
        n = sum(1 for r in results if r["status"] == st)
        if n:
            print(f"  {st:9} {n}")

    print("\n== still untested, deliberately ==")
    for name, why in NOT_TESTED.items():
        print(f"  {name:24} {why}")

    out = args.out or os.path.join(tempfile.gettempdir(), "drobo-sweep-remainder.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"host": host, "results": results}, fh, indent=1)
    print(f"\nRedacted report: {out}")
    print("(outside the repo -- not meant to be committed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
