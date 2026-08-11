"""
Offline tests for the real Drobo 5N status path (nasd/esatm.py).

Uses a sanitized capture of an actual <ESATMUpdate> greeting (fake serials, fake
name) so the parser is regression-tested without hardware and without any real
device identity in the repo. No network is touched.

    py test_esatm.py
"""
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd.models import STATE_EMPTY, STATE_OK
from drobo_nasd import esatm

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "esatm-sample.xml")

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


payload = open(FIXTURE, "rb").read()

print("\n== parse a real (sanitized) ESATMUpdate greeting ==")
snap = esatm.parse_esatm(payload)
d = snap.to_dict()
check("reachable", d["reachable"])
check("overall state ok", d["overall_state"] == STATE_OK, d["overall_state"])
check("device name from mDroboName", d["device"]["name"] == "TestDrobo", d["device"]["name"])
check("firmware parsed", d["device"]["firmware"].startswith("4.3.1"), d["device"]["firmware"])
check("model is Drobo 5N", d["device"]["model"] == "Drobo 5N")

present = [dr for dr in d["drives"] if dr["present"]]
check("five drives present", len(present) == 5, len(present))
# A 5N reports mSlotCountExp = 6, but it only has five physical bays: the sixth
# is a firmware placeholder that is always empty. parse_esatm() trims it, so a
# fully-populated 5N shows five bays and no empty one. (This assertion used to
# expect the phantom bay back when we surfaced it.)
check("no phantom sixth bay", sum(1 for dr in d["drives"] if not dr["present"]) == 0,
      [dr["bay"] for dr in d["drives"] if not dr["present"]])
check("exactly five bays reported", len(d["drives"]) == 5, len(d["drives"]))
check("all present drives ok", all(dr["state"] == STATE_OK for dr in present))
check("drive capacity ~3TB", all(2.9e12 < dr["capacity_bytes"] < 3.1e12 for dr in present))
# Per-slot mSerial is the DISK's serial, which is a different field from the
# device's mESAID. Confusing the two was a real bug once, so assert they are
# distinct per drive and disk-shaped, not that they equal the device serial.
check("each disk has its own distinct serial",
      len({dr["serial"] for dr in present}) == len(present),
      [dr["serial"] for dr in present])
check("disk serials are disk-shaped, not the device serial",
      all(dr["serial"].startswith("WD-") for dr in present),
      [dr["serial"] for dr in present])
check("temperature reported as unknown (greeting sends 0)",
      all(dr["temperature_c"] is None for dr in present))

print("\n== health signals the greeting already sends ==")
check("all healthy drives report zero errors",
      all(dr["error_count"] == 0 for dr in present),
      [dr["error_count"] for dr in present])
check("firmware rev parsed per drive",
      all(dr["firmware_rev"] for dr in present),
      [dr["firmware_rev"] for dr in present])
check("ssd_life_remaining parsed per drive",
      [dr["ssd_life_remaining"] for dr in present] == [100, 100, 100, 100, 100],
      [dr["ssd_life_remaining"] for dr in present])
check("rotational_speed parsed per drive (bay 1 spins down to 0, others 27)",
      [dr["rotational_speed"] for dr in present] == [0, 27, 27, 27, 27],
      [dr["rotational_speed"] for dr in present])
check("managed_capacity_bytes parsed per drive (0 on this healthy array)",
      all(dr["managed_capacity_bytes"] == 0 for dr in present),
      [dr["managed_capacity_bytes"] for dr in present])

ph = d["pack_health"]
check("disk_pack_status matches the fixture", ph["disk_pack_status"] == 0, ph["disk_pack_status"])
check("dnas_status matches the fixture", ph["dnas_status"] == 6, ph["dnas_status"])
check("device_status matches the fixture (mStatus)", ph["device_status"] == 98304, ph["device_status"])
check("status_ex matches the fixture", ph["status_ex"] == 0, ph["status_ex"])
check("relayout_count zero on a healthy array", ph["relayout_count"] == 0, ph["relayout_count"])
check("double_degraded_count zero on a healthy array", ph["double_degraded_count"] == 0, ph["double_degraded_count"])
check("real_time_integrity_checking matches the fixture",
      ph["real_time_integrity_checking"] == 0, ph["real_time_integrity_checking"])
check("red_threshold_fraction is 0.95 (device's own 95% level)",
      ph["red_threshold_fraction"] == 0.95, ph["red_threshold_fraction"])
check("yellow_threshold_fraction is 0.85 (device's own 85% level)",
      ph["yellow_threshold_fraction"] == 0.85, ph["yellow_threshold_fraction"])
check("total_capacity_unprotected_bytes matches the fixture (0)",
      ph["total_capacity_unprotected_bytes"] == 0, ph["total_capacity_unprotected_bytes"])
check("use_unprotected_capacity is false on the fixture",
      ph["use_unprotected_capacity"] is False, ph["use_unprotected_capacity"])

c = d["capacity"]
check("usable ~8.76 TB", 8.5e12 < c["usable_bytes"] < 9.0e12, c["usable_bytes"])
check("used < free (array nearly empty)", c["used_bytes"] < c["free_bytes"])
check("raw is sum of the pack (~15 TB)", 14e12 < c["raw_bytes"] < 16e12, c["raw_bytes"])
check("used_fraction between 0 and 1", 0 < c["used_fraction"] < 1, c["used_fraction"])

