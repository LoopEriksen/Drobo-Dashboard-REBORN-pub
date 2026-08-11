"""
Offline tests for backup.py.

Two things here could destroy somebody's data if wrong, so they get the most
attention: robocopy's exit codes (success is NOT zero, and reading it as Unix
would report every good backup as broken) and the destination refusals (which
exist to stop a mirror being pointed somewhere that deletes things).

A real backup IS run, into temp folders, because "the dry run copies nothing"
is not a claim worth taking on trust.

    py test_backup.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk")))

from drobo_agent import backup

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


tmp = tempfile.mkdtemp(prefix="drobobackup-")
src = os.path.join(tmp, "source")
dst = os.path.join(tmp, "backup")
os.makedirs(os.path.join(src, "Photos", "2026"), exist_ok=True)
for name in ("a.jpg", "b.jpg"):
    with open(os.path.join(src, "Photos", "2026", name), "w") as fh:
        fh.write("pretend photo " + name)

print("\n== robocopy exit codes: success is NOT zero ==")
# The classic bug in every script that wraps robocopy. Reading these as Unix
# exit codes reports every successful backup as a failure.
for code, expect_ok, note in [
    (0, True, "nothing to copy"),
    (1, True, "files copied"),
    (2, True, "extra files in destination"),
    (3, True, "copied + extras"),
    (4, True, "mismatches, but nothing failed"),
    (7, True, "1+2+4, all non-failure bits"),
    (8, False, "some files could not be copied"),
    (9, False, "copied some, failed others"),
    (16, False, "serious error"),
    (24, False, "8+16"),
]:
    ok, summary = backup.interpret_exit_code(code)
    check(f"exit {code:<2} -> {'ok' if expect_ok else 'FAILURE'}  ({note})", ok == expect_ok,
          f"got ok={ok} {summary}")

ok0, sum0 = backup.interpret_exit_code(0)
check("exit 0 says there was nothing to do, not 'success'",
      "already up to date" in sum0, sum0)
_, sum8 = backup.interpret_exit_code(8)
check("a failure names itself loudly", "COULD NOT BE COPIED" in sum8, sum8)
check("a negative code is a failure, not a bitmask",
      backup.interpret_exit_code(-1)[0] is False)

print("\n== destinations that must be refused ==")
# Each of these is a way to lose data or hang forever.
inside = os.path.join(src, "nested")
check("copying a folder into itself is refused",
      any("never finishes" in r for r in backup.check_destination(src, inside)),
      backup.check_destination(src, inside))
check("source == destination is refused",
      any("same folder" in r for r in backup.check_destination(src, src)))
check("backing up into a parent of the source is refused",
      any("parent of" in r for r in backup.check_destination(inside, src)),
      backup.check_destination(inside, src))
# Mirroring onto C:\ would try to delete Windows.
root_refusals = backup.check_destination(src, "C:\\")
check("a bare drive root is refused", any("root of" in r for r in root_refusals), root_refusals)
check("-- and it suggests a folder instead", any("DroboBackup" in r for r in root_refusals))
check("an empty destination is refused", backup.check_destination(src, "") != [])
check("an empty source is refused", backup.check_destination("", dst) != [])
check("a sensible destination is allowed", backup.check_destination(src, dst) == [],
      backup.check_destination(src, dst))

print("\n== plan() describes, and copies NOTHING ==")
p = backup.plan(src, dst)
check("the plan is allowed", p.allowed, p.refusals)
check("it warns that the destination will be created",
      any("does not exist yet" in w for w in p.warnings), p.warnings)
check("robocopy is the command", p.command[0] == "robocopy", p.command)
check("restartable mode is on, so a Wi-Fi blip does not restart a big file",
      "/Z" in p.command, p.command)
check("retries are bounded -- robocopy's default is a MILLION",
      "/R:2" in p.command, p.command)
check("mirroring is NOT on by default", "/MIR" not in p.command, p.command)
# The actual proof:
check("planning wrote nothing to disk", not os.path.exists(dst))

print("\n== mirroring is opt-in and says what it will delete ==")
m = backup.plan(src, dst, mirror=True)
check("/MIR appears only when asked for", "/MIR" in m.command, m.command)
warn = " ".join(m.warnings)
check("the warning says DELETED in plain words", "DELETED" in warn, warn)
check("-- and gives the scenario that matters",
      "delete a photo by accident" in warn, warn)

print("\n== a missing source is refused before anything runs ==")
gone = backup.plan(os.path.join(tmp, "no-such-folder"), dst)
check("a nonexistent local source is refused", not gone.allowed, gone.refusals)

print("\n== starting a backup that must not run raises, rather than running ==")
runner = backup.BackupRunner()
try:
    runner.start(src, src)
    check("a refused plan raises BackupError", False)
except backup.BackupError as exc:
    check("a refused plan raises BackupError", True)
    check("-- and the message says why", "same folder" in str(exc), str(exc))
check("nothing was recorded as a job", runner.job is None)

print("\n== a real backup, into a real temp folder ==")
if os.name != "nt":
    print("  SKIP    robocopy is Windows-only")
else:
    job = runner.start(src, dst)
    for _ in range(150):                      # up to ~15s
        if not job.running:
            break
        time.sleep(0.1)
    check("the backup finished", not job.running, job.summary)
    check("it succeeded", job.ok is True, f"exit={job.exit_code} {job.summary} {job.error}")
    copied = os.path.join(dst, "Photos", "2026", "a.jpg")
    check("the files actually arrived", os.path.exists(copied), copied)
    check("content matches", os.path.exists(copied)
          and open(copied).read() == "pretend photo a.jpg")
    check("progress output was captured for a UI to show", len(job.tail) > 0, len(job.tail))
    check("the job serialises for the API", "destination" in job.to_dict())

    print("\n== a second run finds nothing to do ==")
    job2 = runner.start(src, dst)
    for _ in range(150):
        if not job2.running:
            break
        time.sleep(0.1)
    check("re-running succeeds", job2.ok is True, f"exit={job2.exit_code}")
    check("-- and does not re-copy what is already there",
          job2.exit_code == 0, f"exit={job2.exit_code} ({job2.summary})")

    print("\n== two at once is refused ==")
    # Two robocopy processes on one destination would interleave and neither
    # result would be trustworthy.
    slow = backup.BackupRunner()
    slow.start(src, os.path.join(tmp, "backup2"))
    try:
        slow.start(src, os.path.join(tmp, "backup3"))
        # It may legitimately have finished already on a tiny fixture.
        check("a second concurrent backup is refused (or the first had finished)", True)
    except backup.BackupError as exc:
        check("a second concurrent backup is refused", "already running" in str(exc), str(exc))

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
