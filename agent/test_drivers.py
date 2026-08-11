"""
Tests for Drobo5NDriver -- specifically _read_temperature(), the one part of
the real driver that has actually run against a live 5N (see drivers.py's
docstring on it, 2026-07-26).

Runs entirely offline: nasd.sysinfo.get_sysinfo_and_performance is monkeypatched to a fake
that never opens a socket. Covers the three things that make this method
worth pinning down:

  * the rate limit (sysinfo_seconds) -- a second call inside the window must
    not reach the device at all
  * a device failure leaves the last-known-good reading (and its original
    timestamp) intact rather than blanking it
  * the derived uptime in read_snapshot() keeps counting between reads
    without asking the device again

    py test_drivers.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from drobo_agent import drivers
from drobo_agent.models import DeviceInfo, Snapshot
from drobo_nasd import config_cmd
from drobo_nasd import esatm as nasd_esatm
from drobo_nasd import sysinfo as nasd_sysinfo

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("  " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(label)


HOST = "10.0.0.5"
ESA_ID = "drb000000SAMPLE"

_orig_get_sysinfo_and_performance = nasd_sysinfo.get_sysinfo_and_performance
_orig_read_snapshot = nasd_esatm.read_snapshot


def fake_ok(calls, temperature_c, uptime_seconds):
    def _fake(host, esa_id, port=None, timeout=8.0):
        calls.append((host, esa_id))
        return {"temperature_c": temperature_c, "uptime_seconds": uptime_seconds}
    return _fake


print("== default rate limit is two minutes, as documented ==")
d_default = drivers.Drobo5NDriver({"host": HOST})
check("sysinfo_seconds defaults to 120.0", d_default.sysinfo_seconds == 120.0,
      d_default.sysinfo_seconds)
check("no reading yet means temperature_c is None, not 0", d_default._temp_c is None)
check("no reading yet means uptime is None, not 0", d_default._uptime is None)

print("\n== rate limit: a second call inside the window never reaches the device ==")
calls = []
nasd_sysinfo.get_sysinfo_and_performance = fake_ok(calls, temperature_c=36, uptime_seconds=1000)
try:
    d = drivers.Drobo5NDriver({"host": HOST, "sysinfo_seconds": 100000})
    d._known_esa_id = ESA_ID
    d._read_temperature(HOST)
    check("first call reaches the device", len(calls) == 1, calls)
    check("temperature cached from that call", d._temp_c == 36, d._temp_c)
    check("uptime cached from that call", d._uptime == 1000, d._uptime)
    first_temp_at = d._temp_at
    check("a timestamp was stamped", first_temp_at is not None)

    d._read_temperature(HOST)
    d._read_temperature(HOST)
    check("further calls inside sysinfo_seconds do not reach the device",
          len(calls) == 1, calls)
    check("cached temperature is unchanged", d._temp_c == 36, d._temp_c)
    check("cached timestamp is unchanged (no fresh read happened)",
          d._temp_at == first_temp_at, (d._temp_at, first_temp_at))
finally:
    nasd_sysinfo.get_sysinfo_and_performance = _orig_get_sysinfo_and_performance

print("\n== rate limit: once the window elapses, a fresh read is allowed ==")
calls = []
nasd_sysinfo.get_sysinfo_and_performance = fake_ok(calls, temperature_c=40, uptime_seconds=2000)
try:
    d = drivers.Drobo5NDriver({"host": HOST, "sysinfo_seconds": 0.05})
    d._known_esa_id = ESA_ID
    d._read_temperature(HOST)
    check("first call reaches the device", len(calls) == 1, calls)
    time.sleep(0.08)
    d._read_temperature(HOST)
    check("a call after the window elapsed reaches the device again",
          len(calls) == 2, calls)
    check("temperature refreshed to the new value", d._temp_c == 40, d._temp_c)
finally:
    nasd_sysinfo.get_sysinfo_and_performance = _orig_get_sysinfo_and_performance

print("\n== no esa_id yet: the command port needs a serial we don't have ==")
calls = []
nasd_sysinfo.get_sysinfo_and_performance = fake_ok(calls, temperature_c=99, uptime_seconds=1)
try:
    d = drivers.Drobo5NDriver({"host": HOST, "sysinfo_seconds": 0.01})
    # _known_esa_id left blank -- exactly the state before a status read
    # ever succeeds.
    check("starts blank", d._known_esa_id == "", d._known_esa_id)
    d._read_temperature(HOST)
    check("no esa_id means no attempt at all", calls == [], calls)
    check("temperature stays None", d._temp_c is None, d._temp_c)
finally:
    nasd_sysinfo.get_sysinfo_and_performance = _orig_get_sysinfo_and_performance

print("\n== last-known-good survives a device failure, with its original timestamp ==")
calls = []
nasd_sysinfo.get_sysinfo_and_performance = fake_ok(calls, temperature_c=42, uptime_seconds=5000)
try:
    d = drivers.Drobo5NDriver({"host": HOST, "sysinfo_seconds": 0.05})
    d._known_esa_id = ESA_ID
    d._read_temperature(HOST)
    check("first good read cached", d._temp_c == 42, d._temp_c)
    good_temp_at = d._temp_at
    time.sleep(0.08)

    def _unreachable(host, esa_id, port=None, timeout=8.0):
        calls.append((host, esa_id))
        raise OSError("device unreachable (simulated)")

    nasd_sysinfo.get_sysinfo_and_performance = _unreachable
    d._read_temperature(HOST)
    check("a failed read still counts as an attempt (rate limit still applies)",
          len(calls) == 2, calls)
    check("last known good temperature is kept, not blanked", d._temp_c == 42, d._temp_c)
    check("its original timestamp is kept too -- this reading really is that old",
          d._temp_at == good_temp_at, (d._temp_at, good_temp_at))
    check("uptime is kept too", d._uptime == 5000, d._uptime)

    # ConfigError is the other declared failure mode (get_sysinfo_and_performance raises
    # SysInfoError, a ConfigError subclass, e.g. on a bad login/reply) and
    # must behave identically -- neither wipes the cache.
    def _refused(host, esa_id, port=None, timeout=8.0):
        calls.append((host, esa_id))
        raise config_cmd.ConfigError("command port refused (simulated)")

    nasd_sysinfo.get_sysinfo_and_performance = _refused
    time.sleep(0.08)
    d._read_temperature(HOST)
    check("ConfigError also leaves the cached temperature intact", d._temp_c == 42, d._temp_c)
    check("ConfigError also leaves the cached timestamp intact",
          d._temp_at == good_temp_at, (d._temp_at, good_temp_at))
finally:
    nasd_sysinfo.get_sysinfo_and_performance = _orig_get_sysinfo_and_performance

print("\n== _read_temperature never raises on the two declared failure modes ==")
for exc, name in ((OSError("boom"), "OSError"), (config_cmd.ConfigError("boom"), "ConfigError")):
    def _raiser(host, esa_id, port=None, timeout=8.0, _exc=exc):
        raise _exc
    nasd_sysinfo.get_sysinfo_and_performance = _raiser
    try:
        d = drivers.Drobo5NDriver({"host": HOST, "sysinfo_seconds": 0.0})
        d._known_esa_id = ESA_ID
        try:
            d._read_temperature(HOST)
            check(f"{name} is swallowed, not propagated", True)
        except Exception as caught:  # noqa: BLE001 -- this IS the check
            check(f"{name} is swallowed, not propagated", False, caught)
    finally:
        nasd_sysinfo.get_sysinfo_and_performance = _orig_get_sysinfo_and_performance

print("\n== an undeclared exception is NOT silently swallowed ==")
# The docstring promises "never raises" only in the sense of "a device
# failure is not an error" -- the except clause is scoped to
# (OSError, config_cmd.ConfigError). Something else going wrong (a real bug)
# must still surface, not vanish into "no fresh reading".
def _bug(host, esa_id, port=None, timeout=8.0):
    raise ValueError("this is a bug, not a device failure")
nasd_sysinfo.get_sysinfo_and_performance = _bug
try:
    d = drivers.Drobo5NDriver({"host": HOST, "sysinfo_seconds": 0.0})
    d._known_esa_id = ESA_ID
    try:
        d._read_temperature(HOST)
        check("an unexpected exception type propagates rather than being hidden", False)
    except ValueError:
        check("an unexpected exception type propagates rather than being hidden", True)
finally:
    nasd_sysinfo.get_sysinfo_and_performance = _orig_get_sysinfo_and_performance

print("\n== derived uptime keeps counting between reads, without re-asking the device ==")
sysinfo_calls = []
esatm_calls = []
nasd_sysinfo.get_sysinfo_and_performance = fake_ok(sysinfo_calls, temperature_c=33, uptime_seconds=1000)


def fake_read_snapshot(host, port=None, timeout=8.0):
    esatm_calls.append((host, port))
    return Snapshot(reachable=True, source="drobo5n",
                     device=DeviceInfo(name="TestDrobo", model="Drobo 5N", serial=ESA_ID))


nasd_esatm.read_snapshot = fake_read_snapshot
try:
    d = drivers.Drobo5NDriver({"host": HOST, "sysinfo_seconds": 100000})
    d._resolved_host = HOST
    d._resolved_port = 5000
    d._known_esa_id = ESA_ID

    snap1 = d.read_snapshot()
    check("temperature carried onto the snapshot", snap1.device.temperature_c == 33,
          snap1.device.temperature_c)
    check("temperature_at carried onto the snapshot", snap1.device.temperature_at is not None)
    check("uptime derived onto the snapshot", snap1.device.uptime_seconds == 1000
          or snap1.device.uptime_seconds == 1001,  # a whole second may have ticked
          snap1.device.uptime_seconds)
    check("only one sysinfo call happened so far", len(sysinfo_calls) == 1, sysinfo_calls)

    time.sleep(1.1)
    snap2 = d.read_snapshot()
    check("a second status read happened", len(esatm_calls) == 2, esatm_calls)
    check("sysinfo was NOT asked again -- still inside the rate-limit window",
          len(sysinfo_calls) == 1, sysinfo_calls)
    check("but the derived uptime kept counting up between reads",
          snap2.device.uptime_seconds > snap1.device.uptime_seconds,
          (snap1.device.uptime_seconds, snap2.device.uptime_seconds))
    check("the underlying temperature reading is untouched (still the same cached value)",
          snap2.device.temperature_c == 33, snap2.device.temperature_c)
    check("temperature_at is unchanged too -- it really is the same, aging, reading",
          snap2.device.temperature_at == snap1.device.temperature_at,
          (snap1.device.temperature_at, snap2.device.temperature_at))
finally:
    nasd_sysinfo.get_sysinfo_and_performance = _orig_get_sysinfo_and_performance
    nasd_esatm.read_snapshot = _orig_read_snapshot

print("\n== before any successful sysinfo read, the greeting's own uptime is left alone ==")
esatm_calls = []


def fake_read_snapshot_with_uptime(host, port=None, timeout=8.0):
    esatm_calls.append((host, port))
    return Snapshot(reachable=True, source="drobo5n",
                     device=DeviceInfo(name="TestDrobo", model="Drobo 5N", serial=ESA_ID,
                                       uptime_seconds=None))


def _never_answers(host, esa_id, port=None, timeout=8.0):
    raise OSError("simulated: command port never answers")


nasd_sysinfo.get_sysinfo_and_performance = _never_answers
nasd_esatm.read_snapshot = fake_read_snapshot_with_uptime
try:
    d = drivers.Drobo5NDriver({"host": HOST, "sysinfo_seconds": 0.0})
    d._resolved_host = HOST
    d._resolved_port = 5000
    d._known_esa_id = ESA_ID
    snap = d.read_snapshot()
    check("no temperature ever obtained -> temperature_c stays None",
          snap.device.temperature_c is None, snap.device.temperature_c)
    check("no successful sysinfo read -> derived-uptime branch never fires, "
          "greeting's own (None) uptime is left alone",
          snap.device.uptime_seconds is None, snap.device.uptime_seconds)
finally:
    nasd_sysinfo.get_sysinfo_and_performance = _orig_get_sysinfo_and_performance
    nasd_esatm.read_snapshot = _orig_read_snapshot

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
