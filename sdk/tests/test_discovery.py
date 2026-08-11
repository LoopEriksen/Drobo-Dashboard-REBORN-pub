"""
Offline tests for finding the Drobo without a static IP (nasd/discovery.py).

Everything here runs without a LAN or a real device: mDNS packets are built by
hand, and "the Drobo" for identity-verification tests is a local TCP server
that plays back the real captured greeting at sdk/tests/fixtures/esatm-sample.xml
(esa_id "FAKESERIALFAKES"). The one exception -- a live end-to-end check
against real hardware, at the address in the DROBO_LIVE_HOST environment
variable -- SKIPs gracefully when no device answers, so this suite passes on a machine with no Drobo too.

    py test_discovery.py

Run this after changing anything under drobo_nasd/discovery.py or the
identity bits of nasd/esatm.py.
"""
import contextlib
import os
import socket
import struct
import sys
import threading
import warnings

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import discovery, esatm

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "esatm-sample.xml")
SAMPLE_ESA_ID = "FAKESERIALFAKES"
# Overridable so no real address needs to live in this file. The default is a
# placeholder; the live check simply SKIPs unless a device answers there.
LIVE_HOST = os.environ.get("DROBO_LIVE_HOST", "10.0.0.50")
LIVE_PORT = 5000

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


PAYLOAD = open(FIXTURE, "rb").read()


# ---------------------------------------------------------------------------
# A tiny fake nasd: serve one DRINASD greeting on localhost, exactly like the
# real device does unprompted on connect, so verify_candidate/resolve() can
# be exercised end-to-end through the real socket path.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def fake_drobo(payload: bytes):
    frame = esatm.SIGNATURE + b"\x01\x01\x00\x00" + struct.pack(">I", len(payload)) + payload
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()

    def serve():
        srv.settimeout(2.0)
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.sendall(frame)
        except OSError:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield host, port
    finally:
        srv.close()
        t.join(timeout=2.0)


# ---------------------------------------------------------------------------
print("\n== verify_candidate: identity match ==")
# ---------------------------------------------------------------------------

with fake_drobo(PAYLOAD) as (host, port):
    cand = discovery.Candidate(host, port, "test")
    verified = discovery.verify_candidate(cand, expected_esa_id=SAMPLE_ESA_ID)
    check("match returns Verified", isinstance(verified, discovery.Verified))
    check("match esa_id correct", verified.identity["esa_id"] == SAMPLE_ESA_ID, verified.identity["esa_id"])
    check("match is not flagged first_run", verified.first_run is False)
    check("identity carries disk_pack_id", verified.identity["disk_pack_id"] == "0000000000000000",
          verified.identity["disk_pack_id"])
    check("identity carries name", verified.identity["name"] == "TestDrobo", verified.identity["name"])
    check("identity carries model", verified.identity["model"] == "Drobo 5N", verified.identity["model"])
    check("identity carries firmware", verified.identity["firmware"].startswith("4.3.1"),
          verified.identity["firmware"])

# ---------------------------------------------------------------------------
print("\n== verify_candidate: identity mismatch is REFUSED ==")
# ---------------------------------------------------------------------------

with fake_drobo(PAYLOAD) as (host, port):
    cand = discovery.Candidate(host, port, "test")
    try:
        discovery.verify_candidate(cand, expected_esa_id="drb-not-the-real-one")
        check("mismatch raises IdentityMismatch", False)
    except discovery.IdentityMismatch as exc:
        check("mismatch raises IdentityMismatch", True)
        check("mismatch message names both ids",
              "drb-not-the-real-one" in str(exc) and SAMPLE_ESA_ID in str(exc), str(exc))

# ---------------------------------------------------------------------------
print("\n== verify_candidate: first run, no expectation configured ==")
# ---------------------------------------------------------------------------

