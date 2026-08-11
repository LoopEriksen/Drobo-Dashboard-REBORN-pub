"""
DroboSync -- Drobo-to-Drobo replication. Read-only, and dormant by design.

DroboSync copied one Drobo's shares to another Drobo, on a schedule. It is the
one feature in this project that cannot be developed normally, because
exercising it needs TWO of a discontinued NAS and the owner has one.

So this ships built and quiet, at the owner's decision on 2026-07-28: the read
path exists, it asks the device honestly, and it reports "not configured"
instead of pretending the feature is missing. If a second unit ever appears, it
lights up with no new protocol work. A button that fails is worse than a page
that explains itself.

WHAT WE ACTUALLY KNOW
---------------------
Four read commands, all confirmed present in the firmware table:

    eCmdGetDroboSyncSettings      35
    eCmdGetDroboSyncSummaryLog    37
    eCmdGetDroboSyncDetailedLog   38
    eCmdGetDroboSyncLog           39

All four were swept against the live 5N on 2026-07-26 and all four answered
EMPTY -- see docs/command-surface.md. That is the expected answer for a device
with no sync partner configured, and it is the only answer this project has
ever observed.

WHAT WE DO NOT KNOW, AND WILL NOT PRETEND TO
--------------------------------------------
**Nobody here has ever seen a populated DroboSync reply.** The field names
below are read defensively and the raw document is always carried alongside,
because a parser written against a reply nobody has seen is a guess wearing a
function signature. When a real one finally arrives, `raw_xml` is what tells us
what the shape actually is -- and this module should be corrected from that
evidence rather than trusted because it looks finished.

Three sibling commands are WRITES and stay refused by commands.DO_NOT_SEND:
eCmdSetDroboSyncSettings (36), eCmdSyncNow (40), eCmdStopSync (41). Triggering a
replication run between two arrays is emphatically not a safe write -- it moves
real data -- and it could not be tested here even if it were allowed. The
assertion at the bottom of this module holds us to that.
"""
from __future__ import annotations

import socket
import xml.etree.ElementTree as ET

from . import commands as _commands
from .config_cmd import (
    DEFAULT_PORT,
    TYPE_COMMAND,
    TYPE_LOGIN,
    TYPE_LOGIN_OK,
    TYPE_RESULT,
    _frame,
    _login_payload,
    _read_frame,
)
from .sysinfo import SysInfoError, _inner_document

#: The read-only half. Keys are what a caller asks for; values are the firmware
#: command names.
READ_COMMANDS = {
    "settings": "eCmdGetDroboSyncSettings",
    "summary_log": "eCmdGetDroboSyncSummaryLog",
    "detailed_log": "eCmdGetDroboSyncDetailedLog",
    "log": "eCmdGetDroboSyncLog",
}

#: The write half, listed so this module's refusal is visible rather than
#: implied by their absence. Every one is in DO_NOT_SEND; see the assertion.
WRITE_COMMANDS = ("eCmdSetDroboSyncSettings", "eCmdSyncNow", "eCmdStopSync")


class DroboSyncError(SysInfoError):
    """A DroboSync read failed. Subclasses SysInfoError so existing callers
    that already handle command-port failures keep working unchanged."""


