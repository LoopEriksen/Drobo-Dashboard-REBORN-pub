"""
eCmdIdentify -- make the Drobo flash its lights.

THE FIRST THING THIS PROJECT EVER WRITES TO THE HARDWARE
--------------------------------------------------------
Everything else here reads. This module is the exception, and it lives in its
own file specifically so that exception is easy to find and easy to audit --
rather than being one more branch buried inside a module that otherwise only
asks questions.

Why this command and not another. Of the 103 commands recovered from firmware,
this is the least consequential one that does anything at all: it blinks the
front-panel lights so you can tell which box is which, and it stops on its own.
It cannot touch the disk pack, cannot affect availability, and there is no
state to corrupt. If it goes wrong the worst outcome is that some lights do
not blink.

That makes it the right way to prove the write path works before anything with
real stakes is attempted. See commands.SAFE_WRITES for the four tests a
command has to pass to be sent at all.

THE PARAMETER -- FOUND 2026-08-06
---------------------------------
For most of this module's life it sent an EMPTY <Params></Params>, because the
firmware table gives the command's id (26) and its name but not its arguments,
and this project does not guess parameters. The device accepted every one of
those sends and no light ever blinked.

The answer was not in the firmware and not on the wire -- it was in the
original Dashboard's own binary. `Drobo Dashboard.exe` 3.5.0 contains
`IdentifyInterval` inside a contiguous run of <Params> tag names
(ScaleFactor, ForceShutdown, ForceRestart, LunNum, RotationalSpeed...). That
run is the tag-name pool, so IdentifyInterval is an element name, and Identify
is the only command it can belong to.

The theory was that this explained the silence: an empty parameter block means
no interval, an interval read as zero means blink for zero time, so the
command would be accepted with nothing to see.

TESTED ON HARDWARE 2026-08-07. THE THEORY IS WRONG.

Both forms were sent to a live 5N, 25 seconds apart, control first, with the
owner watching the unit. Neither blinked. The device accepted both and
returned `<Result>` 0x200000000 with an empty `<ResultDetails>` -- and that is
the same value eCmdGetSysInfo and eCmdGetConfig return when they work, so it
means "accepted", not "done". See envelope.RESULT_ACCEPTED.

`IdentifyInterval` is also absent from all three readable config sections, so
it is not a stored setting we could have been reading either.

WHAT IS ACTUALLY UNKNOWN, NOW STATED PROPERLY
---------------------------------------------
Nobody has ever seen the original Dashboard press this button. The
2026-08-06 capture contains no command 26 and no `dentify` anywhere in either
direction -- not because Dashboard avoids it, but because the operator could
not find the control. It is called **Blink Lights**, on the Tools page, and
that was only worked out afterwards.

So the chain from "Blink Lights" to "command 26" is inference: the binary has
`DroboDevice::Identify()` and lists `eCmdIdentify` among its command names.
Reasonable, and still unproven. The value of `IdentifyInterval`, its units,
and whether it even belongs to this command are all downstream of that
unproven link.

The measurement that settles all of it is one capture, thirty seconds long,
of somebody pressing Tools -> Blink Lights. Everything else here is guessing
at a question that a single packet would answer.

This module keeps sending the interval by default. It is harmless, it is the
best-supported guess available, and changing it back would discard evidence
for no gain -- but it is a guess, and the docstring above should not be read
as saying otherwise.

WHAT IS STILL A GUESS: THE UNITS
--------------------------------
The same binary's warning string says the lights "blink continuously for 15
minutes", so the Dashboard's own value is fifteen of something. Fifteen
minutes and nine hundred seconds are both consistent with everything known,
and nothing here decides between them.

DEFAULT_INTERVAL is therefore 15, which is the value the original product
used under whichever reading is correct: if the unit is minutes we match
Dashboard exactly, and if it is seconds we blink briefly and learn that from
watching. Both outcomes are harmless and reversible -- the mode ends by
itself, and the original UI stops it early by pressing the same button.

This is a departure from "never send a guessed parameter", and it is a
deliberate one, narrowly scoped to the single command whose entire effect is
light. The rule exists because a guessed parameter is an unreviewed
instruction to hardware holding someone's data. Identify cannot reach the disk
pack, cannot affect availability, and has no state to corrupt. Nothing else
gets this latitude.
"""
from __future__ import annotations

import re
import socket
import time

from . import commands as _commands
from . import envelope as _envelope

