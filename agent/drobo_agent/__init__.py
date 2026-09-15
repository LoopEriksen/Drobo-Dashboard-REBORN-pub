"""
Drobo Agent - the always-on service behind the Windows and iOS apps.

Layout:
    models.py    the plain data shapes everything speaks
    drivers.py   the ONLY file that knows how a Drobo talks
    monitor.py   polling loop, history, alert rules
    notify.py    where alerts go (console, ntfy, later APNs)
    photos.py    DroboPix-style photo backup, receiving half
    api.py       the HTTP/JSON server
    webui.py     the self-contained web dashboard served at /
    config.py    config loading and first-run token generation
"""

__version__ = "1.0.0"

# The Drobo protocol lives in its own package, `drobo_nasd`, a sibling of this
# one rather than a subpackage. That split is deliberate: the library knows
# about Drobos and nothing about applications, so it is useful to anyone with
# this hardware, not just to this dashboard.
#
# The sdk/ that ships in this checkout always wins over anything installed in
# the interpreter. This repo and its counterpart both publish a package named
# `drobo_nasd`, and the two trees are not identical, so a single `pip install`
# from either one would otherwise silently redirect this agent's imports into
# the other repo's copy. Binding to the sibling sdk/ first keeps this checkout
# running its own code and nothing else. Only fall back to an installed copy
# when there is no sdk/ next to us (an unpacked release rather than a clone).
import os as _os
import sys as _sys

_sdk = _os.path.abspath(
    _os.path.join(_os.path.dirname(__file__), "..", "..", "sdk"))
if _os.path.isdir(_os.path.join(_sdk, "drobo_nasd")):
    if _sdk in _sys.path:
        _sys.path.remove(_sdk)
    _sys.path.insert(0, _sdk)  # ahead of site-packages, not merely present
else:  # no checkout beside us; an installed copy is the only option
    import drobo_nasd as _probe  # noqa: F401
