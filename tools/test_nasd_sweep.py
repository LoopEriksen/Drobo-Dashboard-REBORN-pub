#!/usr/bin/env python3
"""
test_nasd_sweep.py - offline self-test for nasd_sweep.py. No hardware needed.

Two things matter most here, because they are the two safety guarantees the
task depends on:

  1. ALLOWLIST must never overlap commands.DO_NOT_SEND, every name in it must
     resolve to a confirmed numeric id, and assert_allowlist_safe() must
     actually catch it when a candidate list breaks either rule (not just
     "happen to pass" on the current list).
  2. Redaction must fire on every response BEFORE anything is returned to a
     caller that might print/log/write it -- proven against a response shaped
     like the real device's admin-config answer, which is known to contain
     plaintext credentials.

The rest exercises send_one_command() and run_sweep() end to end against an
in-process fake Drobo (same technique as agent/test_config_cmd.py), including
the three-consecutive-timeouts abort rule.

    py tools/test_nasd_sweep.py
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent")))

import nasd_sweep as sweep  # noqa: E402
from drobo_nasd import commands as C  # noqa: E402
from drobo_nasd import config_cmd as cc  # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, extra="") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("  " + str(extra) if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 1. The allowlist safety guard -- the most important thing in this file.
# ---------------------------------------------------------------------------
print("== allowlist safety ==")

check("the real ALLOWLIST is disjoint from DO_NOT_SEND",
      set(sweep.ALLOWLIST).isdisjoint(C.DO_NOT_SEND),
      set(sweep.ALLOWLIST) & C.DO_NOT_SEND)

check("every ALLOWLIST name resolves to a confirmed id",
      all(n in C.COMMANDS for n in sweep.ALLOWLIST),
      [n for n in sweep.ALLOWLIST if n not in C.COMMANDS])

check("ALLOWLIST has no duplicate names",
      len(set(sweep.ALLOWLIST)) == len(sweep.ALLOWLIST))

check("ALLOWLIST is exactly 39 names (the reviewed set -- change deliberately)",
      len(sweep.ALLOWLIST) == 39, len(sweep.ALLOWLIST))

# assert_allowlist_safe() must have already run at import time without
# raising -- if it had raised, importing nasd_sweep above would have failed
# and this file would never have reached this line.
check("module import succeeded, i.e. assert_allowlist_safe() passed on the real list", True)

# Now prove the guard actually catches bad input, not just that the current
# list happens to be fine.
try:
    sweep.assert_allowlist_safe(["eCmdRunCommand"])
    check("guard rejects a DO_NOT_SEND name", False)
except sweep.SweepSafetyError:
    check("guard rejects a DO_NOT_SEND name", True)

try:
    sweep.assert_allowlist_safe(["eCmdWriteHostBuffer"])
    check("guard rejects a second DO_NOT_SEND name (write-host-buffer)", False)
except sweep.SweepSafetyError:
    check("guard rejects a second DO_NOT_SEND name (write-host-buffer)", True)

# eCmdForceSingleInitiator is a documented near-miss (see the module
# docstring): its name sounds like a query but it is a write, and it is NOT
# in commands.DO_NOT_SEND, so assert_allowlist_safe() would happily accept a
# list containing it -- the guard only enforces DO_NOT_SEND-disjointness and
# valid ids, it is not a semantic safety oracle. The actual protection is
# that a human reviewed ALLOWLIST and left it out. Prove that property
# directly instead of expecting the guard to catch it.
check("the near-miss eCmdForceSingleInitiator is simply not in ALLOWLIST",
      "eCmdForceSingleInitiator" not in sweep.ALLOWLIST)
check("the near-miss eCmdGetNextUniqueLUNID is simply not in ALLOWLIST",
      "eCmdGetNextUniqueLUNID" not in sweep.ALLOWLIST)

try:
    sweep.assert_allowlist_safe(["eCmdTotallyMadeUp"])
    check("guard rejects a name with no confirmed id", False)
except sweep.SweepSafetyError:
    check("guard rejects a name with no confirmed id", True)

try:
    sweep.assert_allowlist_safe(["eCmdGetVersion", "eCmdGetVersion"])
    check("guard rejects a duplicate name", False)
except sweep.SweepSafetyError:
    check("guard rejects a duplicate name", True)

try:
    sweep.assert_allowlist_safe(["eCmdGetVersion", "eCmdGetConfig"])
    check("guard accepts a genuinely safe list", True)
except sweep.SweepSafetyError as exc:
    check("guard accepts a genuinely safe list", False, exc)


# ---------------------------------------------------------------------------
# 2. Redaction fires before anything is handed back.
# ---------------------------------------------------------------------------
print("\n== redaction ==")

ADMIN_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<TMCmd><CmdID>80</CmdID><ResultDetails><DRINASConfig><DRINasAdminConfig>
<UserName>admin</UserName><Password>hunter2</Password>
<ValidPassword>1</ValidPassword>
<EncryptedPassword>QUJDREVGRw==</EncryptedPassword>
</DRINasAdminConfig></DRINASConfig></ResultDetails></TMCmd>\x00"""

SECRETS = ("hunter2", "QUJDREVGRw==")

record = sweep._redact_and_summarize(ADMIN_BODY)
check("redacted record parses successfully", "parse_error" not in record, record)
check("no secret value survives anywhere in the redacted record",
      not any(s in repr(record) for s in SECRETS), record)
check("the field NAME 'Password' is still visible (names are safe, values are not)",
      "Password" in record.get("tags", []), record.get("tags"))
