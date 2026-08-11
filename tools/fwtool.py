#!/usr/bin/env python3
"""
fwtool.py - unpack Drobo firmware (.tdf) files.

Drobo firmware is a container Data Robotics called TDIH. This tool reads it,
lists what's inside, extracts each section, and rebuilds the Linux root
filesystem so its files can be read directly.

Everything here is read-only. Nothing is executed, and nothing is written back
to a Drobo.

    py fwtool.py info    release.Drobo5N.4-2-1.tdf
    py fwtool.py extract release.Drobo5N.4-2-1.tdf -o out/
    py fwtool.py rootfs  out/fw-LXFS.bin -o out/rootfs/
    py fwtool.py strings out/rootfs/sbin/nasd

Standard library only, Python 3.9+.

--------------------------------------------------------------------------
Container layout (worked out from Drobo 5N firmware 4.2.1, all big-endian)

    +0x000  u32   offset of the payload area (0x22c on 5N 4.2.1)
    +0x004  u32   format version (5)
    +0x008  char4 "TDIH" magic
    +0x00c  u32   build number
    +0x010  char  model tag, NUL padded ("5N")
    +0x02c  u32   total payload size
    +0x030  u32   checksum
    +0x034  char  description ("Data Robotics Firmware")
    +0x168  u32   number of section-table entries
    +0x16c  ...   section table, 24 bytes per entry:
                    +0x00 char4  tag
                    +0x04 u32    offset, RELATIVE to the payload area
                    +0x08 u32    size
                    +0x0c u32    reserved
                    +0x10 u32    build number
                    +0x14 u32    checksum

Section tags seen so far:
    VXI2   ARM ELF - the VxWorks-side storage application (BeyondRAID)
    RTPI   ARM ELF - second runtime image
    LXKI   u-boot uImage - the Linux kernel
    LXFS   JFFS2 filesystem - the Linux root filesystem, where nasd lives
--------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import struct
import sys
import zlib

MAGIC_OFFSET = 8
MAGIC = b"TDIH"
COUNT_OFFSET = 0x168
TABLE_OFFSET = 0x16C
ENTRY_SIZE = 24

FORMATS = [
    (b"hsqs", "squashfs (LE)"), (b"sqsh", "squashfs (BE)"),
    (b"\x45\x3d\xcd\x28", "cramfs"), (b"UBI#", "UBI"),
    (b"\x85\x19", "jffs2"), (b"\x1f\x8b\x08", "gzip"),
    (b"\xfd7zXZ\x00", "xz"), (b"\x7fELF", "ELF"),
    (b"\x27\x05\x19\x56", "u-boot uImage"), (b"-rom1fs-", "romfs"),
]


class Section:
    def __init__(self, tag, offset, size, build, checksum):
        self.tag = tag
        self.offset = offset          # absolute
        self.size = size
        self.build = build
        self.checksum = checksum
        self.kind = "unknown"

    def __str__(self):
        return (f"{self.tag:6s} @0x{self.offset:08x}  {self.size:>12,} bytes  "
                f"build=0x{self.build:x}  csum=0x{self.checksum:08x}  {self.kind}")


def read_container(path: str):
    with open(path, "rb") as fh:
        blob = fh.read()
    if blob[MAGIC_OFFSET:MAGIC_OFFSET + 4] != MAGIC:
        raise SystemExit(f"{path}: not a TDIH firmware container "
                         f"(magic was {blob[MAGIC_OFFSET:MAGIC_OFFSET+4]!r})")

    data_off, version = struct.unpack_from(">II", blob, 0)
    build = struct.unpack_from(">I", blob, 0x0C)[0]
    model = blob[0x10:0x2C].split(b"\0")[0].decode("latin1", "replace")
    total = struct.unpack_from(">I", blob, 0x2C)[0]
    checksum = struct.unpack_from(">I", blob, 0x30)[0]
    description = blob[0x34:0x60].split(b"\0")[0].decode("latin1", "replace")
    count = struct.unpack_from(">I", blob, COUNT_OFFSET)[0]

    sections = []
    for i in range(count):
        base = TABLE_OFFSET + i * ENTRY_SIZE
        tag = blob[base:base + 4].decode("latin1")
        off, size, _resv, sbuild, csum = struct.unpack_from(">IIIII", blob, base + 4)
        sec = Section(tag, data_off + off, size, sbuild, csum)
        head = blob[sec.offset:sec.offset + 16]
        for sig, name in FORMATS:
            if head.startswith(sig):
                sec.kind = name
                break
        sections.append(sec)

    meta = {
        "path": path, "size": len(blob), "data_offset": data_off,
        "format_version": version, "build": build, "model": model,
        "payload_size": total, "checksum": checksum,
        "description": description, "sections": sections,
    }
    return blob, meta


def cmd_info(args) -> int:
    blob, meta = read_container(args.file)
    print(f"{os.path.basename(meta['path'])}   {meta['size']:,} bytes")
    print(f"  description     {meta['description']}")
    print(f"  model           {meta['model']}")
    print(f"  format version  {meta['format_version']}")
    print(f"  build           0x{meta['build']:x}")
    print(f"  payload         {meta['payload_size']:,} bytes at 0x{meta['data_offset']:x}")
    print(f"  checksum        0x{meta['checksum']:08x}")
    print(f"  sections        {len(meta['sections'])}")
    covered = 0
    for sec in meta["sections"]:
        print("   ", sec)
        covered += sec.size
    print(f"\n  {covered:,} of {meta['payload_size']:,} payload bytes accounted for"
          f" ({'complete' if covered == meta['payload_size'] else 'INCOMPLETE'})")

    for sec in meta["sections"]:
        if sec.kind == "u-boot uImage":
            u = sec.offset
            size = struct.unpack_from(">I", blob, u + 12)[0]
            load, entry = struct.unpack_from(">II", blob, u + 16)
            name = blob[u + 32:u + 64].rstrip(b"\0").decode("latin1", "replace")
            print(f"\n  kernel: {name}")
            print(f"    {size:,} bytes, load=0x{load:08x}, entry=0x{entry:08x}")
    return 0


def cmd_extract(args) -> int:
    blob, meta = read_container(args.file)
    os.makedirs(args.out, exist_ok=True)
    for sec in meta["sections"]:
        data = blob[sec.offset:sec.offset + sec.size]
        if sec.kind == "u-boot uImage" and not args.raw:
            data = data[64:]  # strip the 64-byte uImage header
        dest = os.path.join(args.out, f"fw-{sec.tag}.bin")
        with open(dest, "wb") as fh:
            fh.write(data)
        print(f"  {sec.tag:6s} -> {dest}  ({len(data):,} bytes, {sec.kind})")
    return 0


# ---------------------------------------------------------------------------
# JFFS2
# ---------------------------------------------------------------------------

J_MAGIC, J_DIRENT, J_INODE = 0x1985, 0xE001, 0xE002
C_NONE, C_ZERO, C_RTIME, C_COPY, C_ZLIB = 0, 1, 2, 4, 6
DTYPE = {1: "fifo", 2: "chr", 4: "dir", 6: "blk", 8: "file", 10: "link", 12: "sock"}


def _rtime(src: bytes, destlen: int) -> bytes:
    positions = [0] * 256
    out = bytearray(destlen)
    outpos = ip = 0
    while outpos < destlen and ip + 1 < len(src):
        value = src[ip]; ip += 1
        out[outpos] = value; outpos += 1
        repeat = src[ip]; ip += 1
        back = positions[value]
        positions[value] = outpos
        if repeat:
            if back + repeat >= outpos:
                while repeat and outpos < destlen:
                    out[outpos] = out[back]
                    outpos += 1; back += 1; repeat -= 1
            else:
                n = min(repeat, destlen - outpos)
                out[outpos:outpos + n] = out[back:back + n]
                outpos += n
    return bytes(out)


def _decompress(comp: int, data: bytes, csize: int, dsize: int):
    if comp in (C_NONE, C_COPY):
        return data[:dsize]
    if comp == C_ZERO:
        return b"\0" * dsize
    if comp == C_ZLIB:
        try:
            return zlib.decompress(data[:csize])
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(data[:csize])
            except Exception:
                return None
    if comp == C_RTIME:
        return _rtime(data[:csize], dsize)
    return None


def cmd_rootfs(args) -> int:
    with open(args.file, "rb") as fh:
        fs = fh.read()
    if fs[:2] != b"\x85\x19":
        print("warning: this does not start with the JFFS2 magic; trying anyway",
              file=sys.stderr)

    dirents = collections.defaultdict(list)
    frags = collections.defaultdict(list)
    unsupported = collections.Counter()

    pos, size = 0, len(fs)
    while pos < size - 12:
        magic, nodetype, totlen = struct.unpack_from("<HHI", fs, pos)
        if magic != J_MAGIC or totlen < 12 or pos + totlen > size:
            pos += 4
            continue
        if nodetype == J_DIRENT and pos + 40 <= size:
            pino, version, ino, _ = struct.unpack_from("<IIII", fs, pos + 12)
            nsize, dtype = struct.unpack_from("<BB", fs, pos + 28)
            name = fs[pos + 40:pos + 40 + nsize]
            if nsize and name:
                dirents[ino].append((version, name.decode("utf-8", "replace"), pino, dtype))
        elif nodetype == J_INODE and pos + 68 <= size:
            ino, version = struct.unpack_from("<II", fs, pos + 12)
            isize = struct.unpack_from("<I", fs, pos + 28)[0]
            offset, csize, dsize = struct.unpack_from("<III", fs, pos + 44)
            comp = fs[pos + 56]
            out = _decompress(comp, fs[pos + 68:pos + 68 + csize], csize, dsize)
            if out is None:
                unsupported[comp] += 1
            else:
                frags[ino].append((version, offset, out, isize))
        pos += (totlen + 3) & ~3

    # JFFS2 is log-structured: the highest version of each node wins.
    best = {}
    for ino, lst in dirents.items():
        lst.sort()
        _v, name, pino, dtype = lst[-1]
        if ino:
            best[ino] = (name, pino, dtype)
    children = collections.defaultdict(list)
    for ino, (name, pino, dtype) in best.items():
        children[pino].append((name, ino, dtype))

    paths = {}

    def walk(ino, prefix, depth=0):
        if depth > 24:
            return
        for name, child, dtype in sorted(children.get(ino, [])):
            p = prefix + "/" + name
            paths[child] = (p, dtype)
            if dtype == 4:
                walk(child, p, depth + 1)

    walk(1, "")

    written = 0
    for ino, (path, dtype) in paths.items():
        if dtype not in (8, 10) or ino not in frags:
            continue
        parts = sorted(frags[ino], key=lambda t: t[0])
        isize = max(p[3] for p in parts)
        buf = bytearray(isize)
        for _v, off, data, _ in parts:
            end = min(off + len(data), isize)
            if end > off:
                buf[off:end] = data[:end - off]
        dest = os.path.join(args.out, path.lstrip("/").replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            with open(dest, "wb") as fh:
                fh.write(bytes(buf))
            written += 1
        except OSError as exc:
            print(f"  ! {path}: {exc}", file=sys.stderr)

    listing = os.path.join(args.out, "..", "rootfs-listing.txt")
    with open(listing, "w", encoding="utf-8") as fh:
        for path, dtype in sorted(paths.values()):
            fh.write(f"{DTYPE.get(dtype, '?'):5s} {path}\n")

    print(f"  {len(paths):,} paths, {written:,} files written to {args.out}")
    print(f"  listing: {os.path.normpath(listing)}")
    if unsupported:
        names = {3: "RUBINMIPS", 5: "DYNRUBIN", 8: "LZO"}
        print("  unsupported compression: "
              + ", ".join(f"{names.get(k, k)} x{v}" for k, v in unsupported.items()))
    return 0


def cmd_strings(args) -> int:
    with open(args.file, "rb") as fh:
        data = fh.read()
    found = [s.decode("latin1") for s in re.findall(rb"[\x20-\x7e]{%d,}" % args.min, data)]
    uniq = list(dict.fromkeys(found))
    if args.grep:
        pat = re.compile(args.grep, re.I)
        uniq = [s for s in uniq if pat.search(s)]
    for s in uniq:
        print(s)
    print(f"\n# {len(uniq):,} strings", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="describe a .tdf firmware container")
    p.add_argument("file")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("extract", help="write each section to its own file")
    p.add_argument("file")
    p.add_argument("-o", "--out", default="fw-out")
    p.add_argument("--raw", action="store_true", help="keep uImage headers")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("rootfs", help="rebuild files from a JFFS2 image")
    p.add_argument("file")
    p.add_argument("-o", "--out", default="rootfs")
    p.set_defaults(func=cmd_rootfs)

    p = sub.add_parser("strings", help="printable strings, optionally filtered")
    p.add_argument("file")
    p.add_argument("--min", type=int, default=4)
    p.add_argument("--grep", default=None, help="regex filter")
    p.set_defaults(func=cmd_strings)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
