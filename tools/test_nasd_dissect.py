#!/usr/bin/env python3
"""
test_nasd_dissect.py - self-test for nasd_dissect.py.

There is no real Drobo to capture from in CI, so this builds a small,
in-memory packet capture with a framing we CHOOSE and therefore KNOW:

    DRINASD4  +  4-byte big-endian length (payload only)  +  XML body

...repeated for a couple of "frames", split across multiple out-of-order TCP
segments to also exercise sequence-number reassembly, then written out as
both a classic pcap file and a pcapng file. The dissector is then run
against each and must recover:

  - the signature, its offset, and that it is DRINASD4
  - the length field's offset/size/endianness
  - the exact XML payload of each frame

This proves the tool works without ever touching hardware. It does not
prove the real Drobo's framing matches this -- that still needs a genuine
capture (see docs/protocol-map.md, "Still open").

Run:
    py tools/test_nasd_dissect.py
"""

from __future__ import annotations

import socket
import struct
import sys
import tempfile
import os

import nasd_dissect as dissect

# Documentation-range addresses (RFC 5737 TEST-NET-1), deliberately not a
# private 192.168.x range: fixtures written in that form are indistinguishable
# from addresses copied out of a real capture of a real network, and this
# project's rule is that no tracked file carries a real one. A documentation
# range makes the synthetic-ness self-evident. These are synthetic endpoints
# in a synthetic capture; any pair of addresses works.
CLIENT_IP = "203.0.113.23"
SERVER_IP = "203.0.113.50"
CLIENT_PORT = 51000
SERVER_PORT = 5000

XML_FRAME_1 = b'<?xml version="1.0"?><DRINETTM><Command>10</Command><Params/></DRINETTM>'
XML_FRAME_2 = b'<?xml version="1.0"?><DRINETTM><Command>85</Command><Params/></DRINETTM>'

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL: {message}")
    else:
        print(f"  ok:   {message}")


# ---------------------------------------------------------------------------
# Build the KNOWN framing: signature + 4-byte BE length + XML, per frame.
# ---------------------------------------------------------------------------
def build_known_stream() -> bytes:
    out = bytearray()
    for xml in (XML_FRAME_1, XML_FRAME_2):
        out += dissect.SIG4
        out += struct.pack(">I", len(xml))
        out += xml
    return bytes(out)


# ---------------------------------------------------------------------------
# Minimal Ethernet + IPv4 + TCP frame builders (checksums left as 0 --
# nasd_dissect.py never validates them, it only reads the fields it needs).
# ---------------------------------------------------------------------------
def eth_frame(ip_packet: bytes) -> bytes:
    dst_mac = bytes.fromhex("001122334455")
    src_mac = bytes.fromhex("665544332211")
    return dst_mac + src_mac + struct.pack(">H", 0x0800) + ip_packet


def ipv4_packet(src_ip: str, dst_ip: str, tcp_segment: bytes) -> bytes:
    total_len = 20 + len(tcp_segment)
    header = struct.pack(">BBHHHBBH", (4 << 4) | 5, 0, total_len, 0, 0, 64, 6, 0)
    header += socket.inet_aton(src_ip) + socket.inet_aton(dst_ip)
    return header + tcp_segment


def tcp_segment(sport: int, dport: int, seq: int, flags: int, payload: bytes) -> bytes:
    header = struct.pack(">HHIIBBHHH", sport, dport, seq, 0, (5 << 4), flags, 65535, 0, 0)
    return header + payload


PSH_ACK = 0x18


def build_frames_for_chunks(chunks: list[tuple[int, bytes]]) -> list[bytes]:
    """chunks: list of (seq, payload_bytes) -- built out of arrival order on
    purpose, to prove reassembly sorts by sequence number, not by packet
    order in the file."""
    frames = []
    for seq, payload in chunks:
        seg = tcp_segment(CLIENT_PORT, SERVER_PORT, seq, PSH_ACK, payload)
        pkt = ipv4_packet(CLIENT_IP, SERVER_IP, seg)
        frames.append(eth_frame(pkt))
    return frames


