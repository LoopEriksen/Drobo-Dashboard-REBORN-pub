"""
Building a modified share configuration -- WITHOUT the ability to send it.

WHY THIS MODULE CANNOT TALK TO A DROBO
--------------------------------------
There is no per-share write command. The 103-command table has
`eCmdGetSharesConfig` to read, but the only way to *change* a share is
`eCmdSetConfig` (id 31), which takes an entire `<DRINASConfig>` document and
replaces what is there.

That is a blunt instrument pointed at the owner's only copy of their data. Send
a document missing a share and the share is gone. Send one missing the network
block and the Drobo may not come back on the network -- at which point the only
remedy is physical access and a factory reset, which loses the pack.

So this module builds the document and nothing else. It opens no socket and
imports nothing that can. Getting the construction provably right is a separate
problem from deciding to transmit it, and doing them in that order means the
risky decision is made once, deliberately, with working code already in hand
rather than as part of the same change.

THE INVARIANTS
--------------
Every function here is read-modify-write over a config the device just gave us,
and each enforces:

  * Only `DRIShareConfig` is ever touched. Network and admin blocks are carried
    through byte-identical.
  * No share may vanish as a side effect. Removing one is its own explicit
    call, and even then only one at a time.
  * The result is diffed against the input, and a change that touches anything
    unexpected raises rather than returning a document nobody reviewed.

WHAT IS NOT KNOWN, AND MATTERS HERE
-----------------------------------
`ShareUserAccess` is an integer. The live 5N uses `0` and `1`; docs and
firmware never say what the values mean. Searching the firmware image settles
it: only Samba's own libraries mention `read list` / `write list` / `valid
users` -- no Drobo binary does, so the mapping is computed at runtime and
cannot be recovered statically.

The consequence is concrete: **this module will not invent an access code.**
It can copy a code that already exists on the device, and it can set `0`, which
every observation agrees means "no access". It will not offer "read-only" vs
"read-write" as a choice, because that would be a guess presented to the owner
as a fact about their permissions.
"""
from __future__ import annotations

import copy

#: The only access code we are confident about. Every share observed with `0`
#: was unreachable; every share the owner could open had a non-zero code.
ACCESS_NONE = "0"


class ShareEditError(Exception):
    """The requested change is unsafe or does not make sense."""


def _share_block(config: dict) -> dict:
    """Dig out DRIShareConfig, whatever wrapper depth it arrived in."""
    if not isinstance(config, dict):
        raise ShareEditError("configuration is not a document")
    if "DRIShareConfig" in config:
        return config["DRIShareConfig"] or {}
    inner = config.get("DRINASConfig")
    if isinstance(inner, dict) and "DRIShareConfig" in inner:
        return inner["DRIShareConfig"] or {}
    raise ShareEditError("no DRIShareConfig in this document")


def _shares(block: dict) -> list:
    """The share list, always as a list even when the device sent one dict."""
    raw = (block.get("Shares") or {}).get("Share")
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


def share_names(config: dict) -> list[str]:
    """Every share name in the document, in the order the device gave them."""
    return [(s.get("ShareName") or "").strip()
            for s in _shares(_share_block(config))
            if (s.get("ShareName") or "").strip()]


def _users(share: dict) -> list:
    raw = (share.get("ShareUsers") or {}).get("ShareUser")
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


def _assert_only_shares_changed(before: dict, after: dict) -> None:
    """
    Refuse to return a document that altered anything outside DRIShareConfig.

    This is the check that makes the whole module safe to trust. The write
    command replaces everything, so a bug that silently dropped the network
    block would take the Drobo off the network -- and would look, from the
    caller's side, exactly like a successful edit.
    """
    b = copy.deepcopy(before)
    a = copy.deepcopy(after)
    for doc in (b, a):
        inner = doc.get("DRINASConfig")
        if isinstance(inner, dict):
            inner.pop("DRIShareConfig", None)
        doc.pop("DRIShareConfig", None)
    if b != a:
        raise ShareEditError(
            "the edit changed something outside DRIShareConfig -- refusing. "
            "This is the check that stops a share edit taking the Drobo off "
            "the network.")


