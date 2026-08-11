"""
Finding the Drobo without a static IP.

config.json can hardcode a "host", but DHCP leases change: unplug the Drobo,
plug it back in, or reboot the router, and the address it gets next time is
not guaranteed to be the same one. Rather than ask the owner to fight their
router into handing out a static lease, this module finds the device the same
ways the real Drobo Dashboard would, and -- because "found a Drobo" is not the
same thing as "found *my* Drobo" -- proves the candidate's identity before the
driver trusts it.

Discovery, cheapest/most-specific first:
    a) mDNS browse for "_nasd._tcp" (discover_mdns)
    b) resolve a known hostname, e.g. "MyDrobo.local" (resolve_hostname)
    c) a last-known-good IP the caller remembers from before (just a Candidate)
    d) OPTIONAL last resort: sweep the local /24 for an open nasd port
       (sweep_subnet) -- off unless the caller opts in, because it is the
       slowest and least specific way to find anything.

Identity verification (verify_candidate) is what turns "something answered on
port 5000" into "this is definitely (or definitely not) the Drobo we mean".

HONESTY NOTE, because it matters more than the code: nasd has no
authentication and no encryption. Anything on the LAN can open port 5000 and
read the greeting, and anything can *serve* a fake greeting with any mESAID
it likes -- mDNS is equally unauthenticated. So none of esa_id pinning, MAC
corroboration, or allowed_subnets below is a security boundary. They are
defense-in-depth CORRECTNESS controls: they catch DHCP surprises, wrong-IP
misconfiguration, and clumsy accidents, and they raise the bar for a
deliberate LAN attacker from "trivial" to "has to also fake an address and/or
a MAC". A hostile party already on the LAN does not need to spoof any of
this, because the Drobo itself has zero authentication -- the realistic risk
here is deception (fake health readings reaching the dashboard), not data
theft, and this agent never writes to the device. Comments below repeat this
where it is easy to forget.

The mDNS piece (_encode_name, _decode_name, _build_query, parse_mdns_response,
discover_mdns) is implemented from the specifications, not borrowed from any
library: DNS message format and name compression are RFC 1035 section 4.1,
multicast DNS is RFC 6762. Stdlib socket/struct only -- no dnspython, no
zeroconf.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import subprocess
import sys
import time
import warnings
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import esatm

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
NASD_SERVICE = "_nasd._tcp.local"

# DNS RR types we care about. Same numbering the real protocol uses -- see
# tools/drobo_probe.py's mDNS client, which this borrows the wire-format
# approach from (no dependency pulled in; it's ~50 lines of stdlib code).
QTYPE_A = 1
QTYPE_PTR = 12
QTYPE_SRV = 33


# ---------------------------------------------------------------------------
# Minimal mDNS client -- query encoding and response decoding.
# ---------------------------------------------------------------------------


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        if label:
            out += bytes([len(label)]) + label.encode("utf-8")
    return out + b"\x00"


def _decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed, RFC 1035 sec 4.1.4) DNS name."""
    labels: list[str] = []
    jumped = False
    resume = offset
    guard = 0
    while offset < len(data) and guard < 128:
        guard += 1
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = struct.unpack("!H", data[offset : offset + 2])[0] & 0x3FFF
            if not jumped:
                resume = offset + 2
            jumped = True
            offset = pointer
            continue
        offset += 1
        labels.append(data[offset : offset + length].decode("utf-8", "replace"))
        offset += length
    return ".".join(labels), (resume if jumped else offset)


def _build_query(qname: str, qtype: int = QTYPE_PTR) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    # qclass 0x8001 = IN with the "unicast response please" bit set, so a
    # responder answers straight back to us instead of multicasting.
    return header + _encode_name(qname) + struct.pack("!HH", qtype, 0x8001)


def parse_mdns_response(data: bytes) -> list[dict]:
    """
    Pull PTR/SRV/A records out of one mDNS/DNS response packet.

    Public (not underscore-prefixed) on purpose: tests build synthetic
    packets and hand them straight to this function, so the parsing logic is
    exercised without opening a socket or touching a LAN.

    Each record is {"name": ..., "type": <int qtype>, "value": ...}; SRV
    records additionally carry "port".
    """
    records: list[dict] = []
    if len(data) < 12:
        return records
    _id, _flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(qd):
        _, offset = _decode_name(data, offset)
        offset += 4  # qtype + qclass
    for _ in range(an + ns + ar):
        if offset >= len(data):
            break
        name, offset = _decode_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlen]
        rec: dict = {"name": name, "type": rtype}
        if rtype == QTYPE_PTR:
            rec["value"], _ = _decode_name(data, offset)
        elif rtype == QTYPE_A and rdlen == 4:
            rec["value"] = socket.inet_ntoa(rdata)
        elif rtype == QTYPE_SRV and rdlen >= 6:
            _pri, _wt, port = struct.unpack("!HHH", rdata[:6])
            target, _ = _decode_name(data, offset + 6)
            rec["value"] = target
            rec["port"] = port
        records.append(rec)
        offset += rdlen
    return records