#: Pulled with a regex rather than an XML parse: this runs on a reply that may
#: be empty or truncated, and a parse failure there must not turn "the lights
#: may or may not be blinking" into an exception.
_RESULT_RE = re.compile(r"<Result>\s*(-?\d+)\s*</Result>")
from .config_cmd import (
    ConfigError,
    DEFAULT_PORT,
    TYPE_COMMAND,
    TYPE_LOGIN,
    TYPE_LOGIN_OK,
    TYPE_RESULT,
    _frame,
    _login_payload,
    _read_frame,
)

COMMAND = "eCmdIdentify"

#: The element the original Dashboard carries in <Params>. CONFIRMED as a tag
#: name from Drobo Dashboard.exe 3.5.0; see the module docstring.
INTERVAL_TAG = "IdentifyInterval"

#: What the original product used. Units unverified -- see the docstring.
DEFAULT_INTERVAL = 15

#: Sending zero is how "Turn Blink Lights OFF" most plausibly works, the UI
#: being a toggle driven by one command. GUESS: no capture shows the off
#: press. It is allowed because zero is what an absent interval already
#: behaved like, so it cannot be worse than what this module sent for months.
STOP_INTERVAL = 0

#: Refuse anything wilder than this. Not a device limit -- nothing tells us
#: what the device's limit is -- but a typo guard. If the unit turns out to be
#: seconds, this still allows well over a day of blinking; if minutes, far
#: longer than anyone means to ask for.
MAX_INTERVAL = 86_400


class IdentifyError(Exception):
    """Identify could not be sent, or the device refused it."""


class IdentifyParameterError(IdentifyError, ValueError):
    """
    The interval asked for is not a value we will send.

    Separate from IdentifyError so callers can tell "you asked for something
    impossible" apart from "the Drobo would not take it". The HTTP layer needs
    exactly that distinction -- the first is the caller's fault (400), the
    second is not (502) -- and getting it wrong makes a typo look like a
    hardware fault. Subclasses IdentifyError so existing `except IdentifyError`
    still catches it.
    """


def validate_interval(interval: int | None) -> int | None:
    """
    Check an interval and return it, or raise IdentifyParameterError.

    Public because the rule has to hold on paths that never build a packet.
    The agent's simulated driver is the case that forced this: it does not
    call `identify()` at all, so with the check buried in the XML builder the
    simulator cheerfully accepted an interval of -1 that real hardware would
    have refused. A simulator that is more permissive than the device is worse
    than no simulator, because it certifies things that do not work.
    """
    if interval is None:
        return None
    # bool first: it is an int subclass, so True would otherwise sail through
    # and be sent as 1 -- a value the caller never meant.
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise IdentifyParameterError(
            f"interval must be a whole number of units or None, not "
            f"{type(interval).__name__}")
    if interval < 0:
        raise IdentifyParameterError(f"interval must not be negative (got {interval})")
    if interval > MAX_INTERVAL:
        raise IdentifyParameterError(
            f"interval {interval} is beyond the {MAX_INTERVAL} sanity limit; "
            f"the units are not confirmed, so a large number here is more "
            f"likely a mistake than an intention")
    return interval


def _interval_xml(interval: int | None) -> str:
    """
    The <Params> block.

    `None` reproduces the pre-2026-08-06 behaviour exactly -- an empty
    parameter block. Kept, and kept easy to reach, because it is the control
    case: if a device blinks with an interval and not without one, that is the
    measurement that turns this module's central guess into a fact. A caller
    proving a negative needs to be able to send the old thing on purpose.
    """
    if validate_interval(interval) is None:
        return "<Params></Params>"
    return f"<Params><{INTERVAL_TAG}>{interval}</{INTERVAL_TAG}></Params>"


