"""
The Drobo's file shares, turned into something a UI can act on.

This is the "access my files" half of the Dashboard. The other modules read
health; this one answers "where are my files and why can't I open them".

Two things live here:

1.  Turning the device's `DRIShareConfig` into a flat list with a real UNC path
    per share, so a front-end can offer Open / Connect without knowing anything
    about the Drobo protocol.

2.  The Windows guest-logon problem, explained below, because on a modern
    Windows box that is the actual reason a Drobo looks unreachable -- and
    nothing about the error Windows shows you says so.

    The Drobo's Samba offers its shares to "Everyone", which on the wire means
    a *guest* logon. Windows 11 24H2 turned on `RequireSecuritySignature` by
    default, and a guest session cannot be signed, so Windows refuses the
    logon with NTSTATUS 0xC05D0003 (STATUS_SMB_GUEST_LOGON_BLOCKED). The share
    is fine. The Drobo is fine. Windows simply will not log in as a guest any
    more.

    The right fix is to connect as a *named user* instead of a guest. That is
    a credential the owner types into their own machine, so it is not stored
    here and never travels through the agent -- the front-end hands it
    straight to Windows. See `docs/accessing-your-files.md`.

Nothing in this module talks to the network. It transforms a config document
that `config_cmd` already fetched, which makes it entirely testable offline.
"""

from __future__ import annotations

# Windows returns this when it refuses to complete a guest logon. Worth naming
# rather than leaving as a magic number, because it is the single most likely
# thing to go wrong between a working Drobo and a user who cannot see files.
GUEST_LOGON_BLOCKED = 0xC05D0003

# The Drobo stores a per-user access level on each share. We have confirmed the
# codes the device emits but NOT their meanings -- the vendor is gone and there
# is no document to check against. Same discipline as the pack status codes in
# monitor.py: report what the device said, label it honestly, and do not invent
# a meaning that a UI would then present as fact.
# MEASURED 2026-08-05, from a packet capture of Drobo Dashboard 3.5.0 talking
# to this project's own 5N. The owner confirmed the word the old UI showed for
# one of them, which is the half that cannot be recovered from the bytes.
#
#   1  Read/Write   CONFIRMED. The capture shows a share the owner states is
#                   set to Read/Write carrying ShareUserAccess=1.
#
#   0  UNRESOLVED, and deliberately still labelled by its number. A second
#      share came through as 0 and the owner said it was "supposed to be" read
#      -- a hedge, not an observation, and the difference matters. Zero is the
#      natural value for "none" in almost any permission enumeration, so 0
#      plausibly means NO ACCESS rather than read-only. Recording it as
#      "read only" on the strength of an intention would risk telling somebody
#      a share is readable when nobody can open it.
#
#   2  Never observed. If the old UI offers three levels then 2 exists, but it
#      has not been seen on the wire and is not guessed at here.
#
# Settling 0 takes ten seconds: open that share in Drobo Dashboard 3.5.0 and
# read the word next to Everyone. Until someone does, it stays a number.
ACCESS_LEVELS: dict[str, str] = {
    "0": "no access or read-only -- unconfirmed (code 0)",
    "1": "read/write (code 1)",
    "2": "access allowed (code 2)",
}

#: The codes whose meaning is CONFIRMED against the original Dashboard, as
#: opposed to described from their number. A UI can use this to decide whether
#: to show a word confidently or hedge -- see ACCESS_LEVELS for what is still
#: open and why.
ACCESS_CONFIRMED: frozenset[str] = frozenset({"1"})


def _as_list(node):
    """
    The XML->dict conversion gives a dict for one child and a list for several.
    Shares and users both hit this, so normalise once here.
    """
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _share_config(config: dict) -> dict:
    """Dig `DRIShareConfig` out of whatever wrapper depth it arrived in."""
    if not isinstance(config, dict):
        return {}
    if "DRIShareConfig" in config:
        return config["DRIShareConfig"] or {}
    inner = config.get("DRINASConfig")
    if isinstance(inner, dict):
        return inner.get("DRIShareConfig") or {}
    return {}


def unc_path(host: str, share_name: str) -> str:
    r"""`\\10.0.0.5\Documents` -- what Explorer, `net use` and the phone all want."""
    return "\\\\" + host + "\\" + share_name


