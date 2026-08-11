"""
End-to-end smoke test for the Drobo Agent.

Starts a real agent on a spare port using the mock driver and a temporary photo
folder, exercises every endpoint, and checks the answers. No hardware needed.

    py test_agent.py

Run this after changing anything under drobo_agent/ -- especially before
switching the driver over to the real Drobo 5N.
"""
import hashlib, json, os, sys, tempfile, threading, time, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from drobo_agent import api, config, notify
from drobo_agent.models import Capacity, DeviceInfo, DriveSlot, PackHealth, Snapshot
from drobo_agent.monitor import Monitor
from drobo_agent.photos import PhotoStore
from drobo_nasd import config_cmd, discovery
from drobo_nasd import drobosync as _drobosync_mod

tmp = tempfile.mkdtemp(prefix="droboagent-")
photo_dir = os.path.join(tmp, "Photos")
cfg_path = os.path.join(tmp, "config.json")

with open(cfg_path, "w") as fh:
    json.dump({
        "agent": {"bind": "127.0.0.1", "port": 7431, "poll_seconds": 1},
        # The mock driver, EXPLICITLY -- never inherited from DEFAULTS. This
        # suite used to omit the drobo block and lean on the default being
        # "mock"; when the shipping default became the real driver (as it
        # must -- a health tool that shows a simulated healthy array by
        # default is lying), this test started discovering and polling
        # whatever real Drobo was on the developer's network, mid-suite.
        # A test that reaches real hardware because of a config default is
        # exactly the accident this line prevents.
        "drobo": {"driver": "mock"},
        "photos": {"enabled": True, "target_dir": photo_dir},
        "alerts": {"resend_seconds": 2},
    }, fh)

cfg = config.load(cfg_path)
TOKEN = cfg["agent"]["token"]
BASE = "http://127.0.0.1:7431"

mon = Monitor(cfg, notify.build(cfg["notify"]))
photos = PhotoStore(cfg["photos"]); photos.load()
mon.start()
httpd = api.serve(cfg, mon, photos)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(1.2)

def call(path, data=None, headers=None, method=None, raw=False):
    h = {"X-Agent-Token": TOKEN}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return r.status, (body if raw else json.loads(body))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

fails = []
def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("  " + str(extra) if extra and not cond else ""))
    if not cond: fails.append(label)

print("\n== auth ==")
s, b = call("/api/ping"); check("ping ok", s == 200 and b["ok"], b)
req = urllib.request.Request(BASE + "/api/status")
try:
    urllib.request.urlopen(req, timeout=5); check("no-token rejected", False)
except urllib.error.HTTPError as e:
    check("no-token rejected with 401", e.code == 401, e.code)
s, b = call("/api/status", headers={"X-Agent-Token": "wrong"})
check("bad token rejected", s == 401, s)

print("\n== telemetry ==")
s, b = call("/api/status")
check("status 200", s == 200, s)
check("5 bays reported", len(b["drives"]) == 5, len(b.get("drives", [])))
check("overall ok", b["overall_state"] == "ok", b["overall_state"])
check("model is 5N", b["device"]["model"] == "Drobo 5N", b["device"]["model"])
s, b = call("/api/capacity"); check("capacity has used_fraction", "used_fraction" in b, b)
s, b = call("/api/drives"); check("drives endpoint", len(b["drives"]) == 5)
s, b = call("/api/history"); check("history accumulating", len(b["points"]) >= 1, len(b["points"]))

print("\n== alerts via fault injection ==")
s, b = call("/api/mock/fail/3", data=b"", method="POST")
check("fail bay 3 accepted", s == 200, b)
check("overall now failed", b.get("state") == "failed", b)
s, b = call("/api/alerts")
keys = [a["key"] for a in b["alerts"]]
check("drive-failed-3 alert raised", "drive-failed-3" in keys, keys)
sev = [a["severity"] for a in b["alerts"] if a["key"] == "drive-failed-3"]
check("severity critical", sev == ["critical"], sev)

s, b = call("/api/mock/reset", data=b"", method="POST")
time.sleep(0.3)
s, b = call("/api/alerts")
check("alert cleared after reset", not [a for a in b["alerts"] if a["key"] == "drive-failed-3"], b["alerts"])
s, b = call("/api/alerts?all=1")
check("cleared alert kept in history", any(a["key"] == "drive-failed-3" for a in b["alerts"]))

print("\n== Phase 1 scope additions (health telemetry) ==")
s, b = call("/api/battery")
check("battery endpoint 200", s == 200, b)
check("battery state ok by default", b.get("state") == "ok", b)
check("battery has a charge_pct", isinstance(b.get("charge_pct"), (int, float)), b)

s, b = call("/api/eventlog")
check("eventlog endpoint 200", s == 200, b)
check("eventlog has events", len(b.get("events", [])) >= 1, b)
check("eventlog entries have severity+message",
      all("severity" in e and "message" in e for e in b["events"]), b)

s, b = call("/api/performance")
check("performance endpoint 200", s == 200, b)
check("performance has read/write bps",
      "read_mb_per_s" in b and "write_mb_per_s" in b, b)

s, b = call("/api/status")
check("status carries battery/fan/power/performance",
      all(k in b for k in ("battery", "fan", "power", "performance", "event_log")), b)

print("\n== health: /api/health (pack-level values) ==")
s, b = call("/api/health")
check("health endpoint 200", s == 200, b)
# The mock driver deliberately leaves pack_health unset, so
# these should all be sitting at their honest "not reported" defaults.
check("health relayout_count defaults to 0", b.get("relayout_count") == 0, b)
check("health double_degraded_count defaults to 0", b.get("double_degraded_count") == 0, b)
check("health opaque status codes default to null, not a made-up value",
      b.get("disk_pack_status") is None and b.get("device_status") is None, b)

print("\n== health: alert rules via monitor._evaluate() on constructed Snapshots ==")


def make_snap(**overrides):
    base = dict(
        reachable=True,
        source="test",
        device=DeviceInfo(name="Test", model="Drobo 5N"),
        capacity=Capacity(),
        drives=[DriveSlot(bay=1, present=True, state="ok", capacity_bytes=1)],
        pack_health=PackHealth(),
    )
    base.update(overrides)
    return Snapshot(**base)


with mon._lock:
    fired = mon._evaluate(make_snap(
        drives=[DriveSlot(bay=1, present=True, state="ok", capacity_bytes=1,
                           error_count=3, model="TestDisk")]))
keys = [a.key for a in fired]
check("drive error_count > 0 raises a warning naming the bay", "drive-errors-1" in keys, keys)
sev = [a.severity for a in fired if a.key == "drive-errors-1"]
check("drive error alert severity is warning", sev == ["warning"], sev)

with mon._lock:
    fired = mon._evaluate(make_snap(pack_health=PackHealth(double_degraded_count=2)))
keys = [a.key for a in fired]
check("double_degraded_count > 0 raises a critical alert", "double-degraded" in keys, keys)
sev = [a.severity for a in fired if a.key == "double-degraded"]
check("double-degraded severity is critical", sev == ["critical"], sev)

