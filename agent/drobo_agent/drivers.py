"""
Drobo drivers.

This is the ONLY part of the whole project that knows anything about how a
Drobo talks. Everything above it -- the monitor, the API, the Windows app, the
iOS app -- deals in the plain shapes from models.py.

Two drivers exist today:

  MockDriver     A simulated Drobo 5N. Lets us build and test the entire stack
                 before the protocol is understood, and lets us trigger fake
                 drive failures on demand to prove the alert path works.

  Drobo5NDriver  The real one, and it works against real hardware, not a
                 stub: reads the live status greeting (esatm.py), discovers
                 the device by mDNS when its address changes (discovery.py),
                 and tops up chassis temperature/uptime from the command
                 channel (sysinfo.py), all confirmed against a live Drobo 5N.
                 See docs/protocol-map.md for what's been measured.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque

from .models import (
    SEV_INFO,
    SEV_WARNING,
    STATE_EMPTY,
    STATE_FAILED,
    STATE_OK,
    STATE_WARNING,
    BatteryInfo,
    Capacity,
    DeviceInfo,
    DriveSlot,
    EventLogEntry,
    FanInfo,
    PerformanceInfo,
    PowerInfo,
    Snapshot,
    Volume,
    _gb,
)
from drobo_nasd import config_cmd, discovery, esatm, sysinfo


class ProtocolNotMappedError(NotImplementedError):
    """Raised by the real driver until Phase 1 is finished."""


class DroboDriver:
    """Base class. A driver's whole job is to produce a Snapshot on demand."""

    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def connect(self) -> None:
        """Called once at startup. Raise if the device can't be reached."""

    def read_snapshot(self) -> Snapshot:
        raise NotImplementedError

    def target_host(self) -> str:
        """
        The address this driver is trying to reach, or "" if it hasn't got one.

        Exists so the monitor can explain a failure without reaching into a
        driver's private attributes. "" is a real answer meaning "no opinion":
        a simulated Drobo has no address, and a driver set to auto-discovery
        that has never once succeeded genuinely does not know where its device
        lives -- so nothing downstream may claim the PC is on the wrong network
        for it. See monitor._network_hint.
        """
        return ""

    def close(self) -> None:
        """Called at shutdown."""


# ---------------------------------------------------------------------------
# Mock driver
# ---------------------------------------------------------------------------


