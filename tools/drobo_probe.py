#!/usr/bin/env python3
"""
drobo_probe.py - Phase 1 discovery tool for Drobo Dashboard REBORN.

Finds a Drobo on the local network and reports what it is listening on, so we
can work out how the original Drobo Dashboard talks to it. Nothing here writes
to the Drobo -- it only opens connections and reads what comes back.

Standard library only, Python 3.9+. No admin rights needed.

Usage:
    py drobo_probe.py mdns
        Ask the network to list every advertised service (Bonjour/mDNS).
        This is how Drobo Dashboard finds a Drobo, so the Drobo should answer.

    py drobo_probe.py scan
    py drobo_probe.py scan --cidr 10.0.0.0/24
        Sweep the local subnet for hosts that look like a NAS.

    py drobo_probe.py portsweep 10.0.0.50
        Every port from 1-10000 on one host. Run this once you know the IP.

    py drobo_probe.py fingerprint 10.0.0.50
        Connect to each open port and record whatever it says.

    py drobo_probe.py report 10.0.0.50 -o findings.json
        mdns + portsweep + fingerprint, written out as JSON.

NOTE ON PORT NUMBERS: the "likely Drobo" ports below are candidates, not
confirmed facts. Confirming which port Dashboard actually uses is the whole
point of this tool -- see docs/phase-1-capture-playbook.md.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------
# Candidate ports. Stage-1 scan only, to find the box quickly.
# --------------------------------------------------------------------------
QUICK_PORTS = {
    21: "FTP",
    22: "SSH (dropbear, usually only if a DroboApp enabled it)",
    23: "Telnet",
    80: "HTTP",
    111: "rpcbind / NFS",
    139: "NetBIOS session (SMB1)",
    443: "HTTPS",
    445: "SMB (file sharing)",
    548: "AFP (Apple filing)",
    631: "IPP printing",
    873: "rsync",
    2049: "NFS",
    3260: "iSCSI (business/SAN models)",
    5000: "nasd - the Drobo management protocol (CONFIRMED from firmware)",
    5001: "candidate: Drobo management (unconfirmed)",
    5353: "mDNS (UDP - not tested by TCP scan)",
    8080: "HTTP alt / DroboApp web UI",
    8081: "HTTP alt / DroboApp web UI",
    8443: "HTTPS alt / DroboApp web UI",
    9000: "DroboApp range",
}

SWEEP_RANGE = (1, 10000)

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def local_ipv4() -> str:
    """Best-guess local IP by asking the OS which interface reaches the internet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def default_cidr() -> str:
    ip = local_ipv4()
    return str(ipaddress.ip_network(ip + "/24", strict=False))


def tcp_open(host: str, port: int, timeout: float = 0.35) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def hexdump(data: bytes, limit: int = 256) -> list[str]:
    """Classic offset / hex / ascii dump, capped so output stays readable."""
    out = []
    data = data[:limit]
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:04x}  {hexpart}  {asciipart}")
    return out


# --------------------------------------------------------------------------
# Minimal mDNS (Bonjour) client
#
# We send one query to the multicast address asking for the special
# "_services._dns-sd._udp.local" record, which means "list every service type
# you advertise". We set the QU bit so responders reply straight back to us
# instead of multicasting -- that avoids fighting with Apple's Bonjour service
# for UDP port 5353 on Windows.
# --------------------------------------------------------------------------

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
QTYPE = {1: "A", 12: "PTR", 16: "TXT", 28: "AAAA", 33: "SRV"}


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        if label:
            out += bytes([len(label)]) + label.encode("utf-8")
    return out + b"\x00"


def _decode_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    resume = offset
    guard = 0
    while offset < len(data) and guard < 128:
        guard += 1
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = struct.unpack("!H", data[offset : offset + 2])[0] & 0x3FFF
            if not jumped:
                resume = offset + 2
            jumped = True
            offset = pointer
            continue
        offset += 1
        labels.append(data[offset : offset + length].decode("utf-8", "replace"))
        offset += length
    return ".".join(labels), (resume if jumped else offset)


def _build_query(qname: str, qtype: int = 12) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    # qclass 0x8001 = IN with the "unicast response please" bit set
    return header + _encode_name(qname) + struct.pack("!HH", qtype, 0x8001)