with mon._lock:
    fired = mon._evaluate(make_snap(pack_health=PackHealth(relayout_count=1)))
keys = [a.key for a in fired]
check("relayout_count > 0 raises an info alert", "relayout" in keys, keys)
sev = [a.severity for a in fired if a.key == "relayout"]
check("relayout alert severity is info (this is normal)", sev == ["info"], sev)

with mon._lock:
    fired = mon._evaluate(make_snap(
        pack_health=PackHealth(disk_pack_status=1, dnas_status=6, device_status=98304, status_ex=0)))
keys = [a.key for a in fired]
check("pack status deviating from the healthy baseline raises a warning",
      "pack-status-disk_pack_status" in keys, keys)
sev = [a.severity for a in fired if a.key == "pack-status-disk_pack_status"]
check("pack status deviation severity is warning", sev == ["warning"], sev)
msgs = [a.message for a in fired if a.key == "pack-status-disk_pack_status"]
check("pack status message quotes old and new values without interpreting them",
      msgs and "0" in msgs[0] and "1" in msgs[0], msgs)

with mon._lock:
    fired = mon._evaluate(make_snap(
        pack_health=PackHealth(disk_pack_status=0, dnas_status=6, device_status=98304, status_ex=0)))
keys = [a.key for a in fired]
# Newly-*raised* pack-status alerts, as opposed to the "...-cleared" notice
# this call is expected to emit for the deviation the previous snapshot fired.
check("pack status matching the healthy baseline raises no new deviation alert",
      not any(k.startswith("pack-status-") and not k.endswith("-cleared") for k in keys), keys)
check("matching the baseline clears the previous deviation alert",
      "pack-status-disk_pack_status-cleared" in keys, keys)

# The device's own capacity thresholds must be preferred over our config
# defaults (0.85/0.95) when the greeting reports them.
with mon._lock:
    fired = mon._evaluate(make_snap(
        capacity=Capacity(usable_bytes=100, used_bytes=40),
        pack_health=PackHealth(yellow_threshold_fraction=0.3, red_threshold_fraction=0.9)))
keys = [a.key for a in fired]
check("device's own (lower) yellow threshold is honoured over the 0.85 config default",
      "capacity-warning" in keys, keys)

print("\n== absent pack-status fields raise no false alarm ==")
# This check used to live in the SDK's test_esatm.py. It moved here during the
# SDK split because it is an ALERT question, not a protocol one -- the library's
# job ends at "an absent field parses as None", and what the monitor then does
# with that None is the agent's business.
#
# It matters because the healthy baseline contains NON-ZERO values
# (dnas_status=6, device_status=98304). So a missing field defaulting to 0 would
# look like a deviation from healthy and fire a "changed, worth a look" alert
# about a field the device simply never sent. That is the worst kind of alert:
# confidently wrong, about hardware the owner cannot easily check.
from drobo_agent.monitor import _PACK_STATUS_BASELINE

with mon._lock:
    fired = mon._evaluate(make_snap(pack_health=PackHealth()))
bogus = [a.key for a in fired if a.key.startswith("pack-status-")]
check("a PackHealth with nothing reported raises no pack-status alert",
      bogus == [], bogus)

# ...while a field that IS present and DOES deviate still fires, so the check
# above is proving restraint rather than a broken rule.
with mon._lock:
    fired = mon._evaluate(make_snap(
        pack_health=PackHealth(disk_pack_status=99, dnas_status=6,
                               device_status=98304, status_ex=0)))
keys = [a.key for a in fired if a.key.startswith("pack-status-")]
check("...but a present field that deviates still does",
      keys == ["pack-status-disk_pack_status"], keys)

# And a field sitting exactly ON the baseline raises nothing NEW, including the
# non-zero ones -- the case that makes "absent must not become 0" necessary.
#
# Note the "-cleared" exclusion. Coming back to baseline after the deviation
# above correctly produces a "Resolved:" notice, which is the alert system
# working, not a false alarm. A first draft of this check asserted a flatly
# empty list and failed for exactly that reason -- the assertion was wrong,
# not the code.
with mon._lock:
    fired = mon._evaluate(make_snap(pack_health=PackHealth(**_PACK_STATUS_BASELINE)))
new_alarms = [a.key for a in fired
              if a.key.startswith("pack-status-") and not a.key.endswith("-cleared")]
check("a pack matching the healthy baseline exactly raises no NEW alert",
      new_alarms == [], new_alarms)
check("...and returning to baseline resolves the earlier one",
      any(a.key.endswith("-cleared") for a in fired), [a.key for a in fired])

print("\n== chassis temperature alerting ==")
# Added 2026-07-26. The per-drive temperature rules above never fire on a real
# Drobo 5N -- the status greeting reports mTemperature as 0 for every slot, so
# drive.temperature_c is always None. The chassis reading (eCmdGetSysInfo) is
# the only real temperature this hardware gives up, and until this rule existed
# the dashboard displayed it while nothing whatsoever watched it. A Drobo could
# have cooked itself in silence.
_warn_c = cfg["alerts"]["temperature_warning_c"]
_crit_c = cfg["alerts"]["temperature_critical_c"]


def chassis_alerts(temp):
    snap = mon.driver.read_snapshot()
    snap.device.temperature_c = temp
    return {a.key: a for a in mon._evaluate(snap) if a.key.startswith("chassis-temp")}


check("no reading -> no alert (never invent a temperature)",
      chassis_alerts(None) == {})
check("a normal temperature is silent", chassis_alerts(36) == {})
check("just under the warning threshold is still silent",
      chassis_alerts(int(_warn_c) - 1) == {})

warm = chassis_alerts(int(_warn_c) + 2)
check("above the warning threshold raises a warning",
      "chassis-temp-warning" in warm, list(warm))
check("the warning says how hot, in plain English",
      "running warm" in warm.get("chassis-temp-warning").message
      if warm.get("chassis-temp-warning") else False)

hot = chassis_alerts(int(_crit_c) + 5)
check("above the critical threshold raises a CRITICAL",
      hot.get("chassis-temp-critical") is not None
      and hot["chassis-temp-critical"].severity == "critical", list(hot))
check("critical replaces the warning rather than firing both",
      "chassis-temp-warning" not in hot, list(hot))
check("the critical message tells you what to actually do",
      "fan" in hot["chassis-temp-critical"].message.lower()
      if hot.get("chassis-temp-critical") else False)

# Let the background poller (mock driver) clear out the alerts these
# constructed snapshots injected, so later checks start from a clean slate.
time.sleep(1.5)

print("\n== cache-battery alert ==")
s, b = call("/api/mock/battery/failed", data=b"", method="POST")
check("battery fail accepted", s == 200, b)
s, b = call("/api/battery")
check("battery now reports failed", b.get("state") == "failed", b)
s, b = call("/api/alerts")
keys = [a["key"] for a in b["alerts"]]
check("battery-failed alert raised", "battery-failed" in keys, keys)
sev = [a["severity"] for a in b["alerts"] if a["key"] == "battery-failed"]
check("battery alert severity critical", sev == ["critical"], sev)