def identify(host: str, esa_id: str, port: int = DEFAULT_PORT,
             timeout: float = 8.0, _retry: bool = True,
             interval: int | None = DEFAULT_INTERVAL) -> dict:
    """
    Ask the Drobo to flash its lights. Returns a small result dict.

    `interval` is the value of <IdentifyInterval>. DEFAULT_INTERVAL (15) is
    what the original Dashboard used; STOP_INTERVAL (0) is the best guess at
    stopping an already-running blink; None sends an empty <Params>, which is
    what this module did before the tag was found and is the control case for
    proving the tag matters. See the module docstring for what is confirmed
    and what is not.

    `interval` is keyword-friendly but deliberately LAST in the signature, so
    every existing positional call keeps meaning what it meant.

    Raises IdentifyError if the command is not on the approved-write list, if
    the login is refused, or if the device answers with something unexpected.
    A failure here is never a reason to treat the array as unhealthy -- it
    means the lights did not blink, nothing more.

    DOES NOT RETRY, and `_retry` exists only so the measurement scripts can
    say so explicitly.

    An automatic retry was added here on 2026-07-27 and removed the same day,
    which is worth recording because the reasoning was wrong in an instructive
    way. The observation was real: over six sends, five were accepted in under
    a tenth of a second and one had the device close the connection
    mid-exchange. That was diagnosed as a transient drop and a 0.3 s retry
    added.

    Measuring properly did not support it. With the retry, 2 of 5 sends
    spaced ten seconds apart succeeded; without it, 1 of 5 -- noise, not a
    fix. And the refusals do not behave like a transient fault: sometimes the
    device accepts again after five seconds, sometimes it refuses through
    several attempts spanning minutes. Meanwhile ordinary reads
    (eCmdGetSysInfo, eCmdGetConfig) kept working throughout, so it is not the
    general command-port exhaustion documented in docs/command-surface.md.

    What it actually is remains unknown. The most likely explanation is that
    the device declines while a light sequence is already running, but that
    did not reproduce cleanly either.

    So: no retry. On a command port that measurably dislikes connection
    volume, doubling the attempts to work around a fault we cannot
    characterise is the wrong default. A caller who wants to try again can
    call again -- deliberately, and at a pace it chooses.

    Raises IdentifyError if the command is not on the approved-write list, if
    the login is refused, or if the device will not take it right now. None of
    those is a reason to treat the array as unhealthy: it means the lights did
    not blink, nothing more.
    """
    # Belt and braces. The guard is enforced in commands.py, but this is the
    # one code path in the project that sends a write, so it re-checks rather
    # than trusting that it was called correctly.
    if _commands.is_dangerous(COMMAND):
        raise IdentifyError(f"{COMMAND} is on the do-not-send list")
    if not _commands.is_safe_write(COMMAND):
        raise IdentifyError(
            f"{COMMAND} is not on the approved-write list (commands.SAFE_WRITES)")

    cmd_id = _commands.COMMANDS.get(COMMAND)
    if cmd_id is None:
        raise IdentifyError(f"{COMMAND} has no confirmed command id")

    # Validated BEFORE the socket is opened. A bad interval is a programming
    # error, and it should surface as one rather than as a connection that was
    # made, logged and then abandoned.
    params = _interval_xml(interval)

    xml = (f"<TMCmd><CmdID>{cmd_id}</CmdID>{params}"
           f"<ESAID>{esa_id}</ESAID></TMCmd>").encode() + b"\x00"

    try:
        return _send_once(host, port, esa_id, xml, timeout, cmd_id, interval)
    except (OSError, ConfigError) as exc:
        # Worth wording carefully: the Drobo is fine, and the array is fine.
        # It just would not take this particular command this particular time.
        raise IdentifyError(
            f"the Drobo did not accept Identify just now ({exc}). "
            f"Nothing is wrong with the array -- try again in a few seconds."
        ) from exc


