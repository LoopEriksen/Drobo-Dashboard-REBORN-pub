"""
Offline test suite for the nasd protocol client library
(drobo_nasd/). No hardware needed, nothing touches a socket to a real
Drobo -- this only exercises commands.py, envelope.py, and framing.py.

    py sdk/tests/test_nasd.py

Run this after changing anything under drobo_nasd/.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import client, commands, envelope, framing

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("  " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
print("\n== commands.py: enum completeness ==")
# ---------------------------------------------------------------------------

check("exactly 103 commands", len(commands.COMMANDS) == 103, len(commands.COMMANDS))
check(
    "ids are exactly 0..102, each used once",
    sorted(commands.COMMANDS.values()) == list(range(103)),
)

spot_checks = {
    "eCmdSetAdmin": 0,
    "eCmdGetAdmin": 1,
    "eCmdGetVersion": 7,
    "eCmdDeviceLogin": 10,
    "eCmdRunESACommand": 51,
    "eCmdSendTMCommand": 52,
    "eCmdGetSysInfo": 61,
    "eCmdReadHostBuffer": 70,
    "eCmdWriteHostBuffer": 71,
    "eCmdDoPoll": 75,
    "eCmdGetAllDevicesStatusXML": 85,
    "eCmdRunCommand": 89,
    "eCmdGetNASUsernamePassword": 102,
}
for name, expected in spot_checks.items():
    check(f"spot id {name} == {expected}", commands.COMMANDS.get(name) == expected, commands.COMMANDS.get(name))

check("ID_TO_NAME is the exact inverse", all(commands.ID_TO_NAME[i] == n for n, i in commands.COMMANDS.items()))

print("\n== commands.py: DO-NOT-SEND group ==")
expected_hatches = {
    "eCmdRunESACommand",
    "eCmdRunCommand",
    "eCmdSendTMCommand",
    "eCmdRouter",
    "eCmdReadHostBuffer",
    "eCmdWriteHostBuffer",
}
check("the six escape hatches are exactly as documented",
      commands.ESCAPE_HATCHES == frozenset(expected_hatches))
check("DO_NOT_SEND is both groups combined",
      commands.DO_NOT_SEND == commands.ESCAPE_HATCHES | commands.DESTRUCTIVE)
check("the two groups don't overlap",
      commands.ESCAPE_HATCHES.isdisjoint(commands.DESTRUCTIVE))

# The whole point of the destructive group. Before 2026-07-26 the guard held
# only the escape hatches, so none of these were refused -- on a project whose
# first rule is that it never writes to the pack and never flashes firmware.
print("\n== SAFE_WRITES: the one narrow exception to read-only ==")
# Opened 2026-07-27. This is the boundary that keeps "we don't write to the
# hardware" true, so it is tested harder than anything else in this file.
check("eCmdIdentify is an approved write", commands.is_safe_write("eCmdIdentify"))
check("...and is therefore NOT refused", not commands.is_dangerous("eCmdIdentify"))
check("the approved-write set is exactly one command so far",
      commands.SAFE_WRITES == frozenset({"eCmdIdentify"}), sorted(commands.SAFE_WRITES))
check("refused and approved sets cannot overlap",
      commands.DO_NOT_SEND.isdisjoint(commands.SAFE_WRITES),
      sorted(commands.DO_NOT_SEND & commands.SAFE_WRITES))
check("every approved write is a real command",
      all(n in commands.COMMANDS for n in commands.SAFE_WRITES))

# The things that must NOT have crept in on Identify's coat-tails. Each of
# these fails at least one of the four tests in commands.SAFE_WRITES, and a
# future change that quietly adds one should fail here loudly.
for name in ("eCmdSetDimming", "eCmdRename", "eCmdSetTime", "eCmdToggleNightMode",
             "eCmdStandby", "eCmdRestart", "eCmdShutdown", "eCmdFormatDevice",
             "eCmdInstallFirmware", "eCmdDroboAppsAction", "eCmdUpdateUserGroupList"):
    check(f"{name} is NOT an approved write", not commands.is_safe_write(name))
    check(f"{name} is still refused", commands.is_dangerous(name))

print("\n== the commands that must never reach the hardware ==")
for name in ("eCmdFormatDevice", "eCmdFormatLUN", "eCmdInstallFirmware",
             "eCmdRevertFirmware", "eCmdReset", "eCmdRepair", "eCmdForceRepair",
             "eCmdShutdown", "eCmdRestart", "eCmdSetConfig", "eCmdSetAdmin"):
    check(f"{name} is refused", commands.is_dangerous(name))

# Two traps where the name lies about what the command does.
check("eCmdForceSingleInitiator is refused (reads like a query, is a write)",
      commands.is_dangerous("eCmdForceSingleInitiator"))
check("eCmdGetNextUniqueLUNID is refused (starts with Get, but ALLOCATES)",
      commands.is_dangerous("eCmdGetNextUniqueLUNID"))

# ...without the guard becoming so broad it blocks the reads we depend on.
print("\n== ordinary reads are still allowed ==")
for name in ("eCmdGetConfig", "eCmdGetSysInfo", "eCmdGetSharesConfig",
             "eCmdGetFirmwareInfo", "eCmdGetVersion", "eCmdGetAdminConfig"):
    check(f"{name} is NOT refused", not commands.is_dangerous(name))

check("every destructive name is a real command (no typos)",
      all(n in commands.COMMANDS or n in commands.NAMES_WITHOUT_CONFIRMED_ID
          for n in commands.DESTRUCTIVE),
      sorted(n for n in commands.DESTRUCTIVE
             if n not in commands.COMMANDS
             and n not in commands.NAMES_WITHOUT_CONFIRMED_ID))
check("eCmdRunESACommand is dangerous", commands.is_dangerous("eCmdRunESACommand"))
check("eCmdRunESACommand is dangerous by id (51)", commands.is_dangerous(51))
check("eCmdFormatDevice is dangerous by id (33)", commands.is_dangerous(33))
check("eCmdGetVersion is NOT dangerous", not commands.is_dangerous("eCmdGetVersion"))
check(
    "eCmdRouter has no numeric id (absent from the recovered 103-entry table)",
    "eCmdRouter" not in commands.COMMANDS and "eCmdRouter" in commands.NAMES_WITHOUT_CONFIRMED_ID,
)
check("eCmdRouter is still flagged dangerous by name", commands.is_dangerous("eCmdRouter"))


# ---------------------------------------------------------------------------
print("\n== envelope.py: build/parse round trip ==")
# ---------------------------------------------------------------------------

# a spread of commands: no params, flat params, nested params
cases = [
    ("eCmdGetVersion", None),
    ("eCmdGetAllDevicesStatusXML", {"Verbose": 1}),
    # Note: "Name" is deliberately non-numeric text -- envelope.py's wire
    # format has no type tags, so a digit-only *string* like "1" is
    # indistinguishable from the int 1 after a round trip (both come back as
    # int). Use non-numeric strings and ints here to keep the round-trip
    # comparison meaningful instead of exercising that documented ambiguity.
    ("eCmdGetSysInfo", {"Name": "unit-test", "Count": 5, "Nested": {"A": "alpha", "B": 2}}),
]
for name, params in cases:
    xml_text = envelope.build_command("ESA-1234", name, params)
    check(f"{name}: build() produced the exact XML declaration", xml_text.startswith('<?xml version="1.0"?>\n'))
    check(f"{name}: build() produced a DRINETTM root", "<DRINETTM>" in xml_text)

    env = envelope.parse(xml_text)
    check(f"{name}: round-trip esaid", env.esaid == "ESA-1234", env.esaid)
    check(f"{name}: round-trip command_name", env.command_name == name, env.command_name)
    check(f"{name}: round-trip command_id", env.command_id == commands.COMMANDS[name], env.command_id)
    check(f"{name}: round-trip params", env.params == (params or {}), env.params)

# building by numeric id instead of name should resolve the same way
xml_by_id = envelope.build_command("ESA-1", commands.COMMANDS["eCmdGetVersion"], None)
env_by_id = envelope.parse(xml_by_id)
check("build by numeric id resolves command_name", env_by_id.command_name == "eCmdGetVersion")

# unknown command name must be rejected, not silently accepted
try:
    envelope.build_command("ESA-1", "eCmdTotallyMadeUp", None)
    check("unknown command name raises KeyError", False)
except KeyError:
    check("unknown command name raises KeyError", True)

print("\n== envelope.py: response build/parse (Error / Result) ==")
resp_xml = envelope.build_response("ESA-9", "eCmdGetVersion", error=0, result="4.2.1")
resp_env = envelope.parse(resp_xml)
check("response error round-trips as int 0", resp_env.error == 0, resp_env.error)
check("response result round-trips", resp_env.result == "4.2.1", resp_env.result)

resp_xml_err = envelope.build_response("ESA-9", "eCmdGetVersion", error=1, result_details={"Reason": "denied"})
resp_env_err = envelope.parse(resp_xml_err)
check("response error round-trips as int 1", resp_env_err.error == 1, resp_env_err.error)
check("response result_details round-trips", resp_env_err.result_details == {"Reason": "denied"}, resp_env_err.result_details)

print("\n== envelope.py: malformed input is rejected ==")
try:
    envelope.parse("<NotDRINETTM><ESAID>1</ESAID><Command>1</Command></NotDRINETTM>")
    check("wrong root tag raises EnvelopeError", False)
except envelope.EnvelopeError:
    check("wrong root tag raises EnvelopeError", True)

try:
    envelope.parse("<DRINETTM><ESAID>1</ESAID></DRINETTM>")
    check("missing <Command> raises EnvelopeError", False)
except envelope.EnvelopeError:
    check("missing <Command> raises EnvelopeError", True)


# ---------------------------------------------------------------------------
print("\n== framing.py: length-prefixed frame round trip ==")
# ---------------------------------------------------------------------------

for byteorder in ("big", "little"):
    payload = f"hello nasd ({byteorder})".encode("utf-8")
    frame = framing.encode_frame(payload, byteorder=byteorder)
    decoded, remainder = framing.decode_frame(frame, byteorder=byteorder)
    check(f"encode/decode round trip ({byteorder})", decoded == payload, decoded)
    check(f"no remainder after exact frame ({byteorder})", remainder == b"")

# empty payload is a legitimate frame
empty_frame = framing.encode_frame(b"")
decoded_empty, _ = framing.decode_frame(empty_frame)
check("empty payload round-trips", decoded_empty == b"")

# incomplete buffer must raise, not silently return garbage
try:
    framing.decode_frame(b"\x00\x00")
    check("short-of-length-prefix buffer raises FramingError", False)
except framing.FramingError:
    check("short-of-length-prefix buffer raises FramingError", True)

full_frame = framing.encode_frame(b"0123456789")
try:
    framing.decode_frame(full_frame[:-3])
    check("truncated payload raises FramingError", False)
except framing.FramingError:
    check("truncated payload raises FramingError", True)

# two frames back to back decode in sequence
frame1 = framing.encode_frame(b"first")
frame2 = framing.encode_frame(b"second")
payload_a, rest = framing.decode_frame(frame1 + frame2)
payload_b, rest2 = framing.decode_frame(rest)
check("first of two concatenated frames", payload_a == b"first", payload_a)
check("second of two concatenated frames", payload_b == b"second", payload_b)
check("nothing left after both frames", rest2 == b"")

print("\n== framing.py: FrameAssembler handles fragmented delivery ==")
assembler = framing.FrameAssembler()
whole = framing.encode_frame(b"fragmented-payload")
# split the frame into three arbitrary chunks, as a real socket.recv() would
chunk_points = [3, 9]
chunks = [
    whole[: chunk_points[0]],
    whole[chunk_points[0] : chunk_points[1]],
    whole[chunk_points[1] :],
]
collected = []
for chunk in chunks:
    collected.extend(assembler.feed(chunk))
check("fragmented frame reassembled into exactly one payload", collected == [b"fragmented-payload"], collected)
check("assembler has no leftover bytes buffered", assembler.pending_bytes() == 0, assembler.pending_bytes())

multi = framing.encode_frame(b"one") + framing.encode_frame(b"two") + framing.encode_frame(b"three")
assembler2 = framing.FrameAssembler()
multi_result = assembler2.feed(multi)
check("assembler yields multiple frames fed at once", multi_result == [b"one", b"two", b"three"], multi_result)


print("\n== framing.py: NasdHeader candidates are internally self-consistent ==")
# This does NOT prove any candidate matches a real Drobo -- see NasdHeader's
# docstring. It only proves each candidate's own encode/decode pair agrees
# with itself, which is what client.py depends on structurally.
for cand_name, (encoder, decoder) in framing.NasdHeader.CANDIDATES.items():
    header_bytes = encoder(42)
    sig, length = decoder(header_bytes)
    check(f"NasdHeader.{cand_name}: encode/decode agree on length", length == 42, length)
    check(f"NasdHeader.{cand_name}: header is 12 bytes", len(header_bytes) == 12, len(header_bytes))

check(
    "NasdHeader.SIGNATURE_V4 matches the firmware string 'DRINASD4'",
    framing.NasdHeader.SIGNATURE_V4 == b"DRINASD4",
)


# ---------------------------------------------------------------------------
print("\n== client.py: state machine guards (no socket touched) ==")
# ---------------------------------------------------------------------------

c = client.NasdClient("192.0.2.1", esaid="ESA-1")  # TEST-NET-1, never dialled
check("client starts in state NEW", c.state == client.State.NEW, c.state)

try:
    c.login()
    check("login() before connect() raises NotConnectedError", False)
except client.NotConnectedError:
    check("login() before connect() raises NotConnectedError", True)

try:
    c.send_command("eCmdGetVersion")
    check("send_command() before login() raises NotConnectedError", False)
except client.NotConnectedError:
    check("send_command() before login() raises NotConnectedError", True)

try:
    client.NasdClient.discover()
    check("discover() is explicitly not implemented here", False)
except NotImplementedError:
    check("discover() is explicitly not implemented here", True)

# Fake being logged in with a real (but unconnected-to-anything-real) socket
# object, so this genuinely proves the DO_NOT_SEND guard fires before any
# send-side code runs -- not just before a None-socket check would have.
import socket as _socket

c.state = client.State.LOGGED_IN
c._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
try:
    c.send_command("eCmdRunESACommand")
    check("send_command() refuses DO_NOT_SEND commands", False)
except client.DangerousCommandError:
    check("send_command() refuses DO_NOT_SEND commands", True)
finally:
    c._sock.close()


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# esatm.py -- parsing a REAL <ESATMUpdate> captured from a live Drobo 5N.
# Fixture: sdk/tests/fixtures/esatm-sample.xml, firmware 4.3.1-8.126.117497.
# These lock in behaviour confirmed against actual hardware, so a future
# refactor can't silently regress it.
# ---------------------------------------------------------------------------
print("\n== esatm.py: real Drobo 5N greeting ==")

from drobo_nasd import esatm as _esatm
from drobo_nasd.models import DriveSlot as _DS

_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "esatm-sample.xml")

if not os.path.exists(_FIXTURE):
    check("real-greeting fixture present", False, _FIXTURE)
else:
    _payload = open(_FIXTURE, "rb").read()
    snap = _esatm.parse_esatm(_payload)

    check("payload is NUL-terminated (protocol detail)",
          _payload.rstrip().endswith(b"\x00"))
    check("parses despite the trailing NUL", snap.reachable is True)
    check("device name read", snap.device.name == "TestDrobo", snap.device.name)
    check("firmware read", snap.device.firmware.startswith("4.3.1"),
          snap.device.firmware)

    # The device reports mSlotCountExp=6, but a 5N has five physical bays.
    check("phantom 6th bay trimmed", len(snap.drives) == 5, len(snap.drives))
    check("bays numbered 1..5",
          [d.bay for d in snap.drives] == [1, 2, 3, 4, 5],
          [d.bay for d in snap.drives])
    check("all five bays populated", all(d.present for d in snap.drives),
          [(d.bay, d.present) for d in snap.drives])
    check("every drive healthy", all(d.state == "ok" for d in snap.drives),
          [(d.bay, d.state) for d in snap.drives])
    check("drive models parsed", all("WD" in d.model for d in snap.drives),
          [d.model for d in snap.drives])
    check("serials parsed", all(d.serial.startswith("WD-") for d in snap.drives),
          [d.serial for d in snap.drives])

    # mTemperature is 0 on this hardware. It must read as "unknown", never as a
    # literal 0 C, or the alert rules would treat a healthy array as freezing.
    check("temperature reported as unknown, not 0 C",
          all(d.temperature_c is None for d in snap.drives),
          [d.temperature_c for d in snap.drives])

    check("capacity usable > used",
          snap.capacity.usable_bytes > snap.capacity.used_bytes)
    check("raw capacity is the sum of the disks",
          snap.capacity.raw_bytes == sum(d.capacity_bytes for d in snap.drives),
          (snap.capacity.raw_bytes, sum(d.capacity_bytes for d in snap.drives)))
    check("array reads healthy overall", snap.overall_state == "ok",
          snap.overall_state)

# Safety: the trim must never hide a slot that actually holds a disk.
_over = [_DS(bay=i + 1, present=True, state="ok", capacity_bytes=1) for i in range(6)]
_esatm._trim_phantom_slots(_over, "Drobo 5N")
check("trim never hides a populated slot", len(_over) == 6, len(_over))

_mixed = [_DS(bay=1, present=True, state="ok", capacity_bytes=1),
          _DS(bay=2, present=False, state="empty")]
_esatm._trim_phantom_slots(_mixed, "Unknown Model")
check("trim is a no-op for unknown models", len(_mixed) == 2, len(_mixed))

_five = [_DS(bay=i + 1, present=True, state="ok", capacity_bytes=1) for i in range(5)]
_five.append(_DS(bay=6, present=False, state="empty"))
_esatm._trim_phantom_slots(_five, "Drobo 5N")
check("trailing empty phantom slot is removed", len(_five) == 5, len(_five))

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
if fails:
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
