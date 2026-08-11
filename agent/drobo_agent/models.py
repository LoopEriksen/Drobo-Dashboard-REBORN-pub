"""
Shapes that belong to the AGENT rather than to a Drobo.

The device shapes -- Snapshot, DriveSlot, Capacity and the rest -- moved into
the SDK (`drobo_nasd.models`) when it was split out, because they describe a
Drobo and a Drobo library should own its own return types.

They are re-exported here so that every existing import keeps working, and so
there is exactly ONE definition of each. Re-export, never redefine: two copies
of Snapshot that drift apart would be a genuinely nasty bug, since both sides
would look correct in isolation.

What stays here is what a Drobo knows nothing about. An alert is a judgement
an application makes; the device just reports numbers.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from drobo_nasd.models import (  # noqa: F401  (re-exported on purpose)
    SEV_CRITICAL,
    SEV_INFO,
    SEV_WARNING,
    STATE_EMPTY,
    STATE_FAILED,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_WARNING,
    BatteryInfo,
    Capacity,
    DeviceInfo,
    DriveSlot,
    EventLogEntry,
    FanInfo,
    PackHealth,
    PerformanceInfo,
    PowerInfo,
    Snapshot,
    Volume,
    _gb,
)


@dataclass
class Alert:
    """
    Something the user should know about. Alerts are sticky: they stay active
    until the condition clears, so the apps can show a list of current problems
    rather than a stream of one-off messages.
    """

    key: str  # stable id, e.g. "drive-failed-bay3" -- used for dedupe
    severity: str
    message: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    cleared_at: float | None = None

    @property
    def active(self) -> bool:
        return self.cleared_at is None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["active"] = self.active
        return d