def _text(node, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None else ""


def parse_sync_reply(payload: bytes) -> dict:
    """
    Turn a DroboSync reply into a dict, without inventing what isn't there.

    Returns a dict that ALWAYS carries:
        configured  False when the device answered empty, which is every
                    observation this project has ever made
        raw_xml     the inner document as text, or "" -- kept so the first
                    populated reply anyone sees can be read directly instead
                    of being filtered through assumptions made today
        fields      whatever leaf elements were present, as {tag: text}

    Deliberately generic. Naming specific fields would imply we know a schema,
    and we do not: every reply observed so far has been empty. A caller should
    show `fields` as-is and say where it came from.
    """
    text = payload.rstrip(b"\x00").rstrip()
    if not text:
        return {"configured": False, "raw_xml": "", "fields": {}}

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DroboSyncError(f"malformed result XML: {exc}") from exc

    details = root.find("ResultDetails")
    if details is None:
        return {"configured": False, "raw_xml": "", "fields": {}}

    # Same entity-escaped nesting the rest of this firmware uses. Missing it is
    # what made temperature and the performance counters look unobtainable for
    # weeks, so it is handled here before anything else looks at the content.
    inner = _inner_document(details)
    node = inner if inner is not None else details

    raw_xml = ET.tostring(node, encoding="unicode").strip() if inner is not None else ""
    if not raw_xml:
        raw_xml = (details.text or "").strip()

    fields: dict[str, str] = {}
    for el in node.iter():
        if len(el):
            continue  # a container, not a value
        value = (el.text or "").strip()
        if value:
            fields[el.tag] = value

    # "Configured" means the device told us something, not that sync is
    # currently running -- we have no way to tell those apart yet, and saying
    # otherwise would be a claim we cannot support.
    return {"configured": bool(fields), "raw_xml": raw_xml, "fields": fields}


def read(host: str, esa_id: str, what: str = "settings",
         port: int = DEFAULT_PORT, timeout: float = 8.0) -> dict:
    """
    Ask the Drobo about DroboSync. Read-only; never triggers a sync.

    `what` is one of READ_COMMANDS. An empty answer is returned as
    {"configured": False}, NOT raised as an error -- on a device with no sync
    partner that is the correct and expected reply, and treating it as a
    failure would put a red error on screen for a machine behaving perfectly.
    """
    if what not in READ_COMMANDS:
        raise ValueError(f"unknown DroboSync read {what!r}; "
                         f"choose from {sorted(READ_COMMANDS)}")
    command = READ_COMMANDS[what]

    # Belt and braces. These are queries and none of them is in DO_NOT_SEND,
    # but this module sits next to three commands that move real data between
    # two arrays, and the guard costs one dictionary lookup.
    if _commands.is_dangerous(command):
        raise DroboSyncError(f"{command} is on the do-not-send list")
    cmd_id = _commands.COMMANDS.get(command)
    if cmd_id is None:
        raise DroboSyncError(f"{command} has no confirmed command id")

    xml = (f"<TMCmd><CmdID>{cmd_id}</CmdID><Params></Params>"
           f"<ESAID>{esa_id}</ESAID></TMCmd>").encode() + b"\x00"

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(_frame(TYPE_LOGIN, _login_payload(esa_id)))
        msg_type, _ = _read_frame(sock)
        if msg_type != TYPE_LOGIN_OK:
            raise DroboSyncError(f"login refused (type 0x{msg_type:02x})")
        sock.sendall(_frame(TYPE_COMMAND, xml))
        msg_type, body = _read_frame(sock)
        if msg_type != TYPE_RESULT:
            raise DroboSyncError(f"unexpected reply type 0x{msg_type:02x}")
        result = parse_sync_reply(body)
        result["command"] = command
        result["what"] = what
        return result
    finally:
        sock.close()


#: What an unasked read looks like. Distinct from a read that happened and came
#: back empty -- see summary(), which stops early and must not claim to have
#: asked things it did not ask.
_NOT_ASKED = {"configured": False, "raw_xml": "", "fields": {}, "asked": False}


def summary(host: str, esa_id: str, port: int = DEFAULT_PORT,
            timeout: float = 8.0) -> dict:
    """
    Everything readable about DroboSync.

    Settings are read FIRST, and if the device reports no sync configured the
    three log reads are skipped entirely. That is not just a speed trick: each
    read costs a separate connection to the command port, and this device's
    command port is documented as stopping answering after roughly ninety
    connections in an hour (docs/command-surface.md) -- so spending four on a
    feature that is switched off, every time a page refreshes, would be
    actively harmful to a 2013 box that tires easily. With no partner
    configured there are no logs to fetch by definition.

    Skipped reads are marked `asked: False` rather than reported as empty
    answers. "We did not ask" and "we asked and got nothing" are different
    facts, and a UI that conflates them would be quietly lying about what was
    measured.
    """
    out: dict = {"configured": False, "reads": {}, "note": ""}

    def _read(what: str) -> dict:
        try:
            answer = read(host, esa_id, what, port, timeout)
            answer["asked"] = True
            return answer
        except (DroboSyncError, OSError) as exc:
            return {"configured": False, "error": str(exc),
                    "raw_xml": "", "fields": {}, "asked": True}

    out["reads"]["settings"] = _read("settings")
    if out["reads"]["settings"].get("configured"):
        for what in ("summary_log", "detailed_log", "log"):
            out["reads"][what] = _read(what)
    else:
        for what in ("summary_log", "detailed_log", "log"):
            out["reads"][what] = dict(_NOT_ASKED)

    out["configured"] = any(r.get("configured") for r in out["reads"].values())
    if not out["configured"]:
        out["note"] = (
            "This Drobo has no DroboSync partner set up. DroboSync copied one "
            "Drobo's shares to a second Drobo on a schedule, so it needs two "
            "units -- with one, there is nothing for it to do and the device "
            "correctly reports nothing. Everything needed to display it is "
            "built and waiting."
        )
    return out


# Setting up or starting a replication run moves real data between two arrays.
# It is not a safe write, it could not be tested here, and it stays refused.
assert all(_commands.is_dangerous(name) for name in WRITE_COMMANDS), (
    "DroboSync's write commands must stay in DO_NOT_SEND: "
    f"{[n for n in WRITE_COMMANDS if not _commands.is_dangerous(n)]}"
)