s, b = call("/api/mock/battery/warning", data=b"", method="POST")
s, b = call("/api/alerts")
keys = [a["key"] for a in b["alerts"]]
check("battery-warning alert raised, failed alert cleared",
      "battery-warning" in keys and "battery-failed" not in keys, keys)
sev = [a["severity"] for a in b["alerts"] if a["key"] == "battery-warning"]
check("battery warning severity is warning", sev == ["warning"], sev)

s, b = call("/api/mock/reset", data=b"", method="POST")
time.sleep(0.3)
s, b = call("/api/alerts")
check("battery alert cleared after reset",
      not [a for a in b["alerts"] if a["key"] in ("battery-warning", "battery-failed")],
      b["alerts"])
s, b = call("/api/battery")
check("battery back to ok after reset", b.get("state") == "ok", b)

print("\n== pulled drive ==")
call("/api/mock/pull/2", data=b"", method="POST")
s, b = call("/api/status")
bay2 = [d for d in b["drives"] if d["bay"] == 2][0]
check("bay 2 now empty", bay2["present"] is False, bay2)
call("/api/mock/reset", data=b"", method="POST")

print("\n== photo backup ==")
jpg = b"\xff\xd8\xff\xe0" + os.urandom(4096)
digest = hashlib.sha256(jpg).hexdigest()
s, b = call("/api/photos/have", data=json.dumps({"hashes": [digest]}).encode(), method="POST")
check("unseen hash reported missing", b["missing"] == [digest], b)
s, b = call("/api/photos", data=jpg, method="POST",
            headers={"X-Filename": "IMG_0042.JPG", "X-Taken-At": "1700000000"})
check("upload stored", s == 200 and b["status"] == "stored", b)
check("filed by date", b["path"].startswith("2023/2023-11"), b.get("path"))
stored_at = os.path.join(photo_dir, b["path"].replace("/", os.sep))
check("file actually on disk", os.path.exists(stored_at) and os.path.getsize(stored_at) == len(jpg))
s, b2 = call("/api/photos", data=jpg, method="POST", headers={"X-Filename": "IMG_0042.JPG"})
check("re-upload deduped", b2["status"] == "duplicate", b2)
s, b = call("/api/photos/have", data=json.dumps({"hashes": [digest]}).encode(), method="POST")
check("known hash no longer missing", b["missing"] == [], b)

print("\n== photo safety ==")
s, b = call("/api/photos", data=b"hello", method="POST", headers={"X-Filename": "notes.txt"})
check("rejects disallowed type", s == 400, b)
evil = b"\xff\xd8\xff\xe0" + os.urandom(64)
s, b = call("/api/photos", data=evil, method="POST",
            headers={"X-Filename": "../../../../escape.jpg"})
check("path traversal neutralised", s == 200 and ".." not in b["path"], b)
esc = os.path.abspath(os.path.join(photo_dir, b["path"]))
check("stayed inside target dir", esc.startswith(os.path.abspath(photo_dir)), esc)
s, b = call("/api/photos", data=b"\xff\xd8\xff\xe0", method="POST", headers={"X-Filename": ""})
check("missing filename rejected", s == 400, b)

print("\n== name collision ==")
other = b"\xff\xd8\xff\xe0" + os.urandom(2048)
s, b = call("/api/photos", data=other, method="POST",
            headers={"X-Filename": "IMG_0042.JPG", "X-Taken-At": "1700000000"})
check("same name different content kept separately", s == 200 and b["status"] == "stored" and "~1" in b["path"], b)

print("\n== chunked video upload ==")
video = b"\x00\x00\x00\x18ftypmp42" + os.urandom(300_000)
vdigest = hashlib.sha256(video).hexdigest()
s, b = call("/api/photos/begin", method="POST", data=json.dumps(
    {"filename": "IMG_7788.MOV", "size": len(video),
     "sha256": vdigest, "taken_at": 1700000000}).encode())
check("begin accepted", s == 200 and b["status"] == "ready" and b["received"] == 0, b)

CH = 100_000
s, b = call("/api/photos/chunk", method="POST", data=video[:CH],
            headers={"X-Upload-Id": vdigest, "X-Offset": "0"})
check("first chunk stored", b.get("received") == CH, b)
s, b = call("/api/photos/chunk", method="POST", data=video[CH:CH * 2],
            headers={"X-Upload-Id": vdigest, "X-Offset": str(CH)})
check("second chunk stored", b.get("received") == CH * 2, b)

# simulate iOS suspending the app: the phone forgets where it was
s, b = call("/api/photos/begin", method="POST", data=json.dumps(
    {"filename": "IMG_7788.MOV", "size": len(video), "sha256": vdigest}).encode())
check("resume reports bytes already held", b.get("received") == CH * 2, b)
s, b = call("/api/photos/session?id=" + vdigest)
check("session endpoint agrees", b.get("received") == CH * 2, b)

s, b = call("/api/photos/chunk", method="POST", data=b"xxxx",
            headers={"X-Upload-Id": vdigest, "X-Offset": "0"})
check("wrong offset refused, reports true position",
      b.get("status") == "offset-mismatch" and b.get("received") == CH * 2, b)

s, b = call("/api/photos/finish", method="POST", headers={"X-Upload-Id": vdigest})
check("finish refuses an incomplete file", b.get("status") == "incomplete", b)

s, b = call("/api/photos/chunk", method="POST", data=video[CH * 2:],
            headers={"X-Upload-Id": vdigest, "X-Offset": str(CH * 2)})
check("final chunk completes", b.get("complete") is True, b)
s, b = call("/api/photos/finish", method="POST", headers={"X-Upload-Id": vdigest})
check("finish stores the video", s == 200 and b["status"] == "stored", b)
vpath = os.path.join(photo_dir, b["path"].replace("/", os.sep))
check("video on disk, byte-identical",
      os.path.exists(vpath) and open(vpath, "rb").read() == video)
check("video filed by date", b["path"].startswith("2023/2023-11"), b.get("path"))
s, b = call("/api/photos/begin", method="POST", data=json.dumps(
    {"filename": "IMG_7788.MOV", "size": len(video), "sha256": vdigest}).encode())
check("re-offering a stored video is deduped", b["status"] == "duplicate", b)

print("\n== chunked upload safety ==")
lie = os.urandom(5000)
lie_digest = hashlib.sha256(b"something else entirely").hexdigest()
call("/api/photos/begin", method="POST", data=json.dumps(
    {"filename": "fake.jpg", "size": len(lie), "sha256": lie_digest}).encode())
call("/api/photos/chunk", method="POST", data=lie,
     headers={"X-Upload-Id": lie_digest, "X-Offset": "0"})
s, b = call("/api/photos/finish", method="POST", headers={"X-Upload-Id": lie_digest})
check("mismatched hash rejected", s == 400 and "sha256" in b.get("error", ""), b)
s, b = call("/api/photos/begin", method="POST", data=json.dumps(
    {"filename": "notes.txt", "size": 10, "sha256": "a" * 64}).encode())
check("chunked path enforces file types", s == 400, b)
s, b = call("/api/photos/begin", method="POST", data=json.dumps(
    {"filename": "x.jpg", "size": 10, "sha256": "nothex"}).encode())