#: Element names a user record might be called inside <UserList>. A populated
#: UserList has never been observed -- the only device this project can read
#: has none -- so this is a best effort across the spellings the rest of this
#: firmware uses, not a confirmed schema.
_USER_ELEMENTS = ("User", "ShareUser", "NasUser")
_USERNAME_FIELDS = ("UserName", "ShareUsername", "Username", "Name")


def user_accounts(config: dict) -> list[str] | None:
    """
    The named accounts defined on the Drobo, from `DRIShareConfig/UserList`.

    Returns [] when the device definitely has none, a list of names when they
    can be read, and **None when the list holds something this code could not
    make sense of** -- which is a real possibility, because a populated
    UserList has never been seen. The only device available to this project
    has zero accounts, so the empty case is confirmed and the populated case
    is inference from how the rest of the firmware spells things.

    That three-way answer exists so callers can tell "there are no accounts"
    from "I don't know". The difference matters: the first justifies telling
    somebody to go create one, and the second does not.
    """
    cfg = _share_config(config)
    if "UserList" not in cfg:
        return None
    raw = cfg.get("UserList")

    # The XML->dict conversion renders <UserList /> as None or "" depending on
    # which empty-element form the device sent. Both mean the same thing, and
    # both are what this project has actually measured.
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    if not isinstance(raw, dict):
        return None

    names: list[str] = []
    for element in _USER_ELEMENTS:
        for entry in _as_list(raw.get(element)):
            if isinstance(entry, dict):
                for field in _USERNAME_FIELDS:
                    value = (entry.get(field) or "").strip()
                    if value:
                        names.append(value)
                        break
            elif isinstance(entry, str) and entry.strip():
                names.append(entry.strip())

    # Something was in there and none of the guesses matched. Say so rather
    # than reporting "no accounts", which would be a confident wrong answer.
    if not names:
        return None
    return sorted(set(names), key=str.lower)


def summarize(config: dict, host: str) -> list[dict]:
    """
    Flatten the device's share config into UI-ready records.

    Returns one dict per share:
        name            the share name
        unc             the path to open or map
        time_machine    bool, whether the share is a Time Machine target
        users           [{"name":..., "access_code":..., "access":...}]
        everyone        the access description for "Everyone", or None

    `everyone` is pulled out separately because it is the field that decides
    whether Windows will need real credentials: a share offered to Everyone is
    reached by guest logon, and modern Windows blocks that.
    """
    shares = []
    cfg = _share_config(config)
    for raw in _as_list((cfg.get("Shares") or {}).get("Share")):
        if not isinstance(raw, dict):
            continue
        name = (raw.get("ShareName") or "").strip()
        if not name:
            continue

        users = []
        everyone = None
        for u in _as_list((raw.get("ShareUsers") or {}).get("ShareUser")):
            if not isinstance(u, dict):
                continue
            uname = (u.get("ShareUsername") or "").strip()
            code = str(u.get("ShareUserAccess", "")).strip()
            described = ACCESS_LEVELS.get(code, f"unrecognised code {code}" if code else "unknown")
            users.append({"name": uname, "access_code": code, "access": described})
            if uname.lower() == "everyone":
                everyone = described

        shares.append({
            "name": name,
            "unc": unc_path(host, name) if host else "",
            "time_machine": str(raw.get("TimeMachineEnabled", "0")).strip() == "1",
            "users": users,
            "everyone": everyone,
        })

    shares.sort(key=lambda s: s["name"].lower())
    return shares