class MockDriver(DroboDriver):
    """
    A believable fake Drobo 5N: five bays, four populated, capacity that creeps
    up over time, and temperatures that drift the way real drives do.

    Fault injection lets us test alerts without touching real hardware:
        driver.fail_bay(3)      # simulate a drive failure
        driver.pull_bay(3)      # simulate someone yanking a drive
        driver.reset()          # back to healthy
    """

    name = "mock"

    _MODELS = ["WDC WD40EFRX-68N32N0", "ST4000VN008-2DR166", "TOSHIBA HDWQ140"]

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._lock = threading.Lock()
        self._started = time.time()
        self._overrides: dict[int, str] = {}
        self._bays: list[dict] = []
        rng = random.Random(5150)  # fixed seed so restarts look consistent
        for bay in range(1, 6):
            populated = bay <= 4
            self._bays.append(
                {
                    "bay": bay,
                    "present": populated,
                    "model": rng.choice(self._MODELS) if populated else "",
                    "serial": f"MOCK{rng.randrange(10**7, 10**8)}" if populated else "",
                    "capacity": _gb(4000) if populated else 0,
                    "temp_base": 34.0 + rng.uniform(-2.0, 4.0),
                }
            )
        self._used = _gb(6800)

        # -- Phase 1 scope additions (ROADMAP.md) -- believable baselines that
        # drift a little, same idea as the drive temperatures above.
        self._battery_override: str | None = None
        self._battery_base = 97.0
        self._fan_base = 2350
        self._power_override: str | None = None
        self._event_log: deque[EventLogEntry] = deque(maxlen=200)
        self._log(SEV_INFO, "Drobo started up (mock driver)")

        # Kept only so a config carrying manual_discovery_ips loads cleanly
        # against either driver. The simulator has no network to search, so
        # there is nothing here for an address to point AT -- the setting is
        # read and acted on by Drobo5NDriver, which is where discovery lives.
        self.manual_discovery_ips: list[str] = list(cfg.get("manual_discovery_ips", []))

        # Whether this mock also stands in for the COMMAND port (shares,
        # network and admin config), not just the status greeting.
        #
        # Off by default, and that default matters: test_agent.py drives a
        # MockDriver as a stand-in for real hardware while patching
        # config_cmd.get_config to fake device reads. If the mock answered
        # config queries by itself, those tests would silently stop exercising
        # the path they exist to cover. So it is opt-in, set in
        # config.mock.json, and only the demo simulator turns it on.
        self.simulate_config = bool(cfg.get("simulate_config", False))

        # How many times Identify has been asked for. Exists so a test can
        # prove the command actually reached the driver rather than being
        # swallowed somewhere in the API layer.
        self._identify_count = 0
        self._identify_last_interval: int | None = None

    # -- simulated configuration ------------------------------------------

    #: A believable share layout, in the EXACT shape config_cmd.get_config
    #: returns for a real device. Deliberately so: /api/shares then runs the
    #: real shares.summarize() and link_health() over it, which means demo
    #: mode exercises the production parsing path rather than a parallel
    #: pretend one. If that parser breaks, the simulator notices.
    #:
    #: Chosen to be worth testing against rather than tidy:
    #:   - four shares, not one, so the list has to lay out properly
    #:   - a mix of access codes, including one nobody can reach
    #:   - a Time Machine target, which renders differently
    #:   - a share whose name has a space in it, because UNC paths and
    #:     `net use` both care and it is the classic thing to get wrong
    _MOCK_SHARES = {
        "DRINASConfig": {
            "DRIShareConfig": {
                "ConfigVersionBasis": "14",
                # A real 5N always sends this element, empty when the device
                # has no named accounts -- which is the case on the only unit
                # this project can read. It was missing here, so the simulator
                # produced "accounts unknown" where hardware produces
                # "definitely none", and the two took different branches of
                # shares.access_advice. A simulator that omits a field the
                # device always sends will hide exactly the behaviour that
                # field controls.
                "UserList": None,
                "Shares": {
                    "Share": [
                        {"ShareName": "Documents", "ShareState": "0",
                         "TimeMachineEnabled": "0",
                         "ShareUsers": {"ShareUser": {
                             "ShareUsername": "Everyone", "ShareUserAccess": "1"}}},
                        {"ShareName": "Photo Library", "ShareState": "0",
                         "TimeMachineEnabled": "0",
                         "ShareUsers": {"ShareUser": [
                             {"ShareUsername": "Everyone", "ShareUserAccess": "1"},
                             {"ShareUsername": "guest", "ShareUserAccess": "0"}]}},
                        {"ShareName": "TimeMachine", "ShareState": "0",
                         "TimeMachineEnabled": "1", "ShareMaxTMSizeGB": "1000.000000",
                         "ShareUsers": {"ShareUser": {
                             "ShareUsername": "Everyone", "ShareUserAccess": "1"}}},
                        {"ShareName": "Private", "ShareState": "0",
                         "TimeMachineEnabled": "0",
                         "ShareUsers": {"ShareUser": {
                             "ShareUsername": "Everyone", "ShareUserAccess": "0"}}},
                    ]
                },
            }
        }
    }

    #: Network config, again in the real shape. PortSpeed is 100 on purpose --
    #: a gigabit-capable box negotiating 100 Mbit is a real fault this project
    #: detects and warns about, and demo mode should show that warning rather
    #: than a perfect machine that exercises none of the interesting code.
    _MOCK_NETWORK = {
        "DRINASConfig": {
            "DRINasNetworkConfig": {
                "Version": "11",
                "NasName": "MockDrobo",
                "NasWorkgroup": "WORKGROUP",
                "IPConfig": {"IPConfigType": "0", "IP": "10.0.0.5",
                             "Subnet": "255.255.255.0", "Gateway": "10.0.0.1",
                             "DNS1": "10.0.0.1", "DNS2": "9.9.9.9"},
                "JumboFramesConfig": {"Enabled": "0", "MTUSize": "1500"},
                "MACAddress": "02:00:00:00:00:01",
                "PortSpeed": "100",
                "PortDuplex": "full",
            }
        }
    }

    #: Admin config. The password fields are present because a real device
    #: sends them -- they exist here so the redaction path is exercised in
    #: demo mode too, and so a leak would show up while simulating rather
    #: than only against real hardware.
    _MOCK_ADMIN = {
        "DRINASConfig": {
            "DRINasAdminConfig": {
                "UserName": "admin",
                "Password": "****************",
                "ValidPassword": "1",
                "EncryptedPassword": "*",
            }
        }
    }

    #: What the simulator claims is installed. Shaped like the real reply, and
    #: a plausible app list for a 5N -- including
    #: DroboPix, which is the service Phase 4 sets out to replace. One app is
    #: stopped on purpose so the UI has both states to render.
    _MOCK_DROBOAPPS = {
        "sdk_version": "2.1",
        "apps": [
            {"name": "DroboPix", "version": "1.0.3.80",
             "description": "Uploads photos and videos from a phone to the Drobo.",
             "running": False, "status": "", "has_web_ui": True},
            {"name": "Dropbear", "version": "2019.78",
             "description": "SSH server.",
             "running": True, "status": "Listening on port 22.", "has_web_ui": False},
            {"name": "myDrobo", "version": "1.1.1.121",
             "description": "Remote access to services on your Drobo.",
             "running": True, "status": "", "has_web_ui": True},
            {"name": "python3", "version": "3.9.7.2",
             "description": "Python 3.9.7",
             "running": True, "status": "Python 3 is configured.", "has_web_ui": False},
        ],
    }

    #: LED brightness, as the device reports it. The real 5N returned 59.
    _MOCK_DIMMING = 59

    #: What DroboSync looks like on a device with no partner -- which is what
    #: the real 5N reports, and what almost everyone running this will see.
    #: The simulator does NOT invent a configured sync: nobody on this project
    #: has ever observed a populated reply, so a fake one would be teaching the
    #: UI a shape we made up. Showing the honest empty case is the useful thing.
    _MOCK_DROBOSYNC = {
        "configured": False,
        "reads": {
            "settings": {"configured": False, "raw_xml": "", "fields": {}},
            "summary_log": {"configured": False, "raw_xml": "", "fields": {}},
            "detailed_log": {"configured": False, "raw_xml": "", "fields": {}},
            "log": {"configured": False, "raw_xml": "", "fields": {}},
        },
        "note": (
            "This Drobo has no DroboSync partner set up. DroboSync copied one "
            "Drobo's shares to a second Drobo on a schedule, so it needs two "
            "units -- with one, there is nothing for it to do and the device "
            "correctly reports nothing. Everything needed to display it is "
            "built and waiting."
        ),
    }

    def get_droboapps(self) -> dict:
        return self._MOCK_DROBOAPPS

    def get_drobosync(self) -> dict:
        return self._MOCK_DROBOSYNC

    def get_dimming(self) -> int:
        return self._MOCK_DIMMING

    def get_demo_mode(self) -> dict:
        """Not in demo mode -- so the simulator's capacity numbers are
        "real" in the sense that matters: they are what this fake device
        genuinely holds, not shop-display fiction on top of a fake device."""
        return {"demo_mode": False, "scale_factor": 1}

    def identify(self, interval: int | None = None) -> None:
        """
        Pretend to blink the lights, and record it in the event log.

        The simulator cannot flash anything, but it can prove the whole path
        works -- button, endpoint, guard, result handling -- without touching
        real hardware. That is how the first write in this project's history
        was actually built and tested.

        It records the INTERVAL as well as the count, because since 2026-08-06
        there is a value to carry and "did the interval survive the trip from
        the query string to the driver" is a question the simulator can answer
        and hardware cannot answer cheaply. `interval` defaults to None so
        older callers that pass nothing still work.
        """
        with self._lock:
            self._identify_count += 1
            self._identify_last_interval = interval
            self._log(SEV_INFO,
                      f"Identify: front lights flashing, interval={interval} (mock driver)")

    def get_config(self, section: str) -> dict:
        """Stand in for config_cmd.get_config against a simulated device."""
        try:
            return {
                "shares": self._MOCK_SHARES,
                "network": self._MOCK_NETWORK,
                "admin": self._MOCK_ADMIN,
            }[section]
        except KeyError:
            raise ValueError(f"unknown section {section!r}") from None

    def _log(self, severity: str, message: str) -> None:
        self._event_log.append(EventLogEntry(ts=time.time(), severity=severity, message=message))

    # -- fault injection ---------------------------------------------------

    def fail_bay(self, bay: int) -> None:
        with self._lock:
            self._overrides[bay] = STATE_FAILED

    def warn_bay(self, bay: int) -> None:
        with self._lock:
            self._overrides[bay] = STATE_WARNING

    def pull_bay(self, bay: int) -> None:
        with self._lock:
            self._overrides[bay] = "pulled"

    def fail_battery(self, state: str = STATE_FAILED) -> None:
        """Simulate cache-battery trouble. state is STATE_WARNING or STATE_FAILED."""
        with self._lock:
            self._battery_override = state
            self._log(
                SEV_WARNING if state == STATE_WARNING else SEV_INFO,
                f"Cache battery reports {state} (mock driver)",
            )

    def fail_power(self, state: str = STATE_FAILED) -> None:
        with self._lock:
            self._power_override = state
            self._log(SEV_WARNING, f"PSU reports {state} (mock driver)")

    def reset(self) -> None:
        with self._lock:
            self._overrides.clear()
            self._battery_override = None
            self._power_override = None

    # -- driver interface --------------------------------------------------

    def read_snapshot(self) -> Snapshot:
        with self._lock:
            elapsed = time.time() - self._started
            # Creep usage upward slowly so the capacity graph has something to
            # draw, and wobble temperatures on a slow sine plus noise.
            self._used += int(_gb(0.002))
            drives: list[DriveSlot] = []
            raw = 0
            for spec in self._bays:
                bay = spec["bay"]
                override = self._overrides.get(bay)
                if override == "pulled" or not spec["present"]:
                    drives.append(DriveSlot(bay=bay, present=False, state=STATE_EMPTY))
                    continue
                raw += spec["capacity"]
                temp = (
                    spec["temp_base"]
                    + 3.0 * math.sin(elapsed / 240.0 + bay)
                    + random.uniform(-0.4, 0.4)
                )
                state = override or STATE_OK
                note = ""
                if state == STATE_FAILED:
                    note = "simulated failure (mock driver)"
                    temp = None
                elif state == STATE_WARNING:
                    note = "simulated SMART warning (mock driver)"
                drives.append(
                    DriveSlot(
                        bay=bay,
                        present=True,
                        state=state,
                        model=spec["model"],
                        serial=spec["serial"],
                        capacity_bytes=spec["capacity"],
                        temperature_c=round(temp, 1) if temp is not None else None,
                        note=note,
                    )
                )

            healthy_raw = sum(
                d.capacity_bytes for d in drives if d.present and d.state != STATE_FAILED
            )
            # Single-disk redundancy: usable is roughly raw minus the largest drive.
            largest = max((d.capacity_bytes for d in drives if d.present), default=0)
            usable = max(healthy_raw - largest, 0)
            used = min(self._used, usable)
            capacity = Capacity(
                raw_bytes=raw,
                usable_bytes=usable,
                used_bytes=used,
                free_bytes=max(usable - used, 0),
                protection_bytes=largest,
            )

            battery = self._read_battery(elapsed)
            fan = self._read_fan(elapsed)
            power = self._read_power()
            performance = self._read_performance(elapsed)

            return Snapshot(
                reachable=True,
                source=self.name,
                device=DeviceInfo(
                    name="MockDrobo",
                    model="Drobo 5N",
                    serial="MOCK-5N-0001",
                    firmware="4.2.2",
                    ip="127.0.0.1",
                    uptime_seconds=int(elapsed),
                ),
                capacity=capacity,
                drives=drives,
                # Two volumes, where the real 5N reports one. Deliberate: a
                # single-volume pack is the case that hides layout bugs, and
                # the whole point of the simulator is to show the states real
                # hardware won't. Field values mirror what the device actually
                # sends, including the undocumented partition_type/format.
                volumes=[
                    Volume(lun=0, unique_id="1000", name="",
                           max_size_bytes=70368744177664, used_bytes=used // 2,
                           partition_count=2, partition_type=3,
                           partition_format=64, share_state=0),
                    Volume(lun=1, unique_id="1001", name="Media",
                           max_size_bytes=70368744177664, used_bytes=used // 2,
                           partition_count=1, partition_type=3,
                           partition_format=64, share_state=0),
                ],
                max_volumes=16,
                battery=battery,
                fan=fan,
                power=power,
                performance=performance,
                event_log=list(self._event_log),
            )

    # -- Phase 1 scope additions --------------------------------------------

    def _read_battery(self, elapsed: float) -> BatteryInfo:
        """eCmdCacheBattery. Charge drifts near-full with a slow wobble; a
        failure override drains it and marks the state, same idea as a real
        battery that's stopped holding charge."""
        if self._battery_override == STATE_FAILED:
            return BatteryInfo(
                state=STATE_FAILED, charge_pct=0.0,
                note="cache battery failed (mock driver) -- write cache is not safe across a power loss",
            )
        if self._battery_override == STATE_WARNING:
            charge = max(0.0, 35.0 + 2.0 * math.sin(elapsed / 300.0))
            return BatteryInfo(
                state=STATE_WARNING, charge_pct=round(charge, 1),
                note="cache battery is weak (mock driver) -- consider a replacement",
            )
        charge = self._battery_base + 1.5 * math.sin(elapsed / 600.0) + random.uniform(-0.3, 0.3)
        charge = max(0.0, min(100.0, charge))
        return BatteryInfo(state=STATE_OK, charge_pct=round(charge, 1), note="")

    def _read_fan(self, elapsed: float) -> FanInfo:
        """eCmdGetFanInfo. RPM drifts a little; stalls if the fan bay was
        also given a state via the drive overrides (not modelled -- fans
        just idle around their baseline for now)."""
        rpm = self._fan_base + 60.0 * math.sin(elapsed / 180.0) + random.uniform(-15.0, 15.0)
        rpm = max(0, int(rpm))
        state = STATE_OK if rpm > 800 else STATE_WARNING
        return FanInfo(rpm=rpm, state=state)

    def _read_power(self) -> PowerInfo:
        """eCmdGetPowerInfo."""
        if self._power_override:
            note = "simulated PSU problem (mock driver)"
            return PowerInfo(state=self._power_override, note=note)
        return PowerInfo(state=STATE_OK, note="")

    def _read_performance(self, elapsed: float) -> PerformanceInfo:
        """
        eCmdGetPerformance. A gentle, bounded random walk so the graph has
        something to show without being pure noise.

        Scaled to MEGABYTES per second, matching what the real device reports
        (it sends single- and double-digit integers, not byte counts -- see
        nasd/sysinfo.parse_performance). This used to simulate bytes/second,
        which made the mock produce numbers eight orders of magnitude away from
        anything real hardware ever sends -- exactly the kind of fixture that
        lets a unit bug survive a green test suite.
        """
        base_read = 45.0 + 20.0 * math.sin(elapsed / 90.0)
        base_write = 18.0 + 8.0 * math.sin(elapsed / 130.0 + 1.0)
        read_mb = max(0, int(base_read + random.uniform(-3.0, 3.0)))
        write_mb = max(0, int(base_write + random.uniform(-1.5, 1.5)))
        return PerformanceInfo(
            read_mb_per_s=read_mb,
            write_mb_per_s=write_mb,
            iops=max(0, int(read_mb * 4 + write_mb * 2 + random.uniform(-5, 5))),
            measured=True,
        )


