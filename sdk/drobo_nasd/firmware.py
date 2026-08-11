"""
What the running firmware version means, and what it does not.

The device reports a version string like `4.3.1-8.126.117497` and every UI in
this project already displays it. A version number on its own tells you nothing
unless you happen to know this hardware's history, and that history has one fact
in it worth surfacing:

  4.3.0 (April 2022) and 4.3.1 (May 2022) were the last firmware releases for
  the 5N / 5N2 / B810n, and Drobo's own engineers PULLED both shortly after
  release over user-reported problems. Drobo filed for bankruptcy the following
  month and never shipped a fix. The community's last-known-good is 4.2.2.

So a unit sitting on 4.3.x is running a build its own vendor withdrew, and will
never receive a replacement. That is worth knowing and it is not an emergency:
plenty of 4.3.1 units have run for years without trouble, and this project has
read one of them extensively.

WHAT THIS MODULE WILL NOT DO
----------------------------
It will not tell you to downgrade. Downgrading means writing an unsigned image
from a third-party mirror to the boot storage of an irreplaceable device with no
vendor recovery tool -- the single most dangerous thing anyone could do with
this hardware, in exchange for firmware that is either the one you have or a
different old build. `commands.DO_NOT_SEND` refuses every flashing command in
the table, permanently, and this module exists to inform rather than to set up
that decision.

It also does not know what is WRONG with 4.3.x. "Pulled over user-reported
problems" is the whole of the public record; no changelog, no bug list, no
advisory was ever published, and Drobo's support site is gone. Guessing at
symptoms would be inventing a fault report, so the advice stops at "keep good
backups", which is true on any firmware.

Version strings are compared numerically on the leading dotted components, so
`4.3.1-8.126.117497` and `4.3.1` are the same release. Anything unparseable is
UNKNOWN rather than assumed safe or assumed risky.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Releases the vendor withdrew after shipping them. Not "buggy firmware we
#: measured" -- we have measured no faults on 4.3.1 at all -- but "the vendor
#: took these down and then ceased to exist", which is a different and more
#: durable statement.
PULLED = ((4, 3, 0), (4, 3, 1))

#: The version the community settled on as the last one nobody reported
#: trouble with. Recorded for context in the advisory text; this module never
#: recommends moving to it. See the module docstring.
LAST_KNOWN_GOOD = "4.2.2"

RISK_NONE = "none"        # not one of the pulled builds
RISK_WITHDRAWN = "withdrawn"  # 4.3.0 / 4.3.1
RISK_UNKNOWN = "unknown"  # no version, or one we can't parse

_VERSION_RE = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass(frozen=True)
class Advisory:
    """What we can say about a firmware version. Never an instruction."""

    version: str
    #: (major, minor, patch) with missing components as 0, or None if the
    #: string didn't start with a number at all.
    parsed: tuple[int, int, int] | None
    risk: str
    headline: str
    detail: str

    @property
    def notable(self) -> bool:
        """True when there is something worth putting on screen."""
        return self.risk == RISK_WITHDRAWN

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "release": ".".join(str(n) for n in self.parsed) if self.parsed else None,
            "risk": self.risk,
            "notable": self.notable,
            "headline": self.headline,
            "detail": self.detail,
            "last_known_good": LAST_KNOWN_GOOD,
        }


def parse_version(version: str) -> tuple[int, int, int] | None:
    """
    The release part of a firmware string, as numbers.

    The device sends `4.3.1-8.126.117497`: a three-part release, then a build
    identifier after a dash that varies between units of the same release. Only
    the release matters for this comparison, so the tail is dropped rather than
    compared -- treating build numbers as version components would make two
    units on the same firmware look like they were on different ones.

    Returns None for anything that doesn't begin with a number, including the
    empty string a device that hasn't answered yet leaves behind.
    """
    match = _VERSION_RE.match(version or "")
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def assess(version: str) -> Advisory:
    """
    Classify a firmware version string. Never raises.

    Deliberately quiet on everything except the withdrawn builds. A dashboard
    that comments on healthy firmware trains people to ignore it, and there is
    nothing to say about 4.2.2 beyond the number already on screen.
    """
    parsed = parse_version(version)

    if parsed is None:
        return Advisory(
            version=version or "", parsed=None, risk=RISK_UNKNOWN,
            headline="", detail="",
        )

    if parsed in PULLED:
        release = ".".join(str(n) for n in parsed)
        return Advisory(
            version=version, parsed=parsed, risk=RISK_WITHDRAWN,
            headline=f"This Drobo runs firmware {release}, which Drobo withdrew.",
            detail=(
                f"{release} was pulled by Drobo's own engineers shortly after "
                f"release over problems users reported, and the company went into "
                f"bankruptcy the month after -- so no fix was ever shipped and none "
                f"ever will be. The last version nobody reported trouble with was "
                f"{LAST_KNOWN_GOOD}. "
                # The honest half, and the reason this is not an alarm:
                f"That is worth knowing rather than worth acting on: what was "
                f"actually wrong with it was never published, plenty of units have "
                f"run this build for years, and this one is reporting a healthy "
                f"array right now. Changing firmware means writing an unsigned "
                f"image to hardware with no recovery tool, so this software will "
                f"not do it and does not suggest you do. Keep good backups -- which "
                f"is true on any firmware."
            ),
        )

    return Advisory(version=version, parsed=parsed, risk=RISK_NONE,
                    headline="", detail="")
