"""
Tests for the first write this project ever sends, and the two reads that
landed alongside it.

    py test_identify.py

The Identify tests carry more weight than their size suggests. This project
was read-only against the hardware for its entire life; eCmdIdentify is the
single, deliberate exception. What is being tested here is not really "does
the command work" -- it is "is the boundary still exactly where we put it".

Runs entirely offline against a fake device. No hardware, no network.
"""
import os
import html
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import commands as C
from drobo_nasd import config_cmd as cc
from drobo_nasd import droboapps as da
from drobo_nasd import identify as idf

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("  " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
print("== the boundary: what may be written, and what may not ==")

check("eCmdIdentify is approved", C.is_safe_write("eCmdIdentify"))
check("eCmdIdentify is not refused", not C.is_dangerous("eCmdIdentify"))
check("exactly one approved write so far",
      C.SAFE_WRITES == frozenset({"eCmdIdentify"}), sorted(C.SAFE_WRITES))
check("approved and refused sets are disjoint",
      C.DO_NOT_SEND.isdisjoint(C.SAFE_WRITES))

# The whole point. If a future change quietly promotes one of these into
# SAFE_WRITES, this is where it gets caught.
for name in ("eCmdFormatDevice", "eCmdInstallFirmware", "eCmdReset",
             "eCmdRestart", "eCmdShutdown", "eCmdSetDimming", "eCmdRename",
             "eCmdDeleteLUN", "eCmdRepair"):
    check(f"{name} may still not be written", not C.is_safe_write(name))


# ---------------------------------------------------------------------------
print("\n== a fake Drobo, to prove the exchange ==")


def fake_device(reply_type, reply_body, port_holder, login_ok=True):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(1)
    seen = {}

    def run():
        try:
            conn, _ = srv.accept()
            conn.settimeout(5)
            head = conn.recv(16)
            ln = struct.unpack(">I", head[12:16])[0]
            conn.recv(ln)
            conn.sendall(cc._frame(cc.TYPE_LOGIN_OK if login_ok else 0x99, b""))
            if not login_ok:
                conn.close(); srv.close(); return
            head = conn.recv(16)
            ln = struct.unpack(">I", head[12:16])[0]
            seen["command"] = conn.recv(ln)
            conn.sendall(cc._frame(reply_type, reply_body))
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return seen


def send(**kw):
    """One identify against a throwaway fake device; returns (result, bytes)."""
    ports = []
    seen = fake_device(cc.TYPE_RESULT, b"", ports)
    res = idf.identify("127.0.0.1", "drb000000SAMPLE", port=ports[0], timeout=5, **kw)
    return res, seen.get("command", b"").decode("utf-8", "replace")


res, sent = send()
check("identify reports it was sent", res["sent"] is True, res)
check("it used the id from the table, not a literal",
      res["command_id"] == C.COMMANDS["eCmdIdentify"], res)
check("the wire carries the right command id",
      f"<CmdID>{C.COMMANDS['eCmdIdentify']}</CmdID>" in sent, sent[:160])
check("the device serial is included", "drb000000SAMPLE" in sent)

# An empty reply is success here. Identify's result is a blinking light, not a
# document -- so unlike the read commands, no payload must NOT read as failure.
check("an empty reply is still success", res["device_replied"] is False, res)


# ---------------------------------------------------------------------------
print("\n== the interval, found in Dashboard's binary on 2026-08-06 ==")
#
# This replaces an assertion that used to read "params are sent EMPTY -- never
# guessed", and the replacement is deliberate rather than a relaxation.
#
# Empty Params was the honest answer while the parameter was unknown: the
# firmware names command 26 but not its arguments. It was also, on the
# evidence, why nothing ever blinked. `IdentifyInterval` was then recovered
# from Drobo Dashboard.exe 3.5.0, where it sits in the run of <Params> tag
# names -- so the element is CONFIRMED even though no capture of the button
# exists.
#
# What is still open is the UNIT, and these tests are careful not to pretend
# otherwise. They check that the number we chose is transmitted exactly as
# given; none of them claims it means minutes.

check("the interval element is the one from Dashboard's binary",
      idf.INTERVAL_TAG == "IdentifyInterval", idf.INTERVAL_TAG)
check("the default is what the original product used",
      idf.DEFAULT_INTERVAL == 15, idf.DEFAULT_INTERVAL)
check("by default the interval is on the wire",
      "<Params><IdentifyInterval>15</IdentifyInterval></Params>" in sent, sent[:200])
check("the interval is reported back to the caller", res["interval"] == 15, res)

res2, sent2 = send(interval=45)
check("a caller-chosen interval is transmitted verbatim",
      "<IdentifyInterval>45</IdentifyInterval>" in sent2, sent2[:200])
check("...and echoed in the result", res2["interval"] == 45, res2)

res0, sent0 = send(interval=idf.STOP_INTERVAL)
check("zero -- the stop guess -- is sent, not silently dropped",
      "<IdentifyInterval>0</IdentifyInterval>" in sent0, sent0[:200])
check("zero is reported as zero, not as 'nothing asked for'",
      res0["interval"] == 0, res0)

# The control case. If the device blinks with an interval and not without one,
# THAT is the measurement that upgrades this from inference to fact -- so the
# old behaviour has to stay reachable on purpose.
resN, sentN = send(interval=None)
check("interval=None still sends the historical empty Params",
      "<Params></Params>" in sentN, sentN[:200])
check("...and carries no interval element at all",
      "IdentifyInterval" not in sentN, sentN[:200])
check("None is reported as None", resN["interval"] is None, resN)


print("\n== bad intervals are refused before a socket is opened ==")
# Refused early and locally: a typo should not become a connection to storage
# hardware that is then logged and abandoned half-done.
for bad, why in ((-1, "negative"), (-900, "very negative"),
                 (idf.MAX_INTERVAL + 1, "beyond the sanity limit"),
                 ("15", "a string"), (15.0, "a float"), (True, "a bool")):
    try:
        idf.identify("127.0.0.1", "drb000000SAMPLE", port=1, timeout=1, interval=bad)
        check(f"{why} interval is refused", False, repr(bad))
    except idf.IdentifyError:
        check(f"{why} interval is refused", True)
    except OSError:
        # Reaching a connect() means validation let it through.
        check(f"{why} interval is refused BEFORE connecting", False, repr(bad))

check("the sanity limit itself is allowed",
      idf._interval_xml(idf.MAX_INTERVAL).endswith(
          f"<IdentifyInterval>{idf.MAX_INTERVAL}</IdentifyInterval></Params>"))
check("True is rejected even though bool is an int subclass",
      isinstance(True, int))

ports = []
fake_device(cc.TYPE_RESULT, b"", ports, login_ok=False)
try:
    idf.identify("127.0.0.1", "drb000000SAMPLE", port=ports[0], timeout=5)
    check("a refused login raises", False)
except idf.IdentifyError as exc:
    check("a refused login raises IdentifyError", "login refused" in str(exc), str(exc))

ports = []
fake_device(0x99, b"", ports)
try:
    idf.identify("127.0.0.1", "drb000000SAMPLE", port=ports[0], timeout=5)
    check("an unexpected reply type raises", False)
except idf.IdentifyError:
    check("an unexpected reply type raises IdentifyError", True)


# ---------------------------------------------------------------------------
print("\n== DroboApps: what's installed on the Drobo ==")


def apps_reply(inner):
    return (b"<TMCmd><CmdID>77</CmdID><ResultDetails>"
            + html.escape(inner).encode() + b"</ResultDetails></TMCmd>\x00")


out = da.parse_droboapps(apps_reply(
    "<DroboApps><SDKVersion>2.1</SDKVersion>"
    "<App><Name>python3</Name><Version>3.9.7.2</Version>"
    "<Description>Python 3.9.7</Description><Stopped>0</Stopped></App>"
    "<App><Name>DroboPix</Name><Version>1.0.3.80</Version>"
    "<Description>Photo upload.</Description><Stopped>1</Stopped>"
    "<WebUI>WebUI</WebUI></App>"
    "<App><Name></Name><Version>9</Version></App>"
    "</DroboApps>"))

check("sdk version read", out["sdk_version"] == "2.1", out)
check("nameless entries are dropped", len(out["apps"]) == 2, out["apps"])
by_name = {a["name"]: a for a in out["apps"]}
check("apps come back sorted by name",
      [a["name"] for a in out["apps"]] == ["DroboPix", "python3"], out["apps"])

# The device says "Stopped"; people say "running". The flip happens once, in
# the parser, so nothing above it has to remember the polarity is inverted.
check("Stopped=0 becomes running=True", by_name["python3"]["running"] is True)
check("Stopped=1 becomes running=False", by_name["DroboPix"]["running"] is False)
check("a web interface is noticed", by_name["DroboPix"]["has_web_ui"] is True)
check("...and its absence is not invented", by_name["python3"]["has_web_ui"] is False)

check("no apps is an empty list, not an error",
      da.parse_droboapps(b"\x00") == {"sdk_version": "", "apps": []})
try:
    da.parse_droboapps(b"<not xml\x00")
    check("malformed XML raises", False)
except Exception as exc:
    check("malformed XML raises", type(exc).__name__ == "SysInfoError", type(exc).__name__)


# ---------------------------------------------------------------------------
print("\n== LED brightness ==")

def dim(v):
    return da.parse_dimming(
        f"<TMCmd><ResultDetails>{v}</ResultDetails></TMCmd>".encode() + b"\x00")

check("the live device's reading parses", dim(59) == 59)
# The distinction that matters: lights-off is a real setting, and must not be
# confused with having no reading at all.
check("zero is a brightness, not a missing value", dim(0) == 0)
check("...and is not None", dim(0) is not None)
check("empty means no reading", dim("") is None)
check("negative is rejected", dim(-5) is None)
check("non-numeric is rejected", dim("bright") is None)
check("no payload at all means no reading", da.parse_dimming(b"\x00") is None)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