with fake_drobo(PAYLOAD) as (host, port):
    cand = discovery.Candidate(host, port, "test")
    verified = discovery.verify_candidate(cand, expected_esa_id="")
    check("first run accepts and reports identity", verified.first_run is True)
    check("first run still reports the real esa_id", verified.identity["esa_id"] == SAMPLE_ESA_ID,
          verified.identity["esa_id"])

# ---------------------------------------------------------------------------
print("\n== resolve(): end-to-end refuse-on-mismatch (the safety property) ==")
# ---------------------------------------------------------------------------

_orig_mdns, _orig_hostname, _orig_sweep = discovery.discover_mdns, discovery.resolve_hostname, discovery.sweep_subnet
discovery.discover_mdns = lambda timeout=3.0: []
discovery.resolve_hostname = lambda hostname, port=esatm.DEFAULT_PORT: None
try:
    with fake_drobo(PAYLOAD) as (host, port):
        try:
            discovery.resolve(expected_esa_id="drb-someone-elses-drobo",
                               last_known_ip=host, port=port, mdns_timeout=0.1)
            check("resolve() refuses a mismatched device end-to-end", False)
        except discovery.IdentityMismatch:
            check("resolve() refuses a mismatched device end-to-end", True)

    print("\n== resolve(): end-to-end first-run pairing ==")
    with fake_drobo(PAYLOAD) as (host, port):
        verified = discovery.resolve(expected_esa_id="", last_known_ip=host, port=port, mdns_timeout=0.1)
        check("resolve() accepts and reports on first run", verified.first_run is True)
        check("resolve() found it via last-known", verified.candidate.source == "last-known",
              verified.candidate.source)
        check("resolve() reports the real esa_id", verified.identity["esa_id"] == SAMPLE_ESA_ID,
              verified.identity["esa_id"])

    print("\n== resolve(): nothing found at all ==")
    try:
        discovery.resolve(expected_esa_id="", last_known_ip="", port=59999, mdns_timeout=0.1)
        check("resolve() with no candidates raises LookupError", False)
    except discovery.IdentityMismatch:
        check("resolve() with no candidates raises LookupError, not IdentityMismatch", False)
    except LookupError:
        check("resolve() with no candidates raises LookupError", True)
finally:
    discovery.discover_mdns, discovery.resolve_hostname, discovery.sweep_subnet = _orig_mdns, _orig_hostname, _orig_sweep


# ---------------------------------------------------------------------------
print("\n== mDNS record parsing against synthetic packets (no LAN) ==")
# ---------------------------------------------------------------------------


def _name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        if label:
            out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\x00"


def _rr(name: str, rtype: int, rdata: bytes) -> bytes:
    return _name(name) + struct.pack("!HHIH", rtype, 1, 120, len(rdata)) + rdata


srv_rdata = struct.pack("!HHH", 0, 0, 5000) + _name("MyDrobo.local")
a_rdata = socket.inet_aton("10.0.0.50")
packet = (
    struct.pack("!HHHHHH", 0, 0x8400, 0, 2, 0, 0)
    + _rr("MyDrobo._nasd._tcp.local", discovery.QTYPE_SRV, srv_rdata)
    + _rr("MyDrobo.local", discovery.QTYPE_A, a_rdata)
)
records = discovery.parse_mdns_response(packet)
srv_recs = [r for r in records if r["type"] == discovery.QTYPE_SRV]
a_recs = [r for r in records if r["type"] == discovery.QTYPE_A]
check("two records parsed", len(records) == 2, records)
check("SRV record extracted with port", srv_recs and srv_recs[0]["port"] == 5000, srv_recs)
check("SRV record target name decoded", srv_recs and srv_recs[0]["value"] == "MyDrobo.local", srv_recs)
check("A record extracted", a_recs and a_recs[0]["value"] == "10.0.0.50", a_recs)

