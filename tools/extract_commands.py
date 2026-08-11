#!/usr/bin/env python3
"""
extract_commands.py -- what the ORIGINAL Drobo Dashboard actually sends.

THE PROBLEM THIS EXISTS TO SOLVE
--------------------------------
We recovered all 103 command names and ids from the firmware. What the firmware
does NOT contain is what goes inside each command's <Params> block, and this
project has a standing rule that a guessed parameter is an unreviewed
instruction to somebody's storage -- so every command we send goes out with
empty Params.

That rule is why a whole row of the roadmap is stuck. eCmdSetDimming (LED
brightness), eCmdRename, eCmdGetEventLogs and the share/user management
commands all either need parameters or answer empty without them, and no amount
of reading the firmware will produce them. The one thing that will is watching
the original Dashboard do it, once, and reading the bytes.

So: run a capture while clicking through Drobo Dashboard 3.5.0, point this at
the file, and it prints every command that went past with its parameters
verbatim. One session can unblock several features at once, which is the whole
reason this is a batch tool rather than another one-command script like
capture-identify.ps1.

    py tools/extract_commands.py <capture.pcapng>
    py tools/extract_commands.py <capture.pcapng> --command eCmdSetDimming
    py tools/extract_commands.py <capture.pcapng> --json out.json

WHAT IT DOES NOT DO
-------------------
It does not tell you a parameter is CORRECT. It tells you what one version of
one Dashboard sent to one device on one day. That is evidence, and it is the
best evidence available, but protocol-map.md's CONFIRMED / FIRMWARE / GUESS
discipline still applies: a parameter seen once in a capture is CONFIRMED for
the case captured and nothing more. Two units, two firmware versions, or the
same button pressed in a different state may differ.

It also never sends anything. It reads a file.

SECRETS
-------
nasd has no encryption, so a capture taken while you type an admin password
contains that password in clear text. Anything that looks like a credential is
redacted from this tool's OUTPUT -- but the CAPTURE FILE ITSELF is not
sanitized and must not be committed. See docs/captures/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk")))

import nasd_dissect  # noqa: E402  (sibling tool: pcap reading + TCP reassembly)

try:
    from drobo_nasd.commands import COMMANDS  # name -> id
except ImportError:  # pragma: no cover - only when run outside the repo
    COMMANDS = {}

_ID_TO_NAME = {v: k for k, v in COMMANDS.items()}

#: The command channel. Status (5000) carries no commands worth extracting --
#: the device just pushes its greeting there.
DEFAULT_PORT = 5001

#: Same idea as config_cmd._SECRET: match the FIELD NAME, not the value, so a
#: password is redacted whatever it happens to contain.
_SECRET = re.compile(r"pass|pwd|secret|token|credential|key", re.I)
_REDACTED = "[redacted]"

#: A whole XML document, as this firmware likes to nest them: escaped text
#: inside an element. Mistaking these for plain strings is what hid the
#: temperature and the performance counters for weeks, so we unwrap them here
#: too rather than printing an unreadable one-line blob.
_LOOKS_LIKE_XML = re.compile(r"^\s*<\?xml|^\s*<[A-Za-z]")


def _redact(elem: ET.Element) -> None:
    """Blank any element whose TAG looks like a credential, children and all."""
    for child in list(elem):
        if _SECRET.search(child.tag):
            child.text = _REDACTED
            child.attrib.clear()
            for grandchild in list(child):
                child.remove(grandchild)
            continue
        _redact(child)


def _indent(elem: ET.Element, level: int = 0) -> None:
    """ET.indent equivalent, spelled out so the output is stable across versions."""
    pad = "\n" + "  " * level
    if len(elem):
        if not (elem.text or "").strip():
            elem.text = pad + "  "
        for child in elem:
            _indent(child, level + 1)
            if not (child.tail or "").strip():
                child.tail = pad + "  "
        if not (elem[-1].tail or "").strip():
            elem[-1].tail = pad
    elif level and not (elem.tail or "").strip():
        elem.tail = pad


def _pretty(elem: ET.Element) -> str:
    _indent(elem)
    return ET.tostring(elem, encoding="unicode").strip()


def unwrap_escaped(text: str) -> str | None:
    """
    If `text` is itself an XML document (this firmware nests them as escaped
    text), return it pretty-printed. Otherwise None.
    """
    if not text or not _LOOKS_LIKE_XML.match(text):
        return None
    try:
        inner = ET.fromstring(text)
    except ET.ParseError:
        return None
    _redact(inner)
    return _pretty(inner)


#: The XML root of a command, and of a reply. NOT "DRINETTM" -- that is the
#: eight-byte binary FRAME SIGNATURE that precedes each document on the wire,
#: and the first version of this tool scanned for "<DRINETTM" as though it were
#: a tag. It found nothing on a capture containing 137 perfectly good frames,
#: which is what caught it. Confirmed against a real Drobo Dashboard 3.5.0
#: session, 2026-08-05.
_ROOTS = (b"TMCmd", b"Result")


def parse_envelopes(buf: bytes) -> list[dict]:
    """
    Pull every command/reply document out of a reassembled byte stream.

    Framing-agnostic on purpose. nasd_dissect infers the binary header layout,
    and it is right, but this tool's job is the PAYLOAD -- which is
    self-delimiting text with an unmistakable root tag. Scanning for the tag
    means a header variant we have not seen costs us nothing here, where
    depending on the inferred framing would make this tool fail on exactly the
    novel captures it exists to read.
    """
    found: list[dict] = []
    for root_tag in _ROOTS:
        opening = b"<" + root_tag
        closing = b"</" + root_tag + b">"
        pos = 0
        while True:
            start = buf.find(opening, pos)
            if start < 0:
                break
            end = buf.find(closing, start)
            if end < 0:
                break
            end += len(closing)
            raw = buf[start:end]
            pos = end
            try:
                root = ET.fromstring(raw.decode("utf-8", "replace"))
            except ET.ParseError:
                continue
            found.append({"offset": start, "root": root, "raw": raw})
    found.sort(key=lambda f: f["offset"])
    return found


def _text(root: ET.Element, *names: str) -> str:
    for name in names:
        node = root.find(name)
        if node is not None and node.text:
            return node.text.strip()
    return ""


def describe(root: ET.Element) -> dict:
    """
    Reduce one command (or reply) document to the facts worth printing.

    The id lives in <CmdID>. "Command" is checked too because that is what this
    tool originally guessed the element was called, and a capture where every
    command came back as "(none)" is how the guess was found out -- the real
    documents look like:

        <TMCmd><CmdID>30</CmdID><Params>...</Params><ESAID>...</ESAID></TMCmd>

    Both are accepted rather than one replaced, since a different firmware
    version naming it differently costs nothing to tolerate.
    """
    command = _text(root, "CmdID", "cmdid", "Command", "command")
    name = ""
    cid = None
    if command.isdigit():
        cid = int(command)
        name = _ID_TO_NAME.get(cid, "")
    else:
        name = command
        cid = COMMANDS.get(command)

    params = root.find("Params")
    if params is None:
        params = root.find("params")

    entry: dict = {
        "command_id": cid,
        "command": name or command or "(none)",
        "has_params": params is not None and (len(params) > 0 or bool((params.text or "").strip())),
        "params_xml": "",
        "escaped_documents": [],
        "result": _text(root, "Result", "result"),
    }

    if params is not None:
        clone = ET.fromstring(ET.tostring(params))
        _redact(clone)
        entry["params_xml"] = _pretty(clone)
        for node in clone.iter():
            unwrapped = unwrap_escaped((node.text or "").strip())
            if unwrapped:
                entry["escaped_documents"].append({"under": node.tag, "xml": unwrapped})
    return entry


def extract(path: str, port: int = DEFAULT_PORT) -> dict:
    packets = nasd_dissect.read_capture(path)
    streams = nasd_dissect.collect_streams(packets, port)
    if not streams:
        return {"port": port, "requests": [], "responses": [], "streams": 0}

    requests: list[dict] = []
    responses: list[dict] = []
    for stream in streams.values():
        client = stream.get("client") or ("?", 0)
        server = stream.get("server") or ("?", 0)
        for direction in ("to_server", "to_client"):
            buf = nasd_dissect.reassemble(stream[direction])
            if not buf:
                continue
            # A request carries a Command the client chose; a reply carries a
            # Result. Classified on CONTENT rather than on which side of the
            # socket it came from, so a capture whose directions were guessed
            # the wrong way round still reads correctly.
            for env in parse_envelopes(buf):
                entry = describe(env["root"])
                entry["stream"] = (f"{client[0]}:{client[1]} -> {server[0]}:{server[1]}"
                                   if direction == "to_server"
                                   else f"{server[0]}:{server[1]} -> {client[0]}:{client[1]}")
                (responses if entry["result"] and entry["command"] == "(none)"
                 else requests).append(entry)
    return {"port": port, "requests": requests, "responses": responses,
            "streams": len(streams)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="List every command the original Drobo Dashboard sent, with parameters.")
    ap.add_argument("capture", help="a .pcap or .pcapng file")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"TCP port to read (default {DEFAULT_PORT}, the command channel)")
    ap.add_argument("--command", default="",
                    help="show only this command (name or id), e.g. eCmdSetDimming")
    ap.add_argument("--json", default="", help="also write the full result to this file")
    args = ap.parse_args(argv)

    if not os.path.exists(args.capture):
        print(f"No such capture: {args.capture}")
        return 2

    result = extract(args.capture, args.port)
    requests = result["requests"]

    if args.command:
        wanted = args.command.strip()
        requests = [r for r in requests
                    if r["command"] == wanted or str(r["command_id"]) == wanted]

    print(f"\n{os.path.basename(args.capture)} -- TCP {result['port']}, "
          f"{result['streams']} stream(s), {len(result['requests'])} command(s) sent")

    if not result["streams"]:
        print("\n  Nothing on that port. Two usual reasons:")
        print("    - the capture was taken on the status port (5000); commands are on 5001")
        print("    - the Dashboard was already connected before recording started, so the")
        print("      commands you wanted went past before the capture began")
        return 1

    if not requests:
        print("\n  No commands matched." if args.command else
              "\n  No commands found -- the stream carried no <DRINETTM> documents.")
        return 1

    counts = Counter(r["command"] for r in result["requests"])
    print("\n  Commands seen, most frequent first:")
    for name, n in counts.most_common():
        cid = next((r["command_id"] for r in result["requests"] if r["command"] == name), None)
        print(f"    {n:4d}  {name}" + (f"  (id {cid})" if cid is not None else ""))

    # The point of the whole tool: which commands were sent WITH parameters.
    # Those are the ones whose <Params> we could never recover from firmware.
    with_params = [r for r in requests if r["has_params"]]
    print(f"\n  {len(with_params)} of {len(requests)} shown carried parameters.")

    for entry in requests:
        print("\n" + "-" * 72)
        header = entry["command"]
        if entry["command_id"] is not None:
            header += f"  (id {entry['command_id']})"
        print(header)
        print("-" * 72)
        if not entry["has_params"]:
            print("  <Params> empty -- same as what we already send.")
            continue
        for line in entry["params_xml"].splitlines():
            print("  " + line)
        for doc in entry["escaped_documents"]:
            print(f"\n  ...and nested inside <{doc['under']}>, an escaped document:")
            for line in doc["xml"].splitlines():
                print("    " + line)

    print("\n" + "=" * 72)
    print("Remember: this is what ONE Dashboard sent to ONE device on ONE day.")
    print("Record it in your protocol notes as CONFIRMED for the case captured,")
    print("and not as a general specification. Do not commit the capture file.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(f"\nFull result written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
