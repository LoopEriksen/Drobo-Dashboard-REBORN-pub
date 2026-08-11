"""
The shapes a Drobo reports.

Every field is a number, string, bool or None -- nothing here needs a library
to serialise, and nothing here knows what an application is. A Drobo has no
concept of an "alert"; that lives with whatever is doing the monitoring.

These are the return types of the readers in this package. If you are writing
your own front end, this is the vocabulary you will be working in.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

# Drive / device states. Deliberately short and stable -- callers switch on
# these strings, so adding one is a breaking change.
STATE_OK = "ok"
STATE_WARNING = "warning"
STATE_FAILED = "failed"
STATE_EMPTY = "empty"
STATE_UNKNOWN = "unknown"

SEV_INFO = "info"
SEV_WARNING = "warning"
SEV_CRITICAL = "critical"


def _gb(n: float) -> int:
    """Convenience for writing test data: gigabytes -> bytes."""
    return int(n * 1000**3)


@dataclass
class DriveSlot:
    bay: int
    present: bool = False
    state: str = STATE_EMPTY
    model: str = ""
    serial: str = ""
    capacity_bytes: int = 0
    temperature_c: float | None = None
    # Free-text detail for the UI, e.g. "SMART: 3 reallocated sectors"
    note: str = ""
    # -- health signals the greeting already sends but we used to discard --
    # mErrorCount. Unambiguous: a count above zero is a genuine problem,
    # unlike most of the pack-level codes below, so this is safe to alert on.
    error_count: int = 0
    # SSDLifeRemaining. Observed as a plain reported value (0 or 100 on the
    # spinning disks in hand) -- None only for bays with no disk in them.
    ssd_life_remaining: int | None = None
    # mDiskFwRev, e.g. "82.00A82". Identity, not a health signal by itself.
    firmware_rev: str = ""
    # RotationalSpeed, raw as reported (observed 0 on one bay, 27 on others
    # of the same array). We don't know the unit or what 0 vs 27 means here,
    # so this is surfaced as-is rather than interpreted.
    rotational_speed: int | None = None
    # mManagedCapacity -- lives per-slot in the XML despite reading like a
    # pack-level figure. Observed as 0 on every bay of a healthy array.
    managed_capacity_bytes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Capacity:
    """
    BeyondRAID reports more than a simple RAID sum, so keep the parts separate
    and let the UI decide what to show.
    """

    raw_bytes: int = 0
    usable_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    protection_bytes: int = 0

    @property
    def used_fraction(self) -> float:
        if self.usable_bytes <= 0:
            return 0.0
        return self.used_bytes / self.usable_bytes

    def to_dict(self) -> dict:
        d = asdict(self)
        d["used_fraction"] = round(self.used_fraction, 4)
        return d


@dataclass
class BatteryInfo:
    """Cache-battery health -- eCmdCacheBattery. A real failure point on these
    units: when it goes, the write cache stops being safe across a power loss."""

    state: str = STATE_UNKNOWN
    charge_pct: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FanInfo:
    rpm: int | None = None
    state: str = STATE_UNKNOWN

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PowerInfo:
    """PSU health -- eCmdGetPowerInfo."""

    state: str = STATE_UNKNOWN
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PerformanceInfo:
    """
    Throughput and IOPS -- eCmdGetPerformance.

    The device sends four bare integers with no units anywhere in the document:
    `ReadThroughout`, `WriteThroughout` (Drobo's own spelling), `Iops` and
    `TierIOps`. Megabytes-per-second is *inferred* from measurement, not stated
    by the device -- see nasd/sysinfo.parse_performance for the evidence.

    These lag heavily. Measured on real hardware, the reading stayed at 0 for
    the first ~24 seconds of sustained load and was still reporting the load
    25 seconds after it stopped. Display them as "recent activity", never as a
    live rate, and never use them to decide whether the array is busy now.

    `measured` says whether these came from the real device at all. A driver
    that never got an answer leaves it False, so a caller can tell "idle" from
    "we don't know" -- which matters here, because a genuinely idle array
    reports zeros that look identical to no reading at all.
    """

    read_mb_per_s: int = 0
    write_mb_per_s: int = 0
    iops: int = 0
    tier_iops: int = 0
    measured: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EventLogEntry:
    """One line from the Drobo's own event log -- eCmdGetEventLogs. More
    authoritative than anything we infer by polling, once the real driver
    can fetch it."""

    ts: float
    severity: str
    message: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts))
        return d


@dataclass
class Volume:
    """
    One volume (Drobo calls them LUNs) carved out of the disk pack.

    This is what shows up in Windows as a drive letter. A Drobo splits its pack
    into volumes -- up to sixteen on a 5N -- and each is presented to the
    computer as a separate disk, which is why a 15 TB array can appear as
    several drives.

    Read straight from the status greeting's `mLUNUpdates`, so it costs no
    extra command. The volume-management commands in the firmware table
    (eCmdCreateLUN and friends) all answer empty on this firmware, and every
    one of them is a write we refuse anyway -- but the device volunteers the
    *state* of its volumes unprompted, which is the half worth having.

    `partition_type` is a partition SCHEME, and 3 means GPT. Two independent
    lines of evidence, arrived at 2026-07-28:

      - drobo-utils (https://github.com/petersilva/drobo-utils, by Peter
        Silva; Python 3 fork: https://github.com/n3gwg/drobo-utils) is an
        independent reverse-engineering of Drobo's SCSI/USB "Drobo Management
        Protocol Rev A.0", dating from 2008. It maps scheme 0/1/2/3 to
        None/MBR/APM/GPT. Different transport from ours -- they drive
        direct-attached Drobos over SCSI/USB, we speak nasd over TCP to a
        network Drobo -- but Drobo had no reason to renumber its own
        constants between its own interfaces.
      - Arithmetic settles it independently anyway: this volume reports a
        64 TiB ceiling and MBR cannot address past 2 TiB. It could not be
        anything else.

    The enumeration is credited here because it was ADOPTED, and adopted only
    because that second, unrelated argument agreed with it -- not because
    drobo-utils was assumed to be right. Two of its OTHER tables were tried
    against this project's own readings and REJECTED: its unit-status
    bitfield decodes this array's healthy DNASStatus=6 as "Red alert | Yellow
    alert", and its partition-format table has no entry for the 64 our device
    reports for `partition_format` below. Worth saying plainly: this table was
    tested, not copied on faith, and it happened to survive the test while its
    neighbours didn't. See docs/patents.md for the full account, and
    CREDITS.md for the project-wide summary.

    `partition_format` is still a raw number for exactly that reason: the
    device sends 64, drobo-utils' format table (NO FORMAT / NTFS / HFS / EXT3
    / FAT32) has no 64 in it, so that mapping does NOT transfer and no guess
    is made here -- the same rule the status codes follow.
    """

    lun: int = 0
    unique_id: str = ""
    name: str = ""
    max_size_bytes: int = 0
    used_bytes: int = 0
    partition_count: int | None = None
    partition_type: int | None = None
    partition_format: int | None = None
    share_state: int | None = None

    #: Partition schemes, per drobo-utils' reading of Drobo's own SCSI
    #: management protocol. Only 3 is corroborated independently (see the class
    #: docstring); the rest are carried for completeness and would want the
    #: same second opinion before being trusted.
    _SCHEMES = {0: "No partitions", 1: "MBR", 2: "APM", 3: "GPT"}

    @property
    def partition_scheme(self) -> str:
        """
        The partition scheme in words, or "" when we cannot say.

        Empty string rather than "Unknown" for an unrecognised number: a UI
        should then show the raw value it already has, instead of replacing a
        fact we hold with a word that means nothing.
        """
        if self.partition_type is None:
            return ""
        return self._SCHEMES.get(self.partition_type, "")

    def to_dict(self) -> dict:
        # asdict() only sees fields, and partition_scheme is a property -- so
        # it has to be added explicitly or every front-end would keep rendering
        # the bare number we now know the meaning of.
        return {**asdict(self), "partition_scheme": self.partition_scheme}


@dataclass
class PackHealth:
    """
    Raw pack/device-level health values straight from the greeting -- fields
    we used to throw away entirely. Deliberately modeled as observed
    values with honest labels, NOT as invented enums: we know exactly what a
    healthy array reports, and not much else.

    BASELINE observed on one healthy Drobo 5N, firmware 4.3.1, 2026-07-24
    (this is NOT a specification, just what one working unit reported):
        disk_pack_status=0, dnas_status=6, device_status=98304, status_ex=0,
        relayout_count=0, double_degraded_count=0,
        real_time_integrity_checking=0.

    relayout_count and double_degraded_count ARE unambiguous -- they are
    counts, so above-zero is meaningfully bad (a rebuild in progress, or a
    double-degraded event) regardless of what the opaque status codes mean.
    """

    disk_pack_status: int | None = None
    dnas_status: int | None = None
    # mStatus at the device level -- NOT the same field as a drive slot's own
    # mStatus (that one means "is this bay populated", see esatm.py).
    device_status: int | None = None
    status_ex: int | None = None
    relayout_count: int = 0
    double_degraded_count: int = 0
    real_time_integrity_checking: int | None = None
    # The device's OWN capacity alert levels (mRedThreshold/mYellowThreshold),
    # as fractions (9500 -> 0.95). Prefer these over our hardcoded config
    # defaults when present -- see monitor._evaluate.
    red_threshold_fraction: float | None = None
    yellow_threshold_fraction: float | None = None
    total_capacity_unprotected_bytes: int = 0
    use_unprotected_capacity: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeviceInfo:
    name: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    ip: str = ""
    uptime_seconds: int | None = None

    # Chassis temperature, from eCmdGetSysInfo on the command port. This is a
    # single number for the whole box, not per drive -- the per-drive
    # mTemperature the status greeting carries is 0 on every slot of a 5N.
    #
    # It carries its own timestamp because the device answers this command
    # only sometimes (see nasd/sysinfo.py). A reading from four minutes ago,
    # shown WITH its age, is more useful than a blank; a reading from four
    # hours ago shown as if it were current is worse than a blank. Anything
    # displaying this must show the age too.
    temperature_c: int | None = None
    temperature_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Snapshot:
    """One complete reading of the Drobo, at one moment."""

    taken_at: float = field(default_factory=time.time)
    reachable: bool = True
    source: str = "unknown"  # which driver produced this
    device: DeviceInfo = field(default_factory=DeviceInfo)
    capacity: Capacity = field(default_factory=Capacity)
    drives: list[DriveSlot] = field(default_factory=list)
    volumes: list["Volume"] = field(default_factory=list)
    max_volumes: int | None = None
    battery: BatteryInfo = field(default_factory=BatteryInfo)
    fan: FanInfo = field(default_factory=FanInfo)
    power: PowerInfo = field(default_factory=PowerInfo)
    performance: PerformanceInfo = field(default_factory=PerformanceInfo)
    event_log: list[EventLogEntry] = field(default_factory=list)
    pack_health: PackHealth = field(default_factory=PackHealth)
    error: str = ""

    @property
    def overall_state(self) -> str:
        if not self.reachable:
            return STATE_UNKNOWN
        states = [d.state for d in self.drives if d.present]
        if STATE_FAILED in states:
            return STATE_FAILED
        if STATE_WARNING in states:
            return STATE_WARNING
        return STATE_OK if states else STATE_UNKNOWN

    def to_dict(self) -> dict:
        return {
            "taken_at": self.taken_at,
            "taken_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.taken_at)),
            "reachable": self.reachable,
            "source": self.source,
            "overall_state": self.overall_state,
            "device": self.device.to_dict(),
            "capacity": self.capacity.to_dict(),
            "drives": [d.to_dict() for d in self.drives],
            "volumes": [v.to_dict() for v in self.volumes],
            "max_volumes": self.max_volumes,
            "battery": self.battery.to_dict(),
            "fan": self.fan.to_dict(),
            "power": self.power.to_dict(),
            "performance": self.performance.to_dict(),
            "event_log": [e.to_dict() for e in self.event_log],
            "pack_health": self.pack_health.to_dict(),
            "error": self.error,
        }