# name compression: a second record whose name is a pointer back into the packet
compressed_ptr = struct.pack("!H", 0xC000 | 12)  # points at offset 12, the first record's name
packet2 = (
    struct.pack("!HHHHHH", 0, 0x8400, 0, 2, 0, 0)
    + _rr("MyDrobo._nasd._tcp.local", discovery.QTYPE_SRV, srv_rdata)
    + compressed_ptr + struct.pack("!HHIH", discovery.QTYPE_A, 1, 120, len(a_rdata)) + a_rdata
)
records2 = discovery.parse_mdns_response(packet2)
check("compressed name pointer resolves", len(records2) == 2 and records2[1]["name"] == "MyDrobo._nasd._tcp.local",
      records2)

# a short/garbage packet must not raise
check("garbage packet yields no records, no crash", discovery.parse_mdns_response(b"\x00" * 4) == [])

# ---------------------------------------------------------------------------
print("\n== discovery ordering: mDNS, hostname, last-known, sweep (opt-in) ==")
# ---------------------------------------------------------------------------

_orig_mdns, _orig_hostname, _orig_sweep = discovery.discover_mdns, discovery.resolve_hostname, discovery.sweep_subnet
discovery.discover_mdns = lambda timeout=3.0: [discovery.Candidate("10.0.0.5", 5000, "mdns")]
discovery.resolve_hostname = lambda hostname, port=esatm.DEFAULT_PORT: (
    discovery.Candidate("10.0.0.6", port, "hostname") if hostname else None
)
discovery.sweep_subnet = lambda **kw: [discovery.Candidate("10.0.0.7", 5000, "sweep")]
try:
    cands = discovery._gather_candidates(hostname="drobo.local", last_known_ip="10.0.0.9",
                                          try_sweep=False, mdns_timeout=0.1)
    check("sweep excluded when not opted in", all(c.source != "sweep" for c in cands), cands)
    check("order is mdns, hostname, last-known",
          [c.source for c in cands] == ["mdns", "hostname", "last-known"],
          [c.source for c in cands])

    cands_sweep = discovery._gather_candidates(hostname="drobo.local", last_known_ip="10.0.0.9",
                                                try_sweep=True, mdns_timeout=0.1)
    check("sweep included and last when opted in",
          [c.source for c in cands_sweep] == ["mdns", "hostname", "last-known", "sweep"],
          [c.source for c in cands_sweep])

    cands_last_only = discovery._gather_candidates(hostname="", last_known_ip="10.0.0.9",
                                                     try_sweep=False, mdns_timeout=0.1)
    check("last-known IP is tried even with no hostname",
          any(c.host == "10.0.0.9" and c.source == "last-known" for c in cands_last_only),
          cands_last_only)

    discovery.discover_mdns = lambda timeout=3.0: [discovery.Candidate("10.0.0.9", 5000, "mdns")]
    cands_dedup = discovery._gather_candidates(hostname="", last_known_ip="10.0.0.9",
                                                try_sweep=False, mdns_timeout=0.1)
    check("duplicate host:port collapsed, higher-priority source kept",
          len(cands_dedup) == 1 and cands_dedup[0].source == "mdns", cands_dedup)

    # -- addresses somebody typed, because mDNS doesn't work on their network --
    discovery.discover_mdns = lambda timeout=3.0: [discovery.Candidate("10.0.0.5", 5000, "mdns")]
    manual = discovery._gather_candidates(hostname="drobo.local", last_known_ip="10.0.0.9",
                                          try_sweep=False, mdns_timeout=0.1,
                                          manual_ips=["10.0.0.77"])
    check("a typed address is tried FIRST, ahead of the multicast browse",
          [c.source for c in manual] == ["manual", "mdns", "hostname", "last-known"],
          [c.source for c in manual])
    # The reason for that order: on a network that filters multicast, mDNS
    # never answers, and making someone wait out a dead browse on every poll
    # when they have told us exactly where the device is would be perverse.
    check("the typed address is the one that was typed",
          manual[0].host == "10.0.0.77", manual[0])

    blanks = discovery._gather_candidates(try_sweep=False, mdns_timeout=0.1,
                                          manual_ips=["  ", "", "10.0.0.77", None])
    check("blank and None entries are dropped rather than probed",
          [c.host for c in blanks if c.source == "manual"] == ["10.0.0.77"], blanks)
    check("whitespace around a typed address is trimmed",
          all(c.host == c.host.strip() for c in blanks))

    dup = discovery._gather_candidates(try_sweep=False, mdns_timeout=0.1,
                                       manual_ips=["10.0.0.5"])
    check("a typed address that mDNS also found is not probed twice",
          len([c for c in dup if c.host == "10.0.0.5"]) == 1, dup)
    check("-- and 'manual' wins, since it was asked for explicitly",
          [c for c in dup if c.host == "10.0.0.5"][0].source == "manual", dup)

    none_given = discovery._gather_candidates(try_sweep=False, mdns_timeout=0.1)
    check("no manual_ips changes nothing", all(c.source != "manual" for c in none_given))
