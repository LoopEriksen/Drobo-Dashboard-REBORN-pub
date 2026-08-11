#!/usr/bin/env python3
"""
Offline tests for fw_params.py.

The firmware itself is gitignored (vendor/**), so this suite cannot assume the
real nasd binary is present. It tests the logic against a synthetic blob built
to mimic nasd's actual .rodata layout, and -- when the real binary IS available
locally -- additionally runs the tool's own --validate check against it.

    py test_fw_params.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fw_params

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def blob(*parts: str) -> bytes:
    """NUL-separated strings, the way a compiler lays .rodata out."""
    return b"\x00".join(p.encode() for p in parts) + b"\x00"


print("\n== strings are found with their offsets ==")
b = blob("hello", "ESADevice::GetPerformance", "Iops", "TierIOps")
found = dict((s, o) for o, s in fw_params.extract_strings(b))
check("every string is extracted", {"hello", "Iops", "TierIOps"} <= set(found), sorted(found))
check("offsets increase with position", found["Iops"] > found["ESADevice::GetPerformance"])
check("runs shorter than 3 chars are skipped",
      all(len(s) >= 3 for _, s in fw_params.extract_strings(blob("ab", "abcd"))))

print("\n== the two kinds of anchor, and why the difference matters ==")
# The bare command names live in ONE contiguous table -- that is how this
# project recovered all 103 commands. A name's neighbours there are just other
# names, never parameters. Anchoring on them was this tool's first mistake.
name_table = blob("eCmdSetConfig", "eCmdGetConfig", "eCmdSetDimming",
                  "eCmdGetDimming", "eCmdUploadDiags")
strings = fw_params.extract_strings(name_table)
near = fw_params.candidates_near(strings, dict((s, o) for o, s in strings)["eCmdSetDimming"])
check("a command name's neighbours yield no parameters", near == [], near)

# The useful anchor is the HANDLER's own debug string.
handler = blob("ESADevice::GetPerformance - failed", "Iops", "ReadThroughout",
               "WriteThroughout", "TierIOps")
markers = fw_params.find_markers(fw_params.extract_strings(handler), "eCmdGetPerformance")
check("a handler string is matched via the stripped command name",
      len(markers) == 1, markers)
check("-- and it is the handler, not a bare name",
      markers and "::" in markers[0][1], markers)

# "::" is required so an ordinary log line mentioning the verb doesn't anchor.
prose = blob("Now going to GetPerformance for the user", "Iops")
check("a plain log line is not mistaken for a handler",
      fw_params.find_markers(fw_params.extract_strings(prose), "eCmdGetPerformance") == [],
      fw_params.find_markers(fw_params.extract_strings(prose), "eCmdGetPerformance"))

print("\n== candidate filtering ==")
noisy = blob("ESADevice::GetPerformance", "Iops", "ERROR", "Failed", "Params",
             "Result", "eCmdGetConfig", "lower", "With Space", "TierIOps")
strings = fw_params.extract_strings(noisy)
anchor = dict((s, o) for o, s in strings)["ESADevice::GetPerformance"]
cands = fw_params.candidates_near(strings, anchor)
check("real tag names survive", {"Iops", "TierIOps"} <= set(cands), cands)
check("log-level noise is dropped", "ERROR" not in cands and "Failed" not in cands, cands)
# Params/Result are envelope structure, not parameters -- listing them would
# send somebody chasing a tag that is already known and already sent.
check("envelope tags are dropped", "Params" not in cands and "Result" not in cands, cands)
check("command names are dropped", "eCmdGetConfig" not in cands, cands)
check("lowercase words are dropped", "lower" not in cands, cands)
check("strings with spaces are dropped", "With Space" not in cands, cands)

print("\n== the window bounds the search ==")
far = blob("ESADevice::GetPerformance", *["Padding" + str(i) for i in range(120)], "FarAwayTag")
strings = fw_params.extract_strings(far)
anchor = dict((s, o) for o, s in strings)["ESADevice::GetPerformance"]
check("a tag beyond the window is not attributed",
      "FarAwayTag" not in fw_params.candidates_near(strings, anchor, window=50))
check("-- but is found with a wide enough window",
      "FarAwayTag" in fw_params.candidates_near(strings, anchor, window=5000))

print("\n== analyse() ranks by how often a tag appears near the command ==")
multi = blob("ESADevice::GetPerformance", "Iops", "Once",
             "ESASession::GetPerformance", "Iops")
result = fw_params.analyse(multi, "eCmdGetPerformance", window=200)
check("a tag near several markers outranks one seen once",
      result["candidates"] and result["candidates"][0] == "Iops", result["candidates"])
check("the command id is carried", result["id"] == 62, result["id"])

print("\n== validation against the real binary, when it is present ==")
# vendor/** is gitignored, so this is conditional by necessity. When the
# firmware IS on the machine, the tool must reproduce field names this project
# confirmed independently ON THE WIRE long before this tool existed. If it
# cannot, nothing else it prints is worth reading -- the same discipline that
# caught the pcapng dissector agreeing with its own fixtures while returning
# zero frames against every real capture.
real = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "vendor", "extracted", "rootfs", "sbin", "nasd")
if os.path.exists(real):
    data = open(real, "rb").read()
    for anchor, expected, got in fw_params.validate(data):
        missing = expected - got
        check(f"{anchor} rediscovers what we measured on the wire",
              not missing, f"missing {sorted(missing)}")
else:
    print("  SKIP    firmware not present locally (vendor/** is gitignored) --"
          " run  py tools/fw_params.py <nasd> --validate  where it is")

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
