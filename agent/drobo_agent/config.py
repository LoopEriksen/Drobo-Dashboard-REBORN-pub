"""
Configuration loading.

One JSON file, sensible defaults for everything, and an access token that gets
generated automatically the first time the agent starts.
"""

from __future__ import annotations

import copy
import json
import os
import secrets

DEFAULTS: dict = {
    "agent": {
        # 127.0.0.1 means "this PC only". Change to 0.0.0.0 when you want the
        # phone to reach it -- but only ever on your own LAN, never forwarded
        # from the internet.
        "bind": "127.0.0.1",
        "port": 7420,
        # Generated on first run if left empty. Every request must send it as
        # the X-Agent-Token header.
        "token": "",
        "poll_seconds": 10,
        "history_length": 8640,  # ~24h at 10s
    },
    "drobo": {
        # The REAL driver, by default, with automatic discovery. This said
        # "mock" long after Drobo5NDriver was working -- a leftover from when
        # the protocol was still unmapped -- which meant a fresh install's
        # first run monitored a SIMULATED healthy Drobo while claiming to
        # watch the owner's array. For storage-health software, showing fake
        # good news by default is the one first-run outcome that must never
        # ship. The simulator is still one edit away ("driver": "mock"), and
        # the demo launcher uses its own config.mock.json.
        "driver": "drobo5n",
        "host": "auto",
        "username": "",
        "password": "",
    },
    "updates": {
        # DORMANT BY DEFAULT, and that is the shipping state.
        #
        # The machinery is built (see updates.py) so it exists before it is
        # needed rather than being assembled in a hurry on release day. With
        # enabled false it opens no sockets and contacts nothing at all -- not
        # a default endpoint that happens to 404, but no network activity
        # whatsoever. There is a test that boobytraps urllib to prove it.
        #
        # To switch on later: set enabled true and point manifest_url at an
        # https:// JSON file carrying {"version", "download_url", "sha256"}.
        # The agent will only ever TELL you an update exists; downloading and
        # installing stays a human decision.
        "enabled": False,
        "manifest_url": "",
        "channel": "stable",
        # Seconds between checks, once enabled. Daily -- this is a NAS
        # dashboard, not a browser.
        "check_seconds": 86400,
    },
    "alerts": {
        "temperature_warning_c": 50.0,
        "temperature_critical_c": 60.0,
        "capacity_warning_fraction": 0.85,
        "capacity_critical_fraction": 0.95,
        # Don't re-send the same alert more often than this.
        "resend_seconds": 3600,
    },
    "notify": {
        # console | ntfy
        "backends": ["console"],
        "ntfy": {
            "server": "https://ntfy.sh",
            "topic": "",
        },
    },
    "photos": {
        "enabled": True,
        # Where uploaded photos land. Point this at a mapped drive or UNC path
        # on the Drobo, e.g. "Z:\\Photos" or "\\\\drobo\\Photos".
        "target_dir": "",
        # Files are filed as target_dir/YYYY/YYYY-MM/name
        "organize_by_date": True,
        "max_upload_mb": 512,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: str) -> dict:
    """Read config from disk, filling in anything missing from DEFAULTS."""
    on_disk: dict = {}
    if os.path.exists(path):
        # utf-8-SIG, not utf-8: it reads plain UTF-8 unchanged and also
        # tolerates a leading byte-order mark. Windows Notepad and PowerShell's
        # `Set-Content -Encoding utf8` both write that mark, so anyone editing
        # this file by hand on Windows produces a config the plain reader
        # rejected with a raw JSONDecodeError traceback and no hint of the
        # cause. Hit for real on 2026-08-07 by exactly that route.
        with open(path, "r", encoding="utf-8-sig") as fh:
            try:
                on_disk = json.load(fh)
            except json.JSONDecodeError as exc:
                # A config file the owner probably just edited. Say where and
                # what, rather than unwinding a stack from json's internals.
                raise SystemExit(
                    f"[config] {path} is not valid JSON: {exc.msg} "
                    f"(line {exc.lineno}, column {exc.colno}).\n"
                    f"[config] Nothing was changed. Fix the file, or delete it "
                    f"to start again from defaults."
                ) from None
    cfg = _merge(DEFAULTS, on_disk)

    # Generate an access token on first run and write it back, so the value is
    # stable across restarts and the apps can be configured once.
    if not cfg["agent"]["token"]:
        cfg["agent"]["token"] = secrets.token_urlsafe(24)
        save(path, cfg)
        print(f"[config] generated a new access token and saved it to {path}")

    return cfg


def save(path: str, cfg: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, path)
