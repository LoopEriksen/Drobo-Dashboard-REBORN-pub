"""
Offline tests for notify.py, with the email backend as the main event.

Two properties matter more than the rest and are tested by BOOBY-TRAPPING
rather than by mocking politely:

  - a config without email must never touch smtplib at all
  - a password must never reach stdout or an exception message

Both are security properties on a service that runs unattended on a machine
holding somebody's only copy of their data, so neither is left to inspection.

    py test_notify.py
"""
import io
import os
import sys
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk")))

from drobo_agent import notify

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


SECRET = "hunter2-do-not-leak"


def email_cfg(**over):
    base = {"host": "smtp.example.invalid", "port": 587, "username": "me@example.invalid",
            "password": SECRET, "from": "me@example.invalid", "to": "you@example.invalid"}
    base.update(over)
    return {"backends": ["email"], "email": base}


print("\n== the existing backends still behave ==")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    notify.build({"backends": ["console"]}).send("T", "hello", "critical")
check("console still prints", "hello" in out.getvalue(), out.getvalue())
check("an empty backend list falls back to console",
      notify.build({}).members[0].name == "console")
check("an unknown backend is ignored, not fatal",
      notify.build({"backends": ["telepathy"]}).members[0].name == "console")

print("\n== a config with no email never loads smtplib ==")
# Not "does not send" -- does not IMPORT. Proven by replacing the module with
# something that explodes on any attribute access.
class _Exploding:
    def __getattr__(self, name):
        raise AssertionError(f"smtplib was touched ({name}) with email not configured")


_real = sys.modules.get("smtplib")
sys.modules["smtplib"] = _Exploding()
try:
    with contextlib.redirect_stdout(io.StringIO()):
        notify.build({"backends": ["console"]}).send("T", "m", "info")
        notify.build({"backends": ["ntfy"], "ntfy": {"topic": ""}}).send("T", "m", "info")
    check("console and a disabled ntfy touch smtplib not at all", True)
except AssertionError as exc:
    check("console and a disabled ntfy touch smtplib not at all", False, exc)
finally:
    if _real is not None:
        sys.modules["smtplib"] = _real
    else:
        del sys.modules["smtplib"]

print("\n== a half-configured email backend is refused, not half-used ==")
# An alert that silently fails to send is worse than one never configured,
# because you believe you are covered.
for missing, label in ((["host"], "no host"), (["to"], "no recipient")):
    cfg = email_cfg()
    for k in missing:
        cfg["email"][k] = ""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        group = notify.build(cfg)
    check(f"{label} -> email backend not built",
          all(m.name != "email" for m in group.members), [m.name for m in group.members])
    check(f"{label} -> and it says so on stdout", "skipping" in out.getvalue(), out.getvalue())

print("\n== a complete config does build the backend ==")
group = notify.build(email_cfg())
mailer = [m for m in group.members if m.name == "email"]
check("the email backend is present", len(mailer) == 1, [m.name for m in group.members])
check("a comma-separated recipient list is split",
      notify.build(email_cfg(to="a@x.invalid, b@x.invalid")).members[0].recipients
      == ["a@x.invalid", "b@x.invalid"])
check("a single recipient still works", mailer[0].recipients == ["you@example.invalid"])
check("'from' defaults to the username when unset",
      notify.build(email_cfg(**{"from": ""})).members[0].sender == "me@example.invalid")

print("\n== THE PASSWORD NEVER ESCAPES ==")
# A rejected SMTP login habitually echoes the credentials back in the server's
# response text. That text must never reach a log line or an exception.
m = mailer[0]
check("it is not a public attribute", not any(
    getattr(m, a, None) == SECRET for a in dir(m) if not a.startswith("_")))
check("repr() does not contain it", SECRET not in repr(m))
check("vars() does not expose it under a public name",
      not any(v == SECRET for k, v in vars(m).items() if not k.startswith("_")))

# Force a failure and check what comes out of it.
class _RudeSMTP:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): pass
    def login(self, u, p):
        raise RuntimeError(f"535 auth failed for user={u} pass={p}")
    def send_message(self, msg): pass


import smtplib as _smtplib
_orig_smtp = _smtplib.SMTP
_smtplib.SMTP = _RudeSMTP
try:
    raised = None
    try:
        m.send("Drobo: action needed", "a drive failed", "critical")
    except Exception as exc:
        raised = exc
    check("a delivery failure raises", raised is not None)
    check("-- and the message does NOT contain the password",
          raised is not None and SECRET not in str(raised), str(raised))
    check("-- nor the server's echoed response", raised is not None and "535" not in str(raised),
          str(raised))
    check("-- but does name the server, so it is diagnosable",
          raised is not None and "smtp.example.invalid" in str(raised), str(raised))
    check("-- and the error TYPE, which is the useful half",
          raised is not None and "RuntimeError" in str(raised), str(raised))

    # A failing backend must not stop the others -- NotifierGroup's contract.
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        notify.build({**email_cfg(), "backends": ["console", "email"]}).send("T", "m", "critical")
    printed = out.getvalue()
    check("a broken email backend does not stop the console one", "m" in printed, printed)
    check("-- and the failure notice carries no password", SECRET not in printed, printed)
finally:
    _smtplib.SMTP = _orig_smtp

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
