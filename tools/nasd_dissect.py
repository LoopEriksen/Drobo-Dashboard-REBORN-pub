#!/usr/bin/env python3
"""
nasd_dissect.py - reconstruct nasd's request framing from a packet capture.

Point this at a Wireshark capture of "tcp port 5000" taken while Drobo
Dashboard talks to a real 5N, and it will:

  1. Read the capture file -- classic pcap or pcapng, parsed by hand
     (stdlib only, no scapy/dpkt).
  2. Keep only TCP port 5000 traffic and reassemble the client->server and
     server->client byte streams, each in sequence-number order.
  3. Look for the "DRINASD"/"DRINASD4" signature strings that
     docs/protocol-map.md recovered from the firmware.
  4. Try a battery of plausible header shapes (signature position, length
     field offset/size/endianness, whether the length includes the header
     or just the payload) against the reassembled bytes, and report which
     shape actually predicts where each XML payload starts and ends.
  5. Print the first decoded XML payloads and a proposed NasdHeader layout.

This tool INFERS framing from evidence in one capture. Nothing it prints is
a substitute for `protocol-map.md`'s CONFIRMED/FIRMWARE/GUESS discipline --
treat everything below as a lead to verify against more captures, and say so
when reporting results. See test_nasd_dissect.py for proof this recovers a
*known* framing when the ground truth is under our control.

The capture-file readers are implemented from public format specifications,
not from anyone's parser source: classic pcap's file/record layout has been a
de facto standard since tcpdump, and pcapng is documented by the IETF opsawg
draft (draft-tuexen-opsawg-pcapng) and by Wireshark's own published format
page. Written by hand against those documents, stdlib only -- no scapy, no
dpkt.

Usage:
    py tools/nasd_dissect.py docs/captures/03-idle-60s.pcapng
    py tools/nasd_dissect.py capture.pcap -o docs/captures/dissect-report.json

Limitations, stated up front:
  - No IP defragmentation, no TCP sequence-wraparound handling. Fine for the
    short, single-connection captures this project's playbook asks for.
  - IPv6 extension headers are not walked; only a bare "next header is TCP"
    IPv6 packet is understood.
  - Retransmission handling in reassembly is a simple seq-ordered de-overlap,
    not a full TCP stack.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Constants from docs/protocol-map.md
# ---------------------------------------------------------------------------
SIG4 = b"DRINASD4"
SIG = b"DRINASD"
SIGNATURES = (SIG4, SIG)  # longest first -- SIG is a byte-prefix of SIG4

# The status port. The COMMAND port is 5001 -- pass --port to look at that
# instead, which is where Identify and every other command actually goes.
TCP_PORT = 5000

ETH_TYPE_IPV4 = 0x0800
ETH_TYPE_IPV6 = 0x86DD
ETH_TYPE_VLAN = 0x8100
ETH_TYPE_VLAN_QINQ = 0x88A8


# ---------------------------------------------------------------------------
# Capture file readers -- classic pcap and pcapng, parsed directly.
# Each returns a list of (timestamp, link_layer_frame_bytes, linktype).
# ---------------------------------------------------------------------------
_CLASSIC_MAGICS = {
    b"\xa1\xb2\xc3\xd4": (">", 1e-6),
    b"\xd4\xc3\xb2\xa1": ("<", 1e-6),
    b"\xa1\xb2\x3c\x4d": (">", 1e-9),
    b"\x4d\x3c\xb2\xa1": ("<", 1e-9),
}


def read_pcap_classic(data: bytes) -> list[tuple[float, bytes, int]]:
    endian, ts_scale = _CLASSIC_MAGICS[data[0:4]]
    _major, _minor, _tz, _sigfigs, _snaplen, network = struct.unpack_from(
        endian + "HHiIII", data, 4)
    pos = 24
    packets: list[tuple[float, bytes, int]] = []
    while pos + 16 <= len(data):
        ts_sec, ts_usec, incl_len, _orig_len = struct.unpack_from(
            endian + "IIII", data, pos)
        pos += 16
        if incl_len < 0 or pos + incl_len > len(data):
            break
        pkt = data[pos:pos + incl_len]
        pos += incl_len
        packets.append((ts_sec + ts_usec * ts_scale, pkt, network))
    return packets


PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_IDB = 0x00000001
PCAPNG_SPB = 0x00000003
PCAPNG_EPB = 0x00000006


def read_pcapng(data: bytes) -> list[tuple[float, bytes, int]]:
    packets: list[tuple[float, bytes, int]] = []
    endian = ">"  # provisional until the first SHB sets it for real
    interfaces: list[int] = []  # linktype per interface id, in this section
    pos = 0
    while pos + 12 <= len(data):
        # Read the block type with the CURRENT section endianness.
        #
        # It is safe to do this before the first section header block has told
        # us the endianness, because 0x0A0D0D0A is a byte-palindrome -- it
        # reads the same either way, so the SHB below is always recognised.
        #
        # Reading it as ">I" unconditionally was a real bug, and a quiet one.
        # Every other block type is an ordinary little-endian integer in a
        # little-endian capture, so an enhanced packet block (type 6, bytes
        # 06 00 00 00) read big-endian comes out as 0x06000000 and matches
        # nothing. Block LENGTHS were already read with the right endianness,
        # so the walk marched happily through the whole file, recognised not a
        # single packet, and reported "Read 0 link-layer frames" -- which reads
        # exactly like an empty capture rather than a broken parser. Every
        # capture Windows produces is little-endian, so this never worked on a
        # real file; the tests passed because their fixtures were big-endian.
        block_type = struct.unpack_from(endian + "I", data, pos)[0]
        if block_type == PCAPNG_SHB:
            magic = data[pos + 8:pos + 12]
            if magic == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            elif magic == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            else:
                raise ValueError(
                    f"pcapng: bad byte-order magic {magic!r} in section header block")
            interfaces = []
        block_total_length = struct.unpack_from(endian + "I", data, pos + 4)[0]
        if block_total_length < 12 or pos + block_total_length > len(data):
            break  # truncated/corrupt block -- stop rather than misread
        body = data[pos + 8:pos + block_total_length - 4]

        if block_type == PCAPNG_IDB and len(body) >= 8:
            linktype = struct.unpack_from(endian + "H", body, 0)[0]
            interfaces.append(linktype)
        elif block_type == PCAPNG_EPB and len(body) >= 20:
            iface_id = struct.unpack_from(endian + "I", body, 0)[0]
            cap_len = struct.unpack_from(endian + "I", body, 12)[0]
            pkt_data = body[20:20 + cap_len]
            linktype = interfaces[iface_id] if iface_id < len(interfaces) else 1
            packets.append((0.0, pkt_data, linktype))
        elif block_type == PCAPNG_SPB and len(body) >= 4:
            pkt_data = body[4:]
            linktype = interfaces[0] if interfaces else 1
            packets.append((0.0, pkt_data, linktype))
        # Other block types (name resolution, interface stats, comments,
        # obsolete "packet block") are skipped -- not needed for framing.

        pos += block_total_length
    return packets


def read_capture(path: str) -> list[tuple[float, bytes, int]]:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 4:
        raise ValueError(f"{path}: too small to be a capture file")
    if data[0:4] in _CLASSIC_MAGICS:
        return read_pcap_classic(data)
    if struct.unpack_from(">I", data, 0)[0] == PCAPNG_SHB:
        return read_pcapng(data)
    raise ValueError(
        f"{path}: not recognised as classic pcap or pcapng "
        f"(first bytes: {data[0:4]!r})")


# ---------------------------------------------------------------------------
# Link layer / IP / TCP parsing -- just enough to get to a TCP payload.
# ---------------------------------------------------------------------------
def strip_link_layer(frame: bytes, linktype: int) -> Optional[bytes]:
    if linktype == 1:  # Ethernet
        if len(frame) < 14:
            return None
        offset = 12
        if offset + 2 > len(frame):
            return None
        ethertype = struct.unpack_from(">H", frame, offset)[0]
        while ethertype in (ETH_TYPE_VLAN, ETH_TYPE_VLAN_QINQ):
            offset += 4
            if offset + 2 > len(frame):
                return None
            ethertype = struct.unpack_from(">H", frame, offset)[0]
        if ethertype in (ETH_TYPE_IPV4, ETH_TYPE_IPV6):
            return frame[offset + 2:]
        return None
    if linktype == 101:  # LINKTYPE_RAW -- IP packet, no link header at all
        return frame
    if linktype == 113:  # LINKTYPE_LINUX_SLL
        if len(frame) < 16:
            return None
        proto = struct.unpack_from(">H", frame, 14)[0]
        if proto in (ETH_TYPE_IPV4, ETH_TYPE_IPV6):
            return frame[16:]
        return None
    # Unknown linktype: best-effort guess between raw-IP and Ethernet.
    if frame and (frame[0] >> 4) in (4, 6):
        return frame
    if len(frame) >= 14:
        ethertype = struct.unpack_from(">H", frame, 12)[0]
        if ethertype in (ETH_TYPE_IPV4, ETH_TYPE_IPV6):
            return frame[14:]
    return None


def parse_ip(pkt: bytes):
    """Returns (proto, src_ip, dst_ip, payload) or None."""
    if not pkt:
        return None
    version = pkt[0] >> 4
    if version == 4:
        if len(pkt) < 20:
            return None
        ihl = (pkt[0] & 0x0F) * 4
        if ihl < 20 or len(pkt) < ihl:
            return None
        total_len = struct.unpack_from(">H", pkt, 2)[0]
        proto = pkt[9]
        src = socket.inet_ntop(socket.AF_INET, pkt[12:16])
        dst = socket.inet_ntop(socket.AF_INET, pkt[16:20])
        end = total_len if 0 < total_len <= len(pkt) else len(pkt)
        return proto, src, dst, pkt[ihl:end]
    if version == 6:
        if len(pkt) < 40:
            return None
        payload_len = struct.unpack_from(">H", pkt, 4)[0]
        next_header = pkt[6]
        src = socket.inet_ntop(socket.AF_INET6, pkt[8:24])
        dst = socket.inet_ntop(socket.AF_INET6, pkt[24:40])
        end = min(40 + payload_len, len(pkt)) if payload_len else len(pkt)
        return next_header, src, dst, pkt[40:end]
    return None


def parse_tcp(seg: bytes):
    """Returns (sport, dport, seq, flags, data) or None."""
    if len(seg) < 20:
        return None
    sport, dport = struct.unpack_from(">HH", seg, 0)
    seq = struct.unpack_from(">I", seg, 4)[0]
    data_offset = (seg[12] >> 4) * 4
    flags = seg[13]
    if data_offset < 20:
        data_offset = 20
    if data_offset > len(seg):
        data_offset = len(seg)
    return sport, dport, seq, flags, seg[data_offset:]


# ---------------------------------------------------------------------------
# TCP stream reassembly, filtered to port 5000.
# ---------------------------------------------------------------------------
def collect_streams(packets: list[tuple[float, bytes, int]],
                    port: int = TCP_PORT) -> dict:
    """Group TCP payload segments for `port` by connection and direction.

    Returns {stream_key: {"to_server": [(seq, bytes), ...],
                           "to_client": [(seq, bytes), ...],
                           "client": (ip, port), "server": (ip, port)}}
    """
    streams: dict = {}
    for _ts, frame, linktype in packets:
        ip_pkt = strip_link_layer(frame, linktype)
        if ip_pkt is None:
            continue
        parsed = parse_ip(ip_pkt)
        if parsed is None:
            continue
        proto, src, dst, payload = parsed
        if proto != 6:  # TCP only
            continue
        tcp = parse_tcp(payload)
        if tcp is None:
            continue
        sport, dport, seq, _flags, data = tcp
        if sport != port and dport != port:
            continue
        if not data:
            continue  # pure ACK/SYN, nothing to reassemble
        key = frozenset({(src, sport), (dst, dport)})
        st = streams.setdefault(
            key, {"to_server": [], "to_client": [], "client": None, "server": None})
        if dport == port:
            st["to_server"].append((seq, data))
            st["client"], st["server"] = (src, sport), (dst, dport)
        else:
            st["to_client"].append((seq, data))
            st["server"], st["client"] = (src, sport), (dst, dport)
    return streams


def reassemble(segments: list[tuple[int, bytes]]) -> bytes:
    """Sort TCP segments by sequence number and stitch them together,
    dropping retransmitted/overlapping bytes. Not a full TCP stack -- no
    wraparound handling, good enough for one short capture."""
    ordered = sorted((s for s in segments if s[1]), key=lambda s: s[0])
    out = bytearray()
    next_seq = None
    for seq, data in ordered:
        if next_seq is None:
            out += data
            next_seq = seq + len(data)
            continue
        if seq >= next_seq:
            out += data
            next_seq = seq + len(data)
        else:
            overlap = next_seq - seq
            if len(data) > overlap:
                out += data[overlap:]
                next_seq = seq + len(data)
            # else: fully-contained retransmission, skip
    return bytes(out)


# ---------------------------------------------------------------------------
# Signature search
# ---------------------------------------------------------------------------
def find_signature_occurrences(buf: bytes) -> list[tuple[int, bytes]]:
    """Every offset where DRINASD or DRINASD4 appears, longest match wins
    when both match the same offset (DRINASD is a prefix of DRINASD4)."""
    hits: dict[int, bytes] = {}
    for sig in SIGNATURES:
        start = 0
        while True:
            idx = buf.find(sig, start)
            if idx == -1:
                break
            if idx not in hits or len(sig) > len(hits[idx]):
                hits[idx] = sig
            start = idx + 1
    return sorted(hits.items())


def _looks_like_xml_start(payload: bytes) -> bool:
    if payload[:5] == b"<?xml":
        return True
    return len(payload) > 1 and payload[0:1] == b"<" and payload[1:2].isalpha()


# ---------------------------------------------------------------------------
# Framing inference
# ---------------------------------------------------------------------------
# Candidate padding between the end of the signature (or start of frame, if
# no signature) and the start of the length field. 0 is the likely case; the
# others are kept in case there's a reserved/version byte in between.
_PAD_CANDIDATES = (0, 1, 2, 4)
_SIZE_CANDIDATES = (4, 2)
_ENDIAN_CANDIDATES = (">", "<")


def _score_login_candidate(buf: bytes, sig_len: int, pad: int, size: int,
                            endian: str, includes_header: bool) -> Optional[dict]:
    field_start = sig_len + pad
    field_end = field_start + size
    if field_end > len(buf):
        return None
    val = int.from_bytes(buf[field_start:field_end], "big" if endian == ">" else "little")
    header_total = field_end  # bytes from frame start through the length field
    payload_len = (val - header_total) if includes_header else val
    if payload_len < 0:
        return None
    frame_end = field_end + payload_len
    if frame_end > len(buf):
        return None
    payload = buf[field_end:frame_end]
    if not _looks_like_xml_start(payload):
        return None  # only entertain candidates whose payload looks like XML

    score = 10 if payload[:5] == b"<?xml" else 6
    reasons = ["payload begins with " +
               ("an XML declaration" if payload[:5] == b"<?xml" else "an XML element")]
    remainder = buf[frame_end:]
    if not remainder:
        score += 2
        reasons.append("consumes exactly to the end of the buffer")
    elif any(remainder.startswith(s) for s in SIGNATURES):
        score += 5
        reasons.append("next bytes are another signature occurrence")
    else:
        score += 1

    return {
        "pad": pad, "length_size": size, "endian": endian,
        "includes_header": includes_header, "length_value": val,
        "field_start": field_start, "field_end": field_end,
        "frame_end": frame_end, "payload": payload,
        "score": score, "reasons": reasons,
    }


def infer_login_frame(buf: bytes) -> Optional[dict]:
    """Find the first signature occurrence and work out the header shape
    (padding/size/endianness/length-meaning) that makes the bytes right
    after it delimit a single XML payload."""
    hits = find_signature_occurrences(buf)
    if not hits:
        return None
    offset, sig = hits[0]
    sub = buf[offset:]
    best = None
    for pad in _PAD_CANDIDATES:
        for size in _SIZE_CANDIDATES:
            for endian in _ENDIAN_CANDIDATES:
                for includes_header in (False, True):
                    cand = _score_login_candidate(sub, len(sig), pad, size, endian, includes_header)
                    if cand and (best is None or cand["score"] > best["score"]):
                        best = cand
    if best is None:
        return None
    best["signature"] = sig
    best["signature_offset"] = offset
    best["frame_end_absolute"] = offset + best["frame_end"]
    return best


def infer_command_frames(buf: bytes) -> Optional[dict]:
    """Walk the rest of a stream (after the login frame) as a repeating
    sequence of frames, trying both "signature repeats every frame" and
    "no signature, just length + XML" hypotheses. Returns the hypothesis
    that consumes the buffer exactly and cleanly, covering the most frames."""
    if not buf:
        return None
    best = None
    for sig_opt in (None,) + SIGNATURES:
        for pad in _PAD_CANDIDATES:
            for size in _SIZE_CANDIDATES:
                for endian in _ENDIAN_CANDIDATES:
                    for includes_header in (False, True):
                        result = _walk_frames(buf, sig_opt, pad, size, endian, includes_header)
                        if result is None:
                            continue
                        frames, consumed = result
                        if consumed != len(buf) or not frames:
                            continue  # only accept configs that account for every byte
                        score = len(frames) * 10
                        cfg = {
                            "signature": sig_opt, "pad": pad, "length_size": size,
                            "endian": endian, "includes_header": includes_header,
                            "frames": frames, "score": score,
                        }
                        if best is None or score > best["score"]:
                            best = cfg
    return best


def _walk_frames(buf: bytes, sig_opt: Optional[bytes], pad: int, size: int,
                  endian: str, includes_header: bool):
    frames = []
    pos = 0
    while pos < len(buf):
        frame_start = pos
        cur = pos
        if sig_opt is not None:
            if buf[cur:cur + len(sig_opt)] != sig_opt:
                return None
            cur += len(sig_opt)
        field_start = cur + pad
        field_end = field_start + size
        if field_end > len(buf):
            return None
        val = int.from_bytes(buf[field_start:field_end], "big" if endian == ">" else "little")
        header_total = field_end - frame_start
        payload_len = (val - header_total) if includes_header else val
        if payload_len < 0:
            return None
        frame_end = field_end + payload_len
        if frame_end > len(buf):
            return None
        payload = buf[field_end:frame_end]
        if not _looks_like_xml_start(payload):
            return None
        frames.append({"start": frame_start, "field_start": field_start,
                        "length_value": val, "payload": payload})
        pos = frame_end
    return frames, pos


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _preview(payload: bytes, n: int = 300) -> str:
    return payload[:n].decode("utf-8", "replace")


def _fmt_login_cfg(login: dict) -> str:
    sig = login["signature"]
    lines = [
        f"  offset 0{'':<10}: signature   {len(sig)} bytes  {sig!r}",
        f"  offset {len(sig)+login['pad']}{'':<8}: length      {login['length_size']} bytes  "
        f"{'big' if login['endian']=='>' else 'little'}-endian uint  "
        f"(GUESS: {'includes header length' if login['includes_header'] else 'payload length only'})",
        f"  offset {login['field_end']}{'':<8}: payload     {login['frame_end']-login['field_end']} bytes  XML",
    ]
    return "\n".join(lines)


def _fmt_command_cfg(cmd: dict) -> str:
    sig = cmd["signature"]
    sig_len = len(sig) if sig else 0
    lines = []
    if sig:
        lines.append(f"  offset 0{'':<10}: signature   {sig_len} bytes  {sig!r}  "
                     f"(GUESS: repeats on every command frame)")
    else:
        lines.append("  (GUESS: no signature on command frames -- only the login carries one)")
    lines.append(
        f"  offset {sig_len+cmd['pad']}{'':<8}: length      {cmd['length_size']} bytes  "
        f"{'big' if cmd['endian']=='>' else 'little'}-endian uint  "
        f"(GUESS: {'includes header length' if cmd['includes_header'] else 'payload length only'})")
    lines.append(f"  offset {sig_len+cmd['pad']+cmd['length_size']}{'':<8}: payload     XML")
    return "\n".join(lines)


def analyse_direction(label: str, buf: bytes, report: dict, max_preview: int) -> None:
    print(f"\n--- {label}: {len(buf)} bytes reassembled ---")
    if not buf:
        print("  (nothing)")
        return

    hits = find_signature_occurrences(buf)
    print(f"  signature occurrences: {len(hits)}"
          + (f" (first at offset {hits[0][0]}, {hits[0][1]!r})" if hits else ""))

    dir_report: dict = {"bytes": len(buf), "signature_hits": hits}

    login = infer_login_frame(buf)
    if login is None:
        print("  could not identify a login frame (no signature found, or no "
              "header shape produced an XML-looking payload).")
        dir_report["login_frame"] = None
    else:
        print(f"  INFERRED login-frame header shape (score {login['score']}, "
              f"reasons: {'; '.join(login['reasons'])}):")
        print(_fmt_login_cfg(login))
        print(f"  first decoded XML payload ({label}, login frame):")
        print("    " + _preview(login["payload"], max_preview).replace("\n", "\n    "))
        dir_report["login_frame"] = {
            "signature": login["signature"].decode("ascii"),
            "signature_offset": login["signature_offset"],
            "pad": login["pad"], "length_size": login["length_size"],
            "endian": login["endian"], "includes_header": login["includes_header"],
            "length_value": login["length_value"],
            "payload_preview": _preview(login["payload"], max_preview),
        }

        remainder = buf[login["frame_end_absolute"]:]
        cmd = infer_command_frames(remainder)
        if cmd is None:
            if remainder:
                print(f"  {len(remainder)} bytes remain after the login frame; "
                      "no consistent command-frame framing was found for them.")
            dir_report["command_frames"] = None
        else:
            print(f"\n  INFERRED command-frame header shape "
                  f"({len(cmd['frames'])} frame(s) recovered):")
            print(_fmt_command_cfg(cmd))
            for i, fr in enumerate(cmd["frames"][:3]):
                print(f"  first decoded XML payload ({label}, command frame {i}):")
                print("    " + _preview(fr["payload"], max_preview).replace("\n", "\n    "))
            dir_report["command_frames"] = {
                "signature": cmd["signature"].decode("ascii") if cmd["signature"] else None,
                "pad": cmd["pad"], "length_size": cmd["length_size"],
                "endian": cmd["endian"], "includes_header": cmd["includes_header"],
                "frame_count": len(cmd["frames"]),
                "payload_previews": [_preview(fr["payload"], max_preview) for fr in cmd["frames"][:5]],
            }

    report.setdefault("directions", {})[label] = dir_report


def print_proposed_header(report: dict) -> None:
    print("\n" + "=" * 72)
    print("PROPOSED NasdHeader layout -- INFERRED from this capture, not FIRMWARE")
    print("or CONFIRMED per docs/protocol-map.md's confidence marks. Treat as a")
    print("lead: verify against additional captures before hard-coding offsets.")
    print("=" * 72)
    any_login = False
    for label, d in report.get("directions", {}).items():
        login = d.get("login_frame")
        if not login:
            continue
        any_login = True
        print(f"\nLogin frame (first seen in direction: {label}):")
        sig = login["signature"].encode("ascii")
        pad, size, endian = login["pad"], login["length_size"], login["endian"]
        print(f"  offset 0            : signature   {len(sig)} bytes  b{login['signature']!r}")
        print(f"  offset {len(sig)+pad:<3}         : length      {size} bytes  "
              f"{'big' if endian=='>' else 'little'}-endian uint  "
              f"(GUESS: {'includes header' if login['includes_header'] else 'payload length only'})")
        print(f"  offset {len(sig)+pad+size:<3}         : payload     XML, variable length")
        cmd = d.get("command_frames")
        if cmd:
            print(f"\nCommand frame(s) after login (direction: {label}):")
            if cmd["signature"]:
                csig = cmd["signature"].encode("ascii")
                print(f"  offset 0            : signature   {len(csig)} bytes  b{cmd['signature']!r}  "
                      f"(GUESS: signature repeats every frame)")
                base = len(csig)
            else:
                print("  (GUESS: no signature -- only the login frame carries one)")
                base = 0
            print(f"  offset {base+cmd['pad']:<3}         : length      {cmd['length_size']} bytes  "
                  f"{'big' if cmd['endian']=='>' else 'little'}-endian uint  "
                  f"(GUESS: {'includes header' if cmd['includes_header'] else 'payload length only'})")
            print(f"  offset {base+cmd['pad']+cmd['length_size']:<3}         : payload     XML, variable length")
    if not any_login:
        print("\nNo signature/framing could be inferred from this capture -- either")
        print("the filter matched no port-" + str(args.port) + " traffic, or none of the header shapes")
        print("tried produced an XML-looking payload. See docs/protocol-map.md and")
        print("fall back to a manual 'Follow TCP Stream' in Wireshark.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="path to a .pcap or .pcapng capture file")
    ap.add_argument("-o", "--out", help="write a JSON report to this path")
    ap.add_argument("--port", type=int, default=TCP_PORT,
                    help="TCP port to dissect: 5000 = status greeting, "
                         "5001 = commands (default: %(default)s)")
    ap.add_argument("--max-preview", type=int, default=400,
                    help="max characters of each XML payload to print (default 400)")
    args = ap.parse_args(argv)

    try:
        packets = read_capture(args.capture)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Read {len(packets)} link-layer frames from {args.capture}")
    streams = collect_streams(packets, args.port)
    print(f"Found {len(streams)} TCP stream(s) touching port {args.port}")

    report: dict = {"capture": args.capture, "frame_count": len(packets),
                     "stream_count": len(streams), "streams": []}

    if not streams:
        print("\nNothing on port 5000 in this capture -- check the Wireshark filter "
              "used when saving it, or that Dashboard actually connected during "
              "the recording.")
        print_proposed_header(report)
    else:
        for key, st in streams.items():
            client = st["client"]
            server = st["server"]
            print(f"\n=== stream {client} <-> {server} ===")
            stream_report = {
                "client": list(client) if client else None,
                "server": list(server) if server else None,
                "directions": {},
            }
            to_server = reassemble(st["to_server"])
            to_client = reassemble(st["to_client"])
            analyse_direction(f"{client}->{server} (client->server)", to_server,
                               stream_report, args.max_preview)
            analyse_direction(f"{server}->{client} (server->client)", to_client,
                               stream_report, args.max_preview)
            report["streams"].append(stream_report)
            print_proposed_header(stream_report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\nWrote {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