print("\n== absent pack-status fields must read as unknown, not zero ==")
# Regression: these codes were parsed with a default of 0. Because the healthy
# baseline includes non-zero values (dnas_status=6, device_status=98304), a
# device that omitted a field would have looked like it CHANGED, firing a false
# alarm. Absent must be None so the alert rule skips it.
import re as _re

_stripped = payload.rstrip(b"\x00")
for _tag in (b"mDiskPackStatus", b"DNASStatus", b"mStatusEx",
             b"mRealTimeIntegrityChecking"):
    _stripped = _re.sub(b"<" + _tag + b">[^<]*</" + _tag + b">", b"", _stripped)

_partial = esatm.parse_esatm(_stripped + b"\x00")
_ph = _partial.pack_health
check("absent disk_pack_status is None, not 0", _ph.disk_pack_status is None,
      _ph.disk_pack_status)
check("absent dnas_status is None, not 0", _ph.dnas_status is None, _ph.dnas_status)
check("absent status_ex is None, not 0", _ph.status_ex is None, _ph.status_ex)
check("absent real_time_integrity_checking is None",
      _ph.real_time_integrity_checking is None, _ph.real_time_integrity_checking)
check("a field that IS present still parses", _ph.device_status == 98304,
      _ph.device_status)

# The ALERT consequence of this -- that absent fields raise no false
# "changed from baseline" alarm -- is an agent concern rather than a protocol
# one, so it lives in agent/test_agent.py under "absent pack-status fields
# raise no false alarm". This suite stops at "absent parses as None", which is
# the part the library is responsible for.
#
# Worth knowing: when this moved out during the SDK split, the comment claiming
# it lived there was written before it did, and the check was briefly lost
# altogether. Restored 2026-07-27.

print("\n== frame reading over a real socket ==")
frame = esatm.SIGNATURE + b"\x01\x01\x00\x00" + struct.pack(">I", len(payload)) + payload
a, b = socket.socketpair()
threading.Thread(target=lambda: (b.sendall(frame), b.close()), daemon=True).start()
got = esatm.read_frame(a)
a.close()
check("read_frame returns the exact payload", got == payload, len(got))

print("\n== a bad signature is rejected ==")
a, b = socket.socketpair()
threading.Thread(target=lambda: (b.sendall(b"NOTDROBO" + b"\x00" * 8), b.close()), daemon=True).start()
try:
    esatm.read_frame(a)
    check("bad signature raises", False)
except esatm.FrameError:
    check("bad signature raises FrameError", True)
finally:
    a.close()


print("\n== volumes, read from the greeting at no extra cost ==")
# The pack is carved into volumes (Drobo calls them LUNs); each becomes a drive
# letter in Windows. Every volume-MANAGEMENT command in the firmware table
# answers empty on this firmware -- but the device volunteers the STATE of its
# volumes in the ordinary status greeting, which is the half worth having and
# costs no extra connection to a device that tires easily.
check("the real greeting carries one volume", len(snap.volumes) == 1, len(snap.volumes))
check("the pack could hold sixteen", snap.max_volumes == 16, snap.max_volumes)
_v = snap.volumes[0]
check("volume number read", _v.lun == 0, _v.lun)
check("unique id read", _v.unique_id == "1000", _v.unique_id)
check("max size read (64 TiB)", _v.max_size_bytes == 70368744177664, _v.max_size_bytes)
check("partition count read", _v.partition_count == 2, _v.partition_count)
check("partition type carried raw", _v.partition_type == 3, _v.partition_type)
# ...and now also decoded. Scheme 3 is GPT, on two independent grounds:
# drobo-utils (Peter Silva's reverse-engineering of Drobo's SCSI management
# protocol) says so, and MBR cannot address the 64 TiB ceiling this volume
# reports. See models.Volume for the full credit, including what got rejected.
check("partition scheme 3 is decoded as GPT", _v.partition_scheme == "GPT", _v.partition_scheme)
check("the decode reaches front-ends via to_dict",
      _v.to_dict().get("partition_scheme") == "GPT", _v.to_dict().get("partition_scheme"))
check("the raw number is still carried alongside the word",
      _v.to_dict()["partition_type"] == 3, _v.to_dict().get("partition_type"))

# The format number stays raw. drobo-utils' format table (NO FORMAT/NTFS/HFS/
# EXT3/FAT32) contains no 64, so that mapping does NOT transfer -- and a
# neighbouring table being right is not evidence for guessing this one.
check("partition format carried raw", _v.partition_format == 64, _v.partition_format)

# An unrecognised scheme yields "" rather than "Unknown", so a UI falls back to
# showing the raw number it already has instead of a word that means nothing.
import dataclasses as _dc
check("an unknown scheme is empty, not 'Unknown'",
      _dc.replace(_v, partition_type=99).partition_scheme == "")
check("an absent scheme is empty too",
      _dc.replace(_v, partition_type=None).partition_scheme == "")
check("scheme 1 is MBR", _dc.replace(_v, partition_type=1).partition_scheme == "MBR")
check("an unnamed volume stays unnamed rather than being invented",
      _v.name == "", repr(_v.name))

# Absent must not read as zero -- a volume reporting no partition count is not
# a volume with zero partitions.
import xml.etree.ElementTree as _ET
_bare = esatm._parse_volumes(
    _ET.fromstring("<r><mLUNUpdates><n0><mLUN>0</mLUN></n0></mLUNUpdates></r>"))
check("a missing field is None, not 0", _bare[0].partition_count is None, _bare[0].partition_count)
check("a greeting with no mLUNUpdates gives no volumes",
      esatm._parse_volumes(_ET.fromstring("<r/>")) == [])

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
