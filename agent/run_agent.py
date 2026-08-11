#!/usr/bin/env python3
"""
Drobo Agent - the always-on piece.

Watches the Drobo, keeps history, raises alerts, receives photo backups, and
serves all of it as JSON for the Windows and iOS apps.

    py run_agent.py                     # uses config.json beside this file
    py run_agent.py --config other.json
    py run_agent.py --once              # take one reading, print it, exit

Nothing here is Drobo-specific: all the hardware knowledge lives in
drobo_agent/drivers.py.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from drobo_agent import api, config, notify  # noqa: E402
from drobo_agent.monitor import Monitor  # noqa: E402
from drobo_agent.photos import PhotoStore  # noqa: E402

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--once", action="store_true", help="one reading, then exit")
    ap.add_argument("--open", action="store_true",
                    help="open the dashboard in your browser once it's up")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)

    notifier = notify.build(cfg["notify"])
    monitor = Monitor(cfg, notifier)

    if args.once:
        monitor.driver.connect()
        print(json.dumps(monitor.poll_once().to_dict(), indent=2))
        return 0

    photos = PhotoStore(cfg["photos"])
    photos.load()

    monitor.start()
    httpd = api.serve(cfg, monitor, photos)

    bind = cfg["agent"]["bind"]
    port = cfg["agent"]["port"]
    shown = "127.0.0.1" if bind in ("0.0.0.0", "") else bind
    print(f"[agent] listening on http://{shown}:{port}", flush=True)
    print(f"[agent] open http://{shown}:{port}/?token={cfg['agent']['token']}", flush=True)
    if bind == "0.0.0.0":
        print("[agent] bound to all interfaces - LAN only, never port-forward this",
              flush=True)

    if args.open:
        # Give the server a moment to bind before the browser knocks.
        url = f"http://{shown}:{port}/?token={cfg['agent']['token']}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    stopping = threading.Event()

    def shutdown(_signum=None, _frame=None):
        if stopping.is_set():
            return
        stopping.set()
        print("\n[agent] shutting down ...", flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGTERM, shutdown)
    except (AttributeError, ValueError):
        pass  # not available on every Windows console

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        shutdown()
    finally:
        monitor.stop()
        photos.flush()
        httpd.server_close()
        print("[agent] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