@dataclass
class Candidate:
    """One address worth trying, and how we found it (for logging/debugging)."""

    host: str
    port: int = esatm.DEFAULT_PORT
    source: str = ""  # "mdns" | "hostname" | "last-known" | "sweep"


def discover_mdns(timeout: float = 3.0) -> list[Candidate]:
    """
    a) Browse for _nasd._tcp on the LAN.

    Whoever answers a _nasd._tcp query is, by definition, advertising nasd --
    so the reply's source address is always a valid candidate, even if the
    packet's own SRV/A records name a different interface. Any A record in
    the same reply is also collected, since real Drobo firmware (Avahi)
    includes one for the SRV target.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    found: dict[str, Candidate] = {}
    try:
        try:
            sock.sendto(_build_query(NASD_SERVICE, QTYPE_PTR), (MDNS_ADDR, MDNS_PORT))
        except OSError:
            return []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            records = parse_mdns_response(data)
            if not records:
                continue
            port = esatm.DEFAULT_PORT
            for rec in records:
                if rec["type"] == QTYPE_SRV:
                    port = rec["port"]
            found.setdefault(addr[0], Candidate(addr[0], port, "mdns"))
            for rec in records:
                if rec["type"] == QTYPE_A:
                    found.setdefault(rec["value"], Candidate(rec["value"], port, "mdns"))
    finally:
        sock.close()
    return list(found.values())


def _is_link_local(host: str) -> bool:
    """
    True for APIPA/link-local addresses (169.254.0.0/16, fe80::/10).

    A link-local address is what a NIC gives itself when DHCP fails -- it
    still answers on the LAN segment, but it is not a routable identity worth
    preferring, and a device advertising both a routable and a link-local
    address (as the 5N does over multiple interfaces/mDNS records) should
    always be reached via the routable one. Non-IP strings (shouldn't happen
    in practice -- see resolve_hostname, which already resolves to an IP)
    are treated as "not link-local" rather than raising.
    """
    try:
        return ipaddress.ip_address(host).is_link_local
    except ValueError:
        return False


def resolve_hostname(hostname: str, port: int = esatm.DEFAULT_PORT) -> Candidate | None:
    """b) Resolve a known name, e.g. 'MyDrobo.local', via the OS resolver."""
    if not hostname:
        return None
    try:
        ip = socket.gethostbyname(hostname)
    except OSError:
        return None
    return Candidate(ip, port, "hostname")


def _local_ipv4() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def sweep_subnet(
    cidr: str | None = None,
    port: int = esatm.DEFAULT_PORT,
    time_budget: float = 3.0,
    workers: int = 256,
) -> list[Candidate]:
    """
    d) OPTIONAL last resort: knock on `port` across the local /24.

    Concurrent and time-bounded -- the per-host timeout is sized so that,
    even in the worst case (every host silently drops the packet), the whole
    sweep finishes within roughly `time_budget` seconds. The caller has to
    opt in (see resolve()'s try_sweep); this is never run automatically.
    """
    if cidr is None:
        cidr = str(ipaddress.ip_network(_local_ipv4() + "/24", strict=False))
    net = ipaddress.ip_network(cidr, strict=False)
    ips = [str(h) for h in net.hosts()]
    if not ips:
        return []
    rounds = max(1, -(-len(ips) // workers))  # ceil(len(ips) / workers)
    per_host_timeout = max(0.05, time_budget / rounds)

    def probe(ip: str) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(per_host_timeout)
        try:
            return s.connect_ex((ip, port)) == 0
        except OSError:
            return False
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(probe, ips)
        return [Candidate(ip, port, "sweep") for ip, ok in zip(ips, results) if ok]


# ---------------------------------------------------------------------------
# MAC address corroboration -- a second, INDEPENDENT identity signal read
# from the local ARP table after we've connected (the OS only populates an
# ARP entry once there has been traffic to that IP, which a completed nasd
# connection guarantees).
#
# Be honest about what this buys: a MAC address is exactly as spoofable as
# everything else on this unauthenticated LAN protocol. Plenty of adapters
# and OSes let software set an arbitrary MAC, and ARP itself has no
# authentication (that's what makes ARP spoofing a thing). So a MAC match
# does not *prove* anything a determined LAN attacker couldn't fake -- it
# corroborates the esa_id check, catching the far more common case of an
# innocent mix-up (DHCP handed our expected IP to a different device) and
# raising the bar slightly for anyone trying to impersonate the Drobo. It is
# never treated as sufficient on its own; see verify_candidate.
# ---------------------------------------------------------------------------

_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}\b")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _normalize_mac(mac: str) -> str:
    return mac.replace("-", ":").lower()


def parse_arp_table(text: str) -> dict[str, str]:
    """
    Pull an {ip: normalized-mac} map out of ARP table text, whatever OS it
    came from.

    Deliberately format-agnostic rather than one parser per OS: Windows
    'arp -a' pads MACs with dashes and lists IP-then-MAC-then-Type; Unix
    'arp -a' reads "? (ip) at mac [ether] on iface"; Unix 'arp -n' and
    /proc/net/arp are column tables with colons. Every one of them puts
    exactly one IPv4 address and one MAC-shaped token on the same line, so
    matching each independently per line and pairing them handles all of
    the layouts (including header/interface lines, which simply have no MAC
    match and are skipped) without a separate parser per format.
    """
    table: dict[str, str] = {}
    for line in text.splitlines():
        ip_match = _IPV4_RE.search(line)
        mac_match = _MAC_RE.search(line)
        if ip_match and mac_match:
            table[ip_match.group(0)] = _normalize_mac(mac_match.group(0))
    return table


def _read_arp_table_text(timeout: float = 2.0) -> str | None:
    """
    Shell out to whatever ARP tool the OS has. Returns None -- never raises
    -- on any failure: a MAC we can't determine is not an error, it's simply
    absent (see mac_for_ip). Nothing here should ever make the agent unusable
    because 'arp' isn't on PATH or the platform is unfamiliar.
    """
    commands = [["arp", "-a"]] if sys.platform.startswith("win") else [["arp", "-n"], ["arp", "-a"]]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    if not sys.platform.startswith("win"):
        try:
            with open("/proc/net/arp", "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass
    return None


def mac_for_ip(ip: str, timeout: float = 2.0) -> str | None:
    """Best-effort MAC for `ip` from the local ARP table, or None if unknown."""
    text = _read_arp_table_text(timeout)
    if text is None:
        return None
    return parse_arp_table(text).get(ip)


# ---------------------------------------------------------------------------
# Address sanity -- allowed_subnets lets a config say "the Drobo only ever
# lives here"; a candidate outside that range is refused before we even open
# a socket to it. Cheap, and it also means a device appearing from somewhere
# unexpected never gets far enough to have a chance at fooling the esa_id/MAC
# checks. Same honesty caveat as everything else here: this blocks devices
# outside the expected range, it does not stop an attacker already inside it.
# ---------------------------------------------------------------------------


def _in_allowed_subnets(host: str, allowed_subnets: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not a bare IP (shouldn't happen -- resolve_hostname already
        # resolves to one). Don't block on something we can't evaluate;
        # identity verification downstream is still the real gate.
        return True
    for cidr in allowed_subnets:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def filter_allowed_subnets(
    candidates: list[Candidate], allowed_subnets: list[str] | None
) -> tuple[list[Candidate], list[Candidate]]:
    """Split `candidates` into (allowed, rejected) by allowed_subnets. A
    falsy allowed_subnets means "no restriction configured" -- everything
    passes."""
    if not allowed_subnets:
        return list(candidates), []
    allowed: list[Candidate] = []
    rejected: list[Candidate] = []
    for cand in candidates:
        (allowed if _in_allowed_subnets(cand.host, allowed_subnets) else rejected).append(cand)
    return allowed, rejected


def _subnet_reject_message(candidate: Candidate, allowed_subnets: list[str]) -> str:
    return (
        f"refusing {candidate.host}:{candidate.port} -- its address is outside the "
        f"allowed subnet(s) {', '.join(allowed_subnets)}. Expected the Drobo to answer "
        f"from one of those ranges; this one didn't, so it was never connected to. If "
        f"you moved the Drobo (or your network) on purpose, update allowed_subnets."
    )


# ---------------------------------------------------------------------------
# Identity verification -- the important part. Finding *a* Drobo is easy;
# confirming it is *the* Drobo is what makes auto-discovery safe to trust.
# See the HONESTY NOTE at the top of this module: none of this is a security
# boundary against a determined LAN attacker, only a correctness/deception
# control, because nasd itself has no authentication.
# ---------------------------------------------------------------------------


class IdentityMismatch(Exception):
    """A candidate answered, but something about it doesn't match what we
    expect -- esa_id, MAC, or (via allowed_subnets) address range.

    Raised instead of ever silently connecting to a stranger's NAS. The
    message always names what changed, what was expected, and what was seen,
    because this is the moment a user finds out either their Drobo was
    replaced or something is impersonating it -- a vague error here would be
    a real failure of the feature.
    """


@dataclass
class Verified:
    candidate: Candidate
    identity: dict
    first_run: bool  # True: no expected_esa_id was configured, so we can't
    #                   confirm identity yet -- caller should report and
    #                   remember `identity["esa_id"]` for next time.
    mac: str | None = None  # Corroborating MAC if the ARP table had one;
    #                         None just means "couldn't be determined", not
    #                         a mismatch -- see mac_for_ip.


def verify_candidate(
    candidate: Candidate,
    expected_esa_id: str = "",
    expected_mac: str = "",
    timeout: float = 5.0,
) -> Verified:
    """
    Connect to `candidate`, read its unprompted greeting, and check identity.

      - expected_esa_id set and it MATCHES    -> Verified(first_run=False)
      - expected_esa_id set and it MISMATCHES -> raises IdentityMismatch
      - expected_esa_id blank (first run)     -> Verified(first_run=True);
        the caller should surface identity["esa_id"] so it can be pinned for
        next time.

    expected_mac is an optional second signal (see mac_for_ip's docstring for
    why it only corroborates, never proves):
      - expected_mac set, observed MAC known and it DIFFERS -> IdentityMismatch,
        exactly like an esa_id mismatch -- refused, not silently accepted.
      - expected_mac set but the MAC could not be read (no arp tool, unknown
        platform, etc.) -> a warnings.warn(), NOT a refusal. We never want a
        missing 'arp' binary to make the agent unusable.
      - expected_mac blank -> MAC is still looked up and returned on Verified
        (for the caller to pin next time) but never checked.

    Only ever reads the greeting nasd pushes on connect -- no command is sent.
    """
    payload = esatm.fetch_greeting(candidate.host, candidate.port, timeout)
    identity = esatm.parse_identity(payload)
    esa_id = identity.get("esa_id", "")
    if expected_esa_id and esa_id != expected_esa_id:
        raise IdentityMismatch(
            f"refusing {candidate.host}:{candidate.port} -- it identifies as "
            f"esa_id={esa_id!r} ({identity.get('name', '?')!r}), not the "
            f"expected {expected_esa_id!r}. This is not your Drobo; not connecting."
        )

    mac = mac_for_ip(candidate.host)
    if expected_mac:
        expected_norm = _normalize_mac(expected_mac)
        if mac is None:
            warnings.warn(
                f"could not confirm MAC address for {candidate.host} (expected "
                f"{expected_norm}) -- local ARP table unavailable or unreadable, so "
                f"MAC corroboration is skipped this time. Proceeding on esa_id alone.",
                RuntimeWarning,
                stacklevel=2,
            )
        elif mac != expected_norm:
            raise IdentityMismatch(
                f"refusing {candidate.host}:{candidate.port} -- its MAC address is "
                f"{mac}, expected {expected_norm}. esa_id matched (esa_id={esa_id!r}), "
                f"but the link-layer address changed -- usually this means the Drobo's "
                f"network adapter or cable was swapped (or you moved to a different "
                f"unit that happens to share the pinned esa_id by coincidence). If this "
                f"was an intentional hardware change, update expected_mac; otherwise "
                f"treat this as suspicious and investigate before trusting it."
            )

    return Verified(candidate, identity, first_run=not expected_esa_id, mac=mac)


def _gather_candidates(
    hostname: str = "",
    last_known_ip: str = "",
    port: int = esatm.DEFAULT_PORT,
    try_sweep: bool = False,
    mdns_timeout: float = 3.0,
    manual_ips: list[str] | None = None,
) -> list[Candidate]:
    """
    Build the ordered candidate list resolve() will try, with no identity
    verification (and no requirement that anything actually answers). Split
    out from resolve() so discovery ordering can be tested without a live
    network or a real Drobo -- see test_discovery.py.

    Order: manual IPs (0), mDNS (a), hostname (b), last-known IP (c), subnet
    sweep (d, only if try_sweep is True). Duplicate host:port pairs are
    collapsed, keeping the first (highest-priority) occurrence. Within that,
    link-local addresses (169.254.0.0/16 APIPA) are always demoted to the end
    -- a real Drobo advertising both a routable LAN address and a self-assigned
    fallback (observed live: real units do advertise both at once) should be
    reached via the routable one, never the other way
    around. The sort is stable, so within "routable" and "link-local" the
    normal source priority above still applies.

    `manual_ips` are addresses a human typed in, and they go FIRST on purpose.
    mDNS is the better mechanism when it works, but it is multicast, and
    multicast is exactly what corporate networks, guest SSIDs, VPN adapters and
    some consumer routers filter -- which is when somebody resorts to typing an
    address. Making them wait three seconds for a broadcast that is never
    answered, on every single poll, would be a poor reward for knowing where
    their own Drobo lives. Identity verification still applies to them in full:
    typing an address gets that address TRIED, never trusted.
    """
    candidates: list[Candidate] = []
    for manual in manual_ips or []:
        manual = (manual or "").strip()
        if manual:
            candidates.append(Candidate(manual, port, "manual"))
    candidates.extend(discover_mdns(mdns_timeout))
    hostname_candidate = resolve_hostname(hostname, port)
    if hostname_candidate:
        candidates.append(hostname_candidate)
    if last_known_ip:
        candidates.append(Candidate(last_known_ip, port, "last-known"))
    if try_sweep:
        candidates.extend(sweep_subnet(port=port))

    seen: set[tuple[str, int]] = set()
    ordered: list[Candidate] = []
    for cand in candidates:
        key = (cand.host, cand.port)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cand)
    ordered.sort(key=lambda c: _is_link_local(c.host))
    return ordered


def resolve(
    expected_esa_id: str = "",
    hostname: str = "",
    last_known_ip: str = "",
    port: int = esatm.DEFAULT_PORT,
    try_sweep: bool = False,
    timeout: float = 5.0,
    mdns_timeout: float = 3.0,
    expected_mac: str = "",
    allowed_subnets: list[str] | None = None,
    manual_ips: list[str] | None = None,
) -> Verified:
    """
    Find the Drobo and prove it's the right one.

    Tries every candidate from _gather_candidates() in order and returns the
    first one whose identity checks out. A candidate that answers but has the
    WRONG identity is refused (IdentityMismatch) and skipped, not silently
    accepted -- so this only ever falls through to "not found" territory, it
    never falls through to "connected to the wrong device".

    manual_ips (optional) are addresses a human supplied, tried before mDNS.
    They are candidates, not overrides: an address that answers with the wrong
    esa_id is refused exactly as any other would be, and one outside
    allowed_subnets is rejected before a socket is opened. Typing an address
    says where to look, never what to believe.

    allowed_subnets (optional list of CIDR strings) is applied first, before
    any candidate is connected to: anything outside those ranges is rejected
    the same way a mismatch is, just without ever opening a socket.
    expected_mac (optional) is checked after esa_id, once connected -- see
    verify_candidate for exactly how it degrades when the MAC can't be read.

    Raises:
        IdentityMismatch  if at least one candidate answered but none matched
                           (esa_id, MAC, or subnet -- only when the relevant
                           expectation is configured).
        LookupError       if nothing answered at all.
    """
    candidates = _gather_candidates(hostname, last_known_ip, port, try_sweep,
                                    mdns_timeout, manual_ips)
    mismatches: list[str] = []
    if allowed_subnets:
        candidates, rejected = filter_allowed_subnets(candidates, allowed_subnets)
        mismatches.extend(_subnet_reject_message(cand, allowed_subnets) for cand in rejected)
    for cand in candidates:
        try:
            return verify_candidate(cand, expected_esa_id, expected_mac, timeout)
        except IdentityMismatch as exc:
            mismatches.append(str(exc))
            continue
        except (OSError, esatm.FrameError, ET.ParseError):
            continue  # unreachable, or answered but wasn't a Drobo greeting

    if mismatches:
        raise IdentityMismatch(
            "found device(s) on the network, but none matched the expected "
            "Drobo:\n  " + "\n  ".join(mismatches)
        )
    tried = ", ".join(f"{c.host}:{c.port} ({c.source})" for c in candidates)
    raise LookupError(
        "no Drobo found. Tried: "
        + (tried if tried else "nothing -- mDNS, hostname, and last-known IP all came up empty")
        + (" (subnet sweep enabled)" if try_sweep else " (subnet sweep not enabled)")
    )
