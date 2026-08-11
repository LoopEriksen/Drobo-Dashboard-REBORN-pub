#!/usr/bin/env python3
"""
Offline tests for extract_commands.py.

Builds a REAL capture file -- genuine pcapng structure, Ethernet/IPv4/TCP
headers, TCP payloads split across segments and delivered out of order -- whose
contents we chose, then checks the tool recovers exactly what we put in.

That shape is deliberate. The dissector this borrows its reader from once
returned "0 frames" on every real capture while its tests passed, because the
fixtures had been written to agree with the code (big-endian block types)
instead of with reality. A fixture that agrees with the implementation proves
only that the implementation is self-consistent. So this one is assembled the
way a capture really is, and reuses test_nasd_dissect.py's builders rather than
inventing a friendlier format.

    py test_extract_commands.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract_commands
import test_nasd_dissect as builders

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def envelope(command_id: int, params_inner: str = "") -> bytes:
    """
    One command document, in the shape a real Drobo Dashboard actually sends.

    CORRECTED 2026-08-05 against a live capture, and the correction matters
    more than the code change it accompanies. This builder used to emit
    <DRINETTM><Command>8</Command>...</DRINETTM>, matching what the extractor
    expected -- so the tests passed while the tool found exactly zero commands
    in a real 6 MB capture containing 137 of them.

    Both halves were wrong in the same direction, which is the failure mode
    this repository has already hit once with nasd_dissect reading pcapng block
    types big-endian: a fixture written to agree with the implementation tests
    only that they agree. The real wire format is

        DRINETTM<8 binary header bytes><TMCmd><CmdID>30</CmdID>...</TMCmd>

    where DRINETTM is the FRAME SIGNATURE, not a tag, and the document root is
    TMCmd carrying CmdID.
    """
    body = f"<CmdID>{command_id}</CmdID><Params>{params_inner}</Params>"
    body += "<ESAID>drb000000SAMPLE</ESAID>"
    # The eight-byte binary frame header the device really prefixes, included
    # so the fixture exercises the scanner against surrounding binary noise
    # rather than clean text.
    return b"DRINETTM" + bytes([0x0A, 0x01, 0, 0, 0, 0, 0, 0]) + f"<TMCmd>{body}</TMCmd>".encode()


# ---------------------------------------------------------------------------
# The traffic we are pretending to have captured. Chosen to be the awkward
# cases, not the easy one:
#   8  eCmdSetDimming     -- a command WITH a parameter, the whole point
#   9  eCmdGetDimming     -- a query with empty Params, like the ones we send
#   25 eCmdRename         -- a parameter that is a string, not a number
#   57 (whatever it maps to) carrying an ESCAPED nested document, because this
#      firmware does that and mistaking it for a plain string is a mistake this
#      project has already made once, at the cost of weeks.
# ---------------------------------------------------------------------------
SET_DIMMING = envelope(8, "<Dimming>42</Dimming>")
GET_DIMMING = envelope(9, "")
RENAME = envelope(25, "<NasName>Basement Drobo</NasName>")
NESTED = envelope(
    57,
    "<Config>&lt;DRINASConfig&gt;&lt;Level&gt;7&lt;/Level&gt;&lt;/DRINASConfig&gt;</Config>")
SECRET = envelope(26, "<UserName>admin</UserName><Password>hunter2</Password>")

REQUEST_STREAM = SET_DIMMING + GET_DIMMING + RENAME + NESTED + SECRET

tmpdir = tempfile.mkdtemp(prefix="extract-cmds-")
capture_path = os.path.join(tmpdir, "session.pcapng")

# Split across three TCP segments and hand them to the writer out of order, so
# the test proves reassembly happens rather than that one packet happened to
# hold one document. A real capture of a chatty Dashboard looks like this.
third = len(REQUEST_STREAM) // 3
chunks = [
    (0, REQUEST_STREAM[:third]),
    (third, REQUEST_STREAM[third:third * 2]),
    (third * 2, REQUEST_STREAM[third * 2:]),
]
out_of_order = [chunks[2], chunks[0], chunks[1]]

frames = [
    builders.eth_frame(builders.ipv4_packet(
        builders.CLIENT_IP, builders.SERVER_IP,
        builders.tcp_segment(51000, 5001, seq, 0x18, payload)))
    for seq, payload in out_of_order
]
builders.write_pcapng(capture_path, frames)

print("\n== reading a capture the tool has never seen ==")
result = extract_commands.extract(capture_path, port=5001)
check("the command stream is found", result["streams"] >= 1, result["streams"])
check("all five commands are recovered despite reordered segments",
      len(result["requests"]) == 5, len(result["requests"]))

by_id = {r["command_id"]: r for r in result["requests"]}
check("command ids are read", sorted(by_id) == [8, 9, 25, 26, 57], sorted(by_id))
check("ids are resolved to firmware names",
      by_id[8]["command"] == "eCmdSetDimming", by_id[8]["command"])
check("-- and for the other known one too",
      by_id[25]["command"] == "eCmdRename", by_id[25]["command"])

print("\n== the parameters, which is the entire reason this exists ==")
# This is the value nobody could get from the firmware: what actually goes
# inside <Params> for a command that changes something.
check("a numeric parameter is recovered verbatim",
      "<Dimming>42</Dimming>" in by_id[8]["params_xml"].replace("\n", "").replace("  ", ""),
      by_id[8]["params_xml"])
check("SetDimming is reported as carrying parameters", by_id[8]["has_params"])
check("a string parameter is recovered with its value intact",
      "Basement Drobo" in by_id[25]["params_xml"], by_id[25]["params_xml"])

print("\n== empty Params is reported as empty, not as missing ==")
# The distinction matters: "sent with nothing inside" is what WE send, so a
# command the Dashboard also sends empty tells us we are already correct.
check("GetDimming carried no parameters", by_id[9]["has_params"] is False, by_id[9])

print("\n== escaped documents are unwrapped, not printed as one long string ==")
# This firmware nests whole XML documents as escaped text. Reading them as
# opaque strings is what made temperature and the performance counters look
# unobtainable for weeks.
nested = by_id[57]["escaped_documents"]
check("the nested document is noticed", len(nested) == 1, nested)
check("it is attributed to the element it was inside",
      nested and nested[0]["under"] == "Config", nested)
check("and it is parsed, not left escaped",
      nested and "<Level>7</Level>" in nested[0]["xml"].replace("\n", "").replace("  ", ""),
      nested)

print("\n== credentials never reach the output ==")
# nasd has no encryption, so a capture taken while somebody types a password
# contains it in clear text. The capture file stays dangerous -- that is
# docs/captures/README.md's problem -- but nothing this tool PRINTS may carry
# one, because its output is what gets pasted into notes and commits.
creds = by_id[26]["params_xml"]
check("the password value is gone", "hunter2" not in creds, creds)
check("it is replaced visibly rather than silently dropped",
      extract_commands._REDACTED in creds, creds)
check("the non-secret field beside it survives", "admin" in creds, creds)

print("\n== a capture with nothing to say, said plainly ==")
empty_path = os.path.join(tmpdir, "wrong-port.pcapng")
builders.write_pcapng(empty_path, frames)
nothing = extract_commands.extract(empty_path, port=5000)  # the status port
check("looking at the wrong port finds no streams", nothing["streams"] == 0, nothing)
check("-- and returns no commands rather than raising", nothing["requests"] == [])

print("\n== classic pcap works too, not just pcapng ==")
classic_path = os.path.join(tmpdir, "session.pcap")
builders.write_pcap_classic(classic_path, frames)
classic = extract_commands.extract(classic_path, port=5001)
check("the same five commands come out of a classic pcap",
      len(classic["requests"]) == 5, len(classic["requests"]))

print("\n== the unwrapper does not mangle ordinary text ==")
check("a plain string is not treated as a document",
      extract_commands.unwrap_escaped("Basement Drobo") is None)
check("an empty string is not a document", extract_commands.unwrap_escaped("") is None)
check("a number is not a document", extract_commands.unwrap_escaped("42") is None)
check("malformed XML is declined rather than crashing",
      extract_commands.unwrap_escaped("<broken") is None)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
