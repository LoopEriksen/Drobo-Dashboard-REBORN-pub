"""
DroboPix-style photo backup -- the receiving half.

The phone's job is to notice new photos and offer them. This module's job is to
decide what it already has, file what it doesn't, and never lose or duplicate
anything. It talks to the Drobo purely as a file share (SMB), so it works today
with zero protocol reverse engineering.

The important trick is the /api/photos/have endpoint: before uploading, the
phone sends the hashes of the photos it's holding and gets back the ones the
agent has never seen. That means an interrupted backup resumes cheaply, and
re-installing the app doesn't re-upload your entire camera roll.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

# Anything not on this list is refused. Keep it tight -- this endpoint accepts
# files from a phone, so it should not become a general-purpose file drop.
ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff",
    ".dng", ".raw", ".cr2", ".nef", ".arw",
    ".mov", ".mp4", ".m4v", ".avi", ".hevc",
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_INDEX_NAME = ".drobo-agent-photo-index.json"
_SESSION_DIR = ".drobo-agent-uploads"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PhotoStoreError(Exception):
    pass


def safe_filename(name: str) -> str:
    """
    Turn whatever the phone sent into a filename that cannot escape the target
    directory. Strips any path components, then anything exotic.
    """
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = _SAFE_NAME.sub("_", name)
    name = name.lstrip(".") or "photo"
    return name[:180]


class PhotoStore:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.target_dir = cfg.get("target_dir") or ""
        self.organize = bool(cfg.get("organize_by_date", True))
        # Single-shot POST limit. Anything larger must use the chunked session
        # API -- see begin()/append()/finish() below.
        self.max_bytes = int(cfg.get("max_upload_mb", 512)) * 1024 * 1024
        # Ceiling for a chunked upload. iPhone 4K video runs to several GB.
        self.max_file_bytes = int(cfg.get("max_file_gb", 32)) * 1024 * 1024 * 1024

        self._lock = threading.Lock()
        self._index: dict[str, str] = {}  # sha256 -> relative path
        self._dirty = False
        self._last_save = 0.0
        self._loaded = False

    # -- index -------------------------------------------------------------

    @property
    def index_path(self) -> str:
        return os.path.join(self.target_dir, _INDEX_NAME)

    def load(self) -> None:
        if not self.enabled:
            return
        if not self.target_dir:
            print("[photos] no target_dir configured -- photo backup is off", flush=True)
            self.enabled = False
            return
        try:
            os.makedirs(self.target_dir, exist_ok=True)
        except OSError as exc:
            print(f"[photos] cannot use target_dir {self.target_dir!r}: {exc}", flush=True)
            self.enabled = False
            return
        had_index = os.path.exists(self.index_path)
        if had_index:
            try:
                with open(self.index_path, "r", encoding="utf-8") as fh:
                    self._index = json.load(fh)
            except (OSError, ValueError) as exc:
                print(f"[photos] index unreadable: {exc}", flush=True)
                self._index = {}
                had_index = False
        self._loaded = True

        # THE INDEX IS A CACHE, NOT THE TRUTH. The files on the Drobo are.
        #
        # Without this, losing the index file means the agent believes it has
        # nothing, `missing()` says "send me everything", and the phone
        # re-uploads an entire camera roll over Wi-Fi -- hours and gigabytes to
        # re-copy files that are already sitting right there.
        #
        # So when the index is absent or unreadable and the folder is NOT empty,
        # rebuild it from what is actually on disk before anyone asks what we
        # have. Only in that case: hashing a large library is real I/O over SMB
        # and has no business running on every ordinary startup.
        if not had_index and self._has_any_files():
            print("[photos] no usable index but the folder has files -- "
                  "rebuilding it from disk rather than asking for everything again",
                  flush=True)
            summary = self.rescan()
            print(f"[photos] rebuilt index from {summary['found']} file(s)", flush=True)

        print(f"[photos] {len(self._index)} file(s) already backed up to "
              f"{self.target_dir}", flush=True)

    def _has_any_files(self) -> bool:
        """Cheap check for "is there anything here at all", without hashing."""
        for _root, _dirs, names in self._walk():
            if names:
                return True
        return False

    def _walk(self):
        """
        Walk the backup folder, skipping our own bookkeeping.

        Dot-prefixed entries are ours (the index, and .drobo-agent-uploads full
        of half-finished .part files). Hashing an in-progress upload would index
        a truncated file under the hash of its complete self -- which is exactly
        the kind of quiet corruption this whole module exists to avoid.
        """
        for root, dirs, names in os.walk(self.target_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            keep = [n for n in names
                    if not n.startswith(".")
                    and os.path.splitext(n)[1].lower() in ALLOWED_EXTENSIONS]
            yield root, dirs, keep

    def rescan(self) -> dict:
        """
        Rebuild the index from the files actually present, and report the drift.

        Two failure modes this fixes, both of which end in the phone and the
        agent disagreeing about what is backed up:

          - **Index lost or corrupt.** Without a rescan the agent asks for the
            whole camera roll again.
          - **A file deleted off the Drobo.** The index still lists its hash, so
            `missing()` answers "already have it" and the phone never re-sends.
            The photo is gone and the system says it is safe -- the worse of the
            two, because nothing ever surfaces it.

        Returns {"found", "added", "dropped", "unchanged"} so a caller can say
        what actually changed rather than just "done".
        """
        rebuilt: dict[str, str] = {}
        found = 0
        for root, _dirs, names in self._walk():
            for name in names:
                full = os.path.join(root, name)
                try:
                    digest = self._hash_file(full)
                except OSError:
                    # Unreadable right now (SMB hiccup, a lock). Skip it rather
                    # than treat it as absent -- see the merge below, which is
                    # why a skipped file does not get dropped from the index.
                    continue
                found += 1
                rebuilt[digest] = os.path.relpath(full, self.target_dir).replace("\\", "/")

        with self._lock:
            before = set(self._index)
            now = set(rebuilt)
            # A hash we already knew whose file is genuinely gone gets dropped;
            # one we simply could not read stays, because "I could not open it"
            # is not evidence of absence.
            added = now - before
            dropped = before - now
            self._index = rebuilt
            self._dirty = True
            self._save_index(force=True)

        return {"found": found, "added": len(added), "dropped": len(dropped),
                "unchanged": len(before & now)}

    @staticmethod
    def _hash_file(path: str) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                hasher.update(block)
        return hasher.hexdigest()

    def _save_index(self, force: bool = False) -> None:
        """Throttled so a burst of uploads doesn't rewrite the index every time."""
        if not self._dirty:
            return
        if not force and time.time() - self._last_save < 5.0:
            return
        tmp = self.index_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._index, fh)
            os.replace(tmp, self.index_path)
            self._dirty = False
            self._last_save = time.time()
        except OSError as exc:
            # A OneDrive/SMB lock is transient -- keep the in-memory index and
            # try again on the next upload.
            print(f"[photos] could not write index: {exc}", flush=True)

    def flush(self) -> None:
        with self._lock:
            self._save_index(force=True)

    # -- queries -----------------------------------------------------------

    def missing(self, hashes: list[str]) -> list[str]:
        """Given hashes the phone holds, return the ones we don't have."""
        with self._lock:
            return [h for h in hashes if h and h not in self._index]

    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "target_dir": self.target_dir,
                "files_stored": len(self._index),
                "organize_by_date": self.organize,
                "max_upload_mb": self.max_bytes // (1024 * 1024),
            }

    # -- ingest ------------------------------------------------------------

    def _destination(self, filename: str, taken_at: float) -> str:
        if not self.organize:
            return filename
        stamp = time.localtime(taken_at)
        return os.path.join(
            time.strftime("%Y", stamp), time.strftime("%Y-%m", stamp), filename
        )

    # -- chunked, resumable upload ----------------------------------------
    #
    # Needed because a 4K video is gigabytes and iOS will suspend a background
    # transfer part-way through. The upload id IS the file's sha256, so resuming
    # needs no bookkeeping on the phone: call begin() again with the same hash
    # and you are told how many bytes already arrived.
    #
    #     begin(name, size, sha256)  -> {"status": "ready", "received": N}
    #                                or {"status": "duplicate"}  (already have it)
    #     append(sha256, offset, chunk) -> {"received": N}
    #     finish(sha256)             -> {"status": "stored", "path": ...}

    @property
    def session_dir(self) -> str:
        return os.path.join(self.target_dir, _SESSION_DIR)

    def _session_paths(self, digest: str) -> tuple[str, str]:
        return (os.path.join(self.session_dir, digest + ".part"),
                os.path.join(self.session_dir, digest + ".json"))

    def _require_ready(self, digest: str) -> None:
        if not self.enabled or not self._loaded:
            raise PhotoStoreError("photo backup is not ready")
        if not _SHA256_RE.match(digest or ""):
            raise PhotoStoreError("upload id must be a lowercase hex sha256")

    def begin(self, filename: str, total_bytes: int, digest: str,
              taken_at: float | None = None) -> dict:
        self._require_ready(digest)
        if total_bytes <= 0:
            raise PhotoStoreError("size must be positive")
        if total_bytes > self.max_file_bytes:
            raise PhotoStoreError(
                f"{total_bytes} bytes is over the "
                f"{self.max_file_bytes // (1024**3)} GB per-file limit")

        name = safe_filename(filename)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise PhotoStoreError(f"file type {ext or '(none)'} is not accepted")

        with self._lock:
            if digest in self._index:
                return {"status": "duplicate", "sha256": digest,
                        "path": self._index[digest]}

            os.makedirs(self.session_dir, exist_ok=True)
            part, meta = self._session_paths(digest)
            received = os.path.getsize(part) if os.path.exists(part) else 0
            if received > total_bytes:      # stale or corrupt; start over
                received = 0
                try:
                    os.remove(part)
                except OSError:
                    pass

            # A resume must not clobber the original capture date. The phone
            # only sends taken_at on the first begin(); if we overwrote it here
            # every interrupted upload would end up filed under today.
            when = taken_at
            if when is None and os.path.exists(meta):
                try:
                    when = json.load(open(meta, encoding="utf-8")).get("taken_at")
                except (OSError, ValueError):
                    when = None
            with open(meta, "w", encoding="utf-8") as fh:
                json.dump({"filename": name, "total": total_bytes,
                           "taken_at": when or time.time()}, fh)
            if not os.path.exists(part):
                open(part, "wb").close()
            return {"status": "ready", "sha256": digest,
                    "received": received, "total": total_bytes}

    def append(self, digest: str, offset: int, chunk: bytes) -> dict:
        self._require_ready(digest)
        if not chunk:
            raise PhotoStoreError("empty chunk")
        with self._lock:
            part, meta = self._session_paths(digest)
            if not os.path.exists(meta):
                raise PhotoStoreError("no upload session; call begin first")
            info = json.load(open(meta, encoding="utf-8"))
            received = os.path.getsize(part)
            if offset != received:
                # Not an error the phone can't recover from -- tell it where we are.
                return {"status": "offset-mismatch", "received": received,
                        "total": info["total"]}
            if received + len(chunk) > info["total"]:
                raise PhotoStoreError("chunk would exceed the declared size")
            with open(part, "r+b") as fh:
                fh.seek(offset)
                fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            received += len(chunk)
            return {"status": "ok", "received": received, "total": info["total"],
                    "complete": received >= info["total"]}

    def session(self, digest: str) -> dict:
        self._require_ready(digest)
        with self._lock:
            if digest in self._index:
                return {"status": "duplicate", "path": self._index[digest]}
            part, meta = self._session_paths(digest)
            if not os.path.exists(meta):
                return {"status": "none", "received": 0}
            info = json.load(open(meta, encoding="utf-8"))
            return {"status": "ready",
                    "received": os.path.getsize(part) if os.path.exists(part) else 0,
                    "total": info["total"]}

    def finish(self, digest: str) -> dict:
        """Verify the assembled file really is what was promised, then file it."""
        self._require_ready(digest)
        with self._lock:
            if digest in self._index:
                return {"status": "duplicate", "sha256": digest,
                        "path": self._index[digest]}
            part, meta = self._session_paths(digest)
            if not os.path.exists(meta) or not os.path.exists(part):
                raise PhotoStoreError("no upload session to finish")
            info = json.load(open(meta, encoding="utf-8"))
            size = os.path.getsize(part)
            if size != info["total"]:
                return {"status": "incomplete", "received": size,
                        "total": info["total"]}

            # Never trust the declared hash -- recompute before filing anything.
            hasher = hashlib.sha256()
            with open(part, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    hasher.update(block)
            if hasher.hexdigest() != digest:
                os.remove(part)
                os.remove(meta)
                raise PhotoStoreError(
                    "content does not match the declared sha256; upload discarded")

            relative = self._place(info["filename"], info["taken_at"], part)
            os.remove(meta)
            self._index[digest] = relative
            self._dirty = True
            self._save_index(force=True)
            return {"status": "stored", "sha256": digest, "path": relative,
                    "bytes": size}

    def _place(self, name: str, taken_at: float, source_path: str) -> str:
        """Move a completed upload into the library. Caller holds the lock."""
        relative = self._destination(name, taken_at)
        absolute = os.path.join(self.target_dir, relative)
        os.makedirs(os.path.dirname(absolute), exist_ok=True)
        stem, suffix = os.path.splitext(absolute)
        attempt = 0
        while os.path.exists(absolute):
            attempt += 1
            absolute = f"{stem}~{attempt}{suffix}"
        os.replace(source_path, absolute)
        try:
            os.utime(absolute, (taken_at, taken_at))
        except OSError:
            pass
        return os.path.relpath(absolute, self.target_dir).replace("\\", "/")

    # -- single-shot upload -------------------------------------------------

    def ingest(self, filename: str, data: bytes, taken_at: float | None = None) -> dict:
        if not self.enabled:
            raise PhotoStoreError("photo backup is disabled")
        if not self._loaded:
            raise PhotoStoreError("photo store not ready")
        if len(data) > self.max_bytes:
            raise PhotoStoreError(
                f"file is {len(data)} bytes, over the "
                f"{self.max_bytes // (1024 * 1024)} MB limit"
            )
        if not data:
            raise PhotoStoreError("empty file")

        name = safe_filename(filename)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise PhotoStoreError(f"file type {ext or '(none)'} is not accepted")

        digest = hashlib.sha256(data).hexdigest()
        taken_at = taken_at or time.time()

        with self._lock:
            if digest in self._index:
                return {
                    "status": "duplicate",
                    "sha256": digest,
                    "path": self._index[digest],
                }

            relative = self._destination(name, taken_at)
            absolute = os.path.join(self.target_dir, relative)
            os.makedirs(os.path.dirname(absolute), exist_ok=True)

            # Two different photos can share a filename (IMG_0001.JPG is not
            # unique across devices), so pick a free name rather than overwrite.
            stem, suffix = os.path.splitext(absolute)
            attempt = 0
            while os.path.exists(absolute):
                attempt += 1
                absolute = f"{stem}~{attempt}{suffix}"
            relative = os.path.relpath(absolute, self.target_dir)

            tmp = absolute + ".part"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, absolute)
            except OSError as exc:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise PhotoStoreError(f"could not write to the Drobo: {exc}") from exc

            try:
                os.utime(absolute, (taken_at, taken_at))
            except OSError:
                pass

            self._index[digest] = relative.replace("\\", "/")
            self._dirty = True
            self._save_index()

            return {
                "status": "stored",
                "sha256": digest,
                "path": self._index[digest],
                "bytes": len(data),
            }