def _assert_no_share_lost(before: dict, after: dict, allow_removed: str | None = None) -> None:
    """No share may disappear except the one explicitly being removed."""
    lost = set(share_names(before)) - set(share_names(after))
    if allow_removed is not None:
        lost.discard(allow_removed)
    if lost:
        raise ShareEditError(
            f"the edit would remove share(s) {sorted(lost)} -- refusing. "
            f"Removing a share is its own deliberate action.")


def set_user_access(config: dict, share_name: str, username: str,
                    access_code: str) -> dict:
    """
    Set one user's access on one share, and change nothing else.

    `access_code` must be `"0"` (no access) or a code that ALREADY appears
    somewhere in this document. Inventing a code is refused -- see the module
    docstring: we do not know what 1 and 2 mean, so we will not conjure one.

    Returns a new document. The input is never mutated.
    """
    out = copy.deepcopy(config)
    block = _share_block(out)

    known = {ACCESS_NONE}
    for s in _shares(_share_block(config)):
        for u in _users(s):
            code = str(u.get("ShareUserAccess", "")).strip()
            if code:
                known.add(code)
    if str(access_code) not in known:
        raise ShareEditError(
            f"access code {access_code!r} has never been seen on this device "
            f"(known: {sorted(known)}). This project does not invent access "
            f"codes -- what 1 and 2 mean is undocumented and unrecovered.")

    target = None
    for s in _shares(block):
        if (s.get("ShareName") or "").strip() == share_name:
            target = s
            break
    if target is None:
        raise ShareEditError(f"no share named {share_name!r} on this device")

    users = _users(target)
    for u in users:
        if (u.get("ShareUsername") or "").strip().lower() == username.strip().lower():
            u["ShareUserAccess"] = str(access_code)
            break
    else:
        users.append({"ShareUsername": username.strip(),
                      "ShareUserAccess": str(access_code)})

    # Normalise back to the device's own shape: one user is a dict, several a
    # list. Writing a list where the device expects a dict is exactly the kind
    # of malformed document that makes eCmdSetConfig dangerous.
    target["ShareUsers"] = {"ShareUser": users[0] if len(users) == 1 else users}

    _assert_only_shares_changed(config, out)
    _assert_no_share_lost(config, out)
    return out


def revoke_user(config: dict, share_name: str, username: str) -> dict:
    """
    Take a user's access away from a share, by setting code 0.

    Deliberately does NOT delete the user entry. Setting "no access" is
    reversible by setting it back; deleting the row loses whatever code was
    there, and since we cannot interpret those codes we could not restore it
    faithfully afterwards.
    """
    return set_user_access(config, share_name, username, ACCESS_NONE)


def describe_change(before: dict, after: dict) -> list[str]:
    """
    A plain-English list of what actually differs, for a confirmation prompt.

    Written for someone to READ before agreeing to a write, so it names shares
    and users rather than XML paths, and reports the raw access codes without
    pretending to know what they mean.
    """
    def index(cfg):
        out = {}
        for s in _shares(_share_block(cfg)):
            name = (s.get("ShareName") or "").strip()
            for u in _users(s):
                out[(name, (u.get("ShareUsername") or "").strip())] = \
                    str(u.get("ShareUserAccess", "")).strip()
        return out

    a, b = index(before), index(after)
    lines = []
    for key in sorted(set(a) | set(b)):
        share, user = key
        old, new = a.get(key), b.get(key)
        if old == new:
            continue
        if old is None:
            lines.append(f"{user} gains access to {share} (code {new})")
        elif new is None:
            lines.append(f"{user} is removed from {share}")
        elif new == ACCESS_NONE:
            lines.append(f"{user} loses access to {share}")
        elif old == ACCESS_NONE:
            lines.append(f"{user} is given access to {share} (code {new})")
        else:
            lines.append(f"{user} on {share}: access code {old} -> {new}")
    return lines
