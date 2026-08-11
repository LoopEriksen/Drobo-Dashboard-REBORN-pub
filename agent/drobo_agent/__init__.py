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
# Until it is pip-installed, make the sibling importable. Checked rather than
# assumed, so a real installed copy always wins over the checkout.
import os as _os
import sys as _sys

try:  # already installed, or already on the path
    import drobo_nasd as _probe  # noqa: F401
except ModuleNotFoundError:  # running from a checkout
    _sdk = _os.path.abspath(
        _os.path.join(_os.path.dirname(__file__), "..", "..", "sdk"))
    if _os.path.isdir(_os.path.join(_sdk, "drobo_nasd")) and _sdk not in _sys.path:
        _sys.path.insert(0, _sdk)