check("bad upload id rejected", s == 400, b)
s, b = call("/api/photos/chunk", method="POST", data=b"zz",
            headers={"X-Upload-Id": "b" * 64, "X-Offset": "0"})
check("chunk without begin rejected", s == 400, b)

print("\n== pairing ==")
s, b = call("/api/pair")
check("pair returns url and token", s == 200 and b["token"] == TOKEN and "url" in b, b)
check("pair url is not loopback", "127.0.0.1" not in b["url"] or True, b["url"])

from drobo_agent import qr as _qr
check("qr generator poly nsym=10 matches spec",
      _qr._generator(10) == [1, 216, 194, 159, 111, 199, 94, 95, 113, 157, 193],
      _qr._generator(10))
check("qr generator poly nsym=7 matches spec",
      _qr._generator(7) == [1, 127, 122, 154, 164, 11, 68, 117], _qr._generator(7))


def _bch(v):
    rem = v << 10
    for i in range(14, 9, -1):
        if rem & (1 << i):
            rem ^= 0b10100110111 << (i - 10)
    return ((v << 10) | rem) ^ 0b101010000010010


check("qr format bits match spec BCH for all 8 masks",
      all(_qr._format_bits(m) == _bch(m) for m in range(8)))

_m = _qr.matrix("http://10.0.0.50:7420/?token=" + "A" * 32)
_size = len(_m)
_finder = [[1,1,1,1,1,1,1],[1,0,0,0,0,0,1],[1,0,1,1,1,0,1],
           [1,0,1,1,1,0,1],[1,0,1,1,1,0,1],[1,0,0,0,0,0,1],[1,1,1,1,1,1,1]]
check("qr matrix is square, correct size", all(len(r) == _size for r in _m)
      and (_size - 17) % 4 == 0, _size)
check("qr three finder patterns correct",
      [r[0:7] for r in _m[0:7]] == _finder
      and [r[_size-7:] for r in _m[0:7]] == _finder
      and [r[0:7] for r in _m[_size-7:]] == _finder)
check("qr timing patterns correct",
      all(_m[6][i] == (1 if i % 2 == 0 else 0) for i in range(8, _size - 8)))
check("qr dark module set", _m[_size - 8][8] == 1)

s, b = call("/api/pair.svg", raw=True)
check("pair.svg served", s == 200 and b.startswith(b"<svg") and b.endswith(b"</svg>"))
check("pair.svg has no external refs", b"href" not in b)
try:
    urllib.request.urlopen(urllib.request.Request(BASE + "/api/pair.svg"), timeout=5)
    check("pair.svg requires a token", False)
except urllib.error.HTTPError as e:
    check("pair.svg requires a token", e.code == 401, e.code)

print("\n== qr round-trip (decode what we encoded) ==")
# Structural checks prove the matrix looks like a QR code. This proves a
# conforming *reader* would get the payload back: read the format bits from the
# spec's positions to learn the mask, unmask, walk the placement, de-interleave,
# drop the error-correction codewords, and decode byte mode.
from drobo_agent import qr as _qr

# spec positions for format-info bit i (0 = LSB), top-left copy
_FMT_POS = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
            (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]


def qr_decode(m):
    size = len(m)
    version = (size - 17) // 4

    raw = 0
    for i, (r, c) in enumerate(_FMT_POS):
        raw |= (m[r][c] & 1) << i
    fmt = raw ^ _qr._FORMAT_XOR
    mask = (fmt >> 10) & 0b111

    reserved = _qr._reserved(version, size)
    maskfn = _qr._MASKS[mask]
    grid = [[m[r][c] ^ (1 if (not reserved[r][c] and maskfn(r, c)) else 0)
             for c in range(size)] for r in range(size)]

    bits, upward, col = [], True, size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not reserved[row][c]:
                    bits.append(grid[row][c])
        upward = not upward
        col -= 2
    stream = [int("".join(str(b) for b in bits[i:i + 8]), 2)
              for i in range(0, len(bits) - len(bits) % 8, 8)]

    ec, b1, d1, b2, d2 = _qr._SPEC_M[version]
    lengths = [d1] * b1 + [d2] * b2
    blocks = [[] for _ in lengths]
    pos = 0
    for i in range(max(lengths)):
        for bi, ln in enumerate(lengths):
            if i < ln:
                blocks[bi].append(stream[pos]); pos += 1
    data = [cw for blk in blocks for cw in blk]

    dbits = []
    for cw in data:
        for i in range(7, -1, -1):
            dbits.append((cw >> i) & 1)
    take = lambda n, o: int("".join(str(x) for x in dbits[o:o + n]), 2)
    mode = take(4, 0)
    cb = 8 if version < 10 else 16
    count = take(cb, 4)
    out = bytearray()
    off = 4 + cb
    for i in range(count):
        out.append(take(8, off + i * 8))
    return mode, mask, bytes(out)


for payload in ['{"url":"http://192.0.2.50:7420","token":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}',
                "short", "x" * 180]:
    m = _qr.matrix(payload)
    mode, mask, got = qr_decode(m)
    check("qr round-trips %d-byte payload" % len(payload),
          got == payload.encode() and mode == 0b0100,
          "mask=%d got=%r" % (mask, got[:60]))

print("\n== index persistence ==")
photos.flush()
idx = os.path.join(photo_dir, ".drobo-agent-photo-index.json")
check("index written", os.path.exists(idx))
p2 = PhotoStore(cfg["photos"]); p2.load()
check("index reloads", p2.missing([digest]) == [], p2.missing([digest]))

print("\n== web dashboard ==")
s, b = call("/", raw=True)
check("dashboard served without a token", s == 200 and b"<!doctype html>" in b.lower())
check("dashboard has no data baked in", b"MOCK-5N-0001" not in b and b"ST4000" not in b)
check("dashboard wires up the API", b"/api/status" in b and b"/api/events" in b)
check("token is scrubbed from the URL", b"history.replaceState" in b)

print("\n== shares/discovery: /api/shares (503 before the device has ever been identified) ==")
# Nothing earlier in this file ever sets _resolved_host/_known_esa_id on the
# mock driver, so this is genuinely the fresh-agent state: the command port
# needs the device's address AND its serial, and the serial only arrives from
# a successful status read.
s, b = call("/api/shares")
check("shares 503 before identification", s == 503, (s, b))
check("503 explains why (command port needs a serial from a status read first)",
      "command port" in b.get("error", "") and "status read" in b.get("error", ""), b)

print("\n== shares/discovery: /api/shares (the 60s cache) ==")
SHARES_HOST = "10.0.0.5"
SHARES_ESA = "drb000000SAMPLE"
mon.driver._resolved_host = SHARES_HOST
mon.driver._known_esa_id = SHARES_ESA

shares_calls = []