finally:
    discovery.discover_mdns, discovery.resolve_hostname, discovery.sweep_subnet = _orig_mdns, _orig_hostname, _orig_sweep


# ---------------------------------------------------------------------------
print("\n== sweep_subnet: time-bounded and off unless requested ==")
# ---------------------------------------------------------------------------

import time as _time
_t0 = _time.time()
swept = discovery.sweep_subnet(cidr="198.51.100.0/28", port=59998, time_budget=1.0, workers=16)
_elapsed = _time.time() - _t0
check("sweep of a TEST-NET-2 /28 finds nothing (nobody there)", swept == [], swept)
check("sweep stays roughly within its time budget", _elapsed < 6.0, _elapsed)


# ---------------------------------------------------------------------------
print("\n== MAC/ARP corroboration: parsing realistic captured arp output (no LAN) ==")
# ---------------------------------------------------------------------------

WINDOWS_ARP_A = """
Interface: 10.0.0.50 --- 0x9
  Internet Address      Physical Address      Type
  10.0.0.1            aa-bb-cc-dd-ee-01     dynamic
  10.0.0.50           e0-ab-cd-12-34-56     dynamic
  169.254.7.77          ff-ee-dd-cc-bb-aa     dynamic
  224.0.0.22             01-00-5e-00-00-16     static
"""

UNIX_ARP_N = """
Address                  HWtype  HWaddress           Flags Mask            Iface
10.0.0.1              ether   aa:bb:cc:dd:ee:01   C                     eth0
10.0.0.50             ether   e0:ab:cd:12:34:56   C                     eth0
"""

UNIX_ARP_A = """
? (10.0.0.1) at aa:bb:cc:dd:ee:01 [ether] on eth0
? (10.0.0.50) at e0:ab:cd:12:34:56 [ether] on eth0
"""

win_table = discovery.parse_arp_table(WINDOWS_ARP_A)
check("windows arp -a: real Drobo IP maps to its MAC",
      win_table.get("10.0.0.50") == "e0:ab:cd:12:34:56", win_table)
check("windows arp -a: dash-separated MAC normalized to colons",
      ":" in win_table.get("10.0.0.50", ""), win_table.get("10.0.0.50"))
check("windows arp -a: header/interface line contributes no bogus entry",
      len(win_table) == 4, win_table)

unix_n_table = discovery.parse_arp_table(UNIX_ARP_N)
check("unix arp -n: real Drobo IP maps to its MAC",
      unix_n_table.get("10.0.0.50") == "e0:ab:cd:12:34:56", unix_n_table)

unix_a_table = discovery.parse_arp_table(UNIX_ARP_A)
check("unix arp -a ('? (ip) at mac' form): real Drobo IP maps to its MAC",
      unix_a_table.get("10.0.0.50") == "e0:ab:cd:12:34:56", unix_a_table)

check("garbage arp text yields empty table, no crash", discovery.parse_arp_table("nonsense\n\n") == {})

# ---------------------------------------------------------------------------
print("\n== MAC/ARP corroboration: absent/unreadable table degrades gracefully ==")
# ---------------------------------------------------------------------------

