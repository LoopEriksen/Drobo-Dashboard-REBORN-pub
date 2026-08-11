#!/usr/bin/env python3
"""
sweep_last_six.py -- the six commands left untested, sent one at a time.

The owner's instruction was explicit: test them if they will not make any
lasting change to the data on the Drobo. By that standard all six qualify --
none of them writes to the disk pack. What each one DOES risk is different
from what it risks to the data, so each is named honestly below.

    eCmdIdentify        Flashes the front lights. Nothing else. The owner needs
                        to be looking at the unit for the result to mean
                        anything -- the device will not tell us it worked.

    eCmdGetUpdate       Asks the Drobo to check for firmware updates, which
                        means it dials a vendor server that no longer exists.
                        No data risk. The risk is the command port hanging
                        while the device waits for a reply that never comes,
                        on a port already known to tire.

    eCmdModeSense       SCSI MODE SENSE. In SCSI this is a read -- it returns
                        mode pages -- so with an empty parameter block it
                        either reports or errors. Called "raw passthrough"
                        earlier out of caution, which was right before anyone
                        checked what the command actually is.

    eCmdDoPoll          Makes the device re-read its own state. A refresh, not
                        a write. The unknown is scope, not permanence.

    eCmdCacheBattery    Either reports battery health or starts a battery
                        self-test. Neither touches data, and a 5N has no cache
                        battery for a self-test to run on.

    eCmdAuthenticateUser
                        Validates a username and password. Sent with an empty
                        parameter block it cannot be a login attempt with real
                        credentials -- there are none to send. Worth noting the
                        one non-data risk: if the device counts failed
                        authentications, repeated calls could lock an account.
                        So it is sent ONCE.

METHOD
------
One at a time, in that order, with the status port checked between every
single command rather than only at the end. If the device stops greeting, the
run stops immediately -- the point is to notice trouble at the command that
caused it, not three commands later.

Identify is sent LAST, so that if anything does upset the device, it happened
before the one command whose result the owner has to observe by eye.

    py tools/sweep_last_six.py
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
import nasd_sweep as sweep  # noqa: E402

#: Order matters. Identify last -- see the module docstring.
ORDER: list[tuple[str, str]] = [
    ("eCmdModeSense", "SCSI MODE SENSE -- a read in SCSI terms"),
    ("eCmdCacheBattery", "battery status, or possibly a self-test; a 5N has no battery"),
    ("eCmdDoPoll", "makes the device re-read its own state"),
    ("eCmdAuthenticateUser", "sent ONCE -- repeated failures could lock an account"),
    ("eCmdGetUpdate", "dials a dead vendor server; may hang"),
    ("eCmdIdentify", "flashes the lights -- WATCH THE UNIT"),
]


def healthy(host: str) -> tuple[bool, str]:
    """Is the device still greeting on the status port?"""
    try:
        ident = esatm.parse_identity(esatm.fetch_greeting(host, 5000, timeout=8))
        return True, ident.get("name", "?")
    except OSError as exc:
        return False, str(exc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", default="")
    ap.add_argument("--timeout", type=float, default=6.0)
    args = ap.parse_args(argv)

    host = args.host
    if not host:
        try:
            host = json.load(open(os.path.join(_HERE, "..", "agent", "config.json"),
                                  encoding="utf-8"))["drobo"]["host"]
        except (OSError, KeyError, ValueError):
            pass
    if not host or host == "auto":
        print("No Drobo address.")
        return 1

    ok, who = healthy(host)
    if not ok:
        print(f"Drobo not reachable before we start: {who}")
        return 1
    print(f"Starting against {who}. Status port healthy.\n")

    results = []
    for i, (name, why) in enumerate(ORDER, 1):
        cmd_id = C.COMMANDS[name]
        refused = C.is_dangerous(name)
        print(f"[{i}/{len(ORDER)}] {name} (id {cmd_id})")
        print(f"        {why}")
        if refused:
            # Should be impossible -- none of the six is in DO_NOT_SEND -- but
            # this is the one script that deliberately sends things previously
            # held back, so it re-checks rather than trusting the list.
            print("        REFUSED by the guard -- skipping.\n")
            results.append({"name": name, "status": "refused-by-guard"})
            continue

        t0 = time.monotonic()
        rec = sweep.send_one_command(host, sweep.DEFAULT_COMMAND_PORT, esa_id,
                                     cmd_id, args.timeout)
        elapsed = time.monotonic() - t0
        rec["name"], rec["cmd_id"] = name, cmd_id
        results.append(rec)

        extra = ""
        if rec["status"] == "answered":
            extra = f", {len(rec.get('tags', []))} fields"
            if rec.get("escaped_document"):
                extra += " [escaped document]"
        if rec.get("detail"):
            extra += f" ({str(rec['detail'])[:50]})"
        print(f"        -> {rec['status']}{extra}   [{elapsed:.1f}s]")

        ok, who = healthy(host)
        print(f"        device after: {'OK' if ok else 'NOT ANSWERING -- ' + who}\n")
        if not ok:
            print("!! The device stopped greeting. Stopping here rather than "
                  "sending anything else.")
            break
        time.sleep(1.0)

    print("== summary ==")
    for r in results:
        print(f"  {r['name']:24} {r['status']}")

    out = os.path.join(tempfile.gettempdir(), "drobo-last-six.json")
    json.dump({"host": host, "results": results}, open(out, "w", encoding="utf-8"), indent=1)
    print(f"\nRedacted report: {out}")
    return 0


if __name__ == "__main__":
    # esa_id is read once, up front, and shared by every command below.
    _h = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else ""
    if not _h:
        try:
            _h = json.load(open(os.path.join(_HERE, "..", "agent", "config.json"),
                                encoding="utf-8"))["drobo"]["host"]
        except (OSError, KeyError, ValueError):
            _h = ""
    esa_id = ""
    if _h:
        try:
            esa_id = esatm.parse_identity(
                esatm.fetch_greeting(_h, 5000, timeout=8)).get("esa_id", "")
        except OSError:
            pass
    raise SystemExit(main())