def access_advice(shares: list[dict], accounts: list[str] | None = None) -> dict:
    """
    What a front-end should tell the owner about opening these shares.

    Deliberately not phrased as an error. Nothing is broken -- it is a change
    Microsoft made to Windows that the Drobo, frozen in 2023, cannot meet.

    `accounts` is the device's named accounts, from `user_accounts()`. It is
    optional and defaults to None, meaning "not supplied", which keeps every
    existing one-argument call working and produces the original wording.

    WHY IT WAS ADDED. Until 2026-08-06 this function said, unconditionally,
    "Connect with your Drobo user name and password instead and Windows will
    let you straight in." On a device with no accounts that is a dead end:
    there is no user name, so the advice sends somebody to a sign-in dialog
    that cannot succeed however carefully they type. Measured on this
    project's own 5N, whose UserList is empty. Advice that cannot be followed
    is worse than none, because the reader assumes they got it wrong.

    Returns, in addition to the text:
        guest_shares    the shares offered to Everyone
        can_sign_in     False when we know there is no account to sign in as,
                        so a UI can disable its Connect-as control rather than
                        offering a door with nothing behind it
        accounts        the names, when known
    """
    guest_only = [s["name"] for s in shares if s.get("everyone")]
    if not guest_only:
        return {
            "guest_shares": [],
            "headline": "",
            "detail": "",
            "can_sign_in": True,
            "accounts": accounts or [],
        }

    # Every Everyone entry sitting on a code whose meaning is not confirmed.
    # Worth saying out loud, because if it turns out to mean "no access" then
    # credentials are not the problem and no amount of signing in will help.
    unconfirmed = [s["name"] for s in shares
                   if s.get("everyone")
                   and any(u.get("name", "").lower() == "everyone"
                           and u.get("access_code") not in ACCESS_CONFIRMED
                           for u in s.get("users", []))]

    windows_part = (
        "The Drobo offers these shares to Everyone, which Windows treats as "
        "a guest login. Windows 11 stopped accepting guest logins to file "
        "servers, so it refuses before the Drobo is ever asked "
        f"(error 0x{GUEST_LOGON_BLOCKED:08X})."
    )

    if accounts is None:
        # Unchanged from the original: we do not know what accounts exist, so
        # we neither promise one nor deny one.
        headline = "Windows needs a username and password for these shares."
        detail = (f"{windows_part} Nothing is wrong with the Drobo. Connect with "
                  "your Drobo user name and password instead and Windows will "
                  "let you straight in.")
        can_sign_in = True
    elif accounts:
        named = ", ".join(accounts)
        headline = "Windows needs a username and password for these shares."
        detail = (f"{windows_part} Nothing is wrong with the Drobo. Sign in with "
                  f"a Drobo account instead and Windows will let you straight "
                  f"in. This Drobo has: {named}.")
        can_sign_in = True
    else:
        headline = "These shares cannot be opened from Windows yet."
        detail = (
            f"{windows_part} The usual answer is to sign in as a named user "
            "instead -- but this Drobo has no user accounts, so there is no "
            "name to sign in with. Create one in Drobo Dashboard and give it "
            "access to the share, then connect with it here."
        )
        can_sign_in = False

    if unconfirmed:
        detail += (
            " Separately, " + _list_phrase(unconfirmed) + " currently set to an "
            "access level this project has not confirmed the meaning of. If it "
            "turns out to mean no access, permissions are a second and quite "
            "different problem, and signing in will not solve it."
        )

    return {
        "guest_shares": guest_only,
        "headline": headline,
        "detail": detail,
        "can_sign_in": can_sign_in,
        "accounts": accounts or [],
    }


def _list_phrase(names: list[str]) -> str:
    """`Alpha is` / `Alpha and Beta are` -- so the sentence reads properly."""
    if len(names) == 1:
        return f"{names[0]} is"
    return ", ".join(names[:-1]) + f" and {names[-1]} are"


def link_health(config: dict) -> dict:
    """
    The Drobo's own view of its network port.

    A 5N has a gigabit port. If it has negotiated 100 Mbit -- which is what a
    damaged cable, a bad wall port or an old switch produces -- every file copy
    runs at a tenth of the speed it should, and nothing on the device says so.
    That is worth telling somebody, so it is computed here and surfaced beside
    the network settings.
    """
    net = {}
    if isinstance(config, dict):
        inner = config.get("DRINASConfig")
        net = (inner or {}).get("DRINasNetworkConfig") or config.get(
            "DRINasNetworkConfig") or {}

    raw_speed = str(net.get("PortSpeed", "")).strip()
    try:
        speed = int(raw_speed)
    except ValueError:
        speed = None

    result = {
        "speed_mbps": speed,
        "duplex": (net.get("PortDuplex") or "").strip(),
        "degraded": False,
        "note": "",
    }
    if speed is not None and 0 < speed < 1000:
        result["degraded"] = True
        result["note"] = (
            f"The Drobo's network port has connected at {speed} Mbit/s. This "
            "model has a gigabit port, so file transfers are running at about "
            f"{1000 // speed if speed else '?'}x slower than they could. The "
            "usual causes are a damaged or low-grade network cable, or a "
            "100 Mbit switch or wall socket. Swapping the cable for a Cat 5e "
            "or better one is the first thing to try."
        )
    if (result["duplex"] or "").lower() == "half":
        result["degraded"] = True
        result["note"] = (result["note"] + " ").strip() + (
            " The port is also in half duplex, which usually means the Drobo "
            "and the switch failed to agree on a speed."
        )
    return result