# ---------------------------------------------------------------------------
# Real driver -- Drobo 5N
# ---------------------------------------------------------------------------


class Drobo5NDriver(DroboDriver):
    """
    The real thing -- confirmed working against a live Drobo 5N on 2026-07-24.

    On connect, nasd (TCP 5000) pushes a complete <ESATMUpdate> status document
    with no login required, so a poll is just: reconnect, read the greeting,
    parse it. All the protocol detail lives in nasd/esatm.py.

    config, static IP (works exactly as before):
        "drobo": {"driver": "drobo5n", "host": "10.0.0.50", "port": 5000}

    config, no static IP needed -- "host" absent or "auto" runs discovery
    (nasd/discovery.py: mDNS, then optional "hostname", then a last-known IP
    remembered in memory, then an opt-in subnet sweep):
        "drobo": {
            "driver": "drobo5n",
            "host": "auto",
            "hostname": "MyDrobo.local",
            "expected_esa_id": "drbXXXXXXXXXXXX",
            "discovery_sweep": false
        }

    (The values above are placeholders. Your own device's serial is printed in
    the agent log on first successful connect -- paste that in. Real serials are
    deliberately kept out of the source tree; see ROADMAP.md Phase 6.)

    expected_esa_id pins discovery to one physical device (mESAID is the
    permanent hardware serial, so it survives IP changes and even a repack).
    Leave it blank for first-run pairing: discovery still connects, but logs
    the discovered esa_id prominently so it can be pasted into config.json.
    Once set, a candidate that answers with a DIFFERENT esa_id is refused,
    never silently trusted -- see discovery.IdentityMismatch.

    Read-only, but no longer silent: as of 2026-07-26 it also asks the device
    for its chassis temperature (eCmdGetSysInfo, a query) at most once every
    two minutes. See _read_temperature() for why it is that infrequent.

    Not available from the greeting (all report 0 or aren't present): per-drive
    temperature, cache battery, fan, PSU, performance. Per-drive temperature
    appears not to exist on a 5N; the chassis reading above is the real one.
    Fan and PSU were probed directly on the command port and never answered
    (docs/command-surface.md). Those health-extra fields stay at their
    STATE_UNKNOWN defaults.
    """

    name = "drobo5n"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.host = (cfg.get("host") or "").strip()
        self.port = int(cfg.get("port", esatm.DEFAULT_PORT))
        self.timeout = float(cfg.get("timeout", 8.0))
        self.hostname = (cfg.get("hostname") or "").strip()
        self.expected_esa_id = (cfg.get("expected_esa_id") or "").strip()
        self.try_sweep = bool(cfg.get("discovery_sweep", False))

        # Addresses somebody typed because mDNS doesn't work on their network.
        # Tried before the multicast browse -- see discovery._gather_candidates
        # for why that order, and why it changes nothing about identity
        # verification. This used to exist on the MOCK driver as a config-shaped
        # placeholder that nothing read; it is now the real thing, on the real
        # driver, where it can actually reach a device.
        self.manual_discovery_ips: list[str] = [
            str(ip).strip() for ip in (cfg.get("manual_discovery_ips") or []) if str(ip).strip()
        ]

        # In-memory only. _resolved_host/_port is whatever address actually
        # worked last, used both to report snap.device.ip and as the
        # "last-known-good IP" discovery tries first on a re-resolve. If
        # expected_esa_id was left blank, _known_esa_id still gets filled in
        # after the first successful contact, so a later re-discovery (after
        # a failure) keeps confirming identity against the SAME device rather
        # than being willing to accept literally anything that answers.
        self._resolved_host: str = ""
        self._resolved_port: int = self.port
        self._known_esa_id: str = self.expected_esa_id

        # Temperature comes from a command, not the status greeting, so it
        # costs a second connection to the device. See _read_temperature().
        self.sysinfo_seconds = float(cfg.get("sysinfo_seconds", 120.0))
        self._temp_c: int | None = None
        self._temp_at: float | None = None
        self._temp_tried_at: float = 0.0
        self._uptime: int | None = None
        # Throughput/IOPS, fetched on the same connection as the temperature.
        # None until the device has answered once -- distinct from "all zeros",
        # which is what a real but idle array reports.
        self._perf: dict | None = None
        self._perf_at: float | None = None

    @property
    def _auto(self) -> bool:
        return not self.host or self.host.lower() == "auto"

    def target_host(self) -> str:
        """
        Whatever address we'd try next: the one that last worked, else the
        configured static IP.

        In auto mode with nothing resolved yet this is "" -- not a guess. A
        Drobo we have never reached is a Drobo whose network we do not know,
        and "your PC is on the wrong network for it" would be an invention.
        """
        if self._resolved_host:
            return self._resolved_host
        return "" if self._auto else self.host

    def _remember(self, esa_id: str, host: str, port: int) -> None:
        self._resolved_host = host
        self._resolved_port = port
        if not self._known_esa_id:
            self._known_esa_id = esa_id

    def _discover(self) -> discovery.Verified:
        last_known = self._resolved_host or (self.host if not self._auto else "")
        try:
            verified = discovery.resolve(
                expected_esa_id=self._known_esa_id,
                hostname=self.hostname,
                last_known_ip=last_known,
                port=self.port,
                try_sweep=self.try_sweep,
                timeout=self.timeout,
                manual_ips=self.manual_discovery_ips,
            )
        except discovery.IdentityMismatch:
            # A device answered, and it is provably not ours. Let this through
            # untouched: discovery.py already produced a precise message naming
            # both serials, and reframing it as "couldn't find anything" would
            # be actively misleading -- we DID find something and refused it.
            # Caught explicitly (rather than relying on the exception hierarchy)
            # so the safety property is visible in the code.
            raise
        except LookupError as exc:
            raise ProtocolNotMappedError(
                "Could not find the Drobo automatically (tried mDNS"
                + (f", {len(self.manual_discovery_ips)} manually-added address(es)"
                   if self.manual_discovery_ips else "")
                + (f", hostname {self.hostname!r}" if self.hostname else "")
                + (f", last-known IP {last_known!r}" if last_known else "")
                + (", subnet sweep" if self.try_sweep else "")
                + f"). {exc}\n"
                'Set "drobo": {"driver": "drobo5n", "host": "<drobo-ip>"} in '
                "config.json, or find the IP with:  py tools/drobo_probe.py mdns"
            ) from exc
        self._remember(verified.identity["esa_id"], verified.candidate.host, verified.candidate.port)
        if verified.first_run:
            print(
                "[drobo5n] first-run pairing: no expected_esa_id configured yet. "
                f"This device identifies as esa_id={verified.identity['esa_id']!r} "
                f"({verified.identity.get('name', '?')!r}, "
                f"{verified.identity.get('model', '?')!r}, "
                f"firmware {verified.identity.get('firmware', '?')!r}). "
                f'Add  "expected_esa_id": "{verified.identity["esa_id"]}"  to the '
                '"drobo" block in config.json so a future stranger on this IP can '
                "never be silently trusted.",
                flush=True,
            )
        return verified

    def connect(self) -> None:
        if self._auto:
            verified = self._discover()
            print(
                f"[drobo5n] discovered {verified.candidate.host}:{verified.candidate.port} "
                f"via {verified.candidate.source} -- "
                f"{verified.identity.get('name', '?')} "
                f"(esa_id={verified.identity['esa_id']})",
                flush=True,
            )
            return

        # Explicit IP: identical to the original behaviour by default. If
        # expected_esa_id IS set, also refuse a mismatch here -- an admin who
        # hardcoded an IP still deserves protection against DHCP quietly
        # handing that address to a different device later.
        snap = esatm.read_snapshot(self.host, self.port, self.timeout)
        if self.expected_esa_id and snap.device.serial != self.expected_esa_id:
            raise discovery.IdentityMismatch(
                f"refusing {self.host}:{self.port} -- it identifies as "
                f"esa_id={snap.device.serial!r}, not the expected "
                f"{self.expected_esa_id!r}. This is not your Drobo; not connecting."
            )
        self._remember(snap.device.serial, self.host, self.port)
        print(f"[drobo5n] connected to {snap.device.name or self.host} "
              f"(firmware {snap.device.firmware})", flush=True)

    def read_snapshot(self) -> Snapshot:
        host, port = self._resolved_host, self._resolved_port
        if not host:
            # connect() was never called (or auto mode hasn't resolved yet):
            # discover now rather than failing with "no host configured".
            verified = self._discover()
            host, port = verified.candidate.host, verified.candidate.port

        try:
            snap = esatm.read_snapshot(host, port, self.timeout)
        except (OSError, esatm.FrameError):
            # The address that used to work doesn't anymore -- almost always
            # a DHCP lease change. Re-run discovery instead of dying, so a
            # Drobo that moved to a new IP self-heals without a restart, then
            # retry once against whatever discovery finds. If discovery also
            # comes up empty, that exception (already framed clearly) is what
            # the caller sees -- this never masks a real outage as success.
            verified = self._discover()
            host, port = verified.candidate.host, verified.candidate.port
            snap = esatm.read_snapshot(host, port, self.timeout)

        self._remember(snap.device.serial, host, port)
        snap.device.ip = host
        self._read_temperature(host)
        snap.device.temperature_c = self._temp_c
        snap.device.temperature_at = self._temp_at
        if self._uptime is not None and self._temp_at:
            # Uptime arrives with the temperature, then keeps counting on its
            # own -- no need to ask the device again just to watch a clock.
            snap.device.uptime_seconds = int(self._uptime + (time.time() - self._temp_at))
        if self._perf is not None:
            # measured=True even when every counter is zero: an idle array
            # really does report zeros, and that is a reading, not a gap.
            snap.performance = PerformanceInfo(
                read_mb_per_s=self._perf.get("read_mb_per_s") or 0,
                write_mb_per_s=self._perf.get("write_mb_per_s") or 0,
                iops=self._perf.get("iops") or 0,
                tier_iops=self._perf.get("tier_iops") or 0,
                measured=True,
            )
        return snap

    # -- temperature -------------------------------------------------------

    def _read_temperature(self, host: str) -> None:
        """
        Top up the cached chassis temperature, at most once every
        `sysinfo_seconds`. Never raises.

        Two reasons this is deliberately infrequent rather than once per poll.

        The honest one: it opens a second TCP connection and sends a command,
        where the rest of this driver only listens. That is a bigger imposition
        on a 2013 embedded box than reading a greeting, and it earns its keep
        at two-minute resolution -- a NAS chassis does not change temperature
        meaningfully in ten seconds.

        The one learned the hard way: on 2026-07-26 this command answered 15
        times out of 15, and then -- after roughly ninety command connections
        within about an hour, across various tools -- stopped answering
        entirely, while eCmdGetConfig on the same port kept working 3/3. The
        device had not crashed and the array stayed healthy, but something
        inside nasd stopped servicing this particular request. We do not know
        what, and we are not going to find out by doing it again to the
        owner's only unit. So: ask rarely, accept a stale answer, and never
        retry a failure in a tight loop.

        It cleared on its own: left alone, with no probing beyond this
        driver's ordinary `sysinfo_seconds`-spaced poll, the command started
        answering again after about 30 minutes, with no restart needed. So
        this is a temporary refusal under load, not damage -- but the
        recovery window is long enough that hammering the port costs a real
        stretch of "no fresh reading", which is the other reason to keep this
        infrequent rather than something to retry.

        A failure is not an error anywhere. It means "no fresh reading", and
        the last good value stays put with its original timestamp so whatever
        displays it can say how old it is.
        """
        now = time.time()
        if now - self._temp_tried_at < self.sysinfo_seconds:
            return
        self._temp_tried_at = now
        if not self._known_esa_id:
            return  # the command port needs the serial; we don't have one yet
        try:
            # Both readings share this one connection. Given the exhaustion
            # described above, connection count is the thing worth conserving --
            # a second command on a socket we already hold costs almost nothing.
            info = sysinfo.get_sysinfo_and_performance(
                host, self._known_esa_id, timeout=min(self.timeout, 6.0))
        except (OSError, config_cmd.ConfigError, sysinfo.SysInfoError):
            return  # unavailable right now; keep whatever we already had
        if info.get("temperature_c") is not None:
            self._temp_c = info["temperature_c"]
            self._temp_at = now
        if info.get("uptime_seconds") is not None:
            self._uptime = info["uptime_seconds"]
            self._temp_at = self._temp_at or now

        perf = info.get("performance")
        if perf and any(v is not None for v in perf.values()):
            self._perf = perf
            self._perf_at = now


DRIVERS: dict[str, type[DroboDriver]] = {
    "mock": MockDriver,
    "drobo5n": Drobo5NDriver,
}


def build(cfg: dict) -> DroboDriver:
    name = (cfg.get("driver") or "mock").lower()
    if name not in DRIVERS:
        raise ValueError(f"unknown driver {name!r}; choose from {sorted(DRIVERS)}")
    return DRIVERS[name](cfg)