def _parse_response(data: bytes) -> list[dict]:
    records: list[dict] = []
    if len(data) < 12:
        return records
    _, _, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(qd):
        _, offset = _decode_name(data, offset)
        offset += 4
    for _ in range(an + ns + ar):
        if offset >= len(data):
            break
        name, offset = _decode_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlen]
        rec = {"name": name, "type": QTYPE.get(rtype, str(rtype))}
        if rtype == 12:  # PTR
            rec["value"], _ = _decode_name(data, offset)
        elif rtype == 1 and rdlen == 4:  # A
            rec["value"] = socket.inet_ntoa(rdata)
        elif rtype == 33 and rdlen >= 6:  # SRV
            _pri, _wt, port = struct.unpack("!HHH", rdata[:6])
            target, _ = _decode_name(data, offset + 6)
            rec["value"] = f"{target}:{port}"
            rec["port"] = port
        elif rtype == 16:  # TXT
            parts, i = [], 0
            while i < len(rdata):
                ln = rdata[i]
                parts.append(rdata[i + 1 : i + 1 + ln].decode("utf-8", "replace"))
                i += 1 + ln
            rec["value"] = " | ".join(parts)
        else:
            rec["value"] = rdata.hex()
        records.append(rec)
        offset += rdlen
    return records


def mdns_discover(wait: float = 4.0) -> list[dict]:
    """Enumerate service types, then ask about anything Drobo-ish we see."""
    queries = [
        "_services._dns-sd._udp.local",
        # Confirmed from firmware: /etc/avahi/services/nasd.service advertises
        # _nasd._tcp on port 5000. This is how Dashboard finds a Drobo.
        "_nasd._tcp.local",
        "_smb._tcp.local",
        "_afpovertcp._tcp.local",
        "_device-info._tcp.local",
    ]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    seen: dict[tuple, dict] = {}
    try:
        for q in queries:
            try:
                sock.sendto(_build_query(q), (MDNS_ADDR, MDNS_PORT))
            except OSError as exc:
                print(f"  ! could not send mDNS query {q}: {exc}", file=sys.stderr)
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            for rec in _parse_response(data):
                rec["from"] = addr[0]
                key = (rec["from"], rec["name"], rec["type"], rec.get("value"))
                seen.setdefault(key, rec)
    finally:
        sock.close()
    return list(seen.values())


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def scan_subnet(cidr: str, ports: dict[int, str], workers: int = 256) -> list[dict]:
    net = ipaddress.ip_network(cidr, strict=False)
    targets = [(str(h), p) for h in net.hosts() for p in ports if p != 5353]
    print(f"  scanning {net} -- {len(targets)} host/port pairs ...")
    hits: dict[str, list[int]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda t: (t, tcp_open(*t)), targets)
        for (host, port), is_open in results:
            if is_open:
                hits.setdefault(host, []).append(port)
    out = []
    for host, plist in sorted(hits.items(), key=lambda kv: ipaddress.ip_address(kv[0])):
        plist.sort()
        try:
            name = socket.gethostbyaddr(host)[0]
        except OSError:
            name = ""
        out.append(
            {
                "ip": host,
                "hostname": name,
                "open_ports": plist,
                "notes": [f"{p}: {ports[p]}" for p in plist],
            }
        )
    return out


def port_sweep(host: str, lo: int, hi: int, workers: int = 512) -> list[int]:
    print(f"  sweeping {host} ports {lo}-{hi} ...")
    ports = range(lo, hi + 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda p: (p, tcp_open(host, p, 0.5)), ports)
        return [p for p, is_open in results if is_open]


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

PROBES: list[tuple[str, bytes]] = [
    ("silence", b""),
    ("crlf", b"\r\n"),
    ("http-get", b"GET / HTTP/1.0\r\nHost: drobo\r\n\r\n"),
    ("xml-ish", b"<?xml version=\"1.0\"?><request/>\r\n"),
]


