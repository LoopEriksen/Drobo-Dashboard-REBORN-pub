"""
The Drobo's own temperature and uptime -- eCmdGetSysInfo.

WHY THIS EXISTS, AND WHY WE THOUGHT IT WAS IMPOSSIBLE
-----------------------------------------------------
For a long time this project reported "this Drobo doesn't measure its own
temperature". That was wrong, and the reason is worth writing down so nobody
repeats it.

The status document the device pushes on TCP 5000 carries a per-drive
`mTemperature` field, and on a 5N it is `0` on every slot. From that we
concluded the hardware had no thermal sensor. It does -- it just isn't in that
document. The chassis temperature comes from a *command*, `eCmdGetSysInfo`, on
TCP 5001.

The second mistake was subtler. Early probes of `eCmdGetSysInfo` looked like
they succeeded about one time in six, which pointed at the RSA challenge-
response that gates the privileged commands. They were actually succeeding far
more often than that; the replies were simply being read wrongly.

`eCmdGetConfig` puts real XML children inside `<ResultDetails>`. `eCmdGetSysInfo`
puts an **entity-escaped XML document** there instead -- `&lt;Temperature&gt;`,
not `<Temperature>`. Anything looking for a `<Temperature>` element found
nothing and called the command unanswered. Measured properly, with the inner
document unescaped and parsed a second time, it answers **15 out of 15**.

So: this needs no key, no handshake, and no firmware change. Two different
commands on the same channel just encode their results two different ways, and
that is the thing to check first if another command ever looks flaky.

Read-only. Sends one command, which is a query.
"""

from __future__ import annotations

import html
import socket
import xml.etree.ElementTree as ET

from . import commands as _commands
from .config_cmd import (
    DEFAULT_PORT,
    TYPE_COMMAND,
    TYPE_LOGIN,
    TYPE_LOGIN_OK,
    TYPE_RESULT,
    ConfigError,
    _frame,
    _login_payload,
    _read_frame,
)

# Anything outside this is not a chassis temperature -- it's a parse that went
# wrong, or a field that means something else. A Drobo that genuinely reached
# 95 C would have shut itself down long before reporting it.
PLAUSIBLE_C = (1, 95)


class SysInfoError(ConfigError):
    """The device would not, or could not, report its system information."""


def _inner_document(details: ET.Element) -> ET.Element | None:
    """
    Get at the document hiding inside <ResultDetails>.

    Two shapes are possible and both are seen in practice:

      * real child elements  -- what eCmdGetConfig returns
      * escaped XML as text  -- what eCmdGetSysInfo returns

    Handle both, so this keeps working if a firmware revision changes its mind.
    """
    if len(details):
        return details

    text = (details.text or "").strip()
    if not text:
        return None
    # html.unescape rather than a manual replace: the device also emits &quot;
    # inside the inner XML declaration.
    unescaped = html.unescape(text).strip()
    # Drop an inner <?xml ...?> declaration; ElementTree rejects one that isn't
    # at the very start of the string it is handed.
    if unescaped.startswith("<?xml"):
        end = unescaped.find("?>")
        if end != -1:
            unescaped = unescaped[end + 2:].strip()
    if not unescaped:
        return None
    try:
        return ET.fromstring(unescaped)
    except ET.ParseError as exc:
        raise SysInfoError(f"could not read the system-info document: {exc}") from exc


def _int_or_none(node: ET.Element | None, tag: str) -> int | None:
    """A missing field and a field we can't read both mean 'no answer', not 0."""
    if node is None:
        return None
    found = node.find(tag)
    if found is None or not (found.text or "").strip():
        return None
    try:
        return int((found.text or "").strip())
    except ValueError:
        return None


def parse_sysinfo(payload: bytes) -> dict:
    """
    Turn an eCmdGetSysInfo result into {'temperature_c', 'uptime_seconds'}.

    Separate from the socket work so it is testable with no device present.
    Either value may be None -- the device answering but omitting a field is a
    real case, and is not an error.
    """
    text = payload.rstrip(b"\x00").rstrip()
    if not text:
        return {"temperature_c": None, "uptime_seconds": None}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SysInfoError(f"malformed result XML: {exc}") from exc

    details = root.find("ResultDetails")
    if details is None:
        raise SysInfoError("result carried no <ResultDetails>")

    inner = _inner_document(details)
    # The device nests <SysInfo> inside <ResultDetails>; tolerate it being
    # either the root of the inner document or a child of it.
    node = inner
    if inner is not None and inner.tag != "SysInfo":
        node = inner.find("SysInfo") or inner

    temp = _int_or_none(node, "Temperature")
    if temp is not None and not (PLAUSIBLE_C[0] <= temp <= PLAUSIBLE_C[1]):
        # Report nothing rather than something wrong. A bogus reading on a
        # dashboard is worse than a blank one.
        temp = None

    uptime = _int_or_none(node, "UpTime")
    if uptime is not None and uptime < 0:
        uptime = None

    return {"temperature_c": temp, "uptime_seconds": uptime}