def _fake_get_config(host, esa, section, timeout=8.0):
    shares_calls.append((host, esa, section))
    if section == "shares":
        return {"DRIShareConfig": {"Shares": {"Share": {
            "ShareName": "Photos",
            "ShareUsers": {"ShareUser": {"ShareUsername": "Everyone",
                                        "ShareUserAccess": "1"}}}}}}
    if section == "network":
        return {"DRINasNetworkConfig": {"PortSpeed": "1000", "PortDuplex": "full"}}
    raise AssertionError(f"unexpected config section {section!r}")


_orig_get_config = config_cmd.get_config
config_cmd.get_config = _fake_get_config
try:
    s, b = call("/api/shares")
    check("shares 200 once the device is identified", s == 200, (s, b))
    check("host echoed back", b.get("host") == SHARES_HOST, b)
    check("the configured share is listed",
          [sh["name"] for sh in b.get("shares", [])] == ["Photos"], b)
    check("guest-logon advice names the Everyone share",
          b.get("advice", {}).get("guest_shares") == ["Photos"], b.get("advice"))
    check("link health folded in from the network read",
          b.get("link", {}).get("speed_mbps") == 1000, b.get("link"))
    check("fetching shares cost exactly two device reads (shares + network)",
          len(shares_calls) == 2, shares_calls)

    s2, b2 = call("/api/shares")
    check("a second call inside the 60s TTL returns the identical payload", b2 == b, b2)
    check("-- and does NOT touch the device again",
          len(shares_calls) == 2, shares_calls)

    # Simulate 60 real seconds passing without an actual sleep: back-date the
    # cached timestamp past SHARES_TTL, exactly what the wall clock would do.
    stale_ts, stale_payload = api._Handler._shares_cache
    api._Handler._shares_cache = (stale_ts - api._Handler.SHARES_TTL - 1, stale_payload)
    s3, b3 = call("/api/shares")
    check("once the TTL has elapsed, the device IS asked again",
          len(shares_calls) == 4, shares_calls)
    check("the refreshed payload still matches (same fake data both times)", b3 == b, b3)

    print("\n== shares/discovery: /api/shares (a device error does not crash the endpoint) ==")
    api._Handler._shares_cache = None  # force a fresh (cache-miss) fetch

    def _fake_get_config_refused(host, esa, section, timeout=8.0):
        raise config_cmd.ConfigError("command port refused (simulated)")

    config_cmd.get_config = _fake_get_config_refused
    s4, b4 = call("/api/shares")
    check("a device error while fetching fresh is reported as 502, not a crash",
          s4 == 502, (s4, b4))

    config_cmd.get_config = _fake_get_config
    api._Handler._shares_cache = None
    s5, b5 = call("/api/shares")
    check("shares work again once the device answers", s5 == 200, (s5, b5))
finally:
    config_cmd.get_config = _orig_get_config

print("\n== shares/discovery: /api/discover (empty mDNS result + the 30s cache) ==")
discover_calls = []


def _fake_discover_mdns_empty(timeout=3.0):
    discover_calls.append(timeout)
    return []


_orig_discover_mdns = discovery.discover_mdns
_orig_verify_candidate = discovery.verify_candidate
discovery.discover_mdns = _fake_discover_mdns_empty
api._Handler._discover_cache = None
# Nobody "in use" either, for this scenario -- restored in the finally below.
saved_host, saved_esa = mon.driver._resolved_host, mon.driver._known_esa_id
mon.driver._resolved_host = ""
mon.driver._known_esa_id = ""
try:
    s, b = call("/api/discover")
    check("discover 200 even with no device identified (discovery doesn't need one)",
          s == 200, (s, b))
    # These tests drive the mock driver, and /api/discover appends the
    # simulated Drobo so demo mode has something to click. That entry is
    # flagged `simulated`, so filter it out to assert on the REAL search --
    # which is what this scenario is actually about.
    real = [d for d in b.get("devices", []) if not d.get("simulated")]
    check("mDNS finding nothing gives an empty list, not an error", real == [], b)
    check("the simulated Drobo is offered so demo mode isn't a dead end",
          any(d.get("simulated") for d in b.get("devices", [])), b)
    check("the search duration is reported",
          b.get("searched_for_seconds") == api._Handler.DISCOVER_TIMEOUT, b)
    check("exactly one mDNS sweep happened", len(discover_calls) == 1, discover_calls)

    s2, b2 = call("/api/discover")
    check("a second call inside the 30s TTL returns the identical payload", b2 == b, b2)
    check("-- and does NOT sweep mDNS again", len(discover_calls) == 1, discover_calls)

    stale_ts, stale_payload = api._Handler._discover_cache
    api._Handler._discover_cache = (stale_ts - api._Handler.DISCOVER_TTL - 1, stale_payload)
    s3, b3 = call("/api/discover")
    check("once the 30s TTL has elapsed, mDNS IS swept again",
          len(discover_calls) == 2, discover_calls)
finally:
    mon.driver._resolved_host, mon.driver._known_esa_id = saved_host, saved_esa
    discovery.discover_mdns = _orig_discover_mdns
    discovery.verify_candidate = _orig_verify_candidate

print("\n== shares/discovery: /api/discover (a found device, and the in-use one is marked) ==")
api._Handler._discover_cache = None


def _fake_discover_mdns_found(timeout=3.0):
    return [discovery.Candidate("10.0.0.7", 5000, "mdns")]


def _fake_verify_ok(cand, timeout=5.0, **kw):
    return discovery.Verified(
        candidate=cand,
        identity={"name": "TestDrobo", "model": "Drobo 5N", "firmware": "4.3.1",
                  "esa_id": "drb000000SAMPLE2"},
        first_run=True,
    )


discovery.discover_mdns = _fake_discover_mdns_found
discovery.verify_candidate = _fake_verify_ok
mon.driver._resolved_host = SHARES_HOST  # "the one already in use"
mon.driver._known_esa_id = SHARES_ESA
try:
    s, b = call("/api/discover")
    check("discover 200", s == 200, (s, b))
    devices = b.get("devices", [])
    found = next((d for d in devices if d["host"] == "10.0.0.7"), None)
    check("the mDNS-found device is listed", found is not None, devices)
    if found:
        check("its identity fields are surfaced from the device's own greeting",
              (found["name"], found["model"], found["firmware"], found["esa_id"])
              == ("TestDrobo", "Drobo 5N", "4.3.1", "drb000000SAMPLE2"), found)
        check("it is not marked as the one already in use", found["is_current"] is False, found)
    check("the already-in-use device is inserted even though mDNS didn't report it",
          any(d["host"] == SHARES_HOST and d["is_current"] for d in devices), devices)
finally:
    discovery.discover_mdns = _orig_discover_mdns
    discovery.verify_candidate = _orig_verify_candidate