# ---------------------------------------------------------------------------
# Classic pcap writer
# ---------------------------------------------------------------------------
def write_pcap_classic(path: str, link_frames: list[bytes]) -> None:
    with open(path, "wb") as fh:
        fh.write(struct.pack(">IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for i, frame in enumerate(link_frames):
            fh.write(struct.pack(">IIII", 1_700_000_000 + i, 0, len(frame), len(frame)))
            fh.write(frame)


# ---------------------------------------------------------------------------
# pcapng writer
# ---------------------------------------------------------------------------
#
# NOTE ON ENDIANNESS -- this is why this file exists in two flavours.
#
# These builders were originally big-endian only, and that hid a real bug for
# weeks. pcapng records its byte order per section, and the dissector was
# reading every block TYPE as big-endian regardless. The section header block
# survived that because 0x0A0D0D0A is a byte-palindrome, and block LENGTHS were
# read correctly, so the parser walked the whole file happily and simply never
# recognised a packet block -- reporting "Read 0 link-layer frames", which looks
# exactly like an empty capture rather than a broken parser.
#
# Every capture tool on Windows writes LITTLE-endian, so the tool failed on
# every real file while these tests stayed green. So: both orders are built and
# both are run, and the little-endian case is the one that matters.
#
def _pcapng_block(block_type: int, body: bytes, endian: str = ">") -> bytes:
    total_len = 4 + 4 + len(body) + 4
    return (struct.pack(endian + "II", block_type, total_len) + body
            + struct.pack(endian + "I", total_len))


def _shb(endian: str = ">") -> bytes:
    # The byte-order magic is the same NUMBER either way -- it is the bytes on
    # disk that differ, which is the whole point of it.
    body = struct.pack(endian + "IHHq", 0x1A2B3C4D, 1, 0, -1)
    return _pcapng_block(0x0A0D0D0A, body, endian)


def _idb(linktype: int = 1, snaplen: int = 65535, endian: str = ">") -> bytes:
    body = struct.pack(endian + "HHI", linktype, 0, snaplen)
    return _pcapng_block(0x00000001, body, endian)


def _epb(iface_id: int, frame: bytes, endian: str = ">") -> bytes:
    pad = (-len(frame)) % 4
    padded = frame + b"\x00" * pad
    body = (struct.pack(endian + "IIIII", iface_id, 0, 0, len(frame), len(frame))
            + padded)
    return _pcapng_block(0x00000006, body, endian)


def write_pcapng(path: str, link_frames: list[bytes], endian: str = ">") -> None:
    with open(path, "wb") as fh:
        fh.write(_shb(endian))
        fh.write(_idb(endian=endian))
        for frame in link_frames:
            fh.write(_epb(0, frame, endian))


# ---------------------------------------------------------------------------
# The test itself
# ---------------------------------------------------------------------------
def run_against(path: str, label: str) -> None:
    print(f"\n=== dissecting {label} ({path}) ===")
    packets = dissect.read_capture(path)
    check(len(packets) > 0, f"{label}: capture file parsed into >0 link-layer frames")

    streams = dissect.collect_streams(packets)
    check(len(streams) == 1, f"{label}: exactly one TCP stream recovered (got {len(streams)})")
    if not streams:
        return
    st = next(iter(streams.values()))
    check(st["client"] == (CLIENT_IP, CLIENT_PORT), f"{label}: client endpoint identified correctly")
    check(st["server"] == (SERVER_IP, SERVER_PORT), f"{label}: server endpoint identified correctly")

    buf = dissect.reassemble(st["to_server"])
    expected = build_known_stream()
    check(buf == expected, f"{label}: reassembled client->server stream matches the known bytes exactly")

    login = dissect.infer_login_frame(buf)
    check(login is not None, f"{label}: a login frame was inferred at all")
    if login is None:
        return
    check(login["signature"] == dissect.SIG4, f"{label}: recovered signature is DRINASD4")
    check(login["signature_offset"] == 0, f"{label}: signature located at offset 0")
    check(login["pad"] == 0, f"{label}: length field placed immediately after the signature (no padding)")
    check(login["length_size"] == 4, f"{label}: length field recovered as 4 bytes")
    check(login["endian"] == ">", f"{label}: length field recovered as big-endian")
    check(login["includes_header"] is False, f"{label}: length recovered as payload-only (excludes header)")
    check(login["payload"] == XML_FRAME_1, f"{label}: first frame's XML payload recovered exactly")

    remainder = buf[login["frame_end_absolute"]:]
    cmd = dissect.infer_command_frames(remainder)
    check(cmd is not None, f"{label}: command-frame region after the login frame was decoded")
    if cmd is None:
        return
    check(cmd["signature"] == dissect.SIG4,
          f"{label}: command frame(s) recovered as also carrying DRINASD4 (repeat-signature framing)")
    check(len(cmd["frames"]) == 1, f"{label}: exactly one command frame recovered (got {len(cmd['frames'])})")
    if cmd["frames"]:
        check(cmd["frames"][0]["payload"] == XML_FRAME_2,
              f"{label}: second frame's XML payload recovered exactly")


def main() -> int:
    known_stream = build_known_stream()
    # Split into three chunks and deliberately list them out of arrival
    # order, so the dissector has to sort by TCP sequence number to get the
    # bytes back in the right order.
    split_a = len(dissect.SIG4) + 4 + 5          # partway into frame 1's XML
    split_b = len(known_stream) - 12             # partway into frame 2's XML
    chunk1 = known_stream[0:split_a]
    chunk2 = known_stream[split_a:split_b]
    chunk3 = known_stream[split_b:]
    base_seq = 5000
    chunks = [
        (base_seq + split_a, chunk2),   # out of order on purpose
        (base_seq, chunk1),
        (base_seq + split_b, chunk3),
    ]
    link_frames = build_frames_for_chunks(chunks)

    tmp_dir = tempfile.mkdtemp(prefix="nasd_dissect_test_")
    pcap_path = os.path.join(tmp_dir, "known.pcap")
    pcapng_path = os.path.join(tmp_dir, "known.pcapng")
    pcapng_le_path = os.path.join(tmp_dir, "known-little-endian.pcapng")
    write_pcap_classic(pcap_path, link_frames)
    write_pcapng(pcapng_path, link_frames, endian=">")
    write_pcapng(pcapng_le_path, link_frames, endian="<")

    print(f"Synthesized {len(known_stream)}-byte known stream, split into "
          f"{len(chunks)} out-of-order TCP segments.")
    print(f"Temp captures written to {tmp_dir}")

    run_against(pcap_path, "classic pcap")
    run_against(pcapng_path, "pcapng (big-endian)")
    # The one that actually matters: every capture tool on Windows writes
    # little-endian, and this case failed silently -- reading zero packets --
    # until 2026-07-26. Without it the suite passes while the tool is useless
    # on every real file.
    run_against(pcapng_le_path, "pcapng (little-endian, as real tools write)")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    # Make sure `import nasd_dissect` finds the sibling module regardless of
    # the working directory this is invoked from.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
