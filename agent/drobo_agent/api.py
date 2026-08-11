"""
The agent's HTTP API.

Plain JSON over loopback (or the LAN, if you turn that on). The Windows app and
the iOS app both talk to this and neither knows the Drobo protocol exists.

Every endpoint except /api/ping requires the access token from config.json,
sent as the  X-Agent-Token  header. /api/events also accepts ?token= because
browser EventSource can't set headers.

    GET  /                      the web dashboard (no auth - it holds no data)
    GET  /api/ping              liveness + agent version (no auth)
    GET  /api/status            everything: device, capacity, drives, state
    GET  /api/device            identity and firmware only
    GET  /api/drives            per-bay detail
    GET  /api/volumes           the volumes the pack is split into
    GET  /api/capacity          capacity numbers only
    GET  /api/battery           cache-battery health
    GET  /api/health            pack/device-level health values (health telemetry) -- error
                                 counts, relayout/double-degraded, raw status codes
    GET  /api/discover[?host=]  Drobos visible on this network, each already
                                 identified, so a front-end can list them and
                                 let you pick one instead of typing an address.
                                 ?host= adds one typed address to the search,
                                 for networks that filter mDNS entirely
    GET  /api/netcheck[?host=]  whether this PC is even on the same network as
                                 a given Drobo -- the usual reason one "vanishes"
    GET  /api/shares            the file shares, with UNC paths a front-end can
                                 open or map, plus why Windows may refuse them
    GET  /api/droboapps         software installed ON the Drobo (name, version,
                                 running state, whether it has a web interface)
    GET  /api/leds              LED brightness as the device reports it, plus
                                 whether the unit is in shop-demo mode (in which
                                 case its capacity numbers are fabricated)
    GET  /api/update            whether a newer version exists. DORMANT unless
                                 config.json's "updates" block is switched on --
                                 it then only ever TELLS you; downloading and
                                 installing stay a human decision
    GET  /api/emailalerts       whether email alerts are configured -- never the
                                 credentials, only host and recipients
    POST /api/emailalerts/test  send a test email through that configuration
    GET  /api/backup/status     progress of a running or finished backup
    POST /api/backup/plan       what a backup WOULD do. Copies nothing
    POST /api/backup/start      actually run it. Needs "confirm": true
    GET  /api/drobosync         Drobo-to-Drobo replication state. Built and
                                 dormant -- it needs a second Drobo, so it
                                 reports "not configured" rather than failing
    GET  /api/eventlog          the Drobo's own device event log (not the SSE stream)
    GET  /api/performance       throughput stats (read/write bytes per second)
    GET  /api/alerts[?all=1]    active alerts (or the full history)
    GET  /api/history[?limit=N] trimmed series for graphing
    GET  /api/events            server-sent events: live snapshots and alerts
    GET  /api/photos/stats      how much has been backed up
    GET  /api/pair              url + token for the phone
    GET  /api/pair.svg          the same, as a scannable QR code
    POST /api/photos/have       {"hashes":[...]} -> {"missing":[...]}
    POST /api/photos/rescan     rebuild the dedupe index from the files actually
                                 on the Drobo -- run after deleting any by hand
    POST /api/photos            raw file body; X-Filename, X-Taken-At headers
    POST /api/identify          flash the Drobo's lights -- the ONLY endpoint
                                 here that changes anything on the device
    POST /api/mock/<action>/<bay>   mock driver only: fail|warn|pull, and reset
    POST /api/mock/battery/<warning|failed>  mock driver only: cache-battery fault injection
    POST /api/mock/power/<warning|failed>    mock driver only: PSU fault injection
"""

from __future__ import annotations

import json
import queue
import socket
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import backup, notify, qr, updates, webui
from drobo_nasd import shares as shares_mod
from drobo_nasd import (config_cmd, discovery, droboapps, drobosync, esatm,
                        identify as identify_cmd, netcheck, sysinfo)
from .drivers import MockDriver
from .photos import PhotoStoreError

#: One backup at a time, for the whole agent. Two robocopy processes writing
#: the same destination would interleave and neither result would be
#: trustworthy, so the runner is a module-level singleton rather than
#: per-request.
_BACKUP = backup.BackupRunner()

VERSION = "1.0.0"
MAX_JSON_BODY = 4 * 1024 * 1024

#: The live 5N reports its LED brightness as 59. The firmware command table
#: names eCmdGetDimming and gives its id, and documents NOTHING about the
#: scale -- so whether 59 means 59%, 59 of 255, or Drobo's own step 59 is
#: unknown. Shown with the number rather than quietly rendered as a
#: percentage, because inventing a unit is how a measurement becomes a lie.
DIMMING_SCALE_NOTE = (
    "The Drobo reports this as a bare number. Its firmware never says what "
    "the scale is -- percent, 0-255, or its own steps -- so it is shown "
    "exactly as reported rather than converted into a unit we would be "
    "guessing at.")


