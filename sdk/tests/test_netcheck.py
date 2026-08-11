"""
Offline tests for netcheck.py -- "is this PC even on the Drobo's network?".

Every parser case uses REAL output shapes from the three tools involved
(ipconfig, ip -o -4 addr, ifconfig), including a localised Windows listing,
because the whole design claim of parse_interfaces is that it reads none of the
labels. No subprocess is spawned and no socket is opened: local_interfaces() is
only exercised through its injectable seam.

    py test_netcheck.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import netcheck
from drobo_nasd.netcheck import Interface

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def nets(text):
    return [str(i.network) for i in netcheck.parse_interfaces(text)]


# --------------------------------------------------------------------------
print("\n== telling a netmask from an address ==")
# This is the whole trick the label-blind parser rests on: a host address is
# never a contiguous netmask, so the classification is exact, not a heuristic.
check("255.255.255.0 is a /24", netcheck._prefix_from_mask("255.255.255.0") == 24)
check("255.255.0.0 is a /16", netcheck._prefix_from_mask("255.255.0.0") == 16)
check("255.255.255.128 is a /25", netcheck._prefix_from_mask("255.255.255.128") == 25)
check("a host address is not a mask", netcheck._prefix_from_mask("10.0.0.50") is None)
check("a gateway address is not a mask", netcheck._prefix_from_mask("10.9.9.1") is None)
check("a non-contiguous mask is refused", netcheck._prefix_from_mask("255.0.255.0") is None)
# 0.0.0.0 IS a valid contiguous netmask, and Windows prints it as the gateway
# of an adapter that has none. Accepting it would declare this PC to be on a
# network containing every address there is -- silently disabling every warning
# this module exists to raise.
check("0.0.0.0 is refused as a mask", netcheck._prefix_from_mask("0.0.0.0") is None)
check("macOS hex mask 0xffffff00 is a /24", netcheck._prefix_from_hex("ffffff00") == 24)

# --------------------------------------------------------------------------
print("\n== Windows ipconfig: mask on the FOLLOWING line ==")
WINDOWS = """
Windows IP Configuration

Wireless LAN adapter Wi-Fi:

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 10.9.9.41
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 10.9.9.1

Ethernet adapter Ethernet 2:

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 10.9.7.1
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . :
"""
check("both adapters found", nets(WINDOWS) == ["10.9.9.0/24", "10.9.7.0/24"],
      nets(WINDOWS))
# The regression that made this parser label-blind in the first place: a
# gateway and a DNS server are lone IPv4 addresses on their own lines, exactly
# like an interface address. Recording 8.8.8.8 as ours would have claimed this
# PC is on 8.8.8.0/24.
DNS_LINES = """
   IPv4 Address. . . . . . . . . . . : 192.168.1.10
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.1.1
   DNS Servers . . . . . . . . . . . : 8.8.8.8
                                       1.1.1.1
   DHCP Server . . . . . . . . . . . : 192.168.1.1
"""
check("a gateway is not mistaken for one of ours", nets(DNS_LINES) == ["192.168.1.0/24"],
      nets(DNS_LINES))
check("DNS servers are not mistaken for our networks",
      "8.8.8.0/24" not in nets(DNS_LINES), nets(DNS_LINES))

print("\n== the same listing in German, which reads no differently ==")
# The point of matching zero labels: this must work without the parser knowing
# the word "Subnetzmaske" -- or "Subnet", for that matter.
GERMAN = """
Drahtlos-LAN-Adapter WLAN:

   Verbindungsspezifisches DNS-Suffix:
   IPv4-Adresse  . . . . . . . . . . : 10.9.9.41
   Subnetzmaske  . . . . . . . . . . : 255.255.255.0
   Standardgateway . . . . . . . . . : 10.9.9.1
"""
check("a localised listing parses identically", nets(GERMAN) == ["10.9.9.0/24"],
      nets(GERMAN))

# --------------------------------------------------------------------------
print("\n== Linux `ip -o -4 addr show`: CIDR on the same line ==")
IPROUTE = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
    "2: eth0    inet 192.168.1.20/24 brd 192.168.1.255 scope global eth0\n"
    "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
)
check("CIDR form parsed", nets(IPROUTE) == ["192.168.1.0/24", "172.17.0.0/16"], nets(IPROUTE))
check("loopback dropped", "127.0.0.0/8" not in nets(IPROUTE))

print("\n== macOS/BSD ifconfig: hex mask on the same line ==")
IFCONFIG = """
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 192.168.1.33 netmask 0xffffff00 broadcast 192.168.1.255
"""
check("hex mask parsed", nets(IFCONFIG) == ["192.168.1.0/24"], nets(IFCONFIG))
# broadcast 192.168.1.255 sits on the same line as the address. It is not a
# valid netmask, so it is never read as one -- and the address is taken from
# the first token, not the last.
check("the broadcast address is not read as an interface",
      "192.168.1.255" not in [i.address for i in netcheck.parse_interfaces(IFCONFIG)])

DOTTED_IFCONFIG = "eth0\n	inet 10.0.0.7 netmask 255.255.255.0 broadcast 10.0.0.255\n"
check("dotted mask on the same line parsed", nets(DOTTED_IFCONFIG) == ["10.0.0.0/24"],
      nets(DOTTED_IFCONFIG))

print("\n== addresses that say nothing are dropped ==")
APIPA = """
   Autoconfiguration IPv4 Address. . : 169.254.14.9
   Subnet Mask . . . . . . . . . . . : 255.255.0.0
