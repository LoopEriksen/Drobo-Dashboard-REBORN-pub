"""
Byte framing for the nasd protocol.

Two different things live in this file, and they have very different
confidence levels -- do not conflate them:

1. A generic length-prefixed frame (encode_frame / decode_frame /
   FrameAssembler). protocol-map.md's "Connection model" section quotes the
   daemon's own error strings as evidence that commands are length-framed:

       NetTMCmds(thread %p/from %s): can't get a complete header.
       Size %d, Received %d.

   i.e. *something* declares a size before the payload arrives. A 4-byte
   length prefix ahead of a payload is the simplest thing that produces that
   exact error shape, so it's used here as the working assumption for the
   command/response path -- but the exact width, byte order, and whether the
   length includes itself are still `GUESS`, pending a capture.

2. NasdHeader -- the connection/login handshake header, as guessed before a
   capture existed. **CORRECTION, 2026-07-25:** the real header and login
   shape were captured and confirmed against live hardware -- see
   docs/protocol-map.md, "THE COMMAND PROTOCOL -- SOLVED". None of the
   CANDIDATE constructors below turned out to match it: the confirmed shape
   is a 16-byte header (`DRINETTM` signature + 1-byte type + 1-byte version +
   2-byte flags + big-endian u32 length) followed by a 220-byte login payload
   that is just the device's serial (ESAID) twice, NUL-padded. That confirmed
   framing is implemented directly in `config_cmd.py` (`_frame`,
   `_login_payload`), not through this class. The CANDIDATE constructors here
   are left as-is -- superseded, not deleted -- so `client.NasdClient`'s
   existing tests keep exercising the same code; don't add a new candidate
   pretending to be the confirmed answer, and don't route new work through
   this class expecting it to reach real hardware.

The frame-size assumption in part 1 (a bare 4-byte length prefix, no
signature) is also superseded for the command channel specifically -- the
confirmed frame there always carries the `DRINETTM` signature and type/version
bytes ahead of the length (see above). Whether a bare 4-byte-prefix frame like
this is used anywhere else in the protocol is untested and still a `GUESS`.
"""

from __future__ import annotations

import struct
from typing import Callable, List, Tuple

# ---------------------------------------------------------------------------
# 1. generic length-prefixed frame (command / response path)
# ---------------------------------------------------------------------------


class FramingError(Exception):
    """Raised when a buffer does not (yet) hold one complete frame."""


_LENGTH_STRUCTS = {
    "big": struct.Struct(">I"),
    "little": struct.Struct("<I"),
}


def encode_frame(payload: bytes, *, byteorder: str = "big") -> bytes:
    """4-byte length prefix (payload length only, prefix not included) +
    payload. `byteorder` is a GUESS -- see module docstring."""
    if byteorder not in _LENGTH_STRUCTS:
        raise ValueError(f"byteorder must be 'big' or 'little', got {byteorder!r}")
    return _LENGTH_STRUCTS[byteorder].pack(len(payload)) + payload


def decode_frame(buf: bytes, *, byteorder: str = "big") -> Tuple[bytes, bytes]:
    """
    Decode one frame from the front of `buf`. Returns (payload, remainder).

    Raises FramingError if `buf` does not yet contain a complete frame -- the
    caller should read more bytes from the socket and retry (this is exactly
    the "Size %d, Received %d" situation the daemon logs).
    """
    lstruct = _LENGTH_STRUCTS.get(byteorder)
    if lstruct is None:
        raise ValueError(f"byteorder must be 'big' or 'little', got {byteorder!r}")
    if len(buf) < lstruct.size:
        raise FramingError(f"need {lstruct.size} bytes for the length prefix, have {len(buf)}")
    (length,) = lstruct.unpack(buf[: lstruct.size])
    end = lstruct.size + length
    if len(buf) < end:
        raise FramingError(f"incomplete frame: need {length} bytes, have {len(buf) - lstruct.size}")
    return buf[lstruct.size : end], buf[end:]


class FrameAssembler:
    """
    Buffers bytes arriving off a socket and yields complete frames as they
    become available. This is the streaming-friendly wrapper around
    encode_frame/decode_frame -- a real socket delivers partial reads, and
    this is where that gets absorbed.
    """

    def __init__(self, byteorder: str = "big"):
        self._buf = b""
        self._byteorder = byteorder

    def feed(self, data: bytes) -> List[bytes]:
        self._buf += data
        frames: List[bytes] = []
        while True:
            try:
                payload, rest = decode_frame(self._buf, byteorder=self._byteorder)
            except FramingError:
                break
            frames.append(payload)
            self._buf = rest
        return frames

    def pending_bytes(self) -> int:
        return len(self._buf)


# ---------------------------------------------------------------------------
# 2. NasdHeader -- THE UNKNOWN. Everything below is a CANDIDATE, not fact.
# ---------------------------------------------------------------------------


