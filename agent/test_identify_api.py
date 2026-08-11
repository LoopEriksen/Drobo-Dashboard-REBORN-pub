"""
End-to-end test of Identify through the agent, against the simulated Drobo.

    py test_identify_api.py

WHY THIS IS A SEPARATE FILE FROM test_agent.py

test_agent.py runs its Monitor with the default driver and `simulate_config`
off, which is right for what it tests. Identify's interesting branch is the
simulated one, and turning `simulate_config` on there would change how every
config read in that suite behaves. So this spins up its own agent on its own
port instead of bending the other one.

WHAT IS ACTUALLY BEING PROVEN

Not "does the Drobo blink" -- nothing here can know that. What this proves is
that the value a caller asks for arrives intact at the driver, that the
simulated path enforces the same rules as the hardware path, and that a bad
interval is reported as the caller's mistake rather than as a fault in
somebody's storage array.

That last one matters more than it looks. `IdentifyParameterError` subclasses
`IdentifyError`, so an `except IdentifyError` written before it existed will
swallow it and answer 502 -- telling the operator their Drobo refused a
command when in fact they made a typo. The ordering test below is the guard
against that regression.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk")))

from drobo_agent import api, config, notify  # noqa: E402
from drobo_agent.monitor import Monitor  # noqa: E402
from drobo_agent.photos import PhotoStore  # noqa: E402
from drobo_nasd import identify as idf  # noqa: E402

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("  " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(label)


tmp = tempfile.mkdtemp(prefix="identify-api-")
cfg_path = os.path.join(tmp, "config.json")
PORT = 7437
BASE = f"http://127.0.0.1:{PORT}"

with open(cfg_path, "w") as fh:
    json.dump({
        "agent": {"bind": "127.0.0.1", "port": PORT, "poll_seconds": 1},
        "drobo": {"driver": "mock", "simulate_config": True},
        "photos": {"enabled": False},
    }, fh)

cfg = config.load(cfg_path)
TOKEN = cfg["agent"]["token"]

mon = Monitor(cfg, notify.build(cfg["notify"]))
photos = PhotoStore(cfg["photos"])
photos.load()
mon.start()
httpd = api.serve(cfg, mon, photos)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(1.0)


def post(path):
    req = urllib.request.Request(BASE + path, data=b"",
                                 headers={"X-Agent-Token": TOKEN}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


driver = mon.driver
check("the simulated driver is the one under test",
      getattr(driver, "simulate_config", False) is True, type(driver).__name__)

try:
    print("\n== the interval reaches the driver ==")

    status, body = post("/api/identify")
    check("a plain identify succeeds", status == 200 and body.get("ok"), (status, body))
    check("it is reported as simulated", body.get("simulated") is True, body)
    check("the default interval is applied, not left empty",
          body.get("interval") == idf.DEFAULT_INTERVAL, body)
    check("the driver actually received it",
          driver._identify_last_interval == idf.DEFAULT_INTERVAL,
          driver._identify_last_interval)

    status, body = post("/api/identify?interval=45")
    check("a chosen interval is accepted", status == 200, (status, body))
    check("...and arrives at the driver unchanged",
          driver._identify_last_interval == 45, driver._identify_last_interval)

    # Zero is the stop guess, and it is falsy -- exactly the value an
    # `if interval:` somewhere in the chain would quietly turn into "unset".
    status, body = post("/api/identify?interval=0")
    check("zero is accepted rather than treated as absent", status == 200, (status, body))
    check("zero arrives as zero, not as the default",
          driver._identify_last_interval == 0, driver._identify_last_interval)
    check("...and is reported back as zero", body.get("interval") == 0, body)

    # The control case: what this module sent for months.
    status, body = post("/api/identify?interval=none")
    check("'none' is accepted", status == 200, (status, body))
    check("'none' reaches the driver as None, not the string",
          driver._identify_last_interval is None, driver._identify_last_interval)

    print("\n== a bad interval is the caller's fault, not the Drobo's ==")
    before = driver._identify_count
    for bad, why in (("-1", "negative"), ("abc", "not a number"),
                     (str(idf.MAX_INTERVAL + 1), "beyond the sanity limit")):
        status, body = post(f"/api/identify?interval={bad}")
        check(f"{why} is refused with 400", status == 400, (bad, status, body))
        check(f"{why} says why", "error" in body, body)
    check("no refused request reached the driver",
          driver._identify_count == before, (before, driver._identify_count))

    print("\n== the simulator is not more permissive than the hardware ==")
    # If these two ever disagree, the simulator starts certifying intervals
    # that a real Drobo would reject.
    for value in (-1, idf.MAX_INTERVAL + 1, "15", 15.0, True):
        sdk_refuses = False
        try:
            idf.validate_interval(value)
        except idf.IdentifyParameterError:
            sdk_refuses = True
        check(f"the SDK refuses {value!r}", sdk_refuses)
    for value in (None, 0, 15, idf.MAX_INTERVAL):
        try:
            idf.validate_interval(value)
            check(f"the SDK allows {value!r}", True)
        except idf.IdentifyParameterError as exc:
            check(f"the SDK allows {value!r}", False, str(exc))

    check("IdentifyParameterError is catchable as IdentifyError",
          issubclass(idf.IdentifyParameterError, idf.IdentifyError))
    check("...and as ValueError, for callers that never heard of this module",
          issubclass(idf.IdentifyParameterError, ValueError))
finally:
    httpd.shutdown()
    mon.stop()
    httpd.server_close()

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