check("username (not a secret) survives",
      "admin" in repr(record.get("redacted_value")), record.get("redacted_value"))

EMPTY_BODY = b"\x00"
empty_record = sweep._redact_and_summarize(EMPTY_BODY)
check("an empty body is reported as empty, not as a parse error",
      empty_record.get("empty") is True, empty_record)

MALFORMED_BODY = b"<not xml\x00"
bad_record = sweep._redact_and_summarize(MALFORMED_BODY)
check("malformed XML is reported, not raised", "parse_error" in bad_record, bad_record)


# ---------------------------------------------------------------------------
# 3. End-to-end against an in-process fake Drobo (same technique as
#    agent/test_config_cmd.py's fake_drobo).
# ---------------------------------------------------------------------------
print("\n== end to end against a fake device ==")

TEST_ESAID = "drb000000SAMPLE"


def fake_drobo_answers(reply_body: bytes, port_holder: list) -> None:
    """Accept one connection, do a normal login, answer the one command with
    reply_body, then close."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(1)

    def run():
        try:
            conn, _ = srv.accept()
            conn.settimeout(5)
            head = conn.recv(16)
            ln = struct.unpack(">I", head[12:16])[0]
            conn.recv(ln)
            conn.sendall(cc._frame(cc.TYPE_LOGIN_OK, b""))
            head = conn.recv(16)
            ln = struct.unpack(">I", head[12:16])[0]
            conn.recv(ln)
            conn.sendall(cc._frame(cc.TYPE_RESULT, reply_body))
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()


def fake_drobo_refuses_login(port_holder: list) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(1)

    def run():
        try:
            conn, _ = srv.accept()
            conn.settimeout(5)
            head = conn.recv(16)
            ln = struct.unpack(">I", head[12:16])[0]
            conn.recv(ln)
            conn.sendall(cc._frame(0x99, b""))  # not TYPE_LOGIN_OK
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()


def fake_drobo_login_ok_then_silent(port_holder: list) -> None:
    """Login succeeds, but the command is never answered -- proves the
    per-command timeout path."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(1)

    def run():
        try:
            conn, _ = srv.accept()
            conn.settimeout(5)
            head = conn.recv(16)
            ln = struct.unpack(">I", head[12:16])[0]
            conn.recv(ln)
            conn.sendall(cc._frame(cc.TYPE_LOGIN_OK, b""))
            # Read the command, but never reply -- the client must time out.
            conn.recv(16)
            time.sleep(5)
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()


ports: list[int] = []
fake_drobo_answers(ADMIN_BODY, ports)
result = sweep.send_one_command("127.0.0.1", ports[0], TEST_ESAID,
                                 C.COMMANDS["eCmdGetAdminConfig"], timeout=5)
check("answered status recorded", result["status"] == "answered", result)
check("no secret leaks through send_one_command either",
      not any(s in repr(result) for s in SECRETS), result)

ports = []
fake_drobo_answers(b"\x00", ports)
result = sweep.send_one_command("127.0.0.1", ports[0], TEST_ESAID,
                                 C.COMMANDS["eCmdGetFanInfo"], timeout=5)
check("empty body -> status 'empty'", result["status"] == "empty", result)

ports = []
fake_drobo_refuses_login(ports)
result = sweep.send_one_command("127.0.0.1", ports[0], TEST_ESAID,
                                 C.COMMANDS["eCmdGetVersion"], timeout=5)
check("refused login -> status 'refused', phase 'login'",
      result["status"] == "refused" and result.get("phase") == "login", result)

ports = []
fake_drobo_login_ok_then_silent(ports)
result = sweep.send_one_command("127.0.0.1", ports[0], TEST_ESAID,
                                 C.COMMANDS["eCmdGetSysInfo"], timeout=0.5)
check("no command reply -> status 'timeout', phase 'command'",
      result["status"] == "timeout" and result.get("phase") == "command", result)

# connect() to a closed port -> refused at the connect phase, not a timeout.
result = sweep.send_one_command("127.0.0.1", 1, TEST_ESAID,
                                 C.COMMANDS["eCmdGetVersion"], timeout=1)
check("connection refused outright -> status 'refused', phase 'connect'",
      result["status"] == "refused" and result.get("phase") == "connect", result)


# ---------------------------------------------------------------------------
# 4. run_sweep() stops after three consecutive timeouts.
# ---------------------------------------------------------------------------
print("\n== three-consecutive-timeouts abort ==")


def fake_drobo_login_hangs(port_holder: list) -> None:
    """Never answers the login at all -- every command sent to this server
    times out at the login phase."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(5)

    def run():
        try:
            while True:
                conn, _ = srv.accept()
                # Accept and go silent forever; never close, never answer.
        except OSError:
            pass

    threading.Thread(target=run, daemon=True).start()


ports = []
fake_drobo_login_hangs(ports)
outcome = sweep.run_sweep("127.0.0.1", TEST_ESAID, port=ports[0], timeout=0.3, pause=0.05,
                           names=["eCmdGetVersion", "eCmdGetConfig", "eCmdGetSysInfo",
                                  "eCmdGetDevices", "eCmdGetFanInfo"])
check("sweep aborts after exactly 3 attempts", outcome["sent"] == 3, outcome["sent"])
check("sweep reports aborted=True", outcome["aborted"] is True, outcome)
check("all 3 recorded attempts are timeouts",
      all(r["status"] == "timeout" for r in outcome["results"]), outcome["results"])


print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