"""
# A self-assigned APIPA address means DHCP FAILED -- the opposite of "on a
# network". Treating it as one would let a disconnected PC claim it shares a
# network with something.
check("link-local (APIPA) dropped", nets(APIPA) == [], nets(APIPA))
check("an empty listing yields nothing", netcheck.parse_interfaces("") == [])
check("prose with no addresses yields nothing",
      netcheck.parse_interfaces("no adapters are present\n") == [])
check("an address with no mask anywhere is discarded, not guessed at",
      netcheck.parse_interfaces("   IPv4 Address. . : 192.168.1.5\n") == [])

# --------------------------------------------------------------------------
print("\n== the diagnosis: the failure this module was written for ==")
# Three times during development the Drobo "timed out" because Windows had
# auto-joined a different SSID. The array was healthy every time.
WIFI = [Interface("10.9.9.41", 24), Interface("10.9.7.1", 24)]
d = netcheck.diagnose("10.0.0.50", WIFI)
check("a different network is spotted", d is not None and not d.same_network)
check("the Drobo's address is named", "10.0.0.50" in d.detail, d.detail)
check("this PC's networks are named",
      "10.9.9.0/24" in d.detail and "10.9.7.0/24" in d.detail, d.detail)
check("it says the Drobo needs no changes", "nothing needs changing" in d.detail, d.detail)
# The claim is an explanation, never a proof: routed networks exist, and a
# tool that confidently blames the wrong thing is worse than a silent one.
check("a mismatch is never stated without its caveat", bool(d.caveat))
check("the caveat admits routing exists", "routed together" in d.caveat, d.caveat)
check("message carries all three parts",
      d.headline in d.message and d.detail in d.message and d.caveat in d.message)
check("explain() returns that message", netcheck.explain("10.0.0.50", WIFI) == d.message)

print("\n== and the cases where it must keep quiet ==")
SAME = [Interface("10.0.0.40", 24)]
same = netcheck.diagnose("10.0.0.50", SAME)
check("same network -> same_network is True", same is not None and same.same_network)
# "Checked, and the network layout isn't the story" is a different answer from
# "couldn't check" -- so a Diagnosis is still returned, but with no text, so
# nothing can display it.
check("same network -> nothing to display", same.message == "", repr(same.message))
check("same network -> explain() is empty", netcheck.explain("10.0.0.50", SAME) == "")

# A wider mask is honoured rather than second-guessed: 10.0.9.41/16 really does
# contain 10.0.0.50, so there is nothing to warn about. Assuming /24 everywhere
# would have invented a mismatch here.
check("a /16 that really does contain the target is respected",
      netcheck.diagnose("10.0.0.50", [Interface("10.0.9.41", 16)]).same_network)
check("no interfaces -> no opinion", netcheck.diagnose("10.0.0.50", []) is None)
check("a hostname target -> no opinion", netcheck.diagnose("drobo.local", WIFI) is None)
check("'auto' target -> no opinion", netcheck.diagnose("auto", WIFI) is None)
check("an empty target -> no opinion", netcheck.diagnose("", WIFI) is None)
check("an IPv6 target -> no opinion", netcheck.diagnose("fe80::1", WIFI) is None)

print("\n== an assumed mask is never presented as a measurement ==")
GUESSED = [Interface("10.9.9.41", netcheck.DEFAULT_PREFIX, assumed=True)]
g = netcheck.diagnose("10.0.0.50", GUESSED)
check("assumed_prefix is flagged", g.assumed_prefix)
check("the guess is admitted in the caveat", "could not be read" in g.caveat, g.caveat)
measured = netcheck.diagnose("10.0.0.50", WIFI)
check("a measured mask makes no such admission", "could not be read" not in measured.caveat)

print("\n== many interfaces stay readable ==")
MANY = [Interface(f"10.{n}.0.5", 24) for n in range(20, 27)]  # none holds the target
many = netcheck.diagnose("10.0.0.50", MANY)
check("the list is summarised, not dumped", "other" in many.detail, many.detail)
check("every network still reaches the structured form",
      len(many.to_dict()["networks"]) == 7)

print("\n== the dict a front-end receives ==")
payload = d.to_dict()
check("carries same_network", payload["same_network"] is False)
check("carries the target", payload["target"] == "10.0.0.50")
check("carries the networks", payload["networks"] == ["10.9.9.0/24", "10.9.7.0/24"])
check("carries a ready-made message", payload["message"] == d.message)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