def _identify_message(interval, simulated: bool = False) -> str:
    """
    What to tell the owner after an Identify, given what was actually sent.

    Three outcomes, and one honesty problem. Identify is a MODE, not a pulse:
    Drobo Dashboard's own warning says the lights blink continuously for
    fifteen minutes and the same button stops them. "Sent, the lights should
    be flashing" -- which is what this said until 2026-08-06 -- describes a
    brief flash, so somebody who wanted a brief flash gets fifteen minutes of
    blinking and no idea how to stop it.

    The honesty problem is the unit. Dashboard says fifteen minutes and sends
    fifteen of something; whether that something is minutes is not confirmed.
    So the duration is attributed to the original product rather than promised
    here, and the way to stop it is offered regardless -- which is useful
    whichever reading turns out to be right.
    """
    who = "Simulated Drobo" if simulated else "The Drobo"
    if interval == identify_cmd.STOP_INTERVAL:
        return f"Stop sent. {who}'s lights should go back to normal."
    if interval is None:
        return ("Sent with no interval -- the control case. If the lights stay "
                "still now but blink when an interval is sent, that settles "
                "what the parameter does.")
    return (f"Sent. {who}'s lights should be blinking now. The original "
            f"Dashboard used this value and blinked for 15 minutes, so use "
            f"Stop when you have found the box.")