def _send_once(host: str, port: int, esa_id: str, xml: bytes,
               timeout: float, cmd_id: int, interval: int | None = None) -> dict:
    """One attempt. Connection problems propagate for the caller to retry."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(_frame(TYPE_LOGIN, _login_payload(esa_id)))
        msg_type, _ = _read_frame(sock)
        if msg_type != TYPE_LOGIN_OK:
            raise IdentifyError(f"login refused (type 0x{msg_type:02x})")

        sock.sendall(_frame(TYPE_COMMAND, xml))
        msg_type, body = _read_frame(sock)
        if msg_type != TYPE_RESULT:
            raise IdentifyError(f"unexpected reply type 0x{msg_type:02x}")

        # An empty body is a perfectly good answer here. Most commands on this
        # firmware reply with nothing at all, and Identify has nothing to
        # report -- the result is a blinking light, not a document. So we
        # deliberately do NOT treat "no payload" as failure, unlike the read
        # commands where an empty body means the question went unanswered.
        text = (body or b"").rstrip(b"\x00").strip()

        # Read <Result>, which this function ignored entirely until 2026-08-07.
        # It reported {"sent": True} on the strength of a well-formed reply and
        # never looked at what the reply said -- so "Identify was accepted" had
        # been recorded twenty-odd times without anyone checking the device
        # agreed. It did agree, as it turns out. But that was luck, not
        # verification, and the same blind spot on a command that DOES refuse
        # would have been reported as success.
        #
        # Note the honest limit: matching RESULT_ACCEPTED means the device
        # raised no objection. Identify returns it and blinks nothing, so this
        # is not evidence the command did anything.
        result_code = None
        match = _RESULT_RE.search(text.decode("utf-8", "replace")) if text else None
        if match:
            try:
                result_code = int(match.group(1))
            except ValueError:
                result_code = None

        return {
            "sent": True,
            "command": COMMAND,
            "command_id": cmd_id,
            "result_code": result_code,
            "accepted": result_code == _envelope.RESULT_ACCEPTED if result_code is not None else None,
            # Reported back so a caller writing down a measurement records
            # what was actually asked for. "The lights blinked" is worth
            # nothing without the interval that produced it, and the whole
            # point of this parameter is that it is still being pinned down.
            "interval": interval,
            "device_replied": bool(text),
            "reply_bytes": len(text),
        }
    finally:
        sock.close()


def _main(argv: list[str] | None = None) -> int:
    """
    Fire Identify at a real Drobo, so somebody can stand next to it and watch.

        py -m drobo_nasd.identify <ip>              # the default, 15
        py -m drobo_nasd.identify <ip> --interval 0 # the stop guess
        py -m drobo_nasd.identify <ip> --none       # empty Params: the control
        py -m drobo_nasd.identify <ip> --compare    # control, pause, then 15

    `--compare` is the one worth running first, and the reason this entry
    point exists at all. The open question is not "does the command reach the
    device" -- it always has -- but whether the interval is what makes the
    lights move. Sending the two forms back to back, in a fixed order, with a
    pause long enough to tell them apart, is the whole experiment. Doing it by
    hand invites sending the same one twice and concluding nothing.

    Exit codes: 0 sent, 1 refused, 2 bad usage.
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="py -m drobo_nasd.identify",
        description="Make a Drobo blink its front lights, and watch what happens.")
    ap.add_argument("host", help="the Drobo's address")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"value for <{INTERVAL_TAG}> (default {DEFAULT_INTERVAL})")
    ap.add_argument("--none", action="store_true",
                    help="send an empty <Params>, as this module did before 2026-08-06")
    ap.add_argument("--compare", action="store_true",
                    help="send the empty form, wait, then the interval form")
    ap.add_argument("--gap", type=float, default=20.0,
                    help="seconds between the two halves of --compare (default 20)")
    args = ap.parse_args(argv)

    # Before any network contact. A usage error should be answered instantly
    # and locally, not after a connection attempt to storage hardware -- and
    # certainly not reported as "could not reach the Drobo", which is what
    # this printed until the CLI was smoke-tested.
    wanted = None if args.none else args.interval
    if not args.compare:
        try:
            validate_interval(wanted)
        except IdentifyParameterError as exc:
            print(exc)
            return 2

    from . import esatm
    try:
        esa = esatm.parse_identity(esatm.fetch_greeting(args.host)).get("esa_id")
    except OSError as exc:
        print(f"Could not reach {args.host} on the status port: {exc}")
        return 1
    if not esa:
        print(f"{args.host} answered but did not give a serial; cannot send commands.")
        return 1

    def fire(interval, label):
        print(f"\n  {label}")
        try:
            res = identify(args.host, esa, interval=interval)
        except IdentifyError as exc:
            print(f"    refused: {exc}")
            return False
        print(f"    sent, interval={res['interval']!r}"
              + (f", device replied {res['reply_bytes']} bytes" if res["device_replied"]
                 else ", device replied with nothing (normal for this command)"))
        return True

    if args.compare:
        print("WATCH THE DROBO. Two sends, and only the second carries an interval.")
        ok = fire(None, "1/2  empty <Params>  -- the old behaviour")
        # A real gap. The device has been seen to decline a second Identify
        # shortly after the first, and a blink that started on send 1 must be
        # over before send 2 or the comparison proves nothing.
        print(f"\n  ...waiting {args.gap:g}s so the two cannot be confused...")
        time.sleep(args.gap)
        ok = fire(DEFAULT_INTERVAL, f"2/2  <{INTERVAL_TAG}>{DEFAULT_INTERVAL}</...>") or ok
        print("\n  Which one blinked? That is the measurement.")
        return 0 if ok else 1

    return 0 if fire(wanted, f"sending interval={wanted!r}") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