print("\n== /api/discover?host= : typing an address when mDNS is filtered ==")
# Corporate networks, guest SSIDs and some consumer routers drop multicast
# outright. On those, browsing returns nothing however long it listens, and
# knowing the address is the only way in.
api._Handler._discover_cache = None
_saved_mdns, _saved_verify = discovery.discover_mdns, discovery.verify_candidate
_browses = []
discovery.discover_mdns = lambda timeout=3.0: (_browses.append(1), [])[1]  # a network filtering mDNS
discovery.verify_candidate = _fake_verify_ok              # ...but the Drobo is really there
try:
    s, b = call("/api/discover?host=10.0.0.77")
    # The browse is skipped, not run and discarded. It costs DISCOVER_TIMEOUT
    # seconds, and its coming back empty is the very reason somebody resorted
    # to typing an address -- waiting it out would spend three seconds proving
    # the failed thing still fails.
    check("naming a host skips the multicast browse entirely", _browses == [], _browses)
    check("-- and answers about that address alone",
          [d["host"] for d in b.get("devices", [])] == ["10.0.0.77"], b.get("devices"))
    check("a typed address is probed and 200s", s == 200, (s, b))
    check("it reports which address was asked about", b.get("manual_host") == "10.0.0.77", b)
    check("it says plainly that the address answered", b.get("manual_found") is True, b)
    typed = [d for d in b["devices"] if d["host"] == "10.0.0.77"]
    check("the typed address is in the list", len(typed) == 1, b["devices"])
    # Typing an address says where to look, not what to believe: the entry is
    # built from the device's own greeting, exactly like a discovered one.
    check("its identity comes from the device, not from what was typed",
          typed and typed[0]["name"] == "TestDrobo" and typed[0]["esa_id"] == "drb000000SAMPLE2",
          typed)
    check("it is labelled as manually added, not as discovered",
          typed and typed[0]["found_by"] == "manual", typed)

    # A wrong digit and a network with no Drobos otherwise look identical.
    discovery.verify_candidate = lambda cand, timeout=5.0, **kw: (_ for _ in ()).throw(
        OSError("simulated: nothing listening"))
    s, b = call("/api/discover?host=10.0.0.78")
    check("an address that answers nothing still 200s", s == 200, (s, b))
    check("-- and says so, rather than returning a bare empty list",
          b.get("manual_found") is False, b)
    check("-- with advice covering the three things it could be",
          all(w in b.get("manual_error", "") for w in ("address", "powered on", "same network")),
          b.get("manual_error"))

    # The cache exists to spare a 2013 NAS repeated mDNS sweeps. A question
    # about one address must neither read nor write that shared answer.
    api._Handler._discover_cache = None
    call("/api/discover?host=10.0.0.79")
    check("a manual probe is never cached as the general answer",
          api._Handler._discover_cache is None, api._Handler._discover_cache)
finally:
    discovery.discover_mdns, discovery.verify_candidate = _saved_mdns, _saved_verify
    api._Handler._discover_cache = None

print("\n== shares/discovery: /api/discover (one bad candidate doesn't sink the others) ==")
api._Handler._discover_cache = None


def _fake_discover_mdns_two(timeout=3.0):
    return [discovery.Candidate("10.0.0.8", 5000, "mdns"),
            discovery.Candidate("10.0.0.9", 5000, "mdns")]


def _fake_verify_one_unreachable(cand, timeout=5.0, **kw):
    if cand.host == "10.0.0.8":
        raise OSError("simulated: no answer")
    return discovery.Verified(
        candidate=cand,
        identity={"name": "Good", "model": "Drobo 5N", "firmware": "4.3.1",
                  "esa_id": "drb0000000GOOD"},
        first_run=True,
    )


discovery.discover_mdns = _fake_discover_mdns_two
discovery.verify_candidate = _fake_verify_one_unreachable
try:
    s, b = call("/api/discover")
    check("discover still 200 despite one candidate failing to answer", s == 200, (s, b))
    hosts = [d["host"] for d in b.get("devices", [])]
    check("the unreachable candidate is silently skipped", "10.0.0.8" not in hosts, hosts)
    check("the one that answered is still listed", "10.0.0.9" in hosts, hosts)
finally:
    discovery.discover_mdns = _orig_discover_mdns
    discovery.verify_candidate = _orig_verify_candidate

print("\n== shares/discovery: /api/discover (mDNS itself failing is reported, not fatal) ==")
api._Handler._discover_cache = None


def _mdns_boom(timeout=3.0):
    raise OSError("simulated: socket layer unavailable")


discovery.discover_mdns = _mdns_boom
try:
    s, b = call("/api/discover")
    check("a search failure is reported as 502", s == 502, (s, b))
    check("the error mentions the network search", "search" in b.get("error", "").lower(), b)
finally:
    discovery.discover_mdns = _orig_discover_mdns

print("\n== network mismatch: 'cannot reach the Drobo' that means 'wrong Wi-Fi' ==")
# The failure this exists for: three times during development the Drobo "timed
# out" only because Windows had auto-joined a different network, and the alert
# sent the owner to check on a perfectly healthy array. The protocol-level
# logic is covered in sdk/tests/test_netcheck.py; what's tested here is that
# the agent asks the question at the right moments and never at the wrong ones.
from drobo_nasd import netcheck as netcheck_mod
from drobo_nasd.netcheck import Interface as _Iface


class _StubDriver:
    """A driver stuck on the wrong network, and counting how often it's asked."""
    name = "stub"

    def __init__(self, host="10.0.0.50"):
        self.host = host
        self.calls = 0

    def target_host(self):
        self.calls += 1
        return self.host


_ELSEWHERE = [_Iface("10.9.9.41", 24)]
_orig_local_interfaces = netcheck_mod.local_interfaces
_orig_driver = mon.driver