def parse_performance(payload: bytes) -> dict:
    """
    Turn an eCmdGetPerformance result into throughput and IOPS counters.

    Returns {'read_mb_per_s', 'write_mb_per_s', 'iops', 'tier_iops'},
    any of which may be None if the device omitted it.

    `ReadThroughout` / `WriteThroughout` are Drobo's own spelling of
    "throughput" -- kept verbatim, like `eCmdGetLunInitatorExtraInfo`.

    UNITS: MEGABYTES per second, inferred -- not stated by the device
    -------------------------------------------------------------------------
    The device sends bare integers with no unit anywhere in the document. These
    were briefly modelled as bytes/second, which a measurement then disproved.

    Measured 2026-07-26, reading files off the array over SMB while polling:

        idle                    read 0    iops 2
        under sustained load    read 10   iops 21

    A reading of 10 cannot be bytes/second -- that would be ten bytes. The unit
    that fits is megabytes/second: this unit's network port has negotiated
    100 Mbit/s (see the link warning in shares.py), a ceiling of about
    12.5 MB/s, and the device reported 10 while genuinely saturated. So MB/s
    fits the observed ceiling almost exactly, and nothing else does.

    Still inferred rather than confirmed, because Drobo never documented it.
    Treated as a magnitude worth showing, not a figure to do arithmetic on.

    THE COUNTERS LAG, BADLY
    -------------------------------------------------------------------------
    Same measurement: the reading stayed at 0 for the first ~24 seconds of
    heavy load before rising, and was still reporting 10 a full 25 seconds
    AFTER the load stopped. So this is a slow-moving average, not an
    instantaneous rate. Anything displaying it should say "recent activity"
    rather than implying live throughput, and should never be used to decide
    whether the array is busy right now.
    """
    text = payload.rstrip(b"\x00").rstrip()
    if not text:
        return {"read_mb_per_s": None, "write_mb_per_s": None,
                "iops": None, "tier_iops": None}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SysInfoError(f"malformed result XML: {exc}") from exc

    details = root.find("ResultDetails")
    if details is None:
        raise SysInfoError("result carried no <ResultDetails>")

    inner = _inner_document(details)
    node = inner
    if inner is not None and inner.tag != "Performance":
        node = inner.find("Performance") or inner

    def non_negative(tag: str) -> int | None:
        v = _int_or_none(node, tag)
        # A negative rate is meaningless; report nothing rather than nonsense.
        return None if v is None or v < 0 else v

    return {
        "read_mb_per_s": non_negative("ReadThroughout"),
        "write_mb_per_s": non_negative("WriteThroughout"),
        "iops": non_negative("Iops"),
        "tier_iops": non_negative("TierIOps"),
    }


def _command_frame(name: str, esa_id: str) -> bytes:
    """Build one command frame, resolving the id by NAME and refusing writes."""
    if name in _commands.DO_NOT_SEND:
        raise SysInfoError(f"{name} is on the do-not-send list")
    cmd_id = _commands.COMMANDS.get(name)
    if cmd_id is None:
        raise SysInfoError(f"{name} has no confirmed command id")
    xml = (f"<TMCmd><CmdID>{cmd_id}</CmdID><Params></Params>"
           f"<ESAID>{esa_id}</ESAID></TMCmd>").encode() + b"\x00"
    return _frame(TYPE_COMMAND, xml)


def _ask(sock, name: str, esa_id: str) -> bytes:
    """Send one command on an already-logged-in socket and return its body."""
    sock.sendall(_command_frame(name, esa_id))
    msg_type, body = _read_frame(sock)
    if msg_type != TYPE_RESULT:
        raise SysInfoError(f"unexpected reply type 0x{msg_type:02x} for {name}")
    if not body:
        raise SysInfoError(f"device returned nothing for {name}")
    return body


def get_sysinfo(host: str, esa_id: str, port: int = DEFAULT_PORT,
                timeout: float = 8.0) -> dict:
    """
    Ask the Drobo how warm it is and how long it has been up. Read-only.

    Raises SysInfoError on anything unexpected. Callers should treat a failure
    as "unavailable right now" and carry on monitoring -- never as a fault.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(_frame(TYPE_LOGIN, _login_payload(esa_id)))
        msg_type, _ = _read_frame(sock)
        if msg_type != TYPE_LOGIN_OK:
            raise SysInfoError(f"login refused (type 0x{msg_type:02x})")
        return parse_sysinfo(_ask(sock, "eCmdGetSysInfo", esa_id))
    finally:
        sock.close()


def get_sysinfo_and_performance(host: str, esa_id: str,
                                port: int = DEFAULT_PORT,
                                timeout: float = 8.0) -> dict:
    """
    Both readings over ONE connection: temperature, uptime and throughput.

    Deliberately not two calls. This device gets tired: after roughly ninety
    command-port connections in an hour it stopped answering `eCmdGetSysInfo`
    entirely and took about half an hour to recover (docs/command-surface.md).
    Connection count is the resource worth conserving, so we log in once and
    ask twice -- one extra command on a socket we already hold is nearly free,
    a second socket is not.

    Performance failing does NOT lose the temperature. The sysinfo reading is
    taken first and returned even if the performance command then errors, since
    temperature is the one with an alert rule attached to it.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(_frame(TYPE_LOGIN, _login_payload(esa_id)))
        msg_type, _ = _read_frame(sock)
        if msg_type != TYPE_LOGIN_OK:
            raise SysInfoError(f"login refused (type 0x{msg_type:02x})")

        out = dict(parse_sysinfo(_ask(sock, "eCmdGetSysInfo", esa_id)))
        out["performance"] = None
        try:
            out["performance"] = parse_performance(
                _ask(sock, "eCmdGetPerformance", esa_id))
        except (SysInfoError, OSError):
            pass  # keep the temperature we already have
        return out
    finally:
        sock.close()
