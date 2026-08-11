"""
Reading the Drobo's status.

Confirmed against a live Drobo 5N (firmware 4.3.1) on 2026-07-24: the moment a
client connects to nasd on TCP 5000, the device pushes one framed message,
unprompted and with no login required, carrying its complete status as an
<ESATMUpdate> XML document. That greeting is everything the dashboard needs, so
the driver polls simply by reconnecting and reading it.

Frame layout (confirmed on the wire):

    offset  bytes  meaning
    0       8      signature "DRINASD\\0"
    8       1      major version (observed 0x01)
    9       1      minor version / message type (observed 0x01)
    10      2      flags / reserved (observed 0x0000)
    12      4      big-endian uint32: payload length in bytes
    16      N      UTF-8 XML payload, NUL-terminated (the NUL is inside N)

See docs/protocol-map.md for the field-by-field mapping.
"""

from __future__ import annotations

import re
import socket
import struct
import xml.etree.ElementTree as ET

from .models import (
    STATE_EMPTY,
    STATE_OK,
    STATE_WARNING,
    Capacity,
    DeviceInfo,
    DriveSlot,
    PackHealth,
    Snapshot,
    Volume,
)

SIGNATURE = b"DRINASD\x00"
HEADER_LEN = 16
DEFAULT_PORT = 5000
MAX_PAYLOAD = 8 * 1024 * 1024  # a greeting is ~7 KB; this is a sane ceiling

# mDiskState -> our state. 16 = healthy, confirmed across five good drives.
# 0 with an empty slot means no disk. Any other non-zero value is unknown, so
# it maps to WARNING rather than being silently treated as OK.
_DISK_STATE = {16: STATE_OK, 0: STATE_EMPTY}
_EMPTY_SLOT_STATUS = 128  # mStatus for a bay with no disk


class FrameError(Exception):
    """The bytes on the wire were not a well-formed DRINASD frame."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf


def read_frame(sock: socket.socket) -> bytes:
    """Read one DRINASD frame and return its raw XML payload (NUL included)."""
    head = _recv_exact(sock, HEADER_LEN)
    if len(head) < HEADER_LEN:
        raise FrameError(f"short header: got {len(head)} of {HEADER_LEN} bytes")
    if head[:8] != SIGNATURE:
        raise FrameError(f"bad signature: {head[:8]!r}")
    length = struct.unpack(">I", head[12:16])[0]
    if length > MAX_PAYLOAD:
        raise FrameError(f"implausible payload length {length}")
    payload = _recv_exact(sock, length)
    if len(payload) < length:
        raise FrameError(f"short payload: got {len(payload)} of {length} bytes")
    return payload


def fetch_greeting(host: str, port: int = DEFAULT_PORT, timeout: float = 8.0) -> bytes:
    """Connect and read the unprompted status greeting. Read-only."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        return read_frame(sock)
    finally:
        sock.close()


def _int(el, tag: str, default: int = 0) -> int:
    node = el.find(tag)
    if node is None or not node.text:
        return default
    try:
        return int(node.text)
    except ValueError:
        return default


def _text(el, tag: str, default: str = "") -> str:
    node = el.find(tag)
    return node.text if node is not None and node.text else default


def _parse_volumes(root) -> list[Volume]:
    """
    The volumes, read out of the greeting's `mLUNUpdates`.

    Each volume lives in its own numbered container (`<n0>`, `<n1>`, ...),
    exactly like the drive bays do. Costs no extra command -- the device
    volunteers this alongside everything else.

    Deliberately forgiving: a container missing a field yields None for it
    rather than 0, because a volume that reports no partition count is not a
    volume with zero partitions. Same rule as the pack status codes.
    """
    updates = root.find("mLUNUpdates")
    if updates is None:
        return []

    out: list[Volume] = []
    for node in updates:
        # Only numbered containers; anything else is not a volume.
        if not re.fullmatch(r"n\d+", node.tag or ""):
            continue
        out.append(Volume(
            lun=_int_or_none(node, "mLUN") or 0,
            unique_id=_text(node, "mUniqueLUNID"),
            # Drobo leaves the name empty on a single-volume pack; the front
            # end decides what to call it rather than us inventing one here.
            name=_text(node, "mLUNName"),
            max_size_bytes=_int_or_none(node, "mMaximumLUNSize") or 0,
            used_bytes=_int_or_none(node, "mUsedCapacityOS") or 0,
            partition_count=_int_or_none(node, "mPartitionCount"),
            partition_type=_int_or_none(node, "mPartitionType"),
            partition_format=_int_or_none(node, "mPartitionFormat"),
            share_state=_int_or_none(node, "mShareState"),
        ))
    out.sort(key=lambda v: v.lun)
    return out