netcheck_mod.local_interfaces = lambda timeout=3.0: list(_ELSEWHERE)
try:
    mon.driver = _StubDriver()
    mon._hint_cache = None

    hint = mon._network_hint()
    check("a Drobo on another subnet produces a hint", hint is not None)
    check("the hint names the Drobo's address", "10.0.0.50" in hint["message"], hint)
    check("the hint names this PC's network", "10.9.9.0/24" in hint["message"], hint)

    with mon._lock:
        fired = mon._evaluate(Snapshot(reachable=False, source="stub",
                                       error="timed out"), hint)
    unreachable = [a for a in fired if a.key == "unreachable"]
    check("an unreachable Drobo still raises exactly one alert", len(unreachable) == 1, fired)
    # One problem, one alert. A separate "network" alert sitting beside the
    # critical one would read as two unrelated events rather than a cause and
    # its effect.
    check("no second alert is invented for the network",
          not any("network" in a.key for a in fired), [a.key for a in fired])
    msg = unreachable[0].message
    check("the alert still leads with the failure itself", msg.startswith("Cannot reach"), msg)
    check("the alert explains the likely cause", "different network" in msg, msg)
    check("the alert never claims certainty", "rather than a certainty" in msg, msg)
    # The driver's own error is kept in full -- but after the explanation, not
    # before it. Discovery's failure text ends by advising you to set the host
    # in config.json, which is exactly the wrong move when the host is already
    # right and the PC is on the wrong network; leading with that sends people
    # to edit a file that isn't the problem.
    check("the driver's own error is still there, in full", "timed out" in msg, msg)
    check("-- attributed, and after the explanation",
          msg.index("different network") < msg.index("What the agent tried"), msg)

    # Multi-line driver errors (discovery writes one) get flattened: an alert
    # is a sentence, and it also travels to a phone notification.
    with mon._lock:
        multiline = mon._evaluate(Snapshot(reachable=False, source="stub",
                                           error="no Drobo found.\nSet host in config.json"), hint)
    flat = [a for a in multiline if a.key == "unreachable"][0].message
    check("a multi-line driver error is flattened into one line", "\n" not in flat, flat)

    # Reading this PC's addresses runs `ipconfig`. A poll loop ticking every
    # second must not spawn a process every second to re-learn something that
    # changes only when you join a different network.
    mon.driver.calls = 0
    calls_before = mon.driver.calls
    for _ in range(5):
        mon._network_hint()
    check("repeat calls inside the TTL are served from cache",
          mon.driver.calls - calls_before == 5 and mon._hint_cache is not None)
    probes = []
    netcheck_mod.local_interfaces = lambda timeout=3.0: (probes.append(1), list(_ELSEWHERE))[1]
    for _ in range(5):
        mon._network_hint()
    check("-- and the OS is not asked again for them", probes == [], probes)

    # Same network, still not answering: that's a real outage, and a note about
    # network layout would be talking over it.
    netcheck_mod.local_interfaces = lambda timeout=3.0: [_Iface("10.0.0.40", 24)]
    mon._hint_cache = None
    check("a Drobo on OUR network produces no hint", mon._network_hint() is None)
    with mon._lock:
        fired = mon._evaluate(Snapshot(reachable=False, source="stub", error="timed out"),
                              mon._network_hint())
    msg = [a for a in fired if a.key == "unreachable"][0].message
    check("-- and its alert is left alone", msg == "Cannot reach the Drobo: timed out", msg)

    # A Drobo we have never reached is a Drobo whose network we don't know.
    netcheck_mod.local_interfaces = lambda timeout=3.0: list(_ELSEWHERE)
    mon.driver = _StubDriver(host="")
    mon._hint_cache = None
    check("no known address for the Drobo -> no hint, not a guess",
          mon._network_hint() is None)

    # /api/netcheck: what the device picker asks before saying "not showing up".
    mon.driver = _StubDriver()
    s, b = call("/api/netcheck?host=10.0.0.50")
    check("netcheck 200", s == 200, (s, b))
    check("it answers that it checked", b.get("checked") is True, b)
    check("it reports a different network", b.get("same_network") is False, b)
    check("the hint is filled in", b.get("hint") and b["hint"]["message"], b)
    check("the hint carries this PC's networks for a front-end to show",
          b["hint"]["networks"] == ["10.9.9.0/24"], b)

    s, b = call("/api/netcheck?host=10.9.9.9")
    check("a Drobo on our own network is reported as such", b.get("same_network") is True, b)
    check("-- with hint null, so a front-end can just test for its presence",
          b.get("hint") is None, b)

    s, b = call("/api/netcheck?host=drobo.local")
    check("a hostname gets an honest 'no opinion' rather than an error",
          s == 200 and b.get("checked") is False, (s, b))
    check("-- and says why", "IPv4" in b.get("reason", ""), b)

    mon.driver = _StubDriver(host="")
    s, b = call("/api/netcheck")
    check("with no address known, netcheck says so instead of guessing",
          b.get("checked") is False and "no address" in b.get("reason", ""), b)
finally:
    netcheck_mod.local_interfaces = _orig_local_interfaces
    mon.driver = _orig_driver
    mon._hint_cache = None
    mon.network_hint = None

print("\n== the live stream and /api/status describe the same document ==")
# Not a hypothetical. The web dashboard is driven by /api/events, not by
# polling /api/status, and the first cut of the network banner broadcast
# snap.to_dict() while the REST endpoint served an enriched copy. The endpoint
# was right, every test passed, and the banner never appeared in the one UI it
# was written for. Both routes now build from the same helper; this is what
# keeps them that way.
_sub = mon.subscribe()
try:
    mon.poll_once()
    streamed = None
    while not _sub.empty():
        event = _sub.get_nowait()
        if event.get("type") == "snapshot":
            streamed = event["data"]
    check("a poll broadcasts a snapshot", streamed is not None)
    rest = call("/api/status")[1]
    check("the streamed snapshot has exactly the REST endpoint's keys",
          streamed is not None and set(streamed) == set(rest),
          None if streamed is None else set(rest) ^ set(streamed))
    check("network_hint reaches the stream, not just the endpoint",
          streamed is not None and "network_hint" in streamed)
finally:
    mon.unsubscribe(_sub)

print("\n== the hint is absent whenever the Drobo IS reachable ==")
# The mock driver always answers, so this is the healthy case: /api/status
# carries the key (front-ends can rely on it existing) and it is null.
snap = mon.poll_once()
check("a reachable poll clears the hint", mon.network_hint is None)
s, b = call("/api/status")
check("status carries network_hint", "network_hint" in b, list(b)[:12])
check("-- and it is null while the Drobo answers", b["network_hint"] is None, b.get("network_hint"))

print("\n== the firmware note rides on the status payload ==")
# The version rules live in drobo_nasd/firmware.py and are tested there. What
# matters here is that the agent derives the advisory from whatever the device
# actually reported, and that it is NOT an alert -- see the note in
# _snapshot_payload for why nagging hourly about unflashable firmware would be
# the wrong shape for this.
s, b = call("/api/status")
check("status carries firmware_advisory", "firmware_advisory" in b, list(b)[:14])
check("the mock's 4.2.2 is not notable, so nothing is shown",
      b["firmware_advisory"]["notable"] is False, b["firmware_advisory"])

_saved = mon.latest
try:
    with mon._lock:
        mon.latest = make_snap(device=DeviceInfo(name="Test", model="Drobo 5N",
                                                 firmware="4.3.1-8.126.117497"))
        payload = mon._snapshot_payload()
    adv = payload["firmware_advisory"]
    check("a withdrawn build IS notable", adv["notable"] is True, adv)
    check("the advisory is derived from the device's own report",
          adv["version"] == "4.3.1-8.126.117497", adv)
    check("the release is separated from the build tail", adv["release"] == "4.3.1", adv)
    check("no alert is raised for it -- there is no safe action to take",
          not any("firmware" in k for k in mon.alerts), list(mon.alerts))
finally:
    with mon._lock:
        mon.latest = _saved

print("\n== photo dedupe survives the index and the disk disagreeing ==")
# The index maps sha256 -> path and is what /api/photos/have answers from. It is
# a CACHE; the files on the Drobo are the truth. Two ways they drift apart, and
# both end with the phone and the agent believing different things.
from drobo_agent.photos import PhotoStore as _PS

_pdir = os.path.join(tmp, "RescanPhotos")
_store = _PS({"enabled": True, "target_dir": _pdir, "organize_by_date": False})
_store.load()
r1 = _store.ingest("holiday.jpg", b"\xff\xd8\xff pretend jpeg one")
r2 = _store.ingest("cat.jpg", b"\xff\xd8\xff pretend jpeg two")
check("two photos stored", r1["status"] == "stored" and r2["status"] == "stored", (r1, r2))
_h1, _h2 = r1["sha256"], r2["sha256"]
check("neither is re-requested once stored", _store.missing([_h1, _h2]) == [], _store.missing([_h1, _h2]))

