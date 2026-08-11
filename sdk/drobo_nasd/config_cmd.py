"""
Asking the Drobo for its configuration -- the first thing we *send* it.

Everything else in this project reads the status the device volunteers on TCP
5000. This module uses the command channel on TCP 5001, recovered 2026-07-25
(see docs/protocol-map.md). It sends exactly one command, eCmdGetConfig, which
is a read.

WHY THIS ONE COMMAND IS THE DEPENDABLE ONE
------------------------------------------
eCmdGetConfig answers reliably after a plain login -- measured repeatedly
against real hardware, including 3/3 at a moment when other commands had
stopped answering entirely. It is the command to build on.

An earlier version of this note said temperature was unreachable, gated behind
the RSA challenge-response. **That was wrong**, and the correction is in
nasd/sysinfo.py: eCmdGetSysInfo needs no handshake, and returns a real chassis
temperature. It only looked gated because its reply nests an *entity-escaped*
XML document inside <ResultDetails>, so code looking for a <Temperature>
element found nothing and recorded the command as unanswered.

What IS true is that eCmdGetSysInfo is not dependable the way this command is --
see sysinfo.py for the measurements and for why it is polled rarely.

CREDENTIALS -- READ THIS BEFORE CHANGING ANYTHING HERE
------------------------------------------------------
Asking for DRINasAdminConfig makes the Drobo hand back <Password>,
<EncryptedPassword> and <ValidPassword> to anyone who completes a login -- and
the login is just the device serial, which the Drobo gives to any
unauthenticated caller on port 5000.

**Measured 2026-07-26 on firmware 4.3.1**, and it is better news than we
previously recorded: <Password> comes back as sixteen *identical* characters --
a mask, not a credential -- and <EncryptedPassword> as a single character. This
firmware does not appear to disclose the real admin password. An earlier version
of this note asserted it handed them over "in the clear"; that was not measured,
and on this firmware it is wrong.

**The redaction stays anyway, and must not be removed.** We have checked one
section, on one firmware, on one device. We cannot promise the same for 4.2.x,
for a business-model Drobo, or for a section nobody has looked at yet. Costing
nothing to keep and being the only thing standing between a device quirk and a
credential in our logs, _strip_secrets() runs on every parsed document before it
is returned -- so those values never enter a Snapshot, never reach the JSON API,
never land in a log, and never sit in the history buffer.

Separately confirmed the same day: the admin account is **not** passwordless.
Presenting `admin` with an empty password makes the Drobo fall back to granting
a *guest* session rather than authenticating the account -- which is proof it
rejected the empty credential.

If you add a section here, assume it may contain credentials until you have
looked.
"""

from __future__ import annotations

import html
import re
import socket
import struct
import xml.etree.ElementTree as ET

from . import commands as _commands

SIGNATURE = b"DRINETTM"
HEADER_LEN = 16
DEFAULT_PORT = 5001
MAX_PAYLOAD = 4 * 1024 * 1024

TYPE_LOGIN = 0x07
TYPE_LOGIN_OK = 0x87
TYPE_COMMAND = 0x0A
TYPE_RESULT = 0x8A

# The login packet is the device serial twice, in two 20-byte NUL-padded slots,
# then padding to 220. Confirmed on the wire.
LOGIN_LEN = 220
_LOGIN_SLOT = 20

# Sections eCmdGetConfig understands. "admin" is deliberately last and
# deliberately noisy in the docs -- see the credentials note above.
SECTIONS = {
    "network": "Network",
    "shares": "DRIShareConfig",
    "admin": "DRINasAdminConfig",
}

# Any element whose name matches this never leaves this module with its value.
_SECRET = re.compile(r"(?i)pass|secret|credential|\bkey\b|token")
_REDACTED = "[redacted]"


class ConfigError(Exception):
    """The command channel refused us, or answered with something unusable."""


def _frame(msg_type: int, payload: bytes) -> bytes:
    return (SIGNATURE + bytes([msg_type, 0x01]) + b"\x00\x00"
            + struct.pack(">I", len(payload)) + payload)


def _login_payload(esa_id: str) -> bytes:
    raw = esa_id.encode("ascii", "ignore")
    if not raw or len(raw) > _LOGIN_SLOT:
        raise ConfigError(f"implausible device serial {esa_id!r}")
    buf = bytearray(LOGIN_LEN)
    buf[0:len(raw)] = raw
    buf[_LOGIN_SLOT:_LOGIN_SLOT + len(raw)] = raw
    return bytes(buf)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    head = _recv_exact(sock, HEADER_LEN)
    if len(head) < HEADER_LEN:
        raise ConfigError(f"short header: {len(head)} of {HEADER_LEN} bytes")
    if head[:8] != SIGNATURE:
        raise ConfigError(f"bad signature {head[:8]!r}")
    length = struct.unpack(">I", head[12:16])[0]
    if length > MAX_PAYLOAD:
        raise ConfigError(f"implausible payload length {length}")
    return head[8], _recv_exact(sock, length)


def _strip_secrets(node: ET.Element) -> None:
    """
    Replace the text of any credential-looking element, in place, before the
    document is turned into anything we keep. Recursive, so a password nested
    inside a share or user entry is caught too.

    Two things here are deliberate, and both were holes until 2026-07-26:

    1. A credential element is emptied ENTIRELY -- children removed, not just
       its own text replaced. Previously a `<Password>` carrying child elements
       rather than text kept those children verbatim, because the old code
       replaced `.text` (empty in that case) and then skipped descending. The
       value walked straight out through _to_dict.

    2. Text that turns out to be an escaped XML document is handled by
       _redact_escaped_document below, not here -- see the note there. That is
       the encoding this firmware genuinely uses.
    """
    for child in node:
        if _SECRET.search(child.tag):
            # Whatever hangs off a credential node is suspect, so nothing from
            # it survives: no text, no attributes, no children.
            child.text = _REDACTED
            child.tail = child.tail  # keep layout, drop nothing else
            child.attrib.clear()
            for grandchild in list(child):
                child.remove(grandchild)
            continue
        _strip_secrets(child)


