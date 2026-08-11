#!/usr/bin/env python3
"""
nasd_sweep.py - measure exactly which nasd commands answer after a plain login.

This project used to believe (docs/protocol-map.md's now-corrected "Two
privilege tiers" section) that a Drobo 5N's command channel (TCP 5001) had
two gates: a plain login (just the device serial, sent twice -- see
config_cmd.py) and, behind that, a second, unreversed authentication step
(characterized but never confirmed as required -- see
docs/crypto-handshake.md). That was wrong for at least one important case:
eCmdGetSysInfo (temperature/uptime) needs no second step at all -- what
looked like a gate was a parsing bug in this project's own code, reading for
a literal <Temperature> element when the reply nests an entity-escaped XML
document instead (see sysinfo.py). eCmdGetConfig is proven to work with
login alone, and this sweep's own results (docs/command-surface.md) found
several more commands answering under a plain login too. This tool measures,
empirically, how many of the other 102 commands answer that way -- so the
project knows exactly how much of the original Dashboard can be rebuilt
*today*, without needing to reverse anything further.

It reuses config_cmd.py's login/framing verbatim (_frame, _login_payload,
_read_frame, the TYPE_* constants) rather than reinventing them -- that module
is the one piece of this codebase already confirmed against real hardware.

    py tools/nasd_sweep.py 10.0.0.5

SAFETY -- read this before touching ALLOWLIST
-----------------------------------------------------------------------------
This sends bytes to the owner's only Drobo, which holds their only copy of
their data. Two traps in the command table make "just send everything that
sounds like a read" unsafe:

  - eCmdForceSingleInitiator (68) sounds like a query. It is a write.
  - eCmdGetNextUniqueLUNID (5) starts with "Get". It *allocates* an id as a
    side effect of asking.

So this tool uses a hardcoded ALLOWLIST of command NAMES -- never an
exclusion rule, never a "starts with Get" heuristic, never a numeric range.
Every name is resolved to its id through commands.COMMANDS[name] at runtime;
no integer command id is ever hardcoded here. assert_allowlist_safe() runs at
import time and refuses to let this module load if the allowlist and
commands.DO_NOT_SEND overlap, if a name has no confirmed id, or if the list
has a duplicate.

The allowlist itself is fixed by design review and is not meant to grow by
editing this file casually. Deliberately excluded, and why:

  - eCmdGetUpdate       -- may make the device dial a now-dead vendor server
                           and hang waiting for a response that will never come.
  - eCmdGetNextUniqueLUNID -- allocates an id; not idempotent, not a read.
  - eCmdCacheBattery    -- the name is not unambiguously a read; could be a
                           battery self-test rather than a status query.
  - eCmdModeSense       -- raw SCSI passthrough.
  - eCmdGetSavedFileContent, eCmdDownloadConfigFile,
    eCmdDownloadAllConfigFiles, eCmdGetAppDataFile -- file-content pulls with
                           unknown parameters; this tool sends <Params></Params>
                           empty and never guesses parameters, so anything that
                           needs a filename/handle to mean anything is left out.
  - eCmdGetNASUsernamePassword -- the name says it hands back a credential.
  - everything else in the 103-entry table, including the whole DO_NOT_SEND
    group -- now both the arbitrary-execution escape hatches AND the
    destructive group (format, firmware flash, reset, repair, restart, every
    eCmdSet*, and more -- see commands.py) -- and every write/format/mount/
    reboot/firmware command not already covered by DO_NOT_SEND.

Every command in the allowlist is sent with an empty <Params></Params> body --
this tool never guesses parameters for a command.

Each command gets its own fresh TCP connection and a short timeout, with a
small pause between commands so the sweep does not hammer the device. If
three commands in a row time out, the whole sweep stops and reports that --
back-to-back timeouts are treated as a sign the device may be unhappy, not
something to push through.

After the sweep, the tool reconnects to TCP 5000 and confirms the device
still volunteers its normal <ESATMUpdate> greeting, and says so explicitly.

REDACTION -- also mandatory
-----------------------------------------------------------------------------
An earlier version of this note claimed the plain login was enough to make
the device hand back a plaintext admin password from eCmdGetAdminConfig.
That was never measured, and it is wrong: measured 2026-07-26 on this
project's firmware 4.3.1, <Password> comes back as sixteen *identical*
characters (a mask) and <EncryptedPassword> as a single character -- see
config_cmd.py's module docstring for the full measurement. eCmdGetAdmin and
eCmdGetAdminConfig stay in the allowlist because the project needs to know
*whether* they answer -- and every response this tool receives is still run
through the same credential-stripping logic as config_cmd._strip_secrets()
before it is printed, logged, or written to any file, with no exception,
regardless of what this one firmware happened to send back. One firmware,
one device, one config section checked is not a promise about every
firmware, every model, or every section -- so the redaction stays. Raw
(unredacted) response bytes are never written to disk at all, and never
printed. The only file this tool writes is a JSON report under the OS
temp/scratch directory, outside the repository, and even that file only
ever holds redacted text -- it may still contain the device serial, disk
serials and the LAN IP (useful for debugging the sweep), but never a
password/secret/credential/key/token field, because those are stripped
before the record is ever created.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import xml.etree.ElementTree as ET

# tools/, agent/ and sdk/ are siblings. The Drobo protocol lives in the SDK
# (sdk/drobo_nasd) -- the WORKING reference already confirmed against real
# hardware -- so point at that rather than reimplementing any of it.
_SDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk")
sys.path.insert(0, os.path.abspath(_SDK_DIR))

from drobo_nasd import commands as C  # noqa: E402
from drobo_nasd import config_cmd as cc  # noqa: E402
from drobo_nasd import esatm  # noqa: E402

DEFAULT_STATUS_PORT = 5000
DEFAULT_COMMAND_PORT = cc.DEFAULT_PORT  # 5001, confirmed in config_cmd.py

# ---------------------------------------------------------------------------
# THE allowlist. See the module docstring for what is excluded and why.
# Do not add to this list without redoing the same-level safety review.
# ---------------------------------------------------------------------------
ALLOWLIST: list[str] = [
    "eCmdGetAdmin", "eCmdGetVersion", "eCmdGetDimming", "eCmdGetDiskPackId",
    "eCmdGetKernelInterfaceInfo", "eCmdGetConfig", "eCmdGetFirmwareInfo",
    "eCmdGetDroboSyncSettings", "eCmdGetDroboSyncSummaryLog",
    "eCmdGetDroboSyncDetailedLog", "eCmdGetDroboSyncLog",
    "eCmdGetReservedDriveLetters", "eCmdGetDemoModeInfo", "eCmdGetUserEmailSettings",
    "eCmdGetDevices", "eCmdIsAutoDiscoveryEnabled", "eCmdGetManualDiscoveryIPList",
    "eCmdIsSCSIConnAvailable", "eCmdGetSysInfo", "eCmdGetPerformance", "eCmdGetFanInfo",
    "eCmdGetPowerInfo", "eCmdGetControllerCard", "eCmdGetExpanderCard", "eCmdGetOSInfo",
    "eCmdGetTMDiagFilePath", "eCmdGetLunInitatorExtraInfo", "eCmdGetDroboAppsStatus",
    "eCmdGetAdminConfig", "eCmdGetSharesConfig", "eCmdGetEventLogs",
    "eCmdGetSessionIPAddress", "eCmdGetAllDevicesStatusXML", "eCmdGetRegisteredEmail",
    "eCmdGetUserGroupList", "eCmdGetUserEventLog", "eCmdGetDeviceNamesToShutdown",
    "eCmdGetDASDeviceNamesWithMountedVolumes", "eCmdGetDeviceMountedVolumesCount",
]


class SweepSafetyError(Exception):
    """The allowlist failed a safety check. Never caught -- meant to abort."""


def assert_allowlist_safe(allowlist: list[str] = ALLOWLIST) -> None:
    """
    Everything this module is allowed to send is checked here, at import
    time, before a socket is ever opened. Any failure aborts the whole
    program -- there is no code path that sends a command without this
    having passed first.
    """
    if len(set(allowlist)) != len(allowlist):
        dupes = sorted({n for n in allowlist if allowlist.count(n) > 1})
        raise SweepSafetyError(f"ALLOWLIST has duplicate entries: {dupes}")

    overlap = sorted(set(allowlist) & set(C.DO_NOT_SEND))
    if overlap:
        raise SweepSafetyError(
            f"ALLOWLIST overlaps commands.DO_NOT_SEND -- refusing to run: {overlap}"
        )

    unknown = sorted(n for n in allowlist if n not in C.COMMANDS)
    if unknown:
        raise SweepSafetyError(
            f"ALLOWLIST contains names with no confirmed numeric id: {unknown}"
        )


assert_allowlist_safe()


# ---------------------------------------------------------------------------
# One command, one fresh connection.
# ---------------------------------------------------------------------------
def _unwrap_escaped(el: ET.Element) -> ET.Element | None:
    """
    If this element's TEXT is really an entity-escaped XML document, parse it.

    This firmware sometimes answers with a document escaped into a text node
    (`&lt;Temperature&gt;42&lt;/Temperature&gt;`) rather than with real child
    elements. eCmdGetSysInfo does exactly this -- and because the first sweep
    only walked child elements, its whole reply counted as zero fields. That is
    how temperature came to be recorded as unobtainable and blamed on a crypto
    gate that has nothing to do with it.

    So the sweep now looks inside. Returns the parsed inner root, or None if
    the text is not an XML document.

    Note ElementTree has already turned `&lt;` back into `<` by the time we see
    the text, so this looks for a literal angle bracket, not for the entity.
    """
    text = (el.text or "").strip()
    if not text or not text.startswith("<"):
        return None
    body = text
    if body.startswith("<?xml"):
        end = body.find("?>")
        if end != -1:
            body = body[end + 2:].strip()
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def _collect_tags(el: ET.Element) -> set[str]:
    tags = {el.tag}
    # Descend into an escaped inner document as if it were ordinary children,
    # marking it so the report shows the reply needed unwrapping.
    inner = _unwrap_escaped(el)
    if inner is not None:
        tags.add(f"{el.tag}/[escaped]")
        tags |= {f"{el.tag}/{t}" for t in _collect_tags(inner)}
    for child in el:
        tags |= _collect_tags(child)
    return tags


def _has_escaped_document(el: ET.Element) -> bool:
    """True if any element in this tree hides an escaped XML document."""
    return any(_unwrap_escaped(node) is not None for node in el.iter())


def _redact_and_summarize(body: bytes) -> dict:
    """
    Turn a raw response body into a report-safe record: redact every
    credential-looking element BEFORE anything else happens to the data,
    then keep only what is safe to carry around -- the redacted value tree
    (may still hold serials/IPs, never secrets) and the plain set of element
    tag names (never carries a value at all).
    """
    text = body.rstrip(b"\x00").rstrip()
    if not text:
        return {"empty": True}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"parse_error": str(exc)}

    # Redact first. Nothing below this line ever sees an unredacted secret.
    # BOTH passes: one for real child elements, one for a credential hidden
    # inside an escaped inner document -- see config_cmd for why that second
    # pass has to exist.
    escaped = _has_escaped_document(root)
    cc._strip_secrets(root)
    cc._redact_escaped_document(root)

    return {
        "tags": sorted(_collect_tags(root)),
        "redacted_value": cc._to_dict(root),
        "escaped_document": escaped,
    }


def send_one_command(host: str, port: int, esa_id: str, cmd_id: int,
                      timeout: float) -> dict:
    """
    Fresh TCP connection: login, send exactly one command with empty
    <Params></Params>, read exactly one response. Never reuses a socket
    across commands -- that is deliberate, so one command's misbehavior can
    never leak state into the next.
    """
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return {"status": "refused", "phase": "connect", "detail": str(exc)}

    sock.settimeout(timeout)
    try:
        try:
            sock.sendall(cc._frame(cc.TYPE_LOGIN, cc._login_payload(esa_id)))
            msg_type, _ = cc._read_frame(sock)
        except (TimeoutError, socket.timeout):
            return {"status": "timeout", "phase": "login"}
        except (cc.ConfigError, OSError) as exc:
            return {"status": "refused", "phase": "login", "detail": str(exc)}

        if msg_type != cc.TYPE_LOGIN_OK:
            return {"status": "refused", "phase": "login",
                    "detail": f"unexpected type 0x{msg_type:02x}"}

        xml = (f"<TMCmd><CmdID>{cmd_id}</CmdID><ESAID>{esa_id}</ESAID>"
               f"<Params></Params></TMCmd>").encode() + b"\x00"
        try:
            sock.sendall(cc._frame(cc.TYPE_COMMAND, xml))
            msg_type, body = cc._read_frame(sock)
        except (TimeoutError, socket.timeout):
            return {"status": "timeout", "phase": "command"}
        except (cc.ConfigError, OSError) as exc:
            return {"status": "refused", "phase": "command", "detail": str(exc)}

        elapsed = time.monotonic() - t0
        if msg_type != cc.TYPE_RESULT:
            return {"status": "refused", "phase": "command",
                    "detail": f"unexpected type 0x{msg_type:02x}", "elapsed_s": elapsed}
        if not body or not body.rstrip(b"\x00").strip():
            return {"status": "empty", "elapsed_s": elapsed}

        summary = _redact_and_summarize(body)
        summary["status"] = "answered"
        summary["elapsed_s"] = elapsed
        return summary
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# The sweep itself.
# ---------------------------------------------------------------------------
def run_sweep(host: str, esa_id: str, *, port: int = DEFAULT_COMMAND_PORT,
              timeout: float = 6.0, pause: float = 0.4,
              names: list[str] = ALLOWLIST) -> dict:
    results = []
    consecutive_timeouts = 0
    aborted = False

    for i, name in enumerate(names):
        cmd_id = C.COMMANDS[name]  # allowlist is pre-validated; always present
        print(f"[{i + 1}/{len(names)}] {name} (id {cmd_id}) ...", flush=True)
        record = send_one_command(host, port, esa_id, cmd_id, timeout)
        record["name"] = name
        record["cmd_id"] = cmd_id
        results.append(record)

        status = record["status"]
        tag_count = len(record.get("tags", []))
        extra = f", {tag_count} fields" if status == "answered" else ""
        if record.get("escaped_document"):
            extra += "  [escaped inner document -- invisible to the first sweep]"
        print(f"    -> {status}{extra}", flush=True)

        if status == "timeout":
            consecutive_timeouts += 1
            if consecutive_timeouts >= 3:
                print("\n!! three consecutive timeouts -- stopping the sweep. "
                      "The device may be unhappy; not sending the rest.", flush=True)
                aborted = True
                break
        else:
            consecutive_timeouts = 0

        if i < len(names) - 1:
            time.sleep(pause)

    return {"results": results, "aborted": aborted, "sent": len(results),
            "planned": len(names)}


def verify_device_healthy(host: str, port: int = DEFAULT_STATUS_PORT,
                           timeout: float = 8.0) -> dict:
    """Reconnect to the status port and confirm the normal greeting still
    arrives. Read-only; this is the post-sweep health check."""
    try:
        payload = esatm.fetch_greeting(host, port=port, timeout=timeout)
    except (OSError, esatm.FrameError) as exc:
        return {"healthy": False, "detail": str(exc)}
    try:
        identity = esatm.parse_identity(payload)
    except ET.ParseError as exc:
        return {"healthy": False, "detail": f"greeting did not parse: {exc}"}
    return {"healthy": bool(identity.get("esa_id")), "detail": "ESATMUpdate greeting received"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="the Drobo's IP address")
    ap.add_argument("--command-port", type=int, default=DEFAULT_COMMAND_PORT)
    ap.add_argument("--status-port", type=int, default=DEFAULT_STATUS_PORT)
    ap.add_argument("--timeout", type=float, default=6.0, help="per-command timeout, seconds")
    ap.add_argument("--pause", type=float, default=0.4, help="pause between commands, seconds")
    ap.add_argument("-o", "--out", default=None,
                    help="where to write the redacted JSON report "
                         "(default: a scratch path OUTSIDE the repo)")
    args = ap.parse_args(argv)

    out_path = args.out
    if out_path is None:
        scratch = os.path.join(
            os.path.expanduser("~"), ".nasd_sweep_scratch")
        out_path = os.path.join(scratch, "sweep-raw.json") if os.path.isdir(scratch) else \
            os.path.join(os.path.expanduser("~"), "sweep-raw.json")

    print(f"Fetching identity from {args.host}:{args.status_port} (no login needed) ...")
    greeting = esatm.fetch_greeting(args.host, port=args.status_port, timeout=8.0)
    identity = esatm.parse_identity(greeting)
    esa_id = identity.get("esa_id", "")
    if not esa_id:
        raise SystemExit("could not read mESAID from the status greeting; aborting")
    print(f"Got esa_id (len {len(esa_id)}). Not printing it -- treat it as sensitive.")

    print(f"\nSending {len(ALLOWLIST)} allow-listed commands to "
          f"{args.host}:{args.command_port}, one fresh login each, "
          f"{args.timeout}s timeout, {args.pause}s pause between.\n")

    sweep = run_sweep(args.host, esa_id, port=args.command_port,
                       timeout=args.timeout, pause=args.pause)

    print("\nVerifying the device is still healthy on the status port ...")
    health = verify_device_healthy(args.host, port=args.status_port)
    print(f"  status-port health check: "
          f"{'OK -- greeting received' if health['healthy'] else 'PROBLEM'} "
          f"({health['detail']})")

    counts: dict[str, int] = {}
    for r in sweep["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n== summary ==")
    for status in ("answered", "empty", "refused", "timeout"):
        print(f"  {status:10s} {counts.get(status, 0)}")
    if sweep["aborted"]:
        print(f"  sweep ABORTED after {sweep['sent']} of {sweep['planned']} commands")

    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": args.host,
        "command_port": args.command_port,
        "status_port": args.status_port,
        "sweep": sweep,
        "post_sweep_health": health,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nRedacted report written to {out_path}")
    print("(outside the repo -- this file is not meant to be committed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