def fingerprint_port(host: str, port: int, timeout: float = 2.5) -> dict:
    """Try each probe; record the first one that gets a reply."""
    result: dict = {"port": port, "attempts": []}
    for label, payload in PROBES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            if sock.connect_ex((host, port)) != 0:
                result["attempts"].append({"probe": label, "error": "connect failed"})
                continue
            if payload:
                sock.sendall(payload)
            chunks = b""
            try:
                while len(chunks) < 2048:
                    piece = sock.recv(1024)
                    if not piece:
                        break
                    chunks += piece
                    sock.settimeout(0.4)
            except socket.timeout:
                pass
            attempt = {"probe": label, "bytes": len(chunks)}
            if chunks:
                attempt["hexdump"] = hexdump(chunks)
                attempt["text"] = chunks[:512].decode("utf-8", "replace")
            result["attempts"].append(attempt)
            if chunks:
                break  # got something; no need to keep poking
        except OSError as exc:
            result["attempts"].append({"probe": label, "error": str(exc)})
        finally:
            sock.close()
    return result


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def print_mdns(records: list[dict]) -> None:
    if not records:
        print("  (no mDNS replies -- see the troubleshooting notes in the playbook)")
        return
    by_host: dict[str, list[dict]] = {}
    for r in records:
        by_host.setdefault(r["from"], []).append(r)
    for host, recs in sorted(by_host.items()):
        print(f"\n  from {host}")
        for r in sorted(recs, key=lambda x: (x["type"], x["name"])):
            print(f"    {r['type']:<5} {r['name']}")
            print(f"          -> {r.get('value','')}")


def print_fingerprint(fp: dict) -> None:
    print(f"\n  --- port {fp['port']} ---")
    for att in fp["attempts"]:
        if "error" in att:
            print(f"    probe {att['probe']:<9} error: {att['error']}")
            continue
        print(f"    probe {att['probe']:<9} {att['bytes']} bytes back")
        for line in att.get("hexdump", []):
            print(f"      {line}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_mdns = sub.add_parser("mdns", help="list advertised network services")
    p_mdns.add_argument("--wait", type=float, default=4.0)

    p_scan = sub.add_parser("scan", help="sweep the subnet for likely NAS hosts")
    p_scan.add_argument("--cidr", default=None)

    p_ps = sub.add_parser("portsweep", help="all ports on one host")
    p_ps.add_argument("host")
    p_ps.add_argument("--lo", type=int, default=SWEEP_RANGE[0])
    p_ps.add_argument("--hi", type=int, default=SWEEP_RANGE[1])

    p_fp = sub.add_parser("fingerprint", help="see what each open port says")
    p_fp.add_argument("host")
    p_fp.add_argument("--ports", default=None, help="comma-separated; default = quick list")

    p_rep = sub.add_parser("report", help="everything, as JSON")
    p_rep.add_argument("host")
    p_rep.add_argument("-o", "--out", default="drobo-findings.json")

    args = ap.parse_args(argv)

    if args.cmd == "mdns":
        print("Asking the network what services it advertises ...")
        print_mdns(mdns_discover(args.wait))
        return 0

    if args.cmd == "scan":
        cidr = args.cidr or default_cidr()
        print(f"Local IP looks like {local_ipv4()}")
        found = scan_subnet(cidr, QUICK_PORTS)
        if not found:
            print("  no hosts answered on any candidate port.")
        for h in found:
            label = f" ({h['hostname']})" if h["hostname"] else ""
            print(f"\n  {h['ip']}{label}")
            for n in h["notes"]:
                print(f"      {n}")
        return 0

    if args.cmd == "portsweep":
        open_ports = port_sweep(args.host, args.lo, args.hi)
        print(f"  open: {open_ports or '(none)'}")
        return 0

    if args.cmd == "fingerprint":
        ports = (
            [int(p) for p in args.ports.split(",")]
            if args.ports
            else [p for p in QUICK_PORTS if p != 5353]
        )
        for port in ports:
            if tcp_open(args.host, port):
                print_fingerprint(fingerprint_port(args.host, port))
        return 0

    if args.cmd == "report":
        print("1/3 mDNS ...")
        mdns = mdns_discover()
        print("2/3 port sweep ...")
        open_ports = port_sweep(args.host, *SWEEP_RANGE)
        print(f"    open: {open_ports}")
        print("3/3 fingerprint ...")
        fps = [fingerprint_port(args.host, p) for p in open_ports]
        blob = {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "target": args.host,
            "scanner_ip": local_ipv4(),
            "mdns": mdns,
            "open_ports": open_ports,
            "fingerprints": fps,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, indent=2)
        print(f"\nWrote {args.out}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
