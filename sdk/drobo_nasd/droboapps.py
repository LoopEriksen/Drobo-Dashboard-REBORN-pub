"""
eCmdGetDroboAppsStatus -- what's installed on the Drobo itself.

DroboApps are packages that run ON the NAS: Python, rsync, Plex, an SSH server.
This is how people extended these boxes, and it is the one place the device
tells you about software rather than storage.

Measured on the live 5N (firmware 4.3.1): answers on a plain login, no crypto
handshake, returning a real list with names, versions, descriptions, whether
each is stopped, and whether it has a web interface.

Worth knowing: **DroboPix is one of the apps that answers here.** That is the
photo-upload service Phase 4 of this project sets out to replace, and it is
sitting installed on the device. Whether it still functions with its vendor
servers gone is a separate question -- but the app, its version and its web
interface are all discoverable from here, which is a better starting point for
Phase 4 than guessing.

Like eCmdGetSysInfo, the reply nests its payload as an entity-escaped XML
document inside <ResultDetails>, so it is parsed with the same unwrapping
helper rather than by looking for child elements that are not there.
"""
from __future__ import annotations

import socket

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

import xml.etree.ElementTree as ET

COMMAND = "eCmdGetDroboAppsStatus"


def _text(node, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None else ""


def parse_droboapps(payload: bytes) -> dict:
    """
    Turn a DroboApps reply into {'sdk_version', 'apps': [...]}.

    Separate from the socket work so it is testable with no device present.
    An app with no name is skipped -- a nameless entry is not something a
    person can act on, and showing a blank row is worse than showing nothing.
    """
    text = payload.rstrip(b"\x00").rstrip()
    if not text:
        return {"sdk_version": "", "apps": []}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SysInfoError(f"malformed result XML: {exc}") from exc

    details = root.find("ResultDetails")
    if details is None:
        raise SysInfoError("result carried no <ResultDetails>")

    inner = _inner_document(details)
    node = inner if inner is not None else details
    if node.tag != "DroboApps":
        node = node.find("DroboApps") or node

    apps = []
    for el in node.findall("App"):
        name = _text(el, "Name")
        if not name:
            continue
        # "Stopped" is the device's own polarity, and it is inverted relative
        # to how anyone would say it out loud. Flip it here so every caller
        # above this line deals in "running", which is what a person reads.
        stopped = _text(el, "Stopped")
        apps.append({
            "name": name,
            "version": _text(el, "Version"),
            "description": _text(el, "Description"),
            "running": stopped == "0",
            "status": _text(el, "ActiveStatus"),
            "has_web_ui": bool(_text(el, "WebUI")),
        })

    apps.sort(key=lambda a: a["name"].lower())
    return {"sdk_version": _text(node, "SDKVersion"), "apps": apps}


def parse_dimming(payload: bytes) -> int | None:
    """
    LED brightness, from eCmdGetDimming.

    Unlike every other command here, this one does NOT nest a document: it
    puts a bare number straight into <ResultDetails>. The live 5N returned
    `59`. What the scale is -- percent, or 0-255, or Drobo's own steps -- is
    not documented anywhere in the firmware table, so this returns the raw
    integer and lets the caller present it without inventing a unit.

    Returns None rather than 0 when there is no reading. Zero is a legitimate
    brightness ("lights off"), so conflating the two would be a real bug.
    """
    text = payload.rstrip(b"\x00").rstrip()
    if not text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SysInfoError(f"malformed result XML: {exc}") from exc
    details = root.find("ResultDetails")
    if details is None:
        return None
    raw = (details.text or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_demo_mode(payload: bytes) -> dict:
    """
    eCmdGetDemoModeInfo -- whether the unit is in shop-display mode.

    Answered on the live 5N with `FirmwareInfo > DemoMode, ScaleFactor`. Demo
    mode is what a retailer switched on so a Drobo on a shelf would show
    plausible fake capacity instead of five empty bays; ScaleFactor is the
    multiplier it invented capacity with.

    Genuinely obscure, and surfaced anyway for one reason: **a unit left in demo
    mode reports capacity that is not real.** Every number this project displays
    would be a lie and nothing else would say so. On a normal unit this reads
    disabled and nobody ever thinks about it again -- but it costs ten lines to
    rule out, and the alternative is a dashboard that cannot tell a fabricated
    array from a real one.

    Returns {"demo_mode": bool|None, "scale_factor": int|None}. None means the
    device did not say, which is NOT the same as "off" -- see the caller, which
    only warns on an explicit yes.
    """
    text = payload.rstrip(b"\x00").rstrip()
    out: dict = {"demo_mode": None, "scale_factor": None}
    if not text:
        return out
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SysInfoError(f"malformed result XML: {exc}") from exc

    details = root.find("ResultDetails")
    if details is None:
        return out
    inner = _inner_document(details)
    node = inner if inner is not None else details
    if node.tag != "FirmwareInfo":
        node = node.find("FirmwareInfo") or node

    raw = _text(node, "DemoMode")
    if raw:
        # The device sends 0/1. Anything else is left as unknown rather than
        # coerced -- a truthy string would otherwise read as "demo mode on"
        # and put a false warning on a perfectly ordinary array.
        if raw in ("0", "1"):
            out["demo_mode"] = raw == "1"
    scale = _text(node, "ScaleFactor")
    if scale:
        try:
            out["scale_factor"] = int(scale)
        except ValueError:
            pass
    return out


def get_scalar(host: str, esa_id: str, command: str, port: int = DEFAULT_PORT,
               timeout: float = 8.0) -> bytes:
    """
    Send one query and hand back its raw reply body.

    Shared by the small reads that need no bespoke socket dance of their own
    (dimming, demo mode). The guard and the login are identical either way, so
    duplicating them per command would just be three more places for the
    do-not-send check to be forgotten.
    """
    if _commands.is_dangerous(command):
        raise SysInfoError(f"{command} is on the do-not-send list")
    cmd_id = _commands.COMMANDS.get(command)
    if cmd_id is None:
        raise SysInfoError(f"{command} has no confirmed command id")

    xml = (f"<TMCmd><CmdID>{cmd_id}</CmdID><Params></Params>"
           f"<ESAID>{esa_id}</ESAID></TMCmd>").encode() + b"\x00"

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(_frame(TYPE_LOGIN, _login_payload(esa_id)))
        msg_type, _ = _read_frame(sock)
        if msg_type != TYPE_LOGIN_OK:
            raise SysInfoError(f"login refused (type 0x{msg_type:02x})")
        sock.sendall(_frame(TYPE_COMMAND, xml))
        msg_type, body = _read_frame(sock)
        if msg_type != TYPE_RESULT:
            raise SysInfoError(f"unexpected reply type 0x{msg_type:02x}")
        return body
    finally:
        sock.close()


def get_dimming(host: str, esa_id: str, port: int = DEFAULT_PORT,
                timeout: float = 8.0) -> int | None:
    """LED brightness as the device reports it. Read-only; see parse_dimming."""
    return parse_dimming(get_scalar(host, esa_id, "eCmdGetDimming", port, timeout))


def get_demo_mode(host: str, esa_id: str, port: int = DEFAULT_PORT,
                  timeout: float = 8.0) -> dict:
    """Whether this unit is in shop-display mode. See parse_demo_mode."""
    return parse_demo_mode(get_scalar(host, esa_id, "eCmdGetDemoModeInfo", port, timeout))


def get_droboapps(host: str, esa_id: str, port: int = DEFAULT_PORT,
                  timeout: float = 8.0) -> dict:
    """Ask the Drobo what is installed on it. Read-only."""
    if _commands.is_dangerous(COMMAND):
        raise SysInfoError(f"{COMMAND} is on the do-not-send list")
    cmd_id = _commands.COMMANDS.get(COMMAND)
    if cmd_id is None:
        raise SysInfoError(f"{COMMAND} has no confirmed command id")

    xml = (f"<TMCmd><CmdID>{cmd_id}</CmdID><Params></Params>"
           f"<ESAID>{esa_id}</ESAID></TMCmd>").encode() + b"\x00"

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(_frame(TYPE_LOGIN, _login_payload(esa_id)))
        msg_type, _ = _read_frame(sock)
        if msg_type != TYPE_LOGIN_OK:
            raise SysInfoError(f"login refused (type 0x{msg_type:02x})")
        sock.sendall(_frame(TYPE_COMMAND, xml))
        msg_type, body = _read_frame(sock)
        if msg_type != TYPE_RESULT:
            raise SysInfoError(f"unexpected reply type 0x{msg_type:02x}")
        return parse_droboapps(body)
    finally:
        sock.close()
