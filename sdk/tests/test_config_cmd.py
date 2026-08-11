"""
Tests for the eCmdGetConfig command path -- the first thing this project SENDS
to a Drobo.

Runs entirely offline against a fake device, so it passes on a machine with no
hardware. The fixtures are shaped exactly like the real responses observed on a
live 5N, including the credential fields the device really does hand out.

The redaction tests are the important ones. Asking a Drobo for its admin config
makes it return <Password>, <EncryptedPassword> and <ValidPassword> in the
clear, to anyone who completes a login -- and the login is just the device
serial, which the Drobo gives to any unauthenticated caller. We cannot fix the
device. We can refuse to become a second copy of the leak, and these tests are
what keep that true.

    py test_config_cmd.py
"""
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from drobo_nasd import config_cmd as cc
from drobo_nasd import commands as C

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("  " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(label)


# --------------------------------------------------------------------------
# Fixtures shaped like the real device's replies
# --------------------------------------------------------------------------
NETWORK = b"""<?xml version="1.0" encoding="UTF-8"?>
<TMCmd><CmdID>30</CmdID><Result>0</Result><ResultDetails><DRINASConfig>
<DRINasNetworkConfig><Version>11</Version><NasName>TestDrobo</NasName>
<NasWorkgroup>WORKGROUP</NasWorkgroup>
<IPConfig><IPConfigType>0</IPConfigType><IP>10.0.0.5</IP>
<Subnet>255.255.255.0</Subnet><Gateway>10.0.0.1</Gateway></IPConfig>
</DRINasNetworkConfig></DRINASConfig></ResultDetails></TMCmd>\x00"""

ADMIN = b"""<?xml version="1.0" encoding="UTF-8"?>
<TMCmd><CmdID>30</CmdID><ResultDetails><DRINASConfig><DRINasAdminConfig>
<UserName>admin</UserName><Password>hunter2</Password>
<ValidPassword>1</ValidPassword>
<EncryptedPassword>QUJDREVGRw==</EncryptedPassword>
</DRINasAdminConfig></DRINASConfig></ResultDetails></TMCmd>\x00"""

SHARES = b"""<?xml version="1.0" encoding="UTF-8"?>
<TMCmd><CmdID>30</CmdID><ResultDetails><DRINASConfig><DRIShareConfig>
<Shares>
<Share><ShareName>Alpha</ShareName><ShareState>0</ShareState>
<ShareUsers><ShareUser><ShareUsername>Everyone</ShareUsername>
<SharePassword>leakme</SharePassword></ShareUser></ShareUsers></Share>
<Share><ShareName>Beta</ShareName><ShareState>0</ShareState></Share>
</Shares></DRIShareConfig></DRINASConfig></ResultDetails></TMCmd>\x00"""

SECRETS = ("hunter2", "QUJDREVGRw==", "leakme")

print("== parsing, and the redaction that matters ==")

net = cc.parse_config_result(NETWORK)
ncfg = net["DRINASConfig"]["DRINasNetworkConfig"]
check("network parses", ncfg["NasName"] == "TestDrobo", ncfg.get("NasName"))
check("nested IPConfig parses", ncfg["IPConfig"]["IP"] == "10.0.0.5", ncfg.get("IPConfig"))

adm = cc.parse_config_result(ADMIN)["DRINASConfig"]["DRINasAdminConfig"]
check("admin username kept (not a secret)", adm["UserName"] == "admin", adm.get("UserName"))
check("Password redacted", adm["Password"] == "[redacted]", adm.get("Password"))
check("ValidPassword redacted", adm["ValidPassword"] == "[redacted]", adm.get("ValidPassword"))
check("EncryptedPassword redacted",
      adm["EncryptedPassword"] == "[redacted]", adm.get("EncryptedPassword"))
check("no admin secret survives anywhere in the result",
      not any(s in repr(adm) for s in SECRETS), adm)

sh = cc.parse_config_result(SHARES)["DRINASConfig"]["DRIShareConfig"]
shares = sh["Shares"]["Share"]
check("repeated <Share> becomes a list", isinstance(shares, list) and len(shares) == 2,
      type(shares).__name__)
check("share names parsed", [s["ShareName"] for s in shares] == ["Alpha", "Beta"],
      [s.get("ShareName") for s in shares])
check("a password NESTED inside a share is redacted too",
      "leakme" not in repr(sh), sh)

print("\n== the two redaction holes found by audit, 2026-07-26 ==")
# Hole 1: this firmware sometimes returns its result as an XML document
# *escaped into a text node* (&lt;Password&gt;, not <Password>) -- which is
# exactly how eCmdGetSysInfo replies. _strip_secrets matches on element tags,
# so against that shape it had nothing to match and the whole document walked
# straight out. eCmdGetAdminConfig is in nasd_sweep's allowlist, so the next
# sweep would have written a live admin password to disk.
import html as _html

LEAK = "hunter2-REAL-SECRET"


def escaped_result(inner: str) -> bytes:
    return (b"<TMCmd><CmdID>30</CmdID><ResultDetails>"
            + _html.escape(inner).encode()
            + b"</ResultDetails></TMCmd>\x00")


esc = cc.parse_config_result(escaped_result(
    f"<DRINasAdminConfig><UserName>admin</UserName>"
    f"<Password>{LEAK}</Password></DRINasAdminConfig>"))
check("secret inside an ESCAPED inner document is redacted", LEAK not in repr(esc), esc)

check("...also when the inner document carries its own <?xml?> declaration",
      LEAK not in repr(cc.parse_config_result(escaped_result(
          '<?xml version="1.0" encoding="UTF-8"?>'
          f"<DRINasAdminConfig><Password>{LEAK}</Password></DRINasAdminConfig>"))))

check("...also when the secret is nested several levels down",
      LEAK not in repr(cc.parse_config_result(escaped_result(
          f"<Shares><Share><ShareName>Docs</ShareName>"
          f"<SharePassword>{LEAK}</SharePassword></Share></Shares>"))))

check("XML-shaped text we CAN'T parse is redacted rather than trusted",
      LEAK not in repr(cc.parse_config_result(escaped_result(f"<Password>{LEAK}"))))

# Hole 2: a credential element whose value lived in CHILD elements rather than
# its own text survived, because the old code replaced .text (empty here) and
# then deliberately skipped descending.
child_doc = (b"<TMCmd><ResultDetails><DRINasAdminConfig>"
             b"<UserName>admin</UserName>"
             b"<Password><Value>" + LEAK.encode() + b"</Value></Password>"
             b"</DRINasAdminConfig></ResultDetails></TMCmd>\x00")
check("a credential element's CHILD elements are redacted too",
      LEAK not in repr(cc.parse_config_result(child_doc)),
      cc.parse_config_result(child_doc))

# ...without the fix becoming so blunt it eats real data.
sysinfo_like = cc.parse_config_result(escaped_result(
    "<SysInfo><UpTime>1521745</UpTime><Temperature>36</Temperature></SysInfo>"))
check("a non-credential escaped document is NOT redacted away",
      "36" in repr(sysinfo_like) and "1521745" in repr(sysinfo_like), sysinfo_like)

plain = cc.parse_config_result(
    b"<TMCmd><ResultDetails><DRINasNetworkConfig><NasName>MyDrobo</NasName>"
    b"<IPConfig><IP>10.0.0.5</IP></IPConfig></DRINasNetworkConfig>"
    b"</ResultDetails></TMCmd>\x00")
check("ordinary config is untouched by the new pass",
      plain["DRINasNetworkConfig"]["NasName"] == "MyDrobo"
      and plain["DRINasNetworkConfig"]["IPConfig"]["IP"] == "10.0.0.5", plain)

print("\n== malformed and empty input ==")
try:
    cc.parse_config_result(b"<TMCmd><CmdID>30</CmdID></TMCmd>\x00")
    check("missing ResultDetails raises", False)
except cc.ConfigError:
    check("missing ResultDetails raises ConfigError", True)
try:
    cc.parse_config_result(b"<not xml at all\x00")
    check("malformed XML raises", False)
except cc.ConfigError:
    check("malformed XML raises ConfigError", True)
check("empty payload gives an empty dict", cc.parse_config_result(b"\x00") == {})

print("\n== frame construction matches the observed wire format ==")
f = cc._frame(cc.TYPE_COMMAND, b"abc")
check("frame starts with DRINETTM", f[:8] == b"DRINETTM", f[:8])
check("type byte placed correctly", f[8] == cc.TYPE_COMMAND, f[8])
check("length is big-endian", struct.unpack(">I", f[12:16])[0] == 3, f[12:16].hex())

lp = cc._login_payload("drb000000SAMPLE")
check("login payload is 220 bytes", len(lp) == 220, len(lp))
check("serial at offset 0", lp[:15] == b"drb000000SAMPLE", lp[:15])
check("serial repeated at offset 20", lp[20:35] == b"drb000000SAMPLE", lp[20:35])
check("rest is NUL padding", set(lp[35:]) == {0})
try:
    cc._login_payload("x" * 40)
    check("over-long serial rejected", False)
except cc.ConfigError:
    check("over-long serial rejected", True)

print("\n== safety guards ==")
check("eCmdGetConfig has a confirmed id", C.COMMANDS.get("eCmdGetConfig") == 30,
      C.COMMANDS.get("eCmdGetConfig"))
check("the dangerous commands are still refused everywhere",
      {"eCmdRunCommand", "eCmdWriteHostBuffer"} <= C.DO_NOT_SEND)
try:
    cc.get_config("127.0.0.1", "drb000000SAMPLE", section="nonsense")
    check("unknown section rejected before connecting", False)
except cc.ConfigError as exc:
    check("unknown section rejected before connecting", "unknown section" in str(exc))


# --------------------------------------------------------------------------
# End to end against a fake Drobo -- proves login + command + parse together
# --------------------------------------------------------------------------
print("\n== end to end against a fake device ==")


def fake_drobo(reply: bytes, port_holder: list, login_ok=True):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(1)

    def run():
        try:
            conn, _ = srv.accept()
            conn.settimeout(5)
            # login frame
            head = conn.recv(16)
            ln = struct.unpack(">I", head[12:16])[0]
            conn.recv(ln)
            conn.sendall(cc._frame(cc.TYPE_LOGIN_OK if login_ok else 0x99, b""))
            if not login_ok:
                conn.close(); srv.close(); return
            head = conn.recv(16)
            ln = struct.unpack(">I", head[12:16])[0]
            conn.recv(ln)
            conn.sendall(cc._frame(cc.TYPE_RESULT, reply))
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()


ports = []
fake_drobo(ADMIN, ports)
got = cc.get_config("127.0.0.1", "drb000000SAMPLE", section="admin",
                    port=ports[0], timeout=5)
a = got["DRINASConfig"]["DRINasAdminConfig"]
check("end-to-end fetch works", a["UserName"] == "admin", a)
check("end-to-end result carries NO credential",
      not any(s in repr(got) for s in SECRETS), got)

ports = []
fake_drobo(b"", ports)
try:
    cc.get_config("127.0.0.1", "drb000000SAMPLE", port=ports[0], timeout=5)
    check("empty device reply raises", False)
except cc.ConfigError:
    check("empty device reply raises ConfigError", True)

ports = []
fake_drobo(NETWORK, ports, login_ok=False)
try:
    cc.get_config("127.0.0.1", "drb000000SAMPLE", port=ports[0], timeout=5)
    check("refused login raises", False)
except cc.ConfigError as exc:
    check("refused login raises ConfigError", "login refused" in str(exc), str(exc))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
