"""
The update check. Built now, dormant until there is something to check.

"Keeping the software updatable as Windows moves on" is the stated point of
this whole project -- an app that cannot ship itself a fix is the exact failure
mode that killed the original. So the machinery goes in while the project is
being actively worked on, rather than being bolted together in a hurry on
release day.

DORMANT MEANS DORMANT
---------------------
With no manifest URL configured, this module opens no sockets, resolves no
names, and contacts nothing. Not "checks a default endpoint that happens to
404" -- it does not run at all. `check()` returns state="disabled" after a
dictionary lookup. There is a test that asserts no network call is attempted,
because "dormant" is a security property here, not a convenience: this software
runs unattended on a machine holding somebody's only copy of their data, and it
must not phone anywhere its owner did not ask it to.

WHAT THIS DOES AND POINTEDLY DOES NOT DO
-----------------------------------------
It fetches a small JSON manifest and compares version numbers. That is all.

It does NOT download the installer, and it does NOT install anything. Those are
deliberately out of scope for a background service: downloading and executing a
binary is the most dangerous thing this project could ever do, and it is not
something that should happen while nobody is looking. It tells a HUMAN that an
update exists and points them at it; they fetch it themselves.

TWO FEEDS, ONE ENGINE
---------------------
Reads either the GitHub Releases API (`tag_name`) or a hand-written manifest
(`version` + `download_url` + `sha256`), detected by content. Both the in-app
check (/api/update) and the installer's "Check for Updates" shortcut
(`py -m drobo_agent.updates`) run THIS module against ONE config block. They
used to be two implementations with two config files, which is how an update
mechanism quietly stops working -- somebody switches one on and assumes they
are covered.

Refusals and honesty rules, enforced in code rather than by convention:

  - plain http:// is refused outright, for the manifest and for the download
    link. An update announcement over plaintext can be rewritten in transit.
  - a manifest bigger than MAX_MANIFEST_BYTES is refused, so a hostile or
    broken endpoint cannot feed the agent an unbounded body.
  - a release with no SHA-256 is still ANNOUNCED, but explicitly flagged as
    unverifiable. An earlier version refused these outright; that was wrong,
    because the GitHub Releases API publishes no checksum at all and the rule
    would have silently suppressed every update from the likeliest feed. The
    control that matters is that nothing is ever downloaded or installed
    automatically -- not withholding the news.

VERSION COMPARISON
------------------
Dotted numeric components, compared numerically, shortest-padded --  so 0.10.0
is correctly newer than 0.9.0, which a string compare gets wrong. Anything
unparseable is treated as "cannot tell", never as "newer".
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

#: Cap on the manifest body. It is a handful of fields; anything larger is
#: either broken or hostile, and either way we stop reading.
MAX_MANIFEST_BYTES = 64 * 1024

#: How long to wait on the network. Short: this runs on a timer and a hung
#: update check must never be why a status poll is late.
DEFAULT_TIMEOUT = 10.0

STATE_DISABLED = "disabled"      # no channel configured -- the shipping default
STATE_CURRENT = "current"        # checked, nothing newer
STATE_AVAILABLE = "available"    # checked, there is a newer version
STATE_ERROR = "error"            # tried, could not tell

_VERSION_PART = re.compile(r"\d+")


@dataclass(frozen=True)
class Release:
    """One published version, as the manifest describes it."""

    version: str
    download_url: str = ""
    sha256: str = ""
    notes_url: str = ""
    released: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "download_url": self.download_url,
            "sha256": self.sha256,
            "notes_url": self.notes_url,
            "released": self.released,
        }


@dataclass(frozen=True)
class UpdateStatus:
    """The answer, in the shape a front-end renders."""

    state: str
    current_version: str
    latest: Release | None = None
    reason: str = ""

    @property
    def update_available(self) -> bool:
        return self.state == STATE_AVAILABLE

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "update_available": self.update_available,
            "current_version": self.current_version,
            "latest": self.latest.to_dict() if self.latest else None,
            "reason": self.reason,
        }


def parse_version(version: str) -> tuple[int, ...] | None:
    """
    A dotted version as numbers, or None if it isn't one.

    Only the leading numeric run matters: "0.2.0-beta3" is release 0.2.0 as far
    as ordering goes, exactly as firmware.parse_version treats the Drobo's own
    build suffix. Pre-release ordering is a rabbit hole this project has no
    need to enter -- if that ever matters, it should be an explicit field in the
    manifest rather than something inferred from punctuation.
    """
    if not version:
        return None
    head = version.strip().split("-")[0].split("+")[0]
    parts = _VERSION_PART.findall(head)
    if not parts:
        return None
    return tuple(int(p) for p in parts)


def is_newer(candidate: str, current: str) -> bool:
    """
    True when `candidate` is a later version than `current`.

    Padded to equal length so 1.2 and 1.2.0 compare equal rather than the
    shorter one losing. Returns False -- never True -- when either side is
    unparseable: "I cannot tell" must never present itself as "an update is
    available", because that is the direction that gets someone to install
    something.
    """
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return a > b


def _read_release(manifest: dict) -> Release | None:
    """
    Read a release out of either shape of feed we support.

    TWO SHAPES, ONE ENGINE. Until 2026-07-28 this project had two separate
    update checkers that had grown up independently and did not know about each
    other: this one, reading a hand-authored manifest, and a PowerShell script
    shipped in the installer reading the GitHub Releases API. Two
    implementations, two config files, two things to switch on. That is how an
    update mechanism quietly stops working -- somebody enables one and assumes
    they are covered.

    So this reads both:

      GitHub Releases     {"tag_name": "v0.2.0", "html_url": ...}
      our own manifest    {"version": "0.2.0", "download_url": ..., "sha256": ...}

    Detected by content rather than configuration, because a URL cannot tell
    you what it will return and asking somebody to declare it is one more thing
    to get wrong.

    GitHub's API carries no checksum for a release, which is why `sha256` comes
    back empty for that shape. That is handled honestly at the call site: the
    update is still announced, flagged as unverifiable, rather than either
    suppressed (unhelpful) or presented as verified (a lie).
    """
    # GitHub Releases: tag_name is the version, conventionally "v"-prefixed.
    tag = str(manifest.get("tag_name") or "").strip()
    if tag:
        return Release(
            version=tag.lstrip("vV"),
            # The browser page, not a binary. Deliberate: this software never
            # downloads an update, and pointing a human at the release page is
            # exactly the right amount of help.
            download_url=str(manifest.get("html_url") or "").strip(),
            sha256="",  # GitHub publishes none
            notes_url=str(manifest.get("html_url") or "").strip(),
            released=str(manifest.get("published_at") or "").strip(),
        )

    version = str(manifest.get("version") or "").strip()
    if not version:
        return None
    return Release(
        version=version,
        download_url=str(manifest.get("download_url") or "").strip(),
        sha256=str(manifest.get("sha256") or "").strip().lower(),
        notes_url=str(manifest.get("notes_url") or "").strip(),
        released=str(manifest.get("released") or "").strip(),
    )


def _validate_url(url: str, what: str = "manifest") -> str:
    """
    Return "" if the URL is acceptable, else why it isn't.

    `what` names the thing being checked, because this guards both the feed URL
    and the download link and a message saying "manifest" while rejecting a
    download link sends the reader to the wrong setting.
    """
    if not url:
        return f"no {what} URL configured"
    if not url.lower().startswith("https://"):
        return (f"{what} URLs must be https:// -- an update announcement over "
                f"plaintext can be tampered with in transit")
    return ""


def _fetch(url: str, timeout: float) -> dict:
    """Fetch and decode the manifest. Raises on anything unexpected."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "DroboDashboardReborn-agent"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        # Read one byte more than the cap so an over-long body is detected
        # rather than silently truncated into something that might still parse.
        raw = response.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest larger than {MAX_MANIFEST_BYTES} bytes; refusing it")
    return json.loads(raw.decode("utf-8"))


