"""
Offline tests for drobosync.py.

The honest situation this suite encodes: every DroboSync reply this project has
ever observed was EMPTY, because the owner has one Drobo and the feature needs
two. So the tests that matter most are about behaving correctly when there is
nothing to report -- and about not pretending to know a schema nobody here has
seen.

    py test_drobosync.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import commands, drobosync

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def result(details_inner: str) -> bytes:
    return (f"<Result><ResultDetails>{details_inner}</ResultDetails></Result>").encode() + b"\x00"


print("\n== the reply we have actually seen: empty ==")
# Swept against the live 5N on 2026-07-26. All four read commands answered
# empty. That is CORRECT for a device with no sync partner, and must not be
# reported as a failure -- a red error for a perfectly healthy machine is worse
# than saying nothing.
for payload in (b"", b"\x00", result(""), b"   \x00"):
    parsed = drobosync.parse_sync_reply(payload)
    check(f"{payload!r} parses as not-configured", parsed["configured"] is False, parsed)
    check(f"{payload!r} raises nothing", isinstance(parsed, dict))

empty = drobosync.parse_sync_reply(b"")
check("an empty reply carries no invented fields", empty["fields"] == {}, empty)
check("-- and no raw document", empty["raw_xml"] == "", empty)

print("\n== a populated reply, whose real shape nobody here has seen ==")
# So the parser stays GENERIC on purpose: it reports the leaf elements it found
# rather than mapping them onto field names we would be guessing at. If this
# ever runs against a real second Drobo, raw_xml is what tells us the truth.
inner = ("&lt;DroboSync&gt;<Enabled>1</Enabled>"
         "<TargetName>Upstairs Drobo</TargetName>"
         "<LastRun>2026-07-20 03:00</LastRun>&lt;/DroboSync&gt;")
populated = drobosync.parse_sync_reply(result(inner))
check("a reply with content is configured", populated["configured"] is True, populated)
check("leaf values are carried under their own tags",
      populated["fields"].get("TargetName") == "Upstairs Drobo", populated["fields"])
check("-- all of them, not a chosen few",
      set(populated["fields"]) >= {"Enabled", "TargetName", "LastRun"}, populated["fields"])

print("\n== escaped nesting is unwrapped, as everywhere else in this firmware ==")
# Reading these as opaque strings is the mistake that made temperature and the
# performance counters look unobtainable for weeks.
nested = drobosync.parse_sync_reply(
    result("&lt;DroboSync&gt;&lt;Enabled&gt;1&lt;/Enabled&gt;&lt;/DroboSync&gt;"))
check("a fully-escaped document is parsed, not treated as text",
      nested["fields"].get("Enabled") == "1", nested)
check("-- and the raw document is kept for whoever sees the first real one",
      "DroboSync" in nested["raw_xml"], nested["raw_xml"])

print("\n== malformed input is refused, not guessed at ==")
try:
    drobosync.parse_sync_reply(b"<Result><broken")
    check("malformed XML raises", False)
except drobosync.DroboSyncError:
    check("malformed XML raises DroboSyncError", True)
check("DroboSyncError is catchable as SysInfoError, so existing handlers work",
      issubclass(drobosync.DroboSyncError, __import__(
          "drobo_nasd.sysinfo", fromlist=["SysInfoError"]).SysInfoError))

print("\n== an unknown read is rejected before any socket is opened ==")
try:
    drobosync.read("10.0.0.50", "drbTEST", what="nonsense")
    check("an unknown read raises", False)
except ValueError as exc:
    check("an unknown read raises ValueError naming the options",
          "settings" in str(exc), str(exc))

print("\n== the four reads we support are the four the firmware has ==")
check("exactly four read commands", len(drobosync.READ_COMMANDS) == 4, drobosync.READ_COMMANDS)
for name in drobosync.READ_COMMANDS.values():
    check(f"{name} has a firmware id", name in commands.COMMANDS, name)
    check(f"{name} is NOT refused (it is a query)", not commands.is_dangerous(name), name)

print("\n== the writes stay refused, permanently ==")
# Triggering a replication run moves real data between two arrays. It is not a
# safe write, it cannot be tested with one Drobo, and it is not going in.
for name in drobosync.WRITE_COMMANDS:
    check(f"{name} is in DO_NOT_SEND", commands.is_dangerous(name), name)
    check(f"{name} is not a safe write", not commands.is_safe_write(name), name)
check("no DroboSync write ever became a SAFE_WRITE",
      not (set(drobosync.WRITE_COMMANDS) & commands.SAFE_WRITES))

print("\n== summary(): what a front-end renders when there is one Drobo ==")
_orig = drobosync.read
_asked: list[str] = []


def _fake_read(host, esa_id, what, port=None, timeout=None):
    _asked.append(what)
    return {"configured": False, "raw_xml": "", "fields": {}, "what": what}


drobosync.read = _fake_read
try:
    s = drobosync.summary("10.0.0.50", "drbTEST")
    check("summary reports not configured", s["configured"] is False, s)
    check("all four reads are represented in the answer",
          set(s["reads"]) == set(drobosync.READ_COMMANDS), s["reads"])

    # Each read costs its own connection, and this device's command port stops
    # answering after roughly ninety connections in an hour. Spending four on a
    # switched-off feature every time a page refreshes would be actively
    # harmful to a box that tires easily -- and with no partner configured
    # there are no logs to fetch by definition.
    check("only settings is actually asked when sync is off", _asked == ["settings"], _asked)
    check("the three skipped reads say they were not asked",
          all(s["reads"][w]["asked"] is False
              for w in ("summary_log", "detailed_log", "log")), s["reads"])
    check("-- while settings records that it WAS asked",
          s["reads"]["settings"]["asked"] is True, s["reads"]["settings"])

    # The note is the whole point of shipping this dormant: explain, don't fail.
    check("it explains WHY rather than showing four empty panels",
          "needs two" in s["note"], s["note"])
    check("it says the work is already done and waiting",
          "built and waiting" in s["note"], s["note"])
    check("it does not read as an error", "error" not in s["note"].lower(), s["note"])
finally:
    drobosync.read = _orig

print("\n== when a partner IS configured, the logs are fetched ==")
_asked.clear()


def _configured_read(host, esa_id, what, port=None, timeout=None):
    _asked.append(what)
    if what == "settings":
        return {"configured": True, "raw_xml": "<DroboSync/>",
                "fields": {"Enabled": "1"}, "what": what}
    return {"configured": False, "raw_xml": "", "fields": {}, "what": what}


drobosync.read = _configured_read
try:
    s = drobosync.summary("10.0.0.50", "drbTEST")
    check("a configured device reports configured", s["configured"] is True, s)
    check("-- and the logs ARE asked for", sorted(_asked) ==
          ["detailed_log", "log", "settings", "summary_log"], _asked)
    check("-- and the explanatory note is dropped, since it no longer applies",
          s["note"] == "", s["note"])
finally:
    drobosync.read = _orig

print("\n== a read that fails is contained, not fatal to the others ==")


def _one_boom(host, esa_id, what, port=None, timeout=None):
    if what == "settings":
        raise drobosync.DroboSyncError("simulated: device stopped answering")
    return {"configured": False, "raw_xml": "", "fields": {}, "what": what}


drobosync.read = _one_boom
try:
    s = drobosync.summary("10.0.0.50", "drbTEST")
    check("a failing read still returns all four entries", len(s["reads"]) == 4, s["reads"])
    check("the failure is recorded against the read it belongs to",
          "error" in s["reads"]["settings"], s["reads"]["settings"])
    check("a failure is not mistaken for 'configured'", s["configured"] is False, s)
finally:
    drobosync.read = _orig

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