def _int_or_none(el, tag: str) -> int | None:
    """
    Like _int, but absent means None rather than 0.

    This matters for the opaque pack-status codes. Their healthy baseline
    includes non-zero values (dnas_status=6, device_status=98304), so a missing
    field defaulting to 0 would look like a *deviation* and fire a false alarm
    claiming the code "changed" -- when in truth we simply never received it.
    None is the honest answer, and monitor._evaluate skips it.
    """
    node = el.find(tag)
    if node is None or not node.text:
        return None
    try:
        return int(node.text)
    except ValueError:
        return None


def _threshold_fraction(el, tag: str) -> float | None:
    """
    mRedThreshold/mYellowThreshold are the Drobo's own capacity alert levels,
    reported as hundredths of a percent (9500 == 95.00%). Observed on one
    healthy 5N: red=9500 (95%), yellow=8500 (85%) -- the exact values this
    project already used as hardcoded config defaults, so preferring the
    device's own numbers when present (see monitor._evaluate) is a faithful
    improvement, not a guess.
    """
    node = el.find(tag)
    if node is None or not node.text:
        return None
    try:
        return int(node.text) / 10000.0
    except ValueError:
        return None


# How many bays each model physically has. A real Drobo 5N reports
# mSlotCountExp = 6 even though it has five bays -- the sixth is a firmware
# placeholder that is always empty. Confirmed against a live 5N on firmware
# 4.3.1-8.126.117497 (see docs/captures/drobo-greeting-00.xml, slot n5:
# state=0, capacity=0, no make).
_BAY_COUNT = {
    "Drobo 5N": 5,
}


def _trim_phantom_slots(drives: list[DriveSlot], model: str) -> None:
    """
    Drop placeholder slots the firmware reports beyond the real bay count.

    Deliberately conservative: only *trailing* and only *empty* slots are
    removed. If the device ever reports a disk in a slot we didn't expect, it
    stays visible -- over-reporting a bay is a cosmetic bug, hiding a real drive
    would be a serious one.
    """
    expected = _BAY_COUNT.get(model)
    if not expected:
        return
    while len(drives) > expected and not drives[-1].present:
        drives.pop()


def _parse_root(payload: bytes) -> ET.Element:
    """Strip the frame's trailing NUL (length includes it) and parse the XML."""
    return ET.fromstring(payload.rstrip(b"\x00").rstrip())


def parse_identity(payload: bytes) -> dict:
    """
    Pull just the identity fields out of a greeting, without building a full
    Snapshot.

    mESAID is the permanent hardware serial -- the best thing to pin an
    "expected device" to, since it survives IP changes, renames, and even a
    repack (unlike DNASDiskPackId, which identifies the disk pack and changes
    if the drives are moved to different hardware). Discovery uses this to
    confirm "this is definitely (or definitely not) my Drobo" before trusting
    anything else the device says -- see nasd/discovery.py.
    """
    root = _parse_root(payload)
    return {
        "esa_id": _text(root, "mESAID"),
        "disk_pack_id": _text(root, "DNASDiskPackId"),
        "name": _text(root, "mDroboName") or _text(root, "mName"),
        "model": _text(root, "mModel"),
        "firmware": _text(root, "mVersion"),
    }


