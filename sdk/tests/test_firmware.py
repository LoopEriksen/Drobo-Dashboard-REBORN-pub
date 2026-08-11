"""
Offline tests for firmware.py -- what the running version means.

    py test_firmware.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import firmware

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


print("\n== the release, separated from the build ==")
# The device sends release-build. Two units on the SAME firmware carry
# different build tails, so comparing the whole string would make them look
# like different releases.
check("a real device string parses to its release",
      firmware.parse_version("4.3.1-8.126.117497") == (4, 3, 1))
check("the build tail is ignored, not compared",
      firmware.parse_version("4.3.1-8.126.117497") == firmware.parse_version("4.3.1"))
check("a two-part version fills the missing component",
      firmware.parse_version("4.3") == (4, 3, 0))
check("a one-part version too", firmware.parse_version("4") == (4, 0, 0))
check("an empty string parses to nothing", firmware.parse_version("") is None)
check("a non-numeric string parses to nothing", firmware.parse_version("unknown") is None)
check("None is tolerated", firmware.parse_version(None) is None)

print("\n== the two builds Drobo withdrew ==")
for version in ("4.3.0", "4.3.1", "4.3.1-8.126.117497"):
    a = firmware.assess(version)
    check(f"{version} is flagged as withdrawn", a.risk == firmware.RISK_WITHDRAWN, a.risk)
    check(f"{version} is worth showing", a.notable)

a = firmware.assess("4.3.1-8.126.117497")
check("the headline names the release, not the build",
      "4.3.1" in a.headline and "117497" not in a.headline, a.headline)
check("it says who withdrew it", "Drobo" in a.detail, a.detail)
check("it names the last-known-good version", firmware.LAST_KNOWN_GOOD in a.detail, a.detail)

# The line this module must not cross. Downgrading means writing an unsigned
# image to hardware with no recovery tool -- the one action that can destroy the
# device. Informing about the firmware is useful; nudging toward a flash is not,
# and DO_NOT_SEND refuses every flashing command anyway.
lowered = a.detail.lower()
check("it never tells you to downgrade", "downgrade" not in lowered, a.detail)
check("it never tells you to update or reflash",
      not any(w in lowered for w in ("you should update", "reflash", "install 4.2.2")), a.detail)
check("it says outright that this software won't do it",
      "will\n" not in a.detail and "not do it" in lowered, a.detail)
check("it admits the fault was never published",
      "never published" in lowered, a.detail)
check("it does not invent symptoms it cannot know",
      not any(w in lowered for w in ("data loss", "corrupt", "crash", "fails to")), a.detail)
check("it ends on advice true regardless of firmware",
      "backups" in lowered, a.detail)

print("\n== everything else stays quiet ==")
# A dashboard that comments on healthy firmware teaches people to ignore it.
for version in ("4.2.2", "4.2.1", "4.1.0", "1.2.7", "5.0.0", "4.4.0"):
    a = firmware.assess(version)
    check(f"{version} says nothing", a.risk == firmware.RISK_NONE and not a.notable, a.risk)
    check(f"{version} carries no text", a.headline == "" and a.detail == "")

print("\n== an unreadable version is unknown, not assumed safe ==")
for version in ("", "unknown", None):
    a = firmware.assess(version)
    check(f"{version!r} is UNKNOWN", a.risk == firmware.RISK_UNKNOWN, a.risk)
    # Unknown must not be NOTABLE either -- a device that hasn't answered yet
    # would otherwise raise a firmware warning on every fresh agent start.
    check(f"{version!r} is not shown to the user", not a.notable)

print("\n== near-misses are not swept in ==")
# 4.3.2 does not exist; if one ever turned up it would not be a build Drobo
# pulled, because Drobo was gone. Matching "4.3.x" as a family would have
# claimed otherwise.
check("4.3.2 is not treated as withdrawn",
      firmware.assess("4.3.2").risk == firmware.RISK_NONE)
check("4.30.1 is not confused with 4.3.1",
      firmware.assess("4.30.1").risk == firmware.RISK_NONE)
check("3.3.1 is not confused with 4.3.1",
      firmware.assess("3.3.1").risk == firmware.RISK_NONE)

print("\n== the dict a front-end receives ==")
d = firmware.assess("4.3.1-8.126.117497").to_dict()
check("carries the full version as reported", d["version"] == "4.3.1-8.126.117497")
check("carries the release separately", d["release"] == "4.3.1", d["release"])
check("carries notable, so a UI can test one field", d["notable"] is True)
check("carries last_known_good for context", d["last_known_good"] == "4.2.2")
quiet = firmware.assess("4.2.2").to_dict()
check("a healthy version's dict is not notable", quiet["notable"] is False)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
