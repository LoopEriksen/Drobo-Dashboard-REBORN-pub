"""
Copy the Drobo to an external hard drive.

WHY THE PC DOES THE COPYING. The Drobo 5N has no USB port -- it is a NAS, and
the only way in or out is the network. So the external drive plugs into the PC,
and the PC copies Drobo -> drive across the LAN. That is not a compromise: the
machine already running this agent is the natural place for it, and a backup
you can reach from the same box that writes the originals is the one you will
actually check on.

WHY ROBOCOPY AND NOT PYTHON. Robocopy ships with Windows, and it is better at
this than anything reasonable to write here: it retries transient network
failures, resumes part-copied files (/Z), preserves timestamps, and handles
paths past 260 characters -- all of which matter when the source is an SMB
share on a 2013 NAS over Wi-Fi. A shutil.copytree loop would be fewer lines and
would lose data the first time the Wi-Fi hiccupped mid-file.

THE TWO THINGS THIS GETS RIGHT ON PURPOSE
-----------------------------------------
**It does not write unless you say so twice.** plan() describes what would
happen and copies nothing. Actually running it takes a separate, explicit call.
An accidental backup is harmless; an accidental anything-else on somebody's
only copy of their photos is not, so the safe direction is the default.

**It does not delete.** Robocopy's /MIR makes the destination match the source
exactly -- which means it DELETES files from your backup that are no longer on
the Drobo. That turns "back up my photos" into "propagate my accidental
deletion to the backup as well", destroying the copy that would have saved you.
Mirroring is therefore opt-in, per-run, and the plan says in plain words what
it will remove before you agree to it.

ROBOCOPY'S EXIT CODES ARE NOT UNIX EXIT CODES. This is the classic bug in every
script that wraps it. Robocopy returns a BITMASK, and success is not zero:

    0   nothing needed copying          success
    1   files were copied               success
    2   extra files exist in the target success
    4   mismatched files or folders     success, but worth mentioning
    8   some files could NOT be copied  FAILURE
    16  serious error, nothing copied   FAILURE

So `code == 0` is "success, and there was nothing to do", `code < 8` is success,
and anything with bit 8 or 16 set is a real failure. Treating nonzero as failure
would report every successful backup as broken.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field

#: Robocopy bits that mean something actually went wrong.
FAILED_BITS = 8 | 16

#: What each bit means, for a human-readable summary.
_BIT_MEANINGS = [
    (1, "files were copied"),
    (2, "extra files or folders exist in the backup that are not on the Drobo"),
    (4, "some files or folders did not match"),
    (8, "SOME FILES COULD NOT BE COPIED"),
    (16, "a serious error occurred and nothing was copied"),
]


class BackupError(Exception):
    """The backup could not be planned or started. Never raised for a copy
    that ran and reported problems -- that comes back as a result."""


def interpret_exit_code(code: int) -> tuple[bool, str]:
    """
    Turn a robocopy exit code into (ok, plain English).

    See the module docstring: this is a bitmask and success is not zero. Kept
    as its own function precisely so it can be tested against every value
    without running a copy.
    """
    if code < 0:
        return False, f"robocopy did not run properly (exit {code})"
    ok = (code & FAILED_BITS) == 0
    if code == 0:
        return True, "Nothing needed copying -- the backup was already up to date."
    parts = [text for bit, text in _BIT_MEANINGS if code & bit]
    if not parts:
        return ok, f"robocopy exited {code}"
    return ok, "; ".join(parts) + "."


@dataclass
class BackupPlan:
    source: str
    destination: str
    mirror: bool = False
    #: Reasons this must not run. Non-empty means refuse.
    refusals: list[str] = field(default_factory=list)
    #: Things worth saying out loud before somebody agrees.
    warnings: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.refusals

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "destination": self.destination,
            "mirror": self.mirror,
            "allowed": self.allowed,
            "refusals": self.refusals,
            "warnings": self.warnings,
            # Shown so somebody can see exactly what would be run, and run it
            # themselves if they would rather not trust this to do it.
            "command": " ".join(self.command),
        }


def _norm(path: str) -> str:
    """Absolute, normalised, trailing separator removed, case-folded for
    comparison on a filesystem that does not care about case."""
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def _is_within(child: str, parent: str) -> bool:
    """True if `child` is `parent` or sits inside it."""
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p + os.sep)


def check_destination(source: str, destination: str) -> list[str]:
    """
    Reasons this destination must be refused. Empty list means it is fine.

    Every one of these is a way to destroy data or spin forever, and each was
    worth writing down rather than discovering.
    """
    problems: list[str] = []

    if not source.strip():
        problems.append("No source was given.")
    if not destination.strip():
        problems.append("No destination was given.")
    if problems:
        return problems

    src, dst = _norm(source), _norm(destination)

    if src == dst:
        problems.append("The source and the destination are the same folder.")

    # Copying a folder into itself makes robocopy walk its own output forever.
    if _is_within(dst, src):
        problems.append(
            "The destination is inside the source. Copying a folder into itself "
            "never finishes -- it keeps finding the copies it just made.")

    # The reverse is the dangerous one, and only with /MIR: mirroring onto a
    # parent of the source would delete everything in that parent that is not
    # in the source.
    if _is_within(src, dst) and src != dst:
        problems.append(
            "The source is inside the destination. Backing up into a parent of "
            "the folder you are backing up is almost never what anyone means.")

    # A drive root is where people accidentally point things, and mirroring
    # onto C:\ would try to delete Windows.
    drive, tail = os.path.splitdrive(dst)
    if drive and tail in ("", os.sep):
        problems.append(
            f"The destination is the root of {drive} itself. Pick a folder on "
            f"the drive -- for example {drive}\\DroboBackup -- so the backup "
            f"cannot touch anything else that happens to be on it.")

    return problems


def plan(source: str, destination: str, mirror: bool = False) -> BackupPlan:
    """
    Work out what a backup WOULD do. Copies nothing.

    Always call this first. It is the half that catches a destination typo
    before the typo matters.
    """
    p = BackupPlan(source=source, destination=destination, mirror=mirror)
    p.refusals = check_destination(source, destination)

    if not os.path.isdir(source) and not source.startswith("\\\\"):
        # A UNC path may be perfectly valid and simply not mounted yet, so it
        # is a warning; a local path that does not exist is a refusal.
        p.refusals.append(f"The source folder does not exist: {source}")

    if p.refusals:
        return p

    if not os.path.exists(destination):
        p.warnings.append(
            f"The destination folder does not exist yet and will be created: {destination}")

    if mirror:
        p.warnings.append(
            "MIRROR IS ON. Anything in the backup that is no longer on the Drobo "
            "will be DELETED from the backup. If you delete a photo by accident "
            "and then run this, the backup copy goes too. Leave mirroring off "
            "unless you specifically want the backup to forget things.")

    p.command = _build_command(source, destination, mirror)
    return p


def _build_command(source: str, destination: str, mirror: bool) -> list[str]:
    """
    The robocopy invocation.

    /E    include subfolders, including empty ones
    /Z    restartable mode -- resume a part-copied file instead of restarting
          it, which is what makes a large video survive a Wi-Fi blip
    /DCOPY:DAT  keep folder timestamps too, not just file ones
    /R:2 /W:5   two retries, five seconds apart. Robocopy's default is a
          MILLION retries thirty seconds apart, which on an unreachable share
          means it hangs effectively forever instead of reporting a problem
    /NP   no per-file percentage -- it spams a progress line per file, and we
          are parsing this output
    /TEE  write to the console as well as the log, so progress is pollable
    """
    args = ["robocopy", source, destination, "/E", "/Z", "/DCOPY:DAT",
            "/R:2", "/W:5", "/NP", "/TEE"]
    if mirror:
        # /MIR implies /E and adds /PURGE, which is the deleting half.
        args.append("/MIR")
    return args


@dataclass
class BackupJob:
    """A running (or finished) backup, pollable while it goes."""

    plan: BackupPlan
    started_at: float
    finished_at: float | None = None
    exit_code: int | None = None
    ok: bool | None = None
    summary: str = "Running..."
    error: str = ""
    #: Last few lines of robocopy output, for a UI to show progress.
    tail: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "source": self.plan.source,
            "destination": self.plan.destination,
            "mirror": self.plan.mirror,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "summary": self.summary,
            "error": self.error,
            "tail": self.tail[-12:],
        }


class BackupRunner:
    """
    Runs one backup at a time, in a background thread.

    One at a time on purpose: two robocopy processes writing the same
    destination would interleave and neither would be trustworthy.
    """

    #: How many output lines to keep. Enough to show progress, bounded so a
    #: multi-hour copy of a full array cannot grow this without limit.
    TAIL_LINES = 200

    def __init__(self):
        self._lock = threading.Lock()
        self._job: BackupJob | None = None
        self._thread: threading.Thread | None = None

    @property
    def job(self) -> BackupJob | None:
        with self._lock:
            return self._job

    def start(self, source: str, destination: str, mirror: bool = False) -> BackupJob:
        """
        Actually run a backup. Raises BackupError if it must not proceed.

        Deliberately separate from plan(): getting here requires a caller to
        have decided, explicitly, that writing is what they want.
        """
        with self._lock:
            if self._job is not None and self._job.running:
                raise BackupError("A backup is already running.")

            p = plan(source, destination, mirror)
            if not p.allowed:
                raise BackupError(" ".join(p.refusals))

            job = BackupJob(plan=p, started_at=time.time())
            self._job = job

        self._thread = threading.Thread(
            target=self._run, args=(job,), name="drobo-backup", daemon=True)
        self._thread.start()
        return job

    def _run(self, job: BackupJob) -> None:
        try:
            os.makedirs(job.plan.destination, exist_ok=True)
            proc = subprocess.Popen(
                job.plan.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                with self._lock:
                    job.tail.append(line)
                    if len(job.tail) > self.TAIL_LINES:
                        del job.tail[: len(job.tail) - self.TAIL_LINES]
            code = proc.wait()
            ok, summary = interpret_exit_code(code)
            with self._lock:
                job.exit_code, job.ok, job.summary = code, ok, summary
        except FileNotFoundError:
            with self._lock:
                job.ok, job.error = False, (
                    "robocopy was not found. It ships with Windows, so this "
                    "usually means the backup was run on something that is not "
                    "Windows.")
                job.summary = "Could not start."
        except OSError as exc:
            with self._lock:
                job.ok, job.error = False, str(exc)
                job.summary = "Could not start."
        finally:
            with self._lock:
                job.finished_at = time.time()
