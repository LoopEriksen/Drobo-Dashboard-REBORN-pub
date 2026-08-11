"""
nasd protocol client -- connection state machine.

Sequence, per docs/protocol-map.md ("Connection model"):

    1. discover           mDNS (_nasd._tcp, TCP 5000) -- or a manual IP,
                           per eCmdAddManualDiscoveryIP existing in the
                           command table.
    2. connect             TCP connect; SledNetAgent allocates a session.
    3. session + login     a fixed-size header (carrying a signature) is
                           sent, then a login packet; refused with
                           "No session yet" if skipped.
    4. command / response  each further message is preceded by a header
                           declaring its size; payload is DRINETTM XML.

CORRECTION, 2026-07-25/26: the command channel's real header and login shape
WERE captured and confirmed against live hardware -- see docs/protocol-map.md,
"THE COMMAND PROTOCOL -- SOLVED". It turned out to be simpler than the
NasdHeader candidates below guessed, and quite different in the details: a
16-byte header (`DRINETTM` signature + 1-byte type + 1-byte version + 2-byte
flags + big-endian u32 length), then, for login, a 220-byte payload that is
just the device's own serial (ESAID) twice, NUL-padded -- no per-connection
secret and no extra signature byte at all. That confirmed implementation
lives in `config_cmd.py` and is exercised for real by `sysinfo.py` and
`../shares.py`.

*This file* (`NasdClient`, built on `framing.NasdHeader`'s CANDIDATE classes)
was the speculative version written before that capture existed, and it was
never reconciled with what the capture actually showed -- none of its
NasdHeader candidates match the confirmed 16-byte header above, so sending
through *this* class still won't talk to a real Drobo. It is kept for its
test coverage of the command/envelope/framing pieces that don't depend on
the header, not as the way to reach real hardware -- use `config_cmd.py` (or
`sysinfo.py`) for that instead.
"""

from __future__ import annotations

import enum
import socket
from dataclasses import dataclass
from typing import Callable, Optional, Union

from . import commands, envelope, framing


class NasdError(Exception):
    """Base class for nasd client errors."""


class DangerousCommandError(NasdError):
    """Raised when asked to send a command in commands.DO_NOT_SEND."""


class NotConnectedError(NasdError):
    """Raised when a state-machine method is called out of order."""


class State(enum.Enum):
    NEW = "new"
    CONNECTED = "connected"
    LOGGED_IN = "logged_in"
    CLOSED = "closed"


@dataclass
class DiscoveredHost:
    host: str
    port: int = 5000
    name: str = ""


class NasdClient:
    """
    Drives the discover -> connect -> session/login -> command -> response
    state machine documented in protocol-map.md.

    `header_factory` is a callable (payload_length: int) -> bytes -- normally
    one of framing.NasdHeader's CANDIDATE constructors -- injected so the
    header is the only unconfirmed dependency this class has.
    """

    DEFAULT_PORT = 5000

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        esaid: str = "",
        header_factory: Optional[Callable[[int], bytes]] = None,
        timeout: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.esaid = esaid
        self.timeout = timeout
        self._header_factory = header_factory or framing.NasdHeader.candidate_a_signature_then_length
        self._sock: Optional[socket.socket] = None
        self.state = State.NEW

    # -- step 1: discover -----------------------------------------------------

    @staticmethod
    def discover(timeout: float = 3.0) -> list:
        """
        mDNS discovery of `_nasd._tcp` (protocol-map.md's Transport table).
        Deliberately NOT reimplemented here: tools/drobo_probe.py already
        does mDNS discovery for this project. This staticmethod exists so the
        state-machine sequence is documented in one place; wire it to
        tools/drobo_probe.py's discovery once this client is otherwise ready.

        eCmdAddManualDiscoveryIP / eCmdGetManualDiscoveryIPList in the command
        table confirm a manual-IP path also exists, so mDNS is not the only
        option if Bonjour is unreliable.
        """
        raise NotImplementedError(
            "mDNS discovery lives in tools/drobo_probe.py; NasdClient accepts a "
            "host/port directly via its constructor."
        )

    # -- step 2: connect --------------------------------------------------

    def connect(self) -> None:
        if self.state != State.NEW:
            raise NasdError(f"connect() called from state {self.state}, expected {State.NEW}")
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.state = State.CONNECTED

    # -- step 3: session + login ------------------------------------------

    def login(self, username: str = "", password: str = "") -> None:
        """
        Send the header + login packet and mark the session logged in.

        TODO(packet-capture): the login packet's actual contents (encoding,
        whether UCrypto obscures the password, any session token returned)
        are unconfirmed -- protocol-map.md's Authentication table lists this
        as still needing a capture. _build_login_payload() below is a
        placeholder that proves the plumbing, not the wire format.
        """
        if self.state != State.CONNECTED:
            raise NotConnectedError(f"login() called from state {self.state}, expected {State.CONNECTED}")
        if self._sock is None:
            raise NotConnectedError("no socket -- call connect() first")

        login_payload = self._build_login_payload(username, password)
        header = self._header_factory(len(login_payload))
        self._sock.sendall(header + login_payload)

        # A real implementation reads and validates a login response here
        # ("cannot send login response (error %d)" in the daemon's own
        # strings proves one exists). Skipped: the response shape is exactly
        # as unconfirmed as the request shape.
        self.state = State.LOGGED_IN

    @staticmethod
    def _build_login_payload(username: str, password: str) -> bytes:
        """
        GUESS ONLY -- see login()'s docstring. Nul-separated username/password
        is the simplest thing that could work and nothing more; there is no
        firmware or capture evidence for this exact shape.
        """
        return f"{username}\x00{password}\x00".encode("utf-8")

    # -- step 4: command / response ------------------------------------------

    def send_command(self, command: Union[int, str], params: Optional[dict] = None) -> envelope.Envelope:
        """Build, frame, and send a DRINETTM command; return the parsed
        response. Refuses anything in commands.DO_NOT_SEND."""
        if self.state != State.LOGGED_IN:
            raise NotConnectedError(f"send_command() called from state {self.state}, expected {State.LOGGED_IN}")

        # Safety guard comes before the socket check, deliberately: refusing a
        # dangerous command must not depend on how far connection setup got.
        name = command if isinstance(command, str) else commands.ID_TO_NAME.get(command)
        if name is not None and name in commands.DO_NOT_SEND:
            raise DangerousCommandError(f"refusing to send {name}: it is in commands.DO_NOT_SEND")

        if self._sock is None:
            raise NotConnectedError("no socket -- call connect() and login() first")

        message = envelope.build_command(self.esaid, command, params)
        payload = message.encode("utf-8")
        header = self._header_factory(len(payload))
        self._sock.sendall(header + payload)
        return self._read_response()

    def _read_response(self) -> envelope.Envelope:
        """
        NOT implemented: reading a response requires knowing the response's
        header/framing, which is exactly the piece docs/protocol-map.md marks
        unconfirmed. This is the other half of "filling in NasdHeader makes
        this class functional" -- see nasd/README.md.
        """
        raise NotImplementedError(
            "reading a response needs the confirmed NasdHeader framing -- see "
            "framing.py and nasd/README.md for how to close this gap"
        )

    # -- teardown -------------------------------------------------------------

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self.state = State.CLOSED

    def __enter__(self) -> "NasdClient":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