class _Handler(BaseHTTPRequestHandler):
    server_version = f"DroboAgent/{VERSION}"
    protocol_version = "HTTP/1.1"

    # /api/droboconfig opens a second connection to the device's command port.
    # Bounded so a slow or sulking Drobo can't stall an API worker thread.
    CONFIG_TIMEOUT = 8.0

    # How long a /api/shares answer stays good for.
    #
    # This is a cache for the device's benefit, not ours. Each call costs two
    # command-port connections, and on 2026-07-26 that port was measurably worn
    # out by heavy use -- eCmdGetSysInfo stopped answering entirely after about
    # ninety connections in an hour, while the array stayed perfectly healthy
    # (see docs/command-surface.md). Share names and network settings change
    # roughly never, so a refreshing dashboard has no business asking again
    # every few seconds.
    SHARES_TTL = 60.0
    _shares_cache: tuple[float, dict] | None = None
    _shares_lock = threading.Lock()

    # Network discovery costs a few seconds of listening, so the front page
    # doesn't get to re-run it on every render.
    DISCOVER_TIMEOUT = 3.0
    DISCOVER_TTL = 30.0
    _discover_cache: tuple[float, dict] | None = None
    _discover_lock = threading.Lock()

    # The update check, cached for its configured interval. A dashboard
    # left open in a tab must not turn a daily check into a per-second one.
    _update_cache: tuple[float, dict] | None = None
    _update_lock = threading.Lock()

    # injected by serve()
    monitor = None
    photos = None
    token = ""

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        if self.path.startswith("/api/events"):
            return
        print(f"[api] {self.address_string()} {fmt % args}", flush=True)

    def _authorised(self, params: dict) -> bool:
        supplied = self.headers.get("X-Agent-Token", "")
        if not supplied:
            supplied = (params.get("token") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, ctype: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _pair_url(self) -> str:
        """
        The address the phone should use. Prefer whatever host the browser
        reached us on, but if that's loopback the phone can't use it -- fall
        back to this machine's LAN address.
        """
        host = (self.headers.get("Host", "") or "").split(":")[0]
        if host in ("", "localhost", "127.0.0.1", "::1", "[::1]"):
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 53))
                host = probe.getsockname()[0]
            except OSError:
                host = "127.0.0.1"
            finally:
                probe.close()
        return f"http://{host}:{self.server.server_address[1]}"

    def _read_body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return b""
        if length <= 0:
            return b""
        if length > limit:
            raise ValueError(f"body of {length} bytes exceeds the {limit} byte limit")
        return self.rfile.read(length)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required name
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/api/ping":
            return self._send_json(
                {"ok": True, "agent": "drobo-agent", "version": VERSION,
                 "uptime_seconds": int(time.time() - self.monitor.started_at)}
            )

        # The dashboard shell carries no data of its own -- everything on it
        # arrives from the authenticated API below -- so it is served without a
        # token. That lets you bookmark it and be prompted for the token once.
        if path == "/":
            return self._send_text(webui.PAGE, ctype="text/html")

        if not self._authorised(params):
            return self._send_json({"error": "missing or invalid X-Agent-Token"}, 401)
        if path == "/api/status":
            return self._send_json(self.monitor.snapshot_dict())
        if path == "/api/device":
            return self._send_json(self.monitor.snapshot_dict().get("device", {}))
        if path == "/api/volumes":
            # The volumes (Drobo calls them LUNs) the pack is carved into --
            # what Windows shows as drive letters. Read straight from the
            # status greeting, so this costs no extra command and no extra
            # connection to a device that tires easily.
            snap = self.monitor.snapshot_dict()
            return self._send_json({"volumes": snap.get("volumes", []),
                                    "max_volumes": snap.get("max_volumes")})
        if path == "/api/drives":
            return self._send_json({"drives": self.monitor.snapshot_dict().get("drives", [])})
        if path == "/api/capacity":
            return self._send_json(self.monitor.snapshot_dict().get("capacity", {}))
        if path == "/api/battery":
            return self._send_json(self.monitor.snapshot_dict().get("battery", {}))
        if path == "/api/health":
            return self._send_json(self.monitor.snapshot_dict().get("pack_health", {}))
        if path == "/api/droboconfig":
            # On demand, not on every poll: this opens a second connection to
            # the command port, and configuration changes rarely.
            section = (params.get("section") or ["network"])[0]
            # The simulator answers from its own canned config, in the same
            # shape a real device returns, so demo mode shows a populated
            # Settings tab instead of "no live Drobo connection yet".
            if getattr(self.monitor.driver, "simulate_config", False):
                try:
                    return self._send_json({
                        "section": section,
                        "config": self.monitor.driver.get_config(section),
                    })
                except ValueError as exc:
                    return self._send_json({"error": str(exc), "section": section}, 400)
            host, esa, err = self._command_target()
            if err:
                return self._send_json(err, 503)
            try:
                return self._send_json({
                    "section": section,
                    "config": config_cmd.get_config(host, esa, section,
                                                    timeout=self.CONFIG_TIMEOUT),
                })
            except config_cmd.ConfigError as exc:
                return self._send_json({"error": str(exc), "section": section}, 502)
            except OSError as exc:
                return self._send_json(
                    {"error": f"could not reach the command port: {exc}"}, 502)
        if path == "/api/shares":
            return self._shares()
        if path == "/api/discover":
            return self._discover((params.get("host") or [""])[0])
        if path == "/api/netcheck":
            return self._netcheck((params.get("host") or [""])[0])
        if path == "/api/droboapps":
            # What's installed ON the Drobo. On-demand, not polled: the app
            # list changes when someone installs something, which is roughly
            # never, and the command port does not enjoy being asked twice.
            driver = self.monitor.driver
            if getattr(driver, "simulate_config", False):
                return self._send_json({**driver.get_droboapps(), "simulated": True})
            host, esa, err = self._command_target()
            if err:
                return self._send_json(err, 503)
            try:
                return self._send_json(
                    droboapps.get_droboapps(host, esa, timeout=self.CONFIG_TIMEOUT))
            except (sysinfo.SysInfoError, OSError) as exc:
                return self._send_json({"error": str(exc), "apps": []}, 502)

        if path == "/api/update":
            # Dormant unless config.json's "updates" block is switched on. With
            # it off this touches no network at all -- see updates.check.
            #
            # Cached, because the answer changes at most daily and this is
            # rendered on a dashboard that refreshes every few seconds. Without
            # the cache, leaving a browser tab open would hammer whatever host
            # ends up serving the manifest.
            with _Handler._update_lock:
                cached = _Handler._update_cache
                ttl = float(self.monitor.cfg.get("updates", {}).get("check_seconds", 86400))
                if cached and time.time() - cached[0] < ttl:
                    return self._send_json(cached[1])
                status = updates.check(self.monitor.cfg.get("updates", {}), VERSION)
                payload = status.to_dict()
                # A disabled check is not worth caching for a day: flipping the
                # setting on should take effect on the next refresh, not
                # tomorrow.
                if status.state != updates.STATE_DISABLED:
                    _Handler._update_cache = (time.time(), payload)
            return self._send_json(payload)

        if path == "/api/leds":
            # eCmdGetDimming and eCmdGetDemoModeInfo: two of the seven commands
            # that answer with real data on this firmware. Both had working
            # parsers and no way to reach them until 2026-07-28 -- a measurement
            # nobody could see.
            #
            # On demand, never polled: neither changes without someone changing
            # it, and each costs a command-port connection on a device that
            # stops answering under sustained use.
            driver = self.monitor.driver
            if getattr(driver, "simulate_config", False):
                return self._send_json({
                    "dimming": driver.get_dimming(),
                    **driver.get_demo_mode(),
                    "scale_note": DIMMING_SCALE_NOTE,
                    "simulated": True,
                })
            host, esa, err = self._command_target()
            if err:
                return self._send_json(err, 503)
            payload: dict = {"scale_note": DIMMING_SCALE_NOTE}
            try:
                payload["dimming"] = droboapps.get_dimming(
                    host, esa, timeout=self.CONFIG_TIMEOUT)
            except (sysinfo.SysInfoError, OSError) as exc:
                payload["dimming"] = None
                payload["error"] = str(exc)
            try:
                payload.update(droboapps.get_demo_mode(
                    host, esa, timeout=self.CONFIG_TIMEOUT))
            except (sysinfo.SysInfoError, OSError):
                # Best-effort. A missing demo-mode read must not cost the
                # brightness reading, which is the half anyone came for.
                payload.setdefault("demo_mode", None)
                payload.setdefault("scale_factor", None)
            return self._send_json(payload)

        if path == "/api/backup/status":
            job = _BACKUP.job
            return self._send_json(job.to_dict() if job else
                                   {"running": False, "summary": "No backup has been run."})

        if path == "/api/emailalerts":
            # Whether email alerts are set up, WITHOUT ever returning the
            # credentials that make them work. The Windows app renders this on
            # its Preferences tab; it must be able to say "configured" without
            # being told the password, which is why this reports a summary
            # rather than the config block.
            email = self.monitor.cfg.get("notify", {}).get("email", {})
            enabled = "email" in self.monitor.cfg.get("notify", {}).get("backends", [])
            configured = bool(enabled and email.get("host") and email.get("to"))
            recipients = email.get("to", "")
            if isinstance(recipients, list):
                recipients = ", ".join(recipients)
            return self._send_json({
                "configured": configured,
                "enabled": enabled,
                # Host and recipients are not secrets and are the two things
                # somebody needs to confirm they set it up the way they meant.
                # The password is never included, at any verbosity.
                "host": email.get("host", ""),
                "to": recipients,
                "summary": (f"Sending to {recipients} via {email.get('host','')}."
                            if configured else
                            "Not configured."),
            })

        if path == "/api/drobosync":
            # Drobo-to-Drobo replication. Built and dormant by decision
            # (ROADMAP, 2026-07-28): it needs two units and the owner has one,
            # so it reports "not configured" honestly rather than being absent.
            driver = self.monitor.driver
            if getattr(driver, "simulate_config", False):
                return self._send_json({**driver.get_drobosync(), "simulated": True})
            host, esa, err = self._command_target()
            if err:
                return self._send_json(err, 503)
            try:
                return self._send_json(drobosync.summary(host, esa,
                                                         timeout=self.CONFIG_TIMEOUT))
            except (drobosync.DroboSyncError, OSError) as exc:
                return self._send_json({"error": str(exc), "configured": False,
                                        "reads": {}}, 502)

        if path == "/api/eventlog":
            return self._send_json({"events": self.monitor.snapshot_dict().get("event_log", [])})
        if path == "/api/performance":
            return self._send_json(self.monitor.snapshot_dict().get("performance", {}))
        if path == "/api/alerts":
            want_all = (params.get("all") or ["0"])[0] not in ("0", "", "false")
            alerts = self.monitor.all_alerts() if want_all else self.monitor.active_alerts()
            return self._send_json({"alerts": alerts})
        if path == "/api/history":
            try:
                limit = int((params.get("limit") or ["720"])[0])
            except ValueError:
                limit = 720
            return self._send_json({"points": self.monitor.history_dicts(max(1, min(limit, 20000)))})
        if path == "/api/photos/stats":
            return self._send_json(self.photos.stats())
        if path == "/api/photos/session":
            digest = ((params.get("id") or [""])[0]
                      or self.headers.get("X-Upload-Id") or "").lower()
            try:
                return self._send_json(self.photos.session(digest))
            except PhotoStoreError as exc:
                return self._send_json({"error": str(exc)}, 400)
        if path == "/api/pair":
            # Everything the phone needs to connect, in one payload.
            return self._send_json({
                "agent": "drobo-agent", "version": VERSION,
                "url": self._pair_url(),
                "token": self.token,
                "photos_enabled": self.photos.enabled,
            })
        if path == "/api/pair.svg":
            # Carries the token, so this stays behind auth like everything else.
            return self._send_text(
                qr.svg(f"{self._pair_url()}/?token={self.token}"),
                ctype="image/svg+xml")
        if path == "/api/events":
            return self._stream_events()

        return self._send_json({"error": "not found", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)

        if not self._authorised(params):
            return self._send_json({"error": "missing or invalid X-Agent-Token"}, 401)

        if path in ("/api/backup/plan", "/api/backup/start"):
            try:
                body = json.loads(self._read_body(MAX_JSON_BODY) or b"{}")
            except (ValueError, UnicodeDecodeError) as exc:
                return self._send_json({"error": f"bad JSON: {exc}"}, 400)
            source = str(body.get("source", "")).strip()
            destination = str(body.get("destination", "")).strip()
            mirror = bool(body.get("mirror", False))

            if path == "/api/backup/plan":
                # Always safe: describes and copies nothing.
                return self._send_json(
                    backup.plan(source, destination, mirror).to_dict())

            # /start writes. It requires confirm:true ON TOP of being a
            # separate endpoint from /plan -- two deliberate acts before
            # anything is written, because the cost of an accidental run with
            # mirror on is somebody's backup deleting itself.
            if body.get("confirm") is not True:
                return self._send_json(
                    {"error": "Refusing to start without \"confirm\": true. Call "
                              "/api/backup/plan first and show the user what it "
                              "says -- especially the mirror warning."}, 400)
            try:
                job = _BACKUP.start(source, destination, mirror)
            except backup.BackupError as exc:
                return self._send_json({"error": str(exc)}, 409)
            return self._send_json(job.to_dict())

        if path == "/api/emailalerts/test":
            # POST, not GET: this sends real mail, and a GET must never have a
            # side effect -- browsers and link prefetchers will happily follow
            # one. Same rule that put /api/identify on POST.
            notify_cfg = self.monitor.cfg.get("notify", {})
            if "email" not in notify_cfg.get("backends", []):
                return self._send_json(
                    {"error": "Email alerts are not switched on. Add \"email\" to the "
                              "notify backends in the agent's config.json."}, 409)
            mailer = notify.build({**notify_cfg, "backends": ["email"]})
            members = [m for m in getattr(mailer, "members", []) if m.name == "email"]
            if not members:
                return self._send_json(
                    {"error": "Email is switched on but incompletely configured -- "
                              "check host and to in config.json. The agent log says "
                              "which field is missing."}, 409)
            try:
                members[0].send(
                    "Drobo Dashboard REBORN: test",
                    "This is a test. If you are reading it, alerts about your Drobo "
                    "will reach you here.",
                    "info")
            except Exception as exc:  # noqa: BLE001 - already sanitised by EmailNotifier
                return self._send_json({"error": str(exc)}, 502)
            return self._send_json({"ok": True, "message": "Sent. Check your inbox."})

        if path == "/api/photos/rescan":
            # Re-read what is actually on the Drobo and rebuild the dedupe
            # index from it. On demand rather than scheduled: it hashes every
            # backed-up file, which is real I/O over SMB.
            #
            # Worth running after deleting photos off the Drobo by hand --
            # otherwise the index still lists them, `have` answers "already got
            # it", and the phone never re-sends. See PhotoStore.rescan.
            try:
                return self._send_json(self.photos.rescan())
            except PhotoStoreError as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/photos/have":
            try:
                payload = json.loads(self._read_body(MAX_JSON_BODY) or b"{}")
            except (ValueError, UnicodeDecodeError) as exc:
                return self._send_json({"error": f"bad JSON: {exc}"}, 400)
            hashes = payload.get("hashes")
            if not isinstance(hashes, list):
                return self._send_json({"error": "expected {\"hashes\": [...]}"}, 400)
            return self._send_json({"missing": self.photos.missing([str(h) for h in hashes])})

        if path == "/api/photos":
            return self._receive_photo()

        # Chunked upload -- the path videos take.
        if path == "/api/photos/begin":
            try:
                p = json.loads(self._read_body(MAX_JSON_BODY) or b"{}")
            except (ValueError, UnicodeDecodeError) as exc:
                return self._send_json({"error": f"bad JSON: {exc}"}, 400)
            try:
                return self._send_json(self.photos.begin(
                    str(p.get("filename", "")), int(p.get("size", 0)),
                    str(p.get("sha256", "")).lower(),
                    float(p["taken_at"]) if p.get("taken_at") else None))
            except (PhotoStoreError, ValueError, TypeError) as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/photos/chunk":
            digest = (self.headers.get("X-Upload-Id") or "").lower()
            try:
                offset = int(self.headers.get("X-Offset", "-1"))
            except ValueError:
                return self._send_json({"error": "X-Offset must be an integer"}, 400)
            if offset < 0:
                return self._send_json(
                    {"error": "X-Upload-Id and X-Offset headers are required"}, 400)
            try:
                chunk = self._read_body(64 * 1024 * 1024)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 413)
            try:
                return self._send_json(self.photos.append(digest, offset, chunk))
            except PhotoStoreError as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/photos/finish":
            digest = (self.headers.get("X-Upload-Id") or "").lower()
            try:
                return self._send_json(self.photos.finish(digest))
            except PhotoStoreError as exc:
                return self._send_json({"error": str(exc)}, 400)

        # POST, not GET, and deliberately so: this is the only endpoint in the
        # agent that changes anything on the Drobo. A GET must never have a
        # side effect -- browsers and link prefetchers will happily follow one.
        if path == "/api/identify":
            # ?interval=N sets <IdentifyInterval>. Omitted uses the SDK
            # default; 0 is the best guess at stopping a running blink; "none"
            # sends the historical empty <Params>, which is the control case
            # for proving the interval is what makes the lights move. Exposed
            # here rather than kept internal precisely so that comparison can
            # be run without editing code.
            raw = (params.get("interval") or [""])[0].strip().lower()
            if not raw:
                interval = identify_cmd.DEFAULT_INTERVAL
            elif raw == "none":
                interval = None
            else:
                try:
                    interval = int(raw)
                except ValueError:
                    return self._send_json(
                        {"error": f"interval must be a whole number or 'none', got {raw!r}"},
                        400)

            # Checked HERE, before the simulated/real fork, so both arms
            # enforce the same rule. Left to the packet builder it would only
            # bind the hardware path, and a simulator that accepts what the
            # device rejects certifies things that do not work.
            try:
                identify_cmd.validate_interval(interval)
            except identify_cmd.IdentifyParameterError as exc:
                return self._send_json({"error": str(exc)}, 400)

            driver = self.monitor.driver
            if getattr(driver, "simulate_config", False):
                # The simulator "blinks" by saying it did. Lets the whole path
                # -- button, endpoint, result handling -- be exercised without
                # hardware, which is how it was built in the first place. It
                # takes the interval too, so the simulated path proves the
                # value is carried rather than dropped on the way through.
                driver.identify(interval)
                return self._send_json({"ok": True, "simulated": True, "interval": interval,
                                        "message": _identify_message(interval, simulated=True)})
            host, esa, err = self._command_target()
            if err:
                return self._send_json(err, 503)
            try:
                result = identify_cmd.identify(host, esa, timeout=self.CONFIG_TIMEOUT,
                                               interval=interval)
            except identify_cmd.IdentifyParameterError as exc:
                # Caller asked for something we will not send. Must be 400 and
                # not 502 -- checked before IdentifyError, which it subclasses.
                return self._send_json({"error": str(exc)}, 400)
            except identify_cmd.IdentifyError as exc:
                return self._send_json({"error": str(exc)}, 502)
            except OSError as exc:
                return self._send_json(
                    {"error": f"could not reach the command port: {exc}"}, 502)
            return self._send_json({"ok": True, **result,
                                    "message": _identify_message(interval)})

        if path.startswith("/api/mock/"):
            return self._mock_control(path)

        return self._send_json({"error": "not found", "path": path}, 404)

    # -- handlers ----------------------------------------------------------

    def _command_target(self):
        """
        Where to send a command, or why we can't yet.

        The command port needs both the device's address and its serial, and
        the serial only arrives from a successful status read -- so a fresh
        agent that hasn't polled yet genuinely cannot ask the Drobo anything.
        Returns (host, esa_id, error_dict); exactly one of esa_id / error is set.
        """
        driver = self.monitor.driver
        host = getattr(driver, "_resolved_host", "") or getattr(driver, "host", "")
        esa = getattr(driver, "_known_esa_id", "")
        if not host or not esa or host in ("auto", ""):
            return "", "", {
                "error": "no live Drobo connection yet -- the command port needs "
                         "the device's address and serial, which come from a "
                         "successful status read first"
            }
        return host, esa, None

    def _netcheck(self, host: str) -> None:
        """
        Is this PC even on the same network as the Drobo?

        /api/status already carries this for the device the agent is
        monitoring, in its "network_hint" field. This endpoint exists for the
        case /api/status cannot cover: a front-end sitting on its device
        picker, having failed to find the Drobo it remembers, with an address
        from its own settings and no snapshot to attach it to. "Your usual
        Drobo isn't showing up" and "cannot reach the Drobo" have the same
        cause far more often than not -- mDNS doesn't cross a subnet boundary
        any more than TCP does -- so both screens deserve the same answer.

        `host` defaults to whatever the agent's own driver is trying to reach.

        Purely local: it reads this machine's interface list and does
        arithmetic. Nothing is connected to, so an arbitrary `host` cannot be
        used to make the agent touch anything -- it is a subnet comparison,
        not a probe. Non-IPv4 input is answered honestly with "no opinion"
        rather than an error, because a hostname is a perfectly reasonable
        thing to ask about and the truthful answer is that this check can't
        speak to it.
        """
        target = (host or self.monitor.driver.target_host() or "").strip()
        result = netcheck.diagnose(target) if target else None
        if result is None:
            return self._send_json({
                "target": target,
                "checked": False,
                "same_network": None,
                "hint": None,
                "reason": "not an IPv4 address" if target
                          else "no address known for the Drobo yet",
            })
        return self._send_json({
            "target": result.target,
            "checked": True,
            "same_network": result.same_network,
            # Null on a match, so a front-end can render `hint` whenever it is
            # present without also having to check same_network and get that
            # test the right way round.
            "hint": None if result.same_network else result.to_dict(),
        })

    def _discover(self, manual_host: str = "") -> None:
        """
        Every Drobo we can see on this network, already identified.

        This exists so the front page can show you your Drobo and let you click
        it, instead of asking you to know its IP address. Each entry is the
        result of actually connecting and reading the device's own greeting --
        so the name, model and firmware shown are the device's own claims about
        itself, not a guess from an mDNS record.

        `is_current` marks the one the agent is already monitoring.

        `?host=<ip>` adds one address you typed to the search, for the networks
        where the automatic half cannot work. mDNS is multicast, and corporate
        networks, guest SSIDs and some consumer routers filter multicast
        outright -- on those, browsing returns nothing no matter how long it
        listens, and knowing your Drobo's address is the only way in. The typed
        address is probed exactly like a discovered one: connect, read the
        greeting, report what the device says about itself. It is listed with
        `found_by: "manual"` so a front-end can be honest about where it came
        from, and it earns no extra trust for having been typed.

        Read-only and safe to call repeatedly, but it does cost a few seconds
        of mDNS listening, so it is cached briefly. A request naming a host
        skips the cache in both directions -- it must actually go and look, and
        its answer must not become the cached answer for callers who didn't ask
        about that address.
        """
        manual_host = (manual_host or "").strip()
        with _Handler._discover_lock:
            cached = _Handler._discover_cache
            if not manual_host and cached and time.time() - cached[0] < self.DISCOVER_TTL:
                return self._send_json(cached[1])

            driver = self.monitor.driver
            current = (getattr(driver, "_resolved_host", "")
                       or getattr(driver, "host", ""))

            found: list[dict] = []
            seen: set[str] = set()

            if manual_host:
                # One address was named, so look at exactly that address. The
                # multicast browse is skipped entirely rather than run and
                # ignored: it costs DISCOVER_TIMEOUT seconds, and the whole
                # reason somebody is typing an address is that the browse comes
                # back empty on their network. Making them wait it out to
                # confirm a host they already know would be three seconds spent
                # proving the thing that failed still fails.
                candidates = [discovery.Candidate(
                    host=manual_host, port=esatm.DEFAULT_PORT, source="manual")]
            else:
                try:
                    candidates = discovery.discover_mdns(timeout=self.DISCOVER_TIMEOUT)
                except OSError as exc:
                    return self._send_json(
                        {"error": f"could not search the network: {exc}",
                         "devices": []}, 502)

                # The address we already know works belongs in the list even if
                # mDNS was quiet -- on some Windows networks it always is.
                if current and current not in [c.host for c in candidates]:
                    candidates.insert(0, discovery.Candidate(
                        host=current,
                        port=getattr(driver, "_resolved_port", esatm.DEFAULT_PORT),
                        source="in-use"))

            for cand in candidates:
                if cand.host in seen:
                    continue
                seen.add(cand.host)
                try:
                    # No expected_esa_id: we are listing what's out there, not
                    # asserting which one is yours. Identity pinning still
                    # happens in the driver when you actually connect.
                    v = discovery.verify_candidate(cand, timeout=self.DISCOVER_TIMEOUT)
                except (OSError, discovery.IdentityMismatch, esatm.FrameError):
                    continue  # answered badly or not at all; not a Drobo we can use
                ident = v.identity
                found.append({
                    "host": cand.host,
                    "port": cand.port,
                    "found_by": cand.source,
                    "name": ident.get("name", ""),
                    "model": ident.get("model", ""),
                    "firmware": ident.get("firmware", ""),
                    # The serial identifies the box permanently and is what
                    # pins a future connection to THIS device. It is not a
                    # secret -- the Drobo hands it to anyone who connects.
                    "esa_id": ident.get("esa_id", ""),
                    "is_current": cand.host == current,
                })

            # The simulated Drobo has no address, so there is nothing on the
            # network to find -- which left the device picker empty and the
            # dashboard unreachable in demo mode. Anyone trying this project
            # without hardware hit a dead end on the very first screen.
            #
            # Appended AFTER the real search rather than replacing it, so the
            # mock driver changes nothing about how discovery behaves. (The
            # first attempt at this short-circuited the whole method and broke
            # ten tests that use the mock driver to exercise the mDNS paths --
            # the mock is the test harness for real discovery, so it must not
            # bypass it.)
            #
            # Flagged `simulated` so a front-end can label it honestly rather
            # than letting anyone believe they are looking at a real array.
            if isinstance(driver, MockDriver) and not manual_host:
                dev = self.monitor.snapshot_dict().get("device", {})
                found.append({
                    "host": "simulated",
                    "port": 0,
                    "found_by": "mock driver",
                    "name": dev.get("name", "MockDrobo"),
                    "model": dev.get("model", "Drobo 5N"),
                    "firmware": dev.get("firmware", ""),
                    "esa_id": dev.get("esa_id", "") or "simulated-device",
                    "is_current": True,
                    "simulated": True,
                })

            payload = {"devices": found, "searched_for_seconds": self.DISCOVER_TIMEOUT}

            if manual_host:
                # Say plainly whether the typed address worked. Without this, a
                # wrong address and a network with no Drobos produce the same
                # empty list, and the person who just typed one has no way to
                # tell "nothing there" from "I got a digit wrong".
                payload["manual_host"] = manual_host
                payload["manual_found"] = any(d["host"] == manual_host for d in found)
                if not payload["manual_found"]:
                    payload["manual_error"] = (
                        f"Nothing at {manual_host} answered as a Drobo. Check the "
                        f"address, that the Drobo is powered on, and that this PC is "
                        f"on the same network as it."
                    )
                # NOT cached: this answer is about one address somebody asked
                # after, and letting it become the cached reply would serve it
                # to the next caller who asked a different question.
                return self._send_json(payload)

            _Handler._discover_cache = (time.time(), payload)
        return self._send_json(payload)

    def _shares(self) -> None:
        """
        The shares, ready to open.

        Two device reads rather than one: the share list, and the network
        config for the port-speed check. The network read is best-effort --
        a missing link report must not cost you the share list, which is the
        part you actually came for.
        """
        # The simulator stands in for the DEVICE, not for this method. Only
        # the fetch is swapped below; the caching, summarising and link check
        # all still run exactly as they do against real hardware.
        #
        # Doing it the other way -- short-circuiting the whole method for the
        # mock -- is a mistake I have now made twice in this file. The mock
        # driver is what the tests use to exercise the real paths, so anything
        # that jumps over those paths quietly stops testing them.
        simulated = getattr(self.monitor.driver, "simulate_config", False)
        if simulated:
            host, esa, err = "10.0.0.5", "", None
        else:
            host, esa, err = self._command_target()
        if err:
            return self._send_json(err, 503)

        # One caller at a time, and everyone else gets the cached answer. Two
        # browser tabs refreshing must not become four connections to a 2013
        # embedded box.
        with _Handler._shares_lock:
            cached = _Handler._shares_cache
            if cached and time.time() - cached[0] < self.SHARES_TTL:
                return self._send_json(cached[1])

            try:
                raw = (self.monitor.driver.get_config("shares") if simulated
                       else config_cmd.get_config(host, esa, "shares",
                                                  timeout=self.CONFIG_TIMEOUT))
            except config_cmd.ConfigError as exc:
                return self._send_json({"error": str(exc)}, 502)
            except OSError as exc:
                return self._send_json(
                    {"error": f"could not reach the command port: {exc}"}, 502)

            listed = shares_mod.summarize(raw, host)
            link = {}
            try:
                link = shares_mod.link_health(
                    self.monitor.driver.get_config("network") if simulated
                    else config_cmd.get_config(host, esa, "network",
                                               timeout=self.CONFIG_TIMEOUT))
            except (config_cmd.ConfigError, OSError):
                pass

            # Read from the SAME config document the shares came from, not a
            # second fetch: the command port dislikes connection volume, and
            # two reads could disagree with each other if a write landed in
            # between. Returns None when the list is present but unreadable,
            # which access_advice treats as "don't know" rather than "none".
            accounts = shares_mod.user_accounts(raw)

            payload = {
                "host": host,
                "shares": listed,
                "accounts": accounts,
                "advice": shares_mod.access_advice(listed, accounts),
                "link": link,
            }
            _Handler._shares_cache = (time.time(), payload)
        return self._send_json(payload)

    def _receive_photo(self) -> None:
        filename = self.headers.get("X-Filename", "")
        if not filename:
            return self._send_json({"error": "X-Filename header is required"}, 400)
        taken_at = None
        raw_taken = self.headers.get("X-Taken-At", "")
        if raw_taken:
            try:
                taken_at = float(raw_taken)  # unix seconds
            except ValueError:
                return self._send_json({"error": "X-Taken-At must be unix seconds"}, 400)
        try:
            data = self._read_body(self.photos.max_bytes + 1024)
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 413)
        try:
            return self._send_json(self.photos.ingest(filename, data, taken_at))
        except PhotoStoreError as exc:
            return self._send_json({"error": str(exc)}, 400)

    def _mock_control(self, path: str) -> None:
        driver = self.monitor.driver
        if not isinstance(driver, MockDriver):
            return self._send_json({"error": "mock controls need the mock driver"}, 409)
        parts = path.split("/")[3:]  # after /api/mock/
        action = parts[0] if parts else ""
        bay = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if action == "reset":
            driver.reset()
        elif action in ("fail", "warn", "pull") and bay:
            {"fail": driver.fail_bay, "warn": driver.warn_bay, "pull": driver.pull_bay}[action](bay)
        elif action == "battery" and len(parts) > 1 and parts[1] in ("warning", "failed"):
            driver.fail_battery(parts[1])
        elif action == "power" and len(parts) > 1 and parts[1] in ("warning", "failed"):
            driver.fail_power(parts[1])
        else:
            return self._send_json(
                {"error": "use /api/mock/fail/<bay>, warn/<bay>, pull/<bay>, "
                          "battery/<warning|failed>, power/<warning|failed> or /api/mock/reset"},
                400,
            )
        snap = self.monitor.poll_once()  # apply immediately so alerts fire now
        return self._send_json({"ok": True, "action": action, "bay": bay,
                                "state": snap.overall_state})

    def _stream_events(self) -> None:
        q = self.monitor.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # Send the current state immediately so a new client isn't blank.
            self._sse({"type": "snapshot", "data": self.monitor.snapshot_dict()})
            while True:
                try:
                    self._sse(q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")  # comment frame
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.monitor.unsubscribe(q)

    def _sse(self, payload: dict) -> None:
        body = json.dumps(payload)
        self.wfile.write(f"event: {payload.get('type','message')}\ndata: {body}\n\n".encode())
        self.wfile.flush()


def serve(cfg: dict, monitor, photos):
    _Handler.monitor = monitor
    _Handler.photos = photos
    _Handler.token = cfg["agent"]["token"]
    bind = cfg["agent"]["bind"]
    port = int(cfg["agent"]["port"])
    httpd = ThreadingHTTPServer((bind, port), _Handler)
    httpd.daemon_threads = True
    return httpd
