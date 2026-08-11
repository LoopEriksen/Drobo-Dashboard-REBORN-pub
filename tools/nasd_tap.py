#!/usr/bin/env python3
"""
nasd_tap.py - sit between Drobo Dashboard and the Drobo, and write down
everything they say to each other.

WHY THIS EXISTS
---------------
This tool was built while the client->device request framing was still the
project's biggest unknown: we knew what the Drobo *says* (it pushes a DRINASD
status greeting the moment you connect) but not how to *ask it questions*.
That framing has since been recovered -- directly, by sending commands
ourselves and reading the replies, not by tapping Dashboard -- see
docs/protocol-map.md, "THE COMMAND PROTOCOL -- SOLVED", and
agent/drobo_nasd/config_cmd.py / sysinfo.py for the working
implementation. So this tool is no longer the way to learn the protocol.

What it can still do: watch whether Dashboard itself ever gets a real answer
out of the battery/fan/PSU/performance screens on this hardware. Our own
sweep (tools/nasd_sweep.py, docs/command-surface.md) sends those commands
directly and mostly gets empty replies or timeouts; tapping Dashboard would
show whether the *original* client fares any differently, which would settle
whether that's a sensor this chassis lacks or a request shape we're still
getting wrong. Nobody has run it for that purpose yet.

The obvious route is Wireshark, but capturing packets on Windows needs the Npcap
kernel driver installed and an elevated shell. This tool needs neither. It is an
ordinary userspace TCP relay: Dashboard connects to it, it connects to the real
Drobo, and it copies bytes between them while logging both directions.

Because it sees the stream *after* TCP reassembly, the output is cleaner than a
packet capture -- no retransmits, no segmentation, no reassembly guesswork.

WHAT IT DOES NOT DO
-------------------
It never modifies a byte. Every byte Dashboard sends reaches the Drobo unchanged
and vice versa, so the Drobo cannot tell the difference. It originates nothing of
its own: it has no opinion about the protocol and never injects a command.

HOW TO USE IT
-------------
1. Run it (no admin needed):

       py tools/nasd_tap.py --drobo 10.0.0.50

   It listens on 127.0.0.1:5000 by default.

2. In Drobo Dashboard, add a Drobo manually by IP and enter  127.0.0.1
   (Dashboard supports manual discovery by address -- eCmdAddManualDiscoveryIP
   exists in the firmware, and the UI exposes it.)

3. Click around Dashboard: the status page, the drive/bay detail, and the tools
   pages. Those are the screens that fetch battery, fan, PSU and performance --
   fields our own commands mostly can't get real data for yet either (see
   docs/command-surface.md); performance is the exception, reachable directly,
   just not wired into the driver.

4. Stop with Ctrl+C. Two files land in docs/captures/:
       nasd-tap-<stamp>.jsonl    every chunk, with direction and timing
       nasd-tap-<stamp>.bin      raw client->server bytes, concatenated

PRIVACY
-------
This protocol is unencrypted. If you type a Drobo password into Dashboard while
the tap is running, that password is in the log in plain text. Change it to
something disposable first, or scrub the files before sharing. See
docs/captures/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time

SIGNATURE = b"DRINASD\x00"
HEADER_LEN = 16

_print_lock = threading.Lock()


def hexdump(data: bytes, limit: int = 160) -> list[str]:
    out = []
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        h = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        a = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"    {off:04x}  {h}  {a}")
    if len(data) > limit:
        out.append(f"    ... {len(data) - limit} more bytes")
    return out


def describe(data: bytes) -> str:
    """
    Best-effort one-line summary. The server's frames are understood; the
    client's are exactly what we're trying to learn, so this stays descriptive
    rather than asserting a structure we haven't confirmed.
    """
    if len(data) >= HEADER_LEN and data[:8] == SIGNATURE:
        major, minor = data[8], data[9]
        flags = struct.unpack(">H", data[10:12])[0]
        length = struct.unpack(">I", data[12:16])[0]
        return (f"DRINASD frame  ver={major}.{minor} flags=0x{flags:04x} "
                f"len={length} (total {len(data)})")
    if data[:8].rstrip(b"\x00").startswith(b"DRINASD"):
        return f"DRINASD-like header, variant signature {data[:8]!r}"
    printable = sum(1 for b in data[:64] if 32 <= b < 127)
    if data.lstrip()[:1] == b"<":
        return "bare XML"
    if printable > len(data[:64]) * 0.8:
        return f"mostly text: {data[:48]!r}"
    return f"binary, {len(data)} bytes"


class Tap:
    def __init__(self, drobo: str, drobo_port: int, jsonl_path: str, bin_path: str):
        self.drobo = drobo
        self.drobo_port = drobo_port
        self.jsonl = open(jsonl_path, "w", encoding="utf-8")
        self.binf = open(bin_path, "wb")
        self.started = time.time()
        self.counts = {"c2s": 0, "s2c": 0}
        self.bytes = {"c2s": 0, "s2c": 0}
        self._lock = threading.Lock()

    def record(self, direction: str, data: bytes, conn_id: int) -> None:
        with self._lock:
            self.counts[direction] += 1
            self.bytes[direction] += len(data)
            self.jsonl.write(json.dumps({
                "t": round(time.time() - self.started, 4),
                "conn": conn_id,
                "dir": direction,
                "len": len(data),
                "summary": describe(data),
                "hex": data.hex(),
            }) + "\n")
            self.jsonl.flush()
            # The client->server direction is the unknown one; keep a raw copy
            # so the dissector has clean bytes to work from.
            if direction == "c2s":
                self.binf.write(data)
                self.binf.flush()

        arrow = "-->" if direction == "c2s" else "<--"
        who = "Dashboard --> Drobo" if direction == "c2s" else "Drobo --> Dashboard"
        with _print_lock:
            print(f"\n[{time.time() - self.started:8.3f}s] conn{conn_id} {arrow} {who}"
                  f"  {len(data)} bytes")
            print(f"    {describe(data)}")
            for line in hexdump(data):
                print(line)
            sys.stdout.flush()

    def close(self) -> None:
        self.jsonl.close()
        self.binf.close()


def pump(src: socket.socket, dst: socket.socket, direction: str,
         tap: Tap, conn_id: int) -> None:
    """Copy bytes one way, logging as we go. Never alters the stream."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            tap.record(direction, data, conn_id)
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket, addr, tap: Tap, conn_id: int) -> None:
    with _print_lock:
        print(f"\n=== conn{conn_id}: Dashboard connected from {addr[0]}:{addr[1]} ===")
        print(f"    opening upstream to {tap.drobo}:{tap.drobo_port}")
        sys.stdout.flush()
    try:
        upstream = socket.create_connection((tap.drobo, tap.drobo_port), timeout=10)
    except OSError as exc:
        with _print_lock:
            print(f"    !! could not reach the Drobo: {exc}")
        client.close()
        return

    upstream.settimeout(None)
    client.settimeout(None)

    threads = [
        threading.Thread(target=pump, args=(client, upstream, "c2s", tap, conn_id), daemon=True),
        threading.Thread(target=pump, args=(upstream, client, "s2c", tap, conn_id), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with _print_lock:
        print(f"=== conn{conn_id} closed ===")
        sys.stdout.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drobo", required=True, help="the real Drobo's IP address")
    ap.add_argument("--drobo-port", type=int, default=5000)
    ap.add_argument("--listen", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--listen-port", type=int, default=5000)
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args(argv)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = args.outdir or os.path.join(repo, "docs", "captures")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    jsonl_path = os.path.join(outdir, f"nasd-tap-{stamp}.jsonl")
    bin_path = os.path.join(outdir, f"nasd-tap-{stamp}.bin")

    tap = Tap(args.drobo, args.drobo_port, jsonl_path, bin_path)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((args.listen, args.listen_port))
    except OSError as exc:
        print(f"Could not listen on {args.listen}:{args.listen_port} -- {exc}")
        print("If the agent is running on this port, stop it first.")
        return 1
    srv.listen(8)

    print(f"nasd tap listening on {args.listen}:{args.listen_port}")
    print(f"  forwarding to the real Drobo at {args.drobo}:{args.drobo_port}")
    print(f"  writing {os.path.basename(jsonl_path)} and {os.path.basename(bin_path)}")
    print()
    print("  Now: in Drobo Dashboard, add a Drobo manually by IP and enter")
    print(f"       {args.listen}")
    print("  Then click the status page, the drive detail, and the tools pages.")
    print("  Ctrl+C when done.")
    print()

    conn_id = 0
    try:
        while True:
            client, addr = srv.accept()
            conn_id += 1
            threading.Thread(target=handle, args=(client, addr, tap, conn_id),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\n\nstopping.")
    finally:
        srv.close()
        tap.close()

    print(f"  Dashboard -> Drobo : {tap.counts['c2s']} chunks, {tap.bytes['c2s']} bytes")
    print(f"  Drobo -> Dashboard : {tap.counts['s2c']} chunks, {tap.bytes['s2c']} bytes")
    print(f"\n  wrote {jsonl_path}")
    print(f"        {bin_path}")
    if tap.counts["c2s"] == 0:
        print("\n  Nothing came from Dashboard -- it never connected here.")
        print("  Check that you added 127.0.0.1 as a manual Drobo address.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