def parse_esatm(payload: bytes) -> Snapshot:
    """Turn an <ESATMUpdate> payload into a Snapshot."""
    # The payload is NUL-terminated and the length includes the NUL, so strip
    # trailing NULs/whitespace before the XML parser sees it.
    root = _parse_root(payload)

    device = DeviceInfo(
        name=_text(root, "mDroboName") or _text(root, "mName"),
        # The model comes from the device itself (mModel) rather than being
        # hardcoded here -- _BAY_COUNT below still keys off the model string,
        # so an unrecognized/missing mModel just means _trim_phantom_slots is
        # a no-op (conservative: never hides a bay we don't have a count for).
        model=_text(root, "mModel"),
        # Top-level mESAID is the permanent hardware serial. NOTE: per-slot
        # mSerial (below) is each *disk's* serial -- a different thing, don't
        # confuse them.
        serial=_text(root, "mESAID"),
        firmware=_text(root, "mVersion"),
    )

    drives: list[DriveSlot] = []
    raw = 0
    for slot in root.iter():
        if slot.find("mSlotNumber") is None:
            continue
        bay = _int(slot, "mSlotNumber") + 1  # device is 0-indexed; UI is 1-indexed
        status = _int(slot, "mStatus")
        phys = _int(slot, "mPhysicalCapacity")
        if status == _EMPTY_SLOT_STATUS or phys <= 0:
            drives.append(DriveSlot(bay=bay, present=False, state=STATE_EMPTY))
            continue
        raw += phys
        disk_state = _int(slot, "mDiskState")
        state = _DISK_STATE.get(disk_state, STATE_WARNING)
        temp = _int(slot, "mTemperature")
        drives.append(
            DriveSlot(
                bay=bay,
                present=True,
                state=state,
                model=(_text(slot, "mMake") or "disk").strip(),
                serial=_text(slot, "mSerial"),
                capacity_bytes=phys,
                # The 5N greeting reports 0 for these drives -- treat 0 as "not
                # reported" rather than a real and alarming 0 C.
                temperature_c=float(temp) if temp > 0 else None,
                note="" if state == STATE_OK else f"disk state code {disk_state}",
                # Surfaced; not previously parsed. mErrorCount is the one
                # unambiguous per-drive signal here -- above zero is bad.
                error_count=_int(slot, "mErrorCount"),
                ssd_life_remaining=_int(slot, "SSDLifeRemaining"),
                firmware_rev=_text(slot, "mDiskFwRev"),
                rotational_speed=_int(slot, "RotationalSpeed"),
                managed_capacity_bytes=_int(slot, "mManagedCapacity"),
            )
        )

    drives.sort(key=lambda d: d.bay)
    _trim_phantom_slots(drives, device.model)

    capacity = Capacity(
        raw_bytes=raw,
        usable_bytes=_int(root, "mTotalCapacityProtected"),
        used_bytes=_int(root, "mUsedCapacityProtected"),
        free_bytes=_int(root, "mFreeCapacityProtected"),
    )

    # Pack/device-level health values the greeting already sends. See
    # PackHealth's docstring for the healthy-baseline values and the
    # discipline of not inventing meanings for the opaque status codes.
    pack_health = PackHealth(
        # Opaque codes: absent must be None, not 0 -- see _int_or_none.
        disk_pack_status=_int_or_none(root, "mDiskPackStatus"),
        dnas_status=_int_or_none(root, "DNASStatus"),
        device_status=_int_or_none(root, "mStatus"),
        status_ex=_int_or_none(root, "mStatusEx"),
        real_time_integrity_checking=_int_or_none(root, "mRealTimeIntegrityChecking"),
        # Counts are genuinely counts: absent sensibly means zero, and the
        # alert rules compare with > 0, so 0 is both safe and truthful here.
        relayout_count=_int(root, "mRelayoutCount"),
        double_degraded_count=_int(root, "mDoubleDegradedCnt"),
        red_threshold_fraction=_threshold_fraction(root, "mRedThreshold"),
        yellow_threshold_fraction=_threshold_fraction(root, "mYellowThreshold"),
        total_capacity_unprotected_bytes=_int(root, "mTotalCapacityUnprotected"),
        use_unprotected_capacity=_int(root, "mUseUnprotectedCapacity") != 0,
    )

    return Snapshot(
        reachable=True,
        source="drobo5n",
        device=device,
        capacity=capacity,
        drives=drives,
        volumes=_parse_volumes(root),
        # How many volumes this pack could hold. 16 on a 5N.
        max_volumes=_int_or_none(root, "mMaxLUNs"),
        pack_health=pack_health,
    )


def read_snapshot(host: str, port: int = DEFAULT_PORT, timeout: float = 8.0) -> Snapshot:
    """Connect to a real Drobo 5N and return a parsed Snapshot. Read-only."""
    return parse_esatm(fetch_greeting(host, port, timeout))