_orig_read_arp = discovery._read_arp_table_text
discovery._read_arp_table_text = lambda timeout=2.0: None
try:
    result = discovery.mac_for_ip("10.0.0.50")
    check("mac_for_ip returns None (not an exception) when arp is unreadable", result is None, result)
finally:
    discovery._read_arp_table_text = _orig_read_arp


def _raising_read_arp(timeout=2.0):
    raise AssertionError("should never be called directly by _read_arp_table_text callers")


# _read_arp_table_text itself must swallow subprocess failure, not just callers of it.
import subprocess as _subprocess  # noqa: E402  (kept local to this check, mirrors module usage)

_orig_run = _subprocess.run
_subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("no arp on PATH"))
try:
    text = discovery._read_arp_table_text(timeout=0.5)
    check("no 'arp' binary on PATH does not raise (Windows path degrades to None)",
          text is None or isinstance(text, str))
finally:
    _subprocess.run = _orig_run

# ---------------------------------------------------------------------------
print("\n== verify_candidate: expected_mac REFUSES on mismatch, WARNS when unknown ==")
# ---------------------------------------------------------------------------

_orig_mac_for_ip = discovery.mac_for_ip

discovery.mac_for_ip = lambda ip, timeout=2.0: "aa:bb:cc:dd:ee:ff"  # "observed" MAC, deliberately wrong
try:
    with fake_drobo(PAYLOAD) as (host, port):
        cand = discovery.Candidate(host, port, "test")
        try:
            discovery.verify_candidate(cand, expected_esa_id=SAMPLE_ESA_ID, expected_mac="11:22:33:44:55:66")
            check("MAC mismatch raises IdentityMismatch even though esa_id matched", False)
        except discovery.IdentityMismatch as exc:
            check("MAC mismatch raises IdentityMismatch even though esa_id matched", True)
            check("MAC mismatch message names both expected and observed MAC",
                  "11:22:33:44:55:66" in str(exc) and "aa:bb:cc:dd:ee:ff" in str(exc), str(exc))
finally:
    discovery.mac_for_ip = _orig_mac_for_ip

discovery.mac_for_ip = lambda ip, timeout=2.0: None  # ARP table had nothing for this host
try:
    with fake_drobo(PAYLOAD) as (host, port):
        cand = discovery.Candidate(host, port, "test")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            verified = discovery.verify_candidate(cand, expected_esa_id=SAMPLE_ESA_ID,
                                                    expected_mac="11:22:33:44:55:66")
            check("expected_mac set but MAC unknown does NOT refuse", isinstance(verified, discovery.Verified))
            check("expected_mac set but MAC unknown still confirms esa_id",
                  verified.first_run is False)
            check("expected_mac set but MAC unknown emits a warning, not silence",
                  any("MAC" in str(w.message) for w in caught), [str(w.message) for w in caught])
finally:
    discovery.mac_for_ip = _orig_mac_for_ip

# ---------------------------------------------------------------------------
print("\n== address sanity: link-local ranks last even when higher-priority source ==")
# ---------------------------------------------------------------------------

_orig_mdns, _orig_hostname, _orig_sweep = discovery.discover_mdns, discovery.resolve_hostname, discovery.sweep_subnet
# mDNS (normally highest priority) hands back the link-local address; the
# routable one only shows up as the lower-priority last-known-good IP.
discovery.discover_mdns = lambda timeout=3.0: [discovery.Candidate("169.254.7.77", 5000, "mdns")]
discovery.resolve_hostname = lambda hostname, port=esatm.DEFAULT_PORT: None
try:
    ranked = discovery._gather_candidates(hostname="", last_known_ip="10.0.0.50",
                                           try_sweep=False, mdns_timeout=0.1)
    check("routable address is ranked before link-local, despite lower source priority",
          [c.host for c in ranked] == ["10.0.0.50", "169.254.7.77"],
          [c.host for c in ranked])
finally:
    discovery.discover_mdns, discovery.resolve_hostname, discovery.sweep_subnet = _orig_mdns, _orig_hostname, _orig_sweep

