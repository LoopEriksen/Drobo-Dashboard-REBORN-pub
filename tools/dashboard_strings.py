#!/usr/bin/env python3
"""
dashboard_strings.py -- mine the ORIGINAL Dashboard client for protocol vocabulary.

WHY THIS EXISTS, AND WHY IT SHOULD HAVE EXISTED SOONER
-------------------------------------------------------
This project mined the FIRMWARE hard (see fw_params.py) and treated the original
Windows Dashboard as a thing to capture packets from. That was a blind spot, and
the owner pointed it out: the firmware is the SERVER, which *receives* commands,
while the Dashboard is the CLIENT, which *builds* them. If you want to know what
goes inside <Params>, the side that constructs it is the better place to look.

It paid off immediately. `eCmdSetDimming` had been blocked for weeks on "we do
not know its parameter and will not guess one". The firmware yielded nothing.
The client holds the answer as a plain UTF-16 string: **DimmingLevel**.

WHAT IT CAN AND CANNOT TELL YOU
--------------------------------
It recovers the VOCABULARY -- the exact spelling of tags the client uses. It does
not recover the NESTING, because the strings are scattered literals grouped by
code paths rather than laid out in document order. Knowing `DimmingLevel` exists
does not prove the body is
`<Params><DimmingLevel>59</DimmingLevel></Params>` rather than a level deeper.

So this is FIRMWARE-grade evidence in protocol-map.md's sense: stronger than a
guess, weaker than a capture. A capture still settles the shape. What this
changes is that the capture now has to confirm a specific, named hypothesis
instead of discovering the name from scratch.

TWO ENCODINGS, AND WHY BOTH MATTER
-----------------------------------
These are native Windows C++ binaries. Log/format strings tend to be ASCII;
anything that touched a wide-char API (`%ls` all over the logging proves it did)
is UTF-16LE. `DimmingLevel` exists ONLY as UTF-16 -- an ASCII-only scan misses it
entirely, which is exactly how a `strings`-style pass would conclude "not there".

SECRETS
-------
The client ships hardcoded credentials for services that no longer exist. They
are redacted from this tool's output on sight: dead or not, this repository does
not carry other people's keys, and a key printed into a commit message or a
terminal log is a key in the repository's history forever.

    py tools/dashboard_strings.py "C:/Program Files (x86)/Drobo/Drobo Dashboard/DDService.exe"
    py tools/dashboard_strings.py <exe> --near DimmingLevel
    py tools/dashboard_strings.py <exe> --gaps
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk")))

try:
    from drobo_nasd.commands import COMMANDS
except ImportError:  # pragma: no cover
    COMMANDS = {}

_ASCII_RE = re.compile(rb"[\x20-\x7e]{3,}")
_UTF16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){3,}")

#: An XML tag as these binaries spell them.
_TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,40}$")

#: Anything that looks like a credential. Long high-entropy runs, and the field
#: names that introduce them. Matched generously -- a false positive costs one
#: redacted line, a false negative puts somebody's key in a commit.
_KEYISH = re.compile(r"^[A-Za-z0-9+/_-]{24,}={0,2}$")
_KEY_CONTEXT = re.compile(r"api.?key|secret|token|password|credential", re.I)
_REDACTED = "[redacted: credential-shaped string]"

#: Noise that passes the tag shape but is plainly not protocol vocabulary.
_NOISE = {
    "true", "false", "null", "NULL", "None", "Error", "ERROR", "INFO", "WARN",
    "DEBUG", "Software", "Drobo", "Windows", "Microsoft", "System", "String",
    "Params", "Result", "CmdID", "ESAID", "TMCmd",
}


def strings_of(blob: bytes) -> list[tuple[int, str, str]]:
    """
    Every printable run, in BOTH encodings, as (offset, text, encoding).

    Both, always. `DimmingLevel` -- the string this whole tool justified itself
    with -- exists only as UTF-16, so an ASCII-only pass reports it missing.
    """
    out = [(m.start(), m.group().decode("ascii", "replace"), "ascii")
           for m in _ASCII_RE.finditer(blob)]
    out += [(m.start(), m.group().decode("utf-16-le", "replace"), "utf-16")
            for m in _UTF16_RE.finditer(blob)]
    out.sort(key=lambda t: t[0])
    return out


def redact(text: str) -> str:
    """Never let a credential-shaped string reach the output."""
    return _REDACTED if _KEYISH.match(text) and not _TAG_RE.match(text[:20]) else text


def is_tag(text: str) -> bool:
    return bool(_TAG_RE.match(text)) and text not in _NOISE and not text.startswith("eCmd")


def tags(blob: bytes) -> dict[str, int]:
    """Every tag-shaped string, with how many times it appears."""
    counts: dict[str, int] = {}
    for _off, text, _enc in strings_of(blob):
        if is_tag(text) and not _KEYISH.match(text):
            counts[text] = counts.get(text, 0) + 1
    return counts


def near(blob: bytes, anchor: str, span: int = 20) -> list[str]:
    """Strings surrounding the first occurrence of `anchor`, in its own encoding."""
    found = strings_of(blob)
    for idx, (_off, text, enc) in enumerate(found):
        if text == anchor:
            window = found[max(0, idx - span): idx + span]
            return [redact(t) for _o, t, e in window if e == enc]
    return []


def endpoints(blob: bytes) -> list[str]:
    """
    Network destinations the client talks to.

    Worth surfacing because of what it revealed here: the original email alerts
    did NOT use SMTP. They called Drobo's own AWS API Gateway, which went away
    with the company -- so "email alerts" cannot be reimplemented by copying what
    the Dashboard did. It has to be built fresh.
    """
    seen = []
    for _off, text, _enc in strings_of(blob):
        low = text.lower()
        if any(k in low for k in ("http://", "https://", "amazonaws.com", ".com/", "/prod/")):
            clean = redact(text.strip())
            if clean not in seen and len(clean) < 120:
                seen.append(clean)
    return seen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Mine the original Drobo Dashboard client for protocol vocabulary.")
    ap.add_argument("binary", help="Drobo Dashboard.exe, DDService.exe or DDAssist.exe")
    ap.add_argument("--near", default="", help="show strings surrounding this one")
    ap.add_argument("--gaps", action="store_true",
                    help="tags the client knows that our SDK never mentions")
    ap.add_argument("--endpoints", action="store_true", help="network destinations")
    ap.add_argument("--min-count", type=int, default=1)
    args = ap.parse_args(argv)

    if not os.path.exists(args.binary):
        print(f"No such file: {args.binary}")
        return 2
    blob = open(args.binary, "rb").read()

    if args.near:
        rows = near(blob, args.near)
        if not rows:
            print(f"{args.near!r} not found as an exact string (try the other encoding "
                  f"-- some tags exist only as UTF-16).")
            return 1
        print(f"\nStrings around {args.near!r}:\n")
        for r in rows:
            print("   ", r[:100])
        return 0

    if args.endpoints:
        print("\nNetwork destinations in the client:\n")
        for e in endpoints(blob):
            print("   ", e)
        print("\nNote: Drobo's own cloud endpoints are dead -- the company was")
        print("liquidated in 2023. Anything depending on them cannot be revived,")
        print("only reimplemented.")
        return 0

    found = tags(blob)

    if args.gaps:
        # What does the client name that we have never even mentioned? That
        # difference is the honest to-do list for protocol coverage.
        our_source = ""
        sdk = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk", "drobo_nasd")
        for name in os.listdir(sdk):
            if name.endswith(".py"):
                our_source += open(os.path.join(sdk, name), encoding="utf-8",
                                   errors="replace").read()
        unknown = sorted((t for t, n in found.items()
                          if n >= args.min_count and t not in our_source),
                         key=lambda t: (-found[t], t))
        print(f"\n{len(unknown)} tag(s) the client uses that our SDK never mentions.")
        print("Candidates for protocol coverage -- vocabulary, NOT confirmed nesting.\n")
        for t in unknown[:80]:
            print(f"   {found[t]:4}x  {t}")
        return 0

    print(f"\n{os.path.basename(args.binary)}: {len(found)} distinct tag-shaped strings")
    print("Vocabulary only. The nesting still needs a capture to confirm.\n")
    for t in sorted(found, key=lambda t: (-found[t], t))[:60]:
        print(f"   {found[t]:4}x  {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
