"""
Tests for building a modified share configuration.

    py test_shareedit.py

These matter more than most. The only way to change a share on a Drobo is
eCmdSetConfig, which replaces the ENTIRE configuration document -- so a bug
that drops the network block does not produce a wrong share list, it produces
a Drobo that is no longer on the network, on hardware with no vendor recovery
tool left.

So the tests below spend most of their effort on what must NOT happen.

Entirely offline. This module cannot open a socket by construction.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import shareedit as se

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("  " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(label)


# Shaped exactly like the live device's reply, including the quirk that one
# user arrives as a dict and several as a list.
CONFIG = {
    "DRINASConfig": {
        "DRINasNetworkConfig": {
            "NasName": "TestDrobo",
            "IPConfig": {"IP": "10.0.0.5", "Gateway": "10.0.0.1"},
            "MACAddress": "02:00:00:00:00:01",
        },
        "DRIShareConfig": {
            "ConfigVersionBasis": "14",
            "Shares": {"Share": [
                {"ShareName": "Documents", "ShareState": "0",
                 "ShareUsers": {"ShareUser": {"ShareUsername": "Everyone",
                                              "ShareUserAccess": "1"}}},
                {"ShareName": "Photo Library", "ShareState": "0",
                 "ShareUsers": {"ShareUser": [
                     {"ShareUsername": "Everyone", "ShareUserAccess": "1"},
                     {"ShareUsername": "guest", "ShareUserAccess": "0"}]}},
                {"ShareName": "Private", "ShareState": "0",
                 "ShareUsers": {"ShareUser": {"ShareUsername": "Everyone",
                                              "ShareUserAccess": "0"}}},
            ]},
        },
    }
}

print("== reading the document ==")
check("every share is found",
      se.share_names(CONFIG) == ["Documents", "Photo Library", "Private"],
      se.share_names(CONFIG))
check("a share name with a space survives", "Photo Library" in se.share_names(CONFIG))

print("\n== the edit itself ==")
before = copy.deepcopy(CONFIG)
out = se.set_user_access(CONFIG, "Private", "Everyone", "1")
priv = [s for s in out["DRINASConfig"]["DRIShareConfig"]["Shares"]["Share"]
        if s["ShareName"] == "Private"][0]
check("the access code is changed",
      priv["ShareUsers"]["ShareUser"]["ShareUserAccess"] == "1", priv)
check("the input document is NOT mutated", CONFIG == before)
check("a single user stays a dict, as the device sends it",
      isinstance(priv["ShareUsers"]["ShareUser"], dict), type(priv["ShareUsers"]["ShareUser"]))

out2 = se.set_user_access(CONFIG, "Documents", "jordan", "1")
docs = [s for s in out2["DRINASConfig"]["DRIShareConfig"]["Shares"]["Share"]
        if s["ShareName"] == "Documents"][0]
check("a new user is added",
      isinstance(docs["ShareUsers"]["ShareUser"], list)
      and len(docs["ShareUsers"]["ShareUser"]) == 2, docs)
check("...and several users become a list, as the device sends them",
      isinstance(docs["ShareUsers"]["ShareUser"], list))

rev = se.revoke_user(CONFIG, "Documents", "Everyone")
d2 = [s for s in rev["DRINASConfig"]["DRIShareConfig"]["Shares"]["Share"]
      if s["ShareName"] == "Documents"][0]
check("revoking sets code 0", d2["ShareUsers"]["ShareUser"]["ShareUserAccess"] == "0")
# Deliberate: the row stays. We cannot interpret access codes, so we could not
# faithfully restore a deleted one.
check("revoking does NOT delete the user row",
      d2["ShareUsers"]["ShareUser"]["ShareUsername"] == "Everyone", d2)

print("\n== what must never happen ==")
check("nothing outside DRIShareConfig moved",
      out["DRINASConfig"]["DRINasNetworkConfig"]
      == CONFIG["DRINASConfig"]["DRINasNetworkConfig"])
check("no share disappeared", se.share_names(out) == se.share_names(CONFIG))

# The central refusal. We do not know what 1 and 2 mean -- the firmware never
# says, and only Samba's own libraries mention read/write lists -- so a code
# that has never been seen on this device is not something to send.
try:
    se.set_user_access(CONFIG, "Documents", "Everyone", "2")
    check("an unseen access code is refused", False)
except se.ShareEditError as exc:
    check("an unseen access code is refused", "never been seen" in str(exc), str(exc))

try:
    se.set_user_access(CONFIG, "Documents", "Everyone", "7")
    check("an invented access code is refused", False)
except se.ShareEditError:
    check("an invented access code is refused", True)

check("code 0 is always allowed -- it is the one we are sure of",
      se.set_user_access(CONFIG, "Documents", "Everyone", "0") is not None)

try:
    se.set_user_access(CONFIG, "Nope", "Everyone", "0")
    check("editing a share that isn't there is refused", False)
except se.ShareEditError as exc:
    check("editing a share that isn't there is refused", "no share named" in str(exc))

try:
    se._share_block({"something": "else"})
    check("a document with no share block is refused", False)
except se.ShareEditError:
    check("a document with no share block is refused", True)

# Prove the guards actually fire, rather than trusting that they would.
tampered = copy.deepcopy(CONFIG)
tampered["DRINASConfig"]["DRINasNetworkConfig"]["IPConfig"]["IP"] = "10.0.0.99"
try:
    se._assert_only_shares_changed(CONFIG, tampered)
    check("a change to the network block IS caught", False)
except se.ShareEditError as exc:
    check("a change to the network block IS caught", "outside DRIShareConfig" in str(exc))

dropped = copy.deepcopy(CONFIG)
dropped["DRINASConfig"]["DRIShareConfig"]["Shares"]["Share"].pop()
try:
    se._assert_no_share_lost(CONFIG, dropped)
    check("a vanished share IS caught", False)
except se.ShareEditError as exc:
    check("a vanished share IS caught", "would remove share" in str(exc), str(exc))

print("\n== the confirmation a person reads before agreeing ==")
lines = se.describe_change(CONFIG, se.set_user_access(CONFIG, "Private", "Everyone", "1"))
check("the change is described in one line", len(lines) == 1, lines)
check("it names the user and the share",
      "Everyone" in lines[0] and "Private" in lines[0], lines)
check("it reports the raw code without claiming to know what it means",
      "code 1" in lines[0], lines)

rev_lines = se.describe_change(CONFIG, se.revoke_user(CONFIG, "Documents", "Everyone"))
check("losing access is described as losing access",
      "loses access" in rev_lines[0], rev_lines)
check("an unchanged document describes nothing",
      se.describe_change(CONFIG, copy.deepcopy(CONFIG)) == [])

print("\n== this module cannot reach the network, by construction ==")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "drobo_nasd", "shareedit.py"),
           encoding="utf-8").read()
for forbidden in ("import socket", "config_cmd", "sendall", "create_connection"):
    check(f"no {forbidden!r} anywhere in the module", forbidden not in src)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