# ---------------------------------------------------------------------------
print("\n== allowed_subnets: rejects an out-of-range candidate before connecting ==")
# ---------------------------------------------------------------------------

# The two addresses must be in genuinely different /24s, or this test passes
# without testing anything. Using the documentation range (RFC 5737) for the
# out-of-range one makes that impossible to break by a search-and-replace.
in_range = discovery.Candidate("10.0.0.50", 5000, "test")
out_of_range = discovery.Candidate("203.0.113.5", 5000, "test")
allowed, rejected = discovery.filter_allowed_subnets([in_range, out_of_range], ["10.0.0.0/24"])
check("in-range candidate kept", allowed == [in_range], allowed)
check("out-of-range candidate rejected", rejected == [out_of_range], rejected)

no_restriction_allowed, no_restriction_rejected = discovery.filter_allowed_subnets(
    [in_range, out_of_range], None
)
check("no allowed_subnets configured means nothing is rejected",
      no_restriction_allowed == [in_range, out_of_range] and no_restriction_rejected == [],
      (no_restriction_allowed, no_restriction_rejected))

# end-to-end via resolve(): the fake Drobo listens on 127.0.0.1, which is
# outside the allowed range, so resolve() must refuse it WITHOUT ever
# connecting -- if it had connected it would return Verified, not raise.
_orig_mdns, _orig_hostname, _orig_sweep = discovery.discover_mdns, discovery.resolve_hostname, discovery.sweep_subnet
discovery.discover_mdns = lambda timeout=3.0: []
discovery.resolve_hostname = lambda hostname, port=esatm.DEFAULT_PORT: None
try:
    with fake_drobo(PAYLOAD) as (host, port):
        try:
            discovery.resolve(expected_esa_id=SAMPLE_ESA_ID, last_known_ip=host, port=port,
                               mdns_timeout=0.1, allowed_subnets=["10.0.0.0/24"])
            check("resolve() refuses an out-of-subnet candidate end-to-end", False)
        except discovery.IdentityMismatch as exc:
            check("resolve() refuses an out-of-subnet candidate end-to-end", True)
            check("subnet-refusal message names the allowed range and the offending host",
                  "10.0.0.0/24" in str(exc) and host in str(exc), str(exc))
finally:
    discovery.discover_mdns, discovery.resolve_hostname, discovery.sweep_subnet = _orig_mdns, _orig_hostname, _orig_sweep


# ---------------------------------------------------------------------------
print("\n== live device (optional) ==")
# ---------------------------------------------------------------------------

try:
    probe = socket.create_connection((LIVE_HOST, LIVE_PORT), timeout=1.5)
    probe.close()
    live_available = True
except OSError:
    live_available = False

if not live_available:
    print(f"  SKIP    no Drobo answered at {LIVE_HOST}:{LIVE_PORT} -- "
          "fine on a machine with no Drobo on the LAN")
else:
    # First-run pairing against the real device: pass no expected id, since
    # hardcoding this unit's serial would both fail on anyone else's Drobo and
    # bake a real device serial into the repo (see ROADMAP.md Phase 6).
    cand = discovery.Candidate(LIVE_HOST, LIVE_PORT, "config")
    verified = discovery.verify_candidate(cand, expected_esa_id="", timeout=5.0)
    live_id = verified.identity["esa_id"]
    check("live device returns a plausible esa_id",
          isinstance(live_id, str) and len(live_id) > 4, live_id)
    check("live device reports a model", bool(verified.identity["model"]),
          verified.identity["model"])

    # And the safety property that actually matters, proven against real
    # hardware rather than a fake server: a wrong expectation is refused.
    try:
        discovery.verify_candidate(cand, expected_esa_id="drb-definitely-not-this",
                                   timeout=5.0)
        check("live device refuses a wrong expected esa_id", False,
              "it accepted a serial that does not match")
    except discovery.IdentityMismatch:
        check("live device refuses a wrong expected esa_id", True)


print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
