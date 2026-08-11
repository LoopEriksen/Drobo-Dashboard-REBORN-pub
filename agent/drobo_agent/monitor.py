"""
The watcher.

Polls the Drobo on a timer, keeps a rolling history, works out which alerts are
active, and pushes notifications when something changes. This is the piece that
makes the agent worth running at all -- it's awake when you aren't.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

from drobo_nasd import firmware, netcheck

from . import drivers as drivers_mod
from .models import (
    SEV_CRITICAL,
    SEV_INFO,
    SEV_WARNING,
    STATE_FAILED,
    STATE_WARNING,
    Alert,
    Snapshot,
)


# Health-telemetry baseline: observed on ONE healthy Drobo 5N, firmware 4.3.1, 2026-07-24.
# NOT a specification -- we don't know what these codes mean, only what a
# working unit reports. A deviation is flagged as "changed, worth a look",
# never as an assertion about what the new value means. Keyed by the
# PackHealth attribute name so _evaluate can walk it generically.
_PACK_STATUS_BASELINE = {
    "disk_pack_status": 0,
    "dnas_status": 6,
    "device_status": 98304,
    "status_ex": 0,
}


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return f"{n:.1f} PB"


class Monitor:
    #: How long a network diagnosis stays good for. Reading this machine's own
    #: addresses means running `ipconfig`, and the poll loop can tick every
    #: couple of seconds -- spawning a process that often to re-learn something
    #: that changes when you join a different Wi-Fi network would be silly.
    #: Short enough that reconnecting clears the warning within half a minute.
    NETWORK_HINT_TTL = 30.0

    def __init__(self, cfg: dict, notifier):
        self.cfg = cfg
        self.notifier = notifier
        self.driver = drivers_mod.build(cfg["drobo"])

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.latest: Snapshot | None = None
        self.history: deque[Snapshot] = deque(maxlen=int(cfg["agent"]["history_length"]))
        self.alerts: dict[str, Alert] = {}
        self._last_sent: dict[str, float] = {}
        self._subscribers: list[queue.Queue] = []
        self.started_at = time.time()

        # Why the Drobo can't be reached, when the answer is "this PC isn't on
        # its network". None whenever the Drobo is reachable, or whenever we
        # can't say. See _network_hint.
        self.network_hint: dict | None = None
        self._hint_cache: tuple[str, float, dict | None] | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        try:
            self.driver.connect()
        except drivers_mod.ProtocolNotMappedError as exc:
            # Fail loudly and clearly rather than limping along. This one is a
            # programming/config error, not a network condition -- retrying it
            # would never help.
            raise SystemExit(f"\n{exc}\n") from exc
        except OSError as exc:
            # The Drobo is not answering right now. That is NOT a reason to
            # refuse to start.
            #
            # This used to crash the agent with a raw traceback, which meant
            # the dashboard would not open at all whenever the Drobo was
            # asleep, rebooting, or -- as actually happened -- the PC had
            # joined a different Wi-Fi network. A monitoring tool that only
            # runs while the thing it monitors is healthy has it exactly
            # backwards: "I can't reach your Drobo" is the single most
            # important thing it might have to tell you, and it cannot say it
            # from a traceback.
            #
            # So: start anyway. The poll loop retries on its own schedule, the
            # snapshot reports unreachable, and the unreachable alert fires
            # through the normal path. If the Drobo comes back, the next poll
            # picks it up with no restart.
            print(f"[monitor] cannot reach the Drobo yet ({exc}). Starting "
                  f"anyway and will keep trying every "
                  f"{self.cfg['agent']['poll_seconds']}s.", flush=True)
        self._thread = threading.Thread(target=self._run, name="drobo-monitor", daemon=True)
        self._thread.start()
        print(f"[monitor] polling every {self.cfg['agent']['poll_seconds']}s "
              f"via the {self.driver.name!r} driver", flush=True)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.driver.close()

    def _run(self) -> None:
        interval = float(self.cfg["agent"]["poll_seconds"])
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(interval)

    # -- polling -----------------------------------------------------------

    def poll_once(self) -> Snapshot:
        try:
            snap = self.driver.read_snapshot()
        except Exception as exc:  # noqa: BLE001 - any driver failure means "unreachable"
            snap = Snapshot(reachable=False, source=self.driver.name, error=str(exc))

        # Worked out BEFORE the lock is taken: this can shell out to `ipconfig`,
        # and no /api/status reader should ever be made to wait behind a
        # subprocess. Only asked for when the Drobo didn't answer -- when it
        # did, the network layout is not the story and nothing needs explaining.
        hint = self._network_hint() if not snap.reachable else None

        with self._lock:
            self.latest = snap
            self.network_hint = hint
            self.history.append(snap)
            fired = self._evaluate(snap, hint)
            # Built here, from the same helper /api/status uses, rather than
            # from snap.to_dict() directly. The web dashboard is driven by this
            # stream and not by polling /api/status, so a field added to one and
            # not the other reaches half the front-ends and no test -- which is
            # exactly how the network banner came to be missing from the very UI
            # it was written for while the REST endpoint served it correctly.
            payload = self._snapshot_payload()

        self._broadcast({"type": "snapshot", "data": payload})
        for alert in fired:
            self._dispatch(alert)
        return snap

    # -- why we can't reach it ---------------------------------------------

    def _network_hint(self) -> dict | None:
        """
        The most common reason a Drobo "disappears", checked before we let the
        dashboard imply the hardware is at fault.

        Three separate times while this project was being built, "Cannot reach
        the Drobo: timed out" meant nothing more than that Windows had
        auto-joined a different Wi-Fi network -- a phone hotspot, a neighbour's
        guest SSID -- and the array was healthy throughout. That message sends
        someone to check on their NAS when the fix is on the machine reading
        this sentence, so when it's true the message should say so.

        Returns None whenever we can't say honestly: no known address for the
        Drobo, no readable addresses for this PC, or a Drobo that IS on our
        network and simply isn't answering (a real outage, which this must not
        talk over). See drobo_nasd.netcheck for what the check can and cannot
        prove -- it is an explanation for a failure that already happened, never
        a prediction, and never a reason not to keep trying.

        Cached per target for NETWORK_HINT_TTL so a fast poll loop doesn't run
        `ipconfig` every tick.
        """
        target = self.driver.target_host()
        if not target:
            return None

        now = time.time()
        cached = self._hint_cache
        if cached and cached[0] == target and now - cached[1] < self.NETWORK_HINT_TTL:
            return cached[2]

        try:
            result = netcheck.diagnose(target)
        except Exception:  # noqa: BLE001 - a diagnosis must never break a poll
            # Nothing in netcheck is supposed to raise, but this runs on every
            # failed poll on somebody's storage monitor. A bug in the
            # explanation must not cost them the alert it was explaining.
            result = None

        hint = result.to_dict() if result and not result.same_network else None
        self._hint_cache = (target, now, hint)
        return hint

    # -- alert rules -------------------------------------------------------

    def _evaluate(self, snap: Snapshot, network_hint: dict | None = None) -> list[Alert]:
        """Work out which conditions hold right now. Returns newly-fired alerts."""
        acfg = self.cfg["alerts"]
        seen: dict[str, tuple[str, str]] = {}  # key -> (severity, message)

        if not snap.reachable:
            error = " ".join((snap.error or "no response").split())
            if network_hint:
                # Kept as ONE alert rather than raised as a second: there is a
                # single problem here, and a critical "cannot reach" sitting
                # next to a separate note explaining it would read as two
                # unrelated events instead of a cause and its effect.
                #
                # The explanation goes BEFORE the driver's own error, and that
                # ordering is the point. Left the other way round, the useful
                # sentence lands after a wall of diagnostics -- and in this
                # exact case the diagnostics actively mislead: discovery's
                # failure message ends with "set host in config.json", which is
                # the wrong advice when the host is already correct and the PC
                # is simply on another network. The raw error is still here in
                # full, attributed and last, where a technical reader can find
                # it and nobody else has to wade through it.
                message = (f"Cannot reach the Drobo. {network_hint['message']} "
                           f"(What the agent tried: {error})")
            else:
                message = f"Cannot reach the Drobo: {error}"
            seen["unreachable"] = (SEV_CRITICAL, message)
        else:
            for drive in snap.drives:
                if not drive.present:
                    continue
                where = f"bay {drive.bay}"
                if drive.state == STATE_FAILED:
                    seen[f"drive-failed-{drive.bay}"] = (
                        SEV_CRITICAL,
                        f"Drive in {where} has failed ({drive.model or 'unknown model'}). "
                        "Replace it as soon as possible.",
                    )
                elif drive.state == STATE_WARNING:
                    seen[f"drive-warning-{drive.bay}"] = (
                        SEV_WARNING,
                        f"Drive in {where} reports a problem. {drive.note}".strip(),
                    )
                # mErrorCount is unambiguous -- above zero is a real problem,
                # regardless of the drive's overall state.
                if drive.error_count > 0:
                    seen[f"drive-errors-{drive.bay}"] = (
                        SEV_WARNING,
                        f"Drive in {where} has reported {drive.error_count} error(s) "
                        f"({drive.model or 'unknown model'}).",
                    )
                temp = drive.temperature_c
                if temp is not None:
                    if temp >= acfg["temperature_critical_c"]:
                        seen[f"temp-critical-{drive.bay}"] = (
                            SEV_CRITICAL,
                            f"Drive in {where} is {temp:.1f} C -- above the "
                            f"{acfg['temperature_critical_c']:.0f} C critical threshold.",
                        )
                    elif temp >= acfg["temperature_warning_c"]:
                        seen[f"temp-warning-{drive.bay}"] = (
                            SEV_WARNING,
                            f"Drive in {where} is running warm at {temp:.1f} C.",
                        )

            # Chassis temperature, from eCmdGetSysInfo on the command port.
            #
            # The per-drive loop above never fires on a Drobo 5N: the status
            # greeting reports mTemperature as 0 for every slot, so
            # drive.temperature_c is always None. Until this rule existed, the
            # dashboard displayed a real chassis temperature that absolutely
            # nothing was watching -- a Drobo could cook itself and no alert
            # would ever be raised.
            #
            # Same thresholds as the per-drive rule. They are a whole-box
            # reading rather than a per-disk one, so the wording says so; a
            # 5N in a cupboard on a warm day is the case this is for.
            chassis = snap.device.temperature_c
            if chassis is not None:
                if chassis >= acfg["temperature_critical_c"]:
                    seen["chassis-temp-critical"] = (
                        SEV_CRITICAL,
                        f"The Drobo is {chassis} C inside -- above the "
                        f"{acfg['temperature_critical_c']:.0f} C critical threshold. "
                        "Check it has room to breathe and that the fan is running.",
                    )
                elif chassis >= acfg["temperature_warning_c"]:
                    seen["chassis-temp-warning"] = (
                        SEV_WARNING,
                        f"The Drobo is running warm at {chassis} C inside. "
                        "Worth checking it has space around it and clear vents.",
                    )

            battery = snap.battery
            if battery.state == STATE_FAILED:
                seen["battery-failed"] = (
                    SEV_CRITICAL,
                    "Cache battery has failed. "
                    f"{battery.note or 'Replace it as soon as possible.'}".strip(),
                )
            elif battery.state == STATE_WARNING:
                seen["battery-warning"] = (
                    SEV_WARNING,
                    f"Cache battery is weak. {battery.note}".strip(),
                )

            ph = snap.pack_health

            frac = snap.capacity.used_fraction
            free = _human_bytes(snap.capacity.free_bytes)
            # Prefer the device's OWN capacity alert levels (mRedThreshold /
            # mYellowThreshold) when the greeting reports them -- we'd then be
            # alerting on exactly the same levels the original Dashboard
            # does. Fall back to our config defaults when absent (e.g. the
            # mock driver, which doesn't set pack_health thresholds).
            crit_fraction = ph.red_threshold_fraction
            if crit_fraction is None:
                crit_fraction = acfg["capacity_critical_fraction"]
            warn_fraction = ph.yellow_threshold_fraction
            if warn_fraction is None:
                warn_fraction = acfg["capacity_warning_fraction"]
            if frac >= crit_fraction:
                seen["capacity-critical"] = (
                    SEV_CRITICAL,
                    f"Only {free} free ({frac:.0%} used). The array is nearly full.",
                )
            elif frac >= warn_fraction:
                seen["capacity-warning"] = (
                    SEV_WARNING,
                    f"{frac:.0%} used, {free} free.",
                )

            # -- pack-level health the greeting already sends ----------
            if ph.double_degraded_count > 0:
                seen["double-degraded"] = (
                    SEV_CRITICAL,
                    f"Double-degraded count is {ph.double_degraded_count} -- two "
                    "drives were degraded at once; the array's protection may "
                    "have been exhausted.",
                )
            if ph.relayout_count > 0:
                seen["relayout"] = (
                    SEV_INFO,
                    f"Relayout count is {ph.relayout_count} -- rebuilding, this "
                    "is normal after a drive change.",
                )
            for attr, baseline in _PACK_STATUS_BASELINE.items():
                current = getattr(ph, attr)
                if current is not None and current != baseline:
                    seen[f"pack-status-{attr}"] = (
                        SEV_WARNING,
                        f"{attr} changed from the healthy baseline ({baseline}) "
                        f"to {current}. We don't know what this code means -- "
                        "just that it's different from before.",
                    )

        now = time.time()
        fired: list[Alert] = []

        for key, (severity, message) in seen.items():
            existing = self.alerts.get(key)
            if existing and existing.active:
                existing.last_seen = now
                existing.message = message
                # Re-send long-running problems occasionally so they aren't forgotten.
                if now - self._last_sent.get(key, 0) >= acfg["resend_seconds"]:
                    fired.append(existing)
            else:
                alert = Alert(key=key, severity=severity, message=message)
                self.alerts[key] = alert
                fired.append(alert)

        # Anything that was active but isn't in `seen` any more has cleared.
        for key, alert in self.alerts.items():
            if alert.active and key not in seen:
                alert.cleared_at = now
                self._last_sent.pop(key, None)
                cleared = Alert(
                    key=key + "-cleared",
                    severity=SEV_INFO,
                    message=f"Resolved: {alert.message}",
                )
                fired.append(cleared)

        return fired

    def _dispatch(self, alert: Alert) -> None:
        self._last_sent[alert.key] = time.time()
        title = {
            SEV_CRITICAL: "Drobo: action needed",
            SEV_WARNING: "Drobo: warning",
        }.get(alert.severity, "Drobo")
        self.notifier.send(title, alert.message, alert.severity)
        self._broadcast({"type": "alert", "data": alert.to_dict()})

    # -- live event stream (used by /api/events) ---------------------------

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, payload: dict) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # a slow client just misses updates; it can re-read /api/status

    # -- read helpers used by the API --------------------------------------

    def _snapshot_payload(self) -> dict:
        """
        The snapshot as every front-end receives it. Caller must hold the lock.

        The ONE place this shape is defined. Both routes out of the agent go
        through here -- /api/status below, and the SSE stream in poll_once --
        because they are the same document to a front-end, and a field present
        on one but not the other is a bug that only shows up in whichever UI
        happens to use the other channel.
        """
        if self.latest is None:
            # Carries network_hint too, even though it is always None here. A
            # front-end that checks for the key rather than its value -- a
            # reasonable thing to do -- must not see a differently-shaped
            # payload just because it asked during the first few seconds of the
            # agent's life.
            return {"reachable": False, "error": "no reading taken yet",
                    "network_hint": None, "firmware_advisory": None}
        # network_hint rides along on the snapshot rather than living inside
        # it: a Snapshot describes a Drobo, and where this PC happens to be
        # plugged in is not a fact about the Drobo. Non-null only when the
        # device is unreachable AND we can actually explain why -- so a
        # front-end can treat "it's there" as reason enough to render the
        # banner. to_dict() builds a fresh dict each call, so adding a key
        # here can't leak into the Snapshot.
        payload = self.latest.to_dict()
        payload["network_hint"] = self.network_hint
        # What the running firmware version means. Cheap (a regex over a string
        # we already hold) and derived rather than stored, so it can never go
        # stale against the device's own report.
        #
        # Deliberately NOT an alert. Alerts in this agent are sticky and re-send
        # on a timer, which is right for a failing drive and wrong for this: the
        # only "fix" is flashing firmware, which this software refuses to do and
        # actively advises against, so an alert would nag hourly about something
        # with no safe action behind it. It is a standing note about the device,
        # so it rides on the device's status and the UIs render it once, calmly.
        payload["firmware_advisory"] = firmware.assess(
            self.latest.device.firmware).to_dict()
        return payload

    def snapshot_dict(self) -> dict:
        with self._lock:
            return self._snapshot_payload()

    def active_alerts(self) -> list[dict]:
        with self._lock:
            return [a.to_dict() for a in self.alerts.values() if a.active]

    def all_alerts(self) -> list[dict]:
        with self._lock:
            return [a.to_dict() for a in self.alerts.values()]

    def history_dicts(self, limit: int = 720) -> list[dict]:
        """Trimmed history for graphing -- just the numbers, not whole snapshots."""
        with self._lock:
            items = list(self.history)[-limit:]
        out = []
        for snap in items:
            temps = [d.temperature_c for d in snap.drives if d.temperature_c is not None]
            out.append(
                {
                    "t": round(snap.taken_at, 1),
                    "reachable": snap.reachable,
                    "state": snap.overall_state,
                    "used_bytes": snap.capacity.used_bytes,
                    "free_bytes": snap.capacity.free_bytes,
                    "max_temp_c": max(temps) if temps else None,
                    "read_mb_per_s": snap.performance.read_mb_per_s,
                    "write_mb_per_s": snap.performance.write_mb_per_s,
                }
            )
        return out