def check(config: dict, current_version: str,
          timeout: float = DEFAULT_TIMEOUT, fetch=None) -> UpdateStatus:
    """
    Ask whether a newer version exists.

    `config` is the "updates" block from config.json:

        {"enabled": false, "manifest_url": "", "channel": "stable"}

    With enabled false or no URL -- the shipping default -- this returns
    immediately and touches nothing. `fetch` is injectable so the tests can
    prove exactly that, and can exercise the parsing without a server.
    """
    config = config or {}
    if not config.get("enabled", False):
        return UpdateStatus(
            STATE_DISABLED, current_version,
            reason="Update checking is switched off. Nothing is contacted.")

    url = (config.get("manifest_url") or "").strip()
    problem = _validate_url(url)
    if problem:
        # Enabled but unusable is a configuration mistake worth surfacing, not
        # a silent no-op -- somebody turned this on and expects it to work.
        return UpdateStatus(STATE_DISABLED, current_version, reason=problem)

    try:
        manifest = (fetch or _fetch)(url, timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return UpdateStatus(STATE_ERROR, current_version,
                            reason=f"Could not check for updates: {exc}")

    if not isinstance(manifest, dict):
        return UpdateStatus(STATE_ERROR, current_version,
                            reason="Update manifest was not a JSON object.")

    # A channel lets one manifest serve stable and beta without the agent
    # needing a second URL. Absent means "this manifest is the only channel".
    channel = (config.get("channel") or "stable").strip()
    if "channels" in manifest and isinstance(manifest["channels"], dict):
        manifest = manifest["channels"].get(channel) or {}
        if not manifest:
            return UpdateStatus(STATE_ERROR, current_version,
                                reason=f"No {channel!r} channel in the update manifest.")

    release = _read_release(manifest)
    if release is None:
        return UpdateStatus(STATE_ERROR, current_version,
                            reason="Update manifest carried no version.")
    version = release.version
    download_url = release.download_url
    sha256 = release.sha256

    if not is_newer(version, current_version):
        return UpdateStatus(STATE_CURRENT, current_version, latest=release,
                            reason="You are on the latest version.")

    # From here we would be pointing somebody at something to install, so the
    # announcement has to be honest about how much it can vouch for.
    #
    # An https:// link is required either way -- an update announcement over
    # plaintext can be rewritten in transit, and that is the one failure this
    # refuses outright rather than annotates.
    download_problem = _validate_url(download_url, "download link")
    if download_problem:
        return UpdateStatus(
            STATE_ERROR, current_version, latest=release,
            reason=(f"Version {version} was announced, but its link is not usable "
                    f"({download_problem}). Not offering it."))

    # The checksum is a different matter. An earlier version of this module
    # REFUSED to mention an update with no SHA-256, on the reasoning that
    # telling someone to run an unverifiable binary is the whole attack. That
    # is right for a file we point at directly -- and wrong as a blanket rule,
    # because the GitHub Releases API publishes no checksum at all, so it would
    # have silently suppressed every update from the most likely feed anyone
    # will actually use.
    #
    # So: announce either way, and say plainly which one this is. Suppressing
    # the news is unhelpful; implying it is verified when it is not would be a
    # lie. Neither shape is ever downloaded or installed automatically, which
    # is the control that actually matters.
    verified = len(sha256) == 64 and all(c in "0123456789abcdef" for c in sha256)
    if verified:
        assurance = ("A SHA-256 is published with it -- check the file you download "
                     "against it before running anything.")
    else:
        assurance = ("No checksum is published with it, so there is nothing to check "
                     "the download against beyond the site it came from. Get it from "
                     "the official page and nowhere else.")

    return UpdateStatus(
        STATE_AVAILABLE, current_version, latest=release,
        reason=(f"Version {version} is available. This software will not download "
                f"or install it for you. {assurance}"))


# ---------------------------------------------------------------------------
# Command line, so the installer's "Check for Updates" shortcut runs THIS code
# rather than a second implementation of it.
#
# The shortcut has one real advantage over the in-app check and it is worth
# keeping: it works whether or not the agent is running. What it must not be is
# a parallel checker with its own config file and its own idea of what a feed
# looks like -- which is exactly what it was until these were merged.
# ---------------------------------------------------------------------------

def _installed_version(install_dir: str) -> str:
    """
    The version the installer recorded, falling back to the package's own.

    version.txt is written at build time and is the truth for an installed
    copy. A dev checkout has no such file and falls back to the constant, which
    is right for a dev checkout.

    TWO PLACES ARE CHECKED, and the second one is the one that matters:
    build-installer.ps1 writes version.txt to the INSTALL ROOT, while the agent
    is installed one level down in <install>\\agent\\. The first version of this
    looked only beside the agent, never found the file, and silently fell back
    to the hardcoded constant -- which drifts from the real build the moment
    anyone bumps the .csproj. A version check quietly comparing against the
    wrong number is worse than no version check: it can report "up to date"
    when you are not.
    """
    candidates = (
        os.path.join(install_dir, "version.txt"),               # dev checkout
        os.path.join(os.path.dirname(install_dir), "version.txt"),  # installed
    )
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                recorded = fh.read().strip()
                if recorded:
                    return recorded
        except OSError:
            continue
    from . import api  # local import: api imports plenty, and this is a rare path
    return api.VERSION


def main(argv: list[str] | None = None) -> int:
    """
    Check for updates from a terminal.

        py -m drobo_agent.updates                 # uses config.json beside the agent
        py -m drobo_agent.updates --config X.json

    Exit status is the answer, so a script can branch without parsing prose:
    0 up to date or disabled, 1 an update is available, 2 could not tell.
    """
    import argparse
    from . import config as config_mod

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Check for a newer Drobo Dashboard REBORN.")
    ap.add_argument("--config", default=os.path.join(here, "config.json"))
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when there is no update (for shortcuts)")
    args = ap.parse_args(argv)

    try:
        cfg = config_mod.load(args.config)
    except Exception:
        # A missing or broken config is not worth an error popup on a launcher.
        cfg = {}

    version = _installed_version(here)
    status = check(cfg.get("updates", {}), version)

    if status.state == STATE_AVAILABLE:
        print(f"A newer version is available: {status.latest.version} "
              f"(you have {version}).")
        print(status.reason)
        if status.latest.notes_url:
            print(f"Release notes: {status.latest.notes_url}")
        return 1

    if status.state == STATE_ERROR:
        # Offline, DNS hiccup, feed returned nonsense. Never alarming: an
        # update check that shouts on a train with no signal is worse than one
        # that says nothing.
        if not args.quiet:
            print(status.reason)
            print(f"You are on version {version}.")
        return 2

    if not args.quiet:
        print("Update checks are off." if status.state == STATE_DISABLED
              else f"You are up to date (version {version}).")
        if status.state == STATE_DISABLED:
            print(status.reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