class NasdHeader:
    """
    The connection/login handshake header. THIS IS THE ONE UNRESOLVED PIECE
    of the whole client library -- see docs/protocol-map.md "Still open".

    Confirmed by firmware string evidence (protocol-map.md "Connection
    model" + "Signatures"):

      - A fixed-size header precedes the login packet and carries a
        signature ("got bad signature" is a distinct failure from "did not
        receive complete header", implying the header is read, then
        validated, as two separate steps).
      - Two signature strings exist side by side in the firmware:
        "DRINASD" and "DRINASD4". `GUESS`: DRINASD4 is current, DRINASD is
        older/legacy.
      - The login packet itself is a *separate* subsequent read ("did not
        receive complete login packet"), so header != login packet.

    NOT confirmed, at all:
      - Byte order, header length, where a length field sits (before/after
        the signature, or absent and implied by a fixed size instead).
      - Whether there's anything else in the header (version, esaID, flags).
      - The login packet's contents (protocol-map.md: PAM-backed auth,
        UCrypto for stored passwords, but "sent in clear over TCP 5000" is
        itself only a `GUESS`).

    TODO(packet-capture): capture a real Dashboard handshake (see
    tools/nasd_probe.py or docs/phase-1-capture-playbook.md), confirm which
    candidate below (if any) matches, delete the others, and rename the
    survivor to drop the "candidate_" prefix. Until that happens, do not add
    a candidate_d/e/... without also adding the evidence for it above.
    """

    SIGNATURE_V4 = b"DRINASD4"
    SIGNATURE_LEGACY = b"DRINASD\x00"  # padded to 8 bytes to match SIGNATURE_V4's width -- GUESS

    # -- candidate encoders --------------------------------------------------
    # Each takes the login/command payload length and returns header bytes to
    # prepend. None of these have been checked against a real Drobo.

    @classmethod
    def candidate_a_signature_then_length(cls, payload_length: int, *, signature: bytes = None) -> bytes:
        """CANDIDATE A: [8-byte signature]['DRINASD4' by default][4-byte
        big-endian payload length]. UNCONFIRMED."""
        sig = signature if signature is not None else cls.SIGNATURE_V4
        return sig + struct.pack(">I", payload_length)

    @classmethod
    def candidate_b_length_then_signature(cls, payload_length: int, *, signature: bytes = None) -> bytes:
        """CANDIDATE B: [4-byte little-endian payload length][8-byte
        signature]. UNCONFIRMED."""
        sig = signature if signature is not None else cls.SIGNATURE_V4
        return struct.pack("<I", payload_length) + sig

    @classmethod
    def candidate_c_legacy_signature(cls, payload_length: int) -> bytes:
        """CANDIDATE C: same shape as A, but with the older 'DRINASD'
        signature instead of 'DRINASD4'. UNCONFIRMED."""
        return cls.candidate_a_signature_then_length(payload_length, signature=cls.SIGNATURE_LEGACY)

    # -- matching decoders ----------------------------------------------------
    # Paired 1:1 with the encoders above, for testing internal consistency
    # only (encode-then-decode round trips). This proves nothing about what a
    # real Drobo would send -- see the class docstring.

    @classmethod
    def parse_candidate_a(cls, header_bytes: bytes) -> Tuple[bytes, int]:
        """Inverse of candidate_a_signature_then_length. Returns (signature, payload_length)."""
        if len(header_bytes) < 12:
            raise FramingError("candidate A header is 12 bytes (8 signature + 4 length)")
        signature, length_bytes = header_bytes[:8], header_bytes[8:12]
        (length,) = struct.unpack(">I", length_bytes)
        return signature, length

    @classmethod
    def parse_candidate_b(cls, header_bytes: bytes) -> Tuple[bytes, int]:
        """Inverse of candidate_b_length_then_signature. Returns (signature, payload_length)."""
        if len(header_bytes) < 12:
            raise FramingError("candidate B header is 12 bytes (4 length + 8 signature)")
        length_bytes, signature = header_bytes[:4], header_bytes[4:12]
        (length,) = struct.unpack("<I", length_bytes)
        return signature, length

    @classmethod
    def parse_candidate_c(cls, header_bytes: bytes) -> Tuple[bytes, int]:
        """Inverse of candidate_c_legacy_signature."""
        return cls.parse_candidate_a(header_bytes)

    # Registry so client.py (or a future capture-driven picker) can iterate
    # candidates instead of hard-coding one. Order is not significance.
    CANDIDATES: dict = {}  # populated below, after the classmethods exist


NasdHeader.CANDIDATES = {
    "a_signature_then_length": (
        NasdHeader.candidate_a_signature_then_length,
        NasdHeader.parse_candidate_a,
    ),
    "b_length_then_signature": (
        NasdHeader.candidate_b_length_then_signature,
        NasdHeader.parse_candidate_b,
    ),
    "c_legacy_signature": (
        NasdHeader.candidate_c_legacy_signature,
        NasdHeader.parse_candidate_c,
    ),
}
