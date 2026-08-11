"""
Tests for eCmdGetSysInfo -- the Drobo's own temperature and uptime.

The fixture is the real reply from a live 5N on 2026-07-26, byte for byte
apart from the serial. The important thing it captures is the *escaping*: the
device puts an entity-escaped XML document inside <ResultDetails>, not real
child elements, and reading it as though it were plain XML is exactly the
mistake that made this command look broken for weeks.

    py test_sysinfo.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import sysinfo

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("  " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(label)


# The real thing, as it comes off the wire.
REAL = b"""<?xml version="1.0" encoding="UTF-8"?>

<TMCmd>
    <CmdID>61</CmdID>
    <Result>8589934592</Result>
    <ResultDetails>&lt;?xml version=&quot;1.0&quot; encoding=&quot;UTF-8&quot;?&gt;

&lt;SysInfo&gt;
    &lt;UpTime&gt;1521745&lt;/UpTime&gt;
    &lt;Temperature&gt;36&lt;/Temperature&gt;
&lt;/SysInfo&gt;
</ResultDetails>
</TMCmd>\x00"""

print("== the real device reply ==")
got = sysinfo.parse_sysinfo(REAL)
check("temperature read through the escaping", got["temperature_c"] == 36, got)
check("uptime read through the escaping", got["uptime_seconds"] == 1521745, got)

print("\n== the other shape, in case firmware ever changes its mind ==")
PLAIN = (b"<TMCmd><CmdID>61</CmdID><ResultDetails><SysInfo>"
         b"<UpTime>42</UpTime><Temperature>31</Temperature>"
         b"</SysInfo></ResultDetails></TMCmd>\x00")
plain = sysinfo.parse_sysinfo(PLAIN)
check("unescaped children parse too", plain == {"temperature_c": 31,
                                                "uptime_seconds": 42}, plain)

print("\n== a value we don't believe is not shown at all ==")
for bogus in (b"0", b"-5", b"250", b"not-a-number"):
    doc = (b"<TMCmd><ResultDetails><SysInfo><Temperature>" + bogus +
           b"</Temperature></SysInfo></ResultDetails></TMCmd>\x00")
    out = sysinfo.parse_sysinfo(doc)
    check(f"temperature {bogus.decode()!r} is rejected, not displayed",
          out["temperature_c"] is None, out)

check("a believable low reading IS kept",
      sysinfo.parse_sysinfo(
          b"<TMCmd><ResultDetails><SysInfo><Temperature>1</Temperature>"
          b"</SysInfo></ResultDetails></TMCmd>\x00")["temperature_c"] == 1)
check("a believable high reading IS kept",
      sysinfo.parse_sysinfo(
          b"<TMCmd><ResultDetails><SysInfo><Temperature>95</Temperature>"
          b"</SysInfo></ResultDetails></TMCmd>\x00")["temperature_c"] == 95)

print("\n== missing data is 'unknown', never zero ==")
none_doc = b"<TMCmd><ResultDetails><SysInfo></SysInfo></ResultDetails></TMCmd>\x00"
check("no fields -> both None, no exception",
      sysinfo.parse_sysinfo(none_doc) == {"temperature_c": None,
                                          "uptime_seconds": None},
      sysinfo.parse_sysinfo(none_doc))
check("empty payload -> both None",
      sysinfo.parse_sysinfo(b"\x00") == {"temperature_c": None,
                                         "uptime_seconds": None})
check("temperature can arrive without uptime",
      sysinfo.parse_sysinfo(
          b"<TMCmd><ResultDetails><SysInfo><Temperature>40</Temperature>"
          b"</SysInfo></ResultDetails></TMCmd>\x00")
      == {"temperature_c": 40, "uptime_seconds": None})
check("empty <ResultDetails> is not an error",
      sysinfo.parse_sysinfo(
          b"<TMCmd><ResultDetails></ResultDetails></TMCmd>\x00")
      == {"temperature_c": None, "uptime_seconds": None})

print("\n== malformed input raises, it doesn't return nonsense ==")
for bad, label in ((b"<not xml\x00", "malformed outer XML"),
                   (b"<TMCmd><CmdID>61</CmdID></TMCmd>\x00", "no ResultDetails"),
                   (b"<TMCmd><ResultDetails>&lt;SysInfo&gt;&lt;/Sys&gt;"
                    b"</ResultDetails></TMCmd>\x00", "malformed inner XML")):
    try:
        sysinfo.parse_sysinfo(bad)
        check(f"{label} raises", False)
    except sysinfo.SysInfoError:
        check(f"{label} raises SysInfoError", True)

print("\n== safety ==")
from drobo_nasd import commands as C
check("eCmdGetSysInfo has a confirmed id", C.COMMANDS.get("eCmdGetSysInfo") == 61,
      C.COMMANDS.get("eCmdGetSysInfo"))
check("eCmdGetSysInfo is not on the do-not-send list",
      not C.is_dangerous("eCmdGetSysInfo"))
check("SysInfoError is catchable as ConfigError (callers already handle that)",
      issubclass(sysinfo.SysInfoError,
                 __import__("drobo_nasd.config_cmd", fromlist=["x"]).ConfigError))


# ---------------------------------------------------------------------------
# eCmdGetPerformance -- added 2026-07-26, after the re-sweep found this command
# answers on a plain login. The first sweep filed it as "empty" only because
# its whole payload arrives as an entity-escaped document, which the tool
# counted as zero fields. Same blind spot that hid temperature.
# ---------------------------------------------------------------------------
print("\n== eCmdGetPerformance ==")

import html as _html  # noqa: E402

_PERF_INNER = ("<Performance><Iops>12</Iops><ReadThroughout>3456</ReadThroughout>"
               "<WriteThroughout>789</WriteThroughout><TierIOps>4</TierIOps></Performance>")


def _perf_result(inner: str) -> bytes:
    return (b"<TMCmd><CmdID>62</CmdID><ResultDetails>"
            + _html.escape(inner).encode() + b"</ResultDetails></TMCmd>\x00")


p = sysinfo.parse_performance(_perf_result(_PERF_INNER))
check("read throughput parsed", p["read_mb_per_s"] == 3456, p)
check("write throughput parsed", p["write_mb_per_s"] == 789, p)
check("iops parsed", p["iops"] == 12, p)
check("tier iops parsed", p["tier_iops"] == 4, p)

# An idle array really does send all zeros. That is a READING, not a gap --
# the driver relies on being able to tell those apart.
z = sysinfo.parse_performance(_perf_result(
    "<Performance><Iops>0</Iops><ReadThroughout>0</ReadThroughout>"
    "<WriteThroughout>0</WriteThroughout><TierIOps>0</TierIOps></Performance>"))
check("an idle array's zeros parse as 0, not None",
      z["iops"] == 0 and z["read_mb_per_s"] == 0, z)

# The device also sometimes answers with real child elements rather than an
# escaped document; both shapes must work.
plain = sysinfo.parse_performance(
    b"<TMCmd><ResultDetails><Performance><Iops>7</Iops>"
    b"<ReadThroughout>10</ReadThroughout></Performance></ResultDetails></TMCmd>\x00")
check("unescaped shape parses too", plain["iops"] == 7 and plain["read_mb_per_s"] == 10, plain)

missing = sysinfo.parse_performance(_perf_result("<Performance><Iops>5</Iops></Performance>"))
check("a missing field is None, not 0", missing["write_mb_per_s"] is None, missing)
check("...while the field that IS present still reads", missing["iops"] == 5, missing)

neg = sysinfo.parse_performance(_perf_result(
    "<Performance><ReadThroughout>-1</ReadThroughout></Performance>"))
check("a negative rate is rejected rather than shown", neg["read_mb_per_s"] is None, neg)

check("empty payload gives all-None rather than raising",
      sysinfo.parse_performance(b"\x00")["iops"] is None)
try:
    sysinfo.parse_performance(b"<not xml\x00")
    check("malformed XML raises", False)
except sysinfo.SysInfoError:
    check("malformed XML raises SysInfoError", True)

check("eCmdGetPerformance is resolved by NAME and is not a write",
      C.COMMANDS["eCmdGetPerformance"] == 62
      and "eCmdGetPerformance" not in C.DO_NOT_SEND)

print("\n== eCmdGetDemoModeInfo: is this unit showing fabricated capacity? ==")
# One of the seven commands that answers with real data. Obscure, and worth
# having for exactly one reason: a Drobo left in shop-display mode reports
# capacity it does not have, and every number this project shows would be a
# lie with nothing else to catch it.
from drobo_nasd import droboapps as _apps


def _demo(inner: str) -> bytes:
    return (f"<Result><ResultDetails>{inner}</ResultDetails></Result>").encode() + b"\x00"


off = _apps.parse_demo_mode(_demo(
    "&lt;FirmwareInfo&gt;<DemoMode>0</DemoMode><ScaleFactor>1</ScaleFactor>&lt;/FirmwareInfo&gt;"))
check("a normal unit reports demo mode off", off["demo_mode"] is False, off)
check("-- with its scale factor", off["scale_factor"] == 1, off)

on = _apps.parse_demo_mode(_demo(
    "&lt;FirmwareInfo&gt;<DemoMode>1</DemoMode><ScaleFactor>16</ScaleFactor>&lt;/FirmwareInfo&gt;"))
check("a shop-display unit reports demo mode ON", on["demo_mode"] is True, on)
check("-- and the multiplier it invents capacity with", on["scale_factor"] == 16, on)

# "Didn't say" is not "off". Defaulting an absent field to False would quietly
# assert a unit is genuine when we simply have not been told.
for payload in (b"", _demo(""), _demo("&lt;FirmwareInfo&gt;&lt;/FirmwareInfo&gt;")):
    got = _apps.parse_demo_mode(payload)
    check(f"{payload[:24]!r} leaves demo mode UNKNOWN, not False",
          got["demo_mode"] is None, got)

odd = _apps.parse_demo_mode(_demo(
    "&lt;FirmwareInfo&gt;<DemoMode>yes</DemoMode>&lt;/FirmwareInfo&gt;"))
check("an unexpected value is unknown rather than coerced to true",
      odd["demo_mode"] is None, odd)
bad_scale = _apps.parse_demo_mode(_demo(
    "&lt;FirmwareInfo&gt;<DemoMode>0</DemoMode><ScaleFactor>lots</ScaleFactor>&lt;/FirmwareInfo&gt;"))
check("a non-numeric scale factor is dropped, not guessed",
      bad_scale["scale_factor"] is None and bad_scale["demo_mode"] is False, bad_scale)

print("\n== eCmdGetDimming: the reading that had no way out ==")
# Parser existed and worked since 2026-07-26; nothing exposed it until
# 2026-07-28. A measurement nobody can see is not a feature.
check("the live device's reading parses", _apps.parse_dimming(_demo("59")) == 59)
check("zero is a real brightness, not 'no reading'", _apps.parse_dimming(_demo("0")) == 0)
check("no reading is None, distinct from zero", _apps.parse_dimming(_demo("")) is None)
check("an empty payload is None", _apps.parse_dimming(b"") is None)
check("a non-numeric reading is None", _apps.parse_dimming(_demo("bright")) is None)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
