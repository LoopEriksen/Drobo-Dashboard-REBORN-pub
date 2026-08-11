#!/usr/bin/env python3
"""
nasd_probe.py - work out how to talk to nasd, without Wireshark.

We know from the firmware that a Drobo 5N runs a daemon called nasd on TCP
port 5000, that it speaks XML, and what all 106 of its commands are called
(see docs/protocol-map.md). What we do NOT know is the framing: whether a
request is length-prefixed, newline-terminated, NUL-terminated, or just raw
XML on the socket.

This tool tries each possibility against a real Drobo and records exactly what
comes back.

**Set expectations honestly**: firmware analysis has since shown that nasd
requires a signed binary header and a login exchange before it accepts any
command (see docs/protocol-map.md, "Connection model"). This probe is therefore
unlikely to get a successful command response on its own. What it *will* do is
show how the daemon behaves and how it rejects each attempt, which narrows the
header layout considerably — and it costs one command to find out.

If it comes back with nothing useful, that is the expected outcome and the
answer is a Wireshark capture of the real Dashboard doing the handshake.

    py nasd_probe.py 10.0.0.50
    py nasd_probe.py 10.0.0.50 -o docs/captures/nasd-probe.json
    py nasd_probe.py 10.0.0.50 --listen-only

SAFETY
------
This tool only ever sends commands that READ. The command list below is a
hand-picked subset of harmless ones -- version, status, system info. It will
not send anything that writes, formats, mounts, unmounts, reboots, updates
firmware, or touches the disk pack.

It specifically will NEVER send eCmdRunESACommand, eCmdRunCommand,
eCmdSendTMCommand, eCmdRouter, eCmdReadHostBuffer or eCmdWriteHostBuffer --
those look like arbitrary command execution on a device that will never get
another security patch. They are named here so the list is explicit, and they
are blocked below.

Even so: this talks to the hardware. If you would rather observe than poke,
use --listen-only, which connects and reads without sending anything.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# Commands we are willing to send. Read-only, all of them.
# ---------------------------------------------------------------------------
SAFE_COMMANDS = [
    "eCmdGetVersion",
    "eCmdGetAllDevicesStatusXML",
    "eCmdGetSysInfo",
    "eCmdGetDevices",
]

# Never sent. Present so the refusal is explicit and greppable.
FORBIDDEN = {
    "eCmdRunESACommand", "eCmdRunCommand", "eCmdSendTMCommand", "eCmdRouter",
    "eCmdReadHostBuffer", "eCmdWriteHostBuffer", "eCmdInstallFirmware",
    "eCmdRevertFirmware", "eCmdFormatDevice", "eCmdFormatLUN", "eCmdReset",
    "eCmdShutdown", "eCmdRestart", "eCmdRepair", "eCmdForceRepair",
    "eCmdSetConfig", "eCmdSetAdmin", "eCmdClearConfig", "eCmdSetFlashConfig",
}

PORT = 5000


# ---------------------------------------------------------------------------
# Candidate request bodies. The firmware shows an XML DOM and <Error>N</Error>
# responses, so these are shaped around that.
# ---------------------------------------------------------------------------
def bodies_for(command: str) -> list[tuple[str, bytes]]:
    return [
        ("bare-element", f"<{command}/>".encode()),
        ("command-element", f"<Command>{command}</Command>".encode()),
        ("command-attr", f'<Command name="{command}"/>'.encode()),
        ("esa-style", f"<ESA><Command>{command}</Command></ESA>".encode()),
        ("decl+command", f'<?xml version="1.0"?><Command>{command}</Command>'.encode()),
        ("tmcmd", f"<TMCmd><Cmd>{command}</Cmd></TMCmd>".encode()),
    ]


# ---------------------------------------------------------------------------
# Candidate framings.
#
# IMPORTANT -- read docs/protocol-map.md "Connection model" first. Firmware
# analysis has since shown that nasd expects a binary header carrying a
# SIGNATURE, then a login packet, before it will accept any command:
#
#     Command Conn login(from %s): got bad signature.
#     Command Conn login(from %s): did not receive complete login packet.
#     SledCommandListener: No session yet so refusing command connection
#
# So the plain framings below are expected to FAIL. They are kept because a
# rejection is itself information -- how nasd rejects us (silence? a reset? an
# error payload?) narrows down the header. The signature-prefixed variants are
# the ones with a real chance.
#
# The signature is "DRINASD4" (or the older "DRINASD"), found beside the
# SledDiscoveryAgent code in the firmware. Its position and the exact header
# layout are still unknown, so several placements are tried.
# ---------------------------------------------------------------------------
SIG4 = b"DRINASD4"
SIG = b"DRINASD"

FRAMINGS = [
    # plain -- expected to be refused, but the manner of refusal is a clue
    ("raw", lambda b: b),
    ("newline", lambda b: b + b"\n"),
    ("nul", lambda b: b + b"\0"),
    ("len32-be", lambda b: struct.pack(">I", len(b)) + b),
    ("len32-le", lambda b: struct.pack("<I", len(b)) + b),
    ("len16-be", lambda b: struct.pack(">H", len(b)) + b),
    # signature-prefixed -- the shapes worth trying
    ("sig4+len32be", lambda b: SIG4 + struct.pack(">I", len(b)) + b),
    ("sig4+len32le", lambda b: SIG4 + struct.pack("<I", len(b)) + b),
    ("sig4+nul+len32be", lambda b: SIG4 + b"\0" + struct.pack(">I", len(b)) + b),
    ("sig+len32be", lambda b: SIG + b"\0" + struct.pack(">I", len(b)) + b),
    ("len32be+sig4", lambda b: struct.pack(">I", len(b) + 8) + SIG4 + b),
    ("sig4+pad8+len32be", lambda b: SIG4 + b"\0" * 8 + struct.pack(">I", len(b)) + b),
]


def hexdump(data: bytes, limit: int = 512) -> list[str]:
    out = []
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        h = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        a = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:04x}  {h}  {a}")
    return out


def looks_like_answer(data: bytes) -> str | None:
    """Score a response. Returns a description if it looks meaningful."""
    if not data:
        return None
    if b"<Error>" in data:
        return "contains <Error> -- this is the nasd response format"
    if data.lstrip()[:5] == b"<?xml":
        return "XML declaration"
    if re.match(rb"\s*<[A-Za-z]", data):
        return "starts with an XML element"
    printable = sum(1 for b in data[:64] if 9 <= b <= 126)
    if printable > len(data[:64]) * 0.9:
        return f"printable text: {data[:60]!r}"
    return f"binary, {len(data)} bytes"


def read_available(sock: socket.socket, first_timeout: float = 3.0,
                   idle: float = 0.6, cap: int = 65536) -> bytes:
    """Read until the peer goes quiet."""
    sock.settimeout(first_timeout)
    chunks = b""
    try:
        while len(chunks) < cap:
            piece = sock.recv(8192)
            if not piece:
                break
            chunks += piece
            sock.settimeout(idle)
    except (socket.timeout, TimeoutError):
        pass
    except OSError:
        pass
    return chunks


def connect(host: str, timeout: float = 5.0) -> socket.socket | None:
    try:
        sock = socket.create_connection((host, PORT), timeout=timeout)
        sock.settimeout(timeout)
        return sock
    except OSError as exc:
        print(f"  ! could not connect to {host}:{PORT} -- {exc}")
        return None


def probe_banner(host: str) -> dict:
    """Connect and say nothing. Does the Drobo speak first?"""
    print("\n== 1. does the server speak first? ==")
    result = {"test": "banner", "bytes": 0}
    sock = connect(host)
    if not sock:
        result["error"] = "connect failed"
        return result
    try:
        data = read_available(sock, first_timeout=4.0)
        result["bytes"] = len(data)
        if data:
            result["hexdump"] = hexdump(data)
            result["verdict"] = looks_like_answer(data)
            print(f"  server sent {len(data)} bytes unprompted -- {result['verdict']}")
            for line in hexdump(data, 128):
                print("    " + line)
        else:
            print("  silence. The client speaks first.")
            result["verdict"] = "silent -- client speaks first"
    finally:
        sock.close()
    return result


def probe_framings(host: str, command: str, persistent: bool) -> list[dict]:
    print(f"\n== 2. framing sweep using {command} ==")
    if command in FORBIDDEN:
        raise SystemExit(f"refusing to send {command}: it is on the forbidden list")

    results = []
    sock = None
    for body_name, body in bodies_for(command):
        for frame_name, frame in FRAMINGS:
            payload = frame(body)
            label = f"{body_name}/{frame_name}"
            if not persistent or sock is None:
                if sock:
                    sock.close()
                sock = connect(host)
                if not sock:
                    return results
            entry = {"body": body_name, "framing": frame_name,
                     "sent": payload[:120].decode("latin1", "replace"),
                     "sent_bytes": len(payload)}
            try:
                sock.sendall(payload)
            except OSError as exc:
                entry["error"] = f"send failed: {exc}"
                results.append(entry)
                sock.close()
                sock = None
                continue
            data = read_available(sock, first_timeout=2.5)
            entry["received_bytes"] = len(data)
            if data:
                entry["hexdump"] = hexdump(data)
                entry["text"] = data[:1024].decode("utf-8", "replace")
                entry["verdict"] = looks_like_answer(data)
                print(f"  {label:34s} -> {len(data):5d} bytes  {entry['verdict']}")
            else:
                print(f"  {label:34s} -> (nothing)")
            results.append(entry)
            if not persistent:
                sock.close()
                sock = None
    if sock:
        sock.close()
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="the Drobo's IP address")
    ap.add_argument("-o", "--out", default="nasd-probe.json")
    ap.add_argument("--command", default="eCmdGetVersion",
                    help=f"which safe command to probe with; one of {SAFE_COMMANDS}")
    ap.add_argument("--listen-only", action="store_true",
                    help="connect and read, but never send anything")
    ap.add_argument("--persistent", action="store_true",
                    help="reuse one connection for every attempt")
    args = ap.parse_args(argv)

    if args.command not in SAFE_COMMANDS:
        raise SystemExit(
            f"{args.command!r} is not in the read-only allow-list.\n"
            f"Allowed: {', '.join(SAFE_COMMANDS)}")

    print(f"Probing nasd at {args.host}:{PORT}")
    print("Only read-only commands will be sent.")

    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": args.host, "port": PORT, "command": args.command,
        "banner": probe_banner(args.host),
    }

    if args.listen_only:
        print("\n--listen-only: stopping without sending anything.")
    else:
        report["framings"] = probe_framings(args.host, args.command, args.persistent)

        good = [r for r in report["framings"] if r.get("received_bytes")]
        print(f"\n== summary ==")
        print(f"  {len(good)} of {len(report['framings'])} attempts got a reply")
        best = [r for r in good if "Error" in (r.get("text") or "")
                or "XML" in (r.get("verdict") or "")]
        if best:
            print("  most promising:")
            for r in best[:5]:
                print(f"    {r['body']}/{r['framing']}: {r['verdict']}")
        elif good:
            print("  something answered, but nothing looked like XML. "
                  "Check the hexdumps in the report.")
        else:
            print("  nothing answered. Either the framing guesses are all wrong,")
            print("  or nasd expects authentication first. Fall back to Wireshark:")
            print(f"    tcp port {PORT} and host {args.host}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nWrote {args.out}")
    print("Keep that file -- a driver can be written from what it recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