def _redact_escaped_document(node: ET.Element) -> None:
    """
    Catch credentials hiding inside an entity-escaped inner document.

    WHY THIS EXISTS
    ---------------
    `_strip_secrets` matches on element *tags*. That works when the device
    returns real XML children -- which is what eCmdGetConfig does. But this
    firmware also returns results as an XML document *escaped into a text
    node*: `&lt;Password&gt;...&lt;/Password&gt;` rather than `<Password>`.
    That is how eCmdGetSysInfo replies (see nasd/sysinfo.py, where the same
    encoding is what made temperature look unobtainable for weeks).

    Against that shape `_strip_secrets` has nothing to match -- the element has
    no children -- so the whole document, credentials included, passed through
    untouched. Both this module's and nasd_sweep's docstrings claimed redaction
    ran on "every response ... with no exception". It didn't.

    So: any text that unescapes into something XML-shaped is parsed, redacted
    with the same rules, and re-serialized. If it cannot be parsed, it is
    replaced wholesale rather than trusted -- an unreadable blob that might be
    a credential document is not something to pass along on the hope that it
    isn't.
    """
    for child in node.iter():
        text = (child.text or "").strip()
        # NOTE: by the time we see it, ElementTree has ALREADY turned &lt; back
        # into '<' -- so look for a literal angle bracket, not for the entity.
        # (Checking for "&lt;" here was the first attempt at this fix, and it
        # never matched a single real document.)
        if not text or "<" not in text:
            continue
        inner = html.unescape(text).strip()
        if not inner.startswith("<"):
            continue
        # Drop an inner <?xml ...?> declaration; ElementTree rejects one that
        # isn't at the very start of the string it is handed.
        body = inner
        if body.startswith("<?xml"):
            end = body.find("?>")
            if end != -1:
                body = body[end + 2:].strip()
        try:
            sub = ET.fromstring(body)
        except ET.ParseError:
            # Unparseable but XML-shaped. Refuse to pass it on.
            if _SECRET.search(text):
                child.text = _REDACTED
            continue
        if _SECRET.search(sub.tag):
            child.text = _REDACTED
            continue
        _strip_secrets(sub)
        child.text = ET.tostring(sub, encoding="unicode")


def _to_dict(node: ET.Element):
    """XML subtree -> plain dict/list/str, with repeated tags becoming lists."""
    children = list(node)
    if not children:
        return (node.text or "").strip()
    out: dict = {}
    for child in children:
        value = _to_dict(child)
        if child.tag in out:
            if not isinstance(out[child.tag], list):
                out[child.tag] = [out[child.tag]]
            out[child.tag].append(value)
        else:
            out[child.tag] = value
    return out


def parse_config_result(payload: bytes) -> dict:
    """
    Turn a command result into a plain dict, credentials removed.

    Kept separate from the socket work so it can be tested against a recorded
    response with no device present.
    """
    text = payload.rstrip(b"\x00").rstrip()
    if not text:
        return {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ConfigError(f"malformed result XML: {exc}") from exc

    details = root.find("ResultDetails")
    if details is None:
        raise ConfigError("result carried no <ResultDetails>")

    # Redact BEFORE converting, so a secret never exists in our structures.
    # Both passes are needed: one for real child elements, one for an inner
    # document the device escaped into a text node.
    _strip_secrets(details)
    _redact_escaped_document(details)
    return _to_dict(details)


def get_config(host: str, esa_id: str, section: str = "network",
               port: int = DEFAULT_PORT, timeout: float = 10.0) -> dict:
    """
    Ask the Drobo for one configuration section. Read-only.

    section is a key of SECTIONS. Raises ConfigError on anything unexpected --
    callers should treat a failure as "this data is unavailable right now",
    never as a reason to stop monitoring.
    """
    if section not in SECTIONS:
        raise ConfigError(f"unknown section {section!r}; choose from {sorted(SECTIONS)}")

    name = "eCmdGetConfig"
    if name in _commands.DO_NOT_SEND:
        raise ConfigError(f"{name} is on the do-not-send list")
    cmd_id = _commands.COMMANDS.get(name)
    if cmd_id is None:
        raise ConfigError(f"{name} has no confirmed command id")

    tag = SECTIONS[section]
    xml = (f"<TMCmd><CmdID>{cmd_id}</CmdID><ESAID>{esa_id}</ESAID>"
           f"<Params><{tag}>{tag}</{tag}></Params></TMCmd>").encode() + b"\x00"

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(_frame(TYPE_LOGIN, _login_payload(esa_id)))
        msg_type, _ = _read_frame(sock)
        if msg_type != TYPE_LOGIN_OK:
            raise ConfigError(f"login refused (type 0x{msg_type:02x})")

        sock.sendall(_frame(TYPE_COMMAND, xml))
        msg_type, body = _read_frame(sock)
        if msg_type != TYPE_RESULT:
            raise ConfigError(f"unexpected reply type 0x{msg_type:02x}")
        if not body:
            raise ConfigError(f"device returned nothing for {section!r}")
        return parse_config_result(body)
    finally:
        sock.close()