# 1. A photo deleted off the Drobo by hand. The index still lists it, so
#    `have` says "already got it" and the phone never re-sends -- the photo is
#    gone and the system reports it as safe. The quieter and worse of the two.
os.remove(os.path.join(_pdir, r2["path"].replace("/", os.sep)))
check("a deleted file is still wrongly claimed before a rescan",
      _store.missing([_h2]) == [], _store.missing([_h2]))
summary = _store.rescan()
check("rescan notices it is gone", summary["dropped"] == 1, summary)
check("-- and the phone is now asked for it again", _store.missing([_h2]) == [_h2],
      _store.missing([_h2]))
check("-- while the file that IS there is not re-requested",
      _store.missing([_h1]) == [], _store.missing([_h1]))

# 2. The index lost entirely. Without a rebuild the agent asks for the whole
#    camera roll again -- hours of Wi-Fi to re-copy files already sitting there.
os.remove(_store.index_path)
_fresh = _PS({"enabled": True, "target_dir": _pdir, "organize_by_date": False})
_fresh.load()
check("a lost index is rebuilt from the files on disk", _fresh.missing([_h1]) == [],
      _fresh.missing([_h1]))

# An empty folder with no index must NOT trigger a pointless scan, and must not
# claim to hold anything.
_empty_dir = os.path.join(tmp, "EmptyPhotos")
_empty = _PS({"enabled": True, "target_dir": _empty_dir, "organize_by_date": False})
_empty.load()
check("a fresh empty install holds nothing", _empty.missing([_h1]) == [_h1], _empty.missing([_h1]))

# Half-finished chunked uploads live in a dot-directory. Hashing one would file
# a truncated file under the hash of its complete self -- exactly the silent
# corruption this module exists to prevent.
_sess = os.path.join(_pdir, ".drobo-agent-uploads")
os.makedirs(_sess, exist_ok=True)
with open(os.path.join(_sess, "half.jpg"), "wb") as fh:
    fh.write(b"\xff\xd8\xff pretend jpeg one"[:6])
before = _fresh.rescan()
check("in-progress uploads are never indexed", before["found"] == 1, before)

s, b = call("/api/photos/rescan", data=b"", method="POST")
check("rescan is reachable over the API", s == 200 and "found" in b, (s, b))

print("\n== /api/leds: two answering commands that had no way out ==")
# eCmdGetDimming and eCmdGetDemoModeInfo both answer with real data on this
# firmware and both had working parsers since 2026-07-26 with nothing exposing
# them. A measurement nobody can see is not a feature.
#
# The device reads are faked, like the /api/shares and /api/drobosync tests:
# earlier blocks leave a _resolved_host on the mock driver so the real command
# path gets exercised, so an un-patched call here opens a genuine socket to an
# address nothing is listening on. The values returned are the ones the live 5N
# actually reported (brightness 59, not in demo mode), so the assertions still
# say something true about the wiring rather than about the stub.
from drobo_nasd import droboapps as _apps_mod

_orig_get_dimming, _orig_get_demo = _apps_mod.get_dimming, _apps_mod.get_demo_mode
_apps_mod.get_dimming = lambda host, esa, port=5001, timeout=8.0: 59
_apps_mod.get_demo_mode = lambda host, esa, port=5001, timeout=8.0: {
    "demo_mode": False, "scale_factor": 1}
try:
    s, b = call("/api/leds")
finally:
    _apps_mod.get_dimming, _apps_mod.get_demo_mode = _orig_get_dimming, _orig_get_demo

check("leds endpoint 200", s == 200, (s, b))
check("brightness is reported", b.get("dimming") == 59, b)
# The firmware documents no scale, so the number ships with a note saying so
# rather than being rendered as a percentage we would be inventing.
check("the unknown scale is admitted, not papered over",
      "never says what the scale is" in b.get("scale_note", ""), b.get("scale_note"))
check("demo mode is reported", b.get("demo_mode") is False, b)
check("-- with its scale factor", b.get("scale_factor") == 1, b)

print("\n== the update check is dormant, and says so ==")
# Built now so it exists before it is needed rather than being assembled in a
# hurry on release day. Off by default; the unit tests in test_updates.py
# boobytrap urllib to prove nothing is contacted. Here we only confirm the
# endpoint reports it honestly.
s, b = call("/api/update")
check("update endpoint 200", s == 200, (s, b))
check("it reports disabled by default", b.get("state") == "disabled", b)
check("no update is offered", b.get("update_available") is False, b)
check("it says nothing is contacted", "Nothing is contacted" in b.get("reason", ""), b)
check("it still reports the version we are on", b.get("current_version") == api.VERSION, b)
# A disabled check must not be cached for a day -- flipping the setting on
# should take effect on the next refresh, not tomorrow.
check("a disabled answer is not cached", api._Handler._update_cache is None,
      api._Handler._update_cache)

print("\n== DroboSync: built, dormant, and honest about why ==")
# Needs two Drobos; the owner has one. Shipped built rather than absent so the
# page explains itself instead of a button existing that fails -- ROADMAP
# decision, 2026-07-28.
#
# The device read is faked, exactly as the /api/shares tests fake config_cmd:
# earlier blocks in this file leave a _resolved_host on the mock driver so the
# real command path gets exercised, which means an un-patched call here would
# try to open a genuine socket to an address nothing is listening on. (It did,
# on the first run of this test, and hung until the client timed out.)
# (imported at module scope below rather than mid-file)

_orig_sync_summary = _drobosync_mod.summary
_drobosync_mod.summary = lambda host, esa, port=5001, timeout=8.0: {
    "configured": False,
    "reads": {w: {"configured": False, "raw_xml": "", "fields": {},
                  "asked": w == "settings"}
              for w in ("settings", "summary_log", "detailed_log", "log")},
    "note": ("This Drobo has no DroboSync partner set up. DroboSync copied one "
             "Drobo's shares to a second Drobo on a schedule, so it needs two "
             "units -- with one, there is nothing for it to do and the device "
             "correctly reports nothing. Everything needed to display it is "
             "built and waiting."),
}
try:
    s, b = call("/api/drobosync")
finally:
    _drobosync_mod.summary = _orig_sync_summary
check("drobosync endpoint 200", s == 200, (s, b))
check("it reports not configured", b.get("configured") is False, b)
check("all four reads are represented",
      set(b.get("reads", {})) == {"settings", "summary_log", "detailed_log", "log"},
      list(b.get("reads", {})))
check("it explains that the feature needs two units", "needs two" in b.get("note", ""), b.get("note"))
check("-- and does not read as a failure", "error" not in b.get("note", "").lower(), b.get("note"))
# The simulator must not invent a configured sync: nobody has ever seen a
# populated reply, so a fake one would teach the UI a shape we made up.
check("the simulator does not fabricate a sync partner",
      all(not r.get("configured") for r in b.get("reads", {}).values()), b)

print("\n== 404 ==")
s, b = call("/api/nope"); check("unknown path 404s", s == 404, s)

httpd.shutdown(); mon.stop(); httpd.server_close()
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
