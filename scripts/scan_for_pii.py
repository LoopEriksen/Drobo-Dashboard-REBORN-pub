#!/usr/bin/env python3
"""
scan_for_pii.py — scan the Drobo-Dashboard-REBORN-public working tree for personal or
identifying information before you push.

Run it any time from the repo root:

    py scripts/scan_for_pii.py

Exits 0 (clean) or 1 (hits found). It looks for the same things the
pre-commit hook blocks, across ALL text files in the tree (not just
staged ones), so you can catch problems before you even stage them.

Pass --tracked to scan only what git actually tracks. That is the mode
CI runs: a leak only matters once it is committed, and the plain
working-tree walk also reads gitignored build output (obj/, bin/),
which buries real hits under hundreds of machine paths from your own
toolchain.

It is deliberately GENERIC — it names no real person or username, so
this script carries no identifying data of its own. To also flag
machine-specific strings (your username, real name), add one regex per
line to scripts/pii_patterns.local (gitignored, optional).

It skips .git/, .claude/, virtualenvs, __pycache__, and binary files.
"""
import argparse
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Generic patterns — no hardcoded personal identifiers.
#
# The user-path pattern deliberately captures the account segment after
# Users\ as well. Reporting the bare "C:\Users\" told you a path existed
# but not whose, so every hit read alike and the allowlist could not tell
# a docs placeholder (C:\Users\<you>\) from a real leak (C:\Users\alice\)
# without allowlisting the prefix and thereby switching the check off.
#
# The segment is required, not optional: what leaks an identity is the
# account name, so a bare "C:\Users\" with nothing after it — a redaction
# pattern, a truncated path — names nobody and is not a finding.
PATTERNS = [
    ("absolute Windows user path",
     re.compile(r"C:[\\/]+Users[\\/]+[A-Za-z0-9._$%<>{}-]+", re.I)),
    ("email address",             re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]

# Emails that are fine: demo accounts and the domains the RFCs reserve for
# documentation and testing. RFC 2606 reserves example.com/net/org and the
# .example/.test/.invalid TLDs; RFC 6761 adds .localhost. Test fixtures
# legitimately use @something.test, and flagging those trained people to
# ignore the scanner — which is how a real hit gets waved through.
EMAIL_OK = re.compile(
    r"@(demo\.com|example\.(com|org|net)"
    r"|[A-Za-z0-9.-]+\.(example|test|invalid|localhost))$", re.I)

# .claude/ holds local machine config (gitignored, never published).
SKIP_DIRS = {".git", ".claude", "__pycache__", ".venv", "venv", "env", "ENV",
             "node_modules", ".pytest_cache", "build", "dist"}
SKIP_EXT = {".db", ".sqlite", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif",
            ".webp", ".bmp", ".ico", ".pyc", ".zip", ".mp4", ".mov",
            ".docx", ".doc", ".pdf", ".xlsx"}


def load_local_patterns():
    """Optional user-private regexes from scripts/pii_patterns.local."""
    out = []
    path = os.path.join(REPO, "scripts", "pii_patterns.local")
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        out.append(("private pattern", re.compile(line, re.I)))
                    except re.error:
                        pass
    return out


def load_allowlist():
    """Reviewed exceptions from scripts/pii_allow.txt.

    The mirror image of pii_patterns.local: that file is private and ADDS
    patterns, this one is committed and SUBTRACTS hits already looked at
    and accepted (a published contact address, say). Without it the scan
    could never reach exit 0 on a repo that contains any intentional
    address, so it could not serve as a CI gate at all — and a gate that
    is always red is one nobody reads. One regex per line, matched
    against the offending text; keep them tight.
    """
    out = []
    path = os.path.join(REPO, "scripts", "pii_allow.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        out.append(re.compile(line, re.I))
                    except re.error:
                        pass
    return out


# The detection scripts (and the pattern/allow lists) legitimately contain
# the patterns below, so they must not scan themselves.
SKIP_FILES = {"scripts/scan_for_pii.py", "scripts/hooks/pre-commit",
              "scripts/pii_patterns.local", "scripts/pii_allow.txt"}


def tracked_files():
    """Every file git tracks, repo-relative. None if this is not a git
    checkout — the caller decides whether that is fatal."""
    try:
        out = subprocess.run(["git", "-C", REPO, "ls-files", "-z"],
                             capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in out.stdout.decode("utf-8", "ignore").split("\0") if p]


def walked_files():
    """Every file in the working tree, repo-relative."""
    found = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = os.path.join(root, name)
            found.append(os.path.relpath(path, REPO).replace("\\", "/"))
    return found


def scan(patterns, allow, rels):
    hits = []
    for rel in rels:
        if rel in SKIP_FILES:
            continue
        if os.path.splitext(rel)[1].lower() in SKIP_EXT:
            continue
        # --tracked yields paths from git, which knows nothing of SKIP_DIRS;
        # honour the same exclusions in both modes so the two agree.
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        path = os.path.join(REPO, rel)
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    for label, rx in patterns:
                        m = rx.search(line)
                        if not m:
                            continue
                        snippet = m.group(0).strip()
                        if label == "email address" and EMAIL_OK.search(m.group(0)):
                            continue
                        if any(a.search(snippet) for a in allow):
                            continue
                        hits.append((rel, lineno, label, snippet))
        except (OSError, UnicodeError):
            continue
    return hits


def main():
    # --path lets this guard another repo (the website, say) instead of only the
    # one it happens to live in. Before this existed, an unknown flag was
    # silently ignored and you got a clean-looking scan of the WRONG directory.
    global REPO
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", help="repo to scan (default: the one this script lives in)")
    ap.add_argument("--tracked", action="store_true",
                    help="scan only git-tracked files (what CI gates on)")
    args = ap.parse_args()
    if args.path:
        REPO = os.path.abspath(args.path)
        if not os.path.isdir(REPO):
            print(f"No such directory: {REPO}")
            return 2

    if args.tracked:
        rels = tracked_files()
        if rels is None:
            # Falling back to the tree walk would report a clean scan of a
            # different file set than the one asked for.
            print(f"Not a git checkout (or git unavailable): {REPO}")
            return 2
        print(f"Scanning {REPO} ({len(rels)} tracked files)")
    else:
        rels = walked_files()
        print(f"Scanning {REPO}")

    hits = scan(PATTERNS + load_local_patterns(), load_allowlist(), rels)
    if not hits:
        print("PII scan: clean — no personal or identifying data found.")
        return 0
    print(f"PII scan: {len(hits)} potential hit(s) found:\n")
    for rel, lineno, label, snippet in hits:
        print(f"  {rel}:{lineno}  [{label}]  {snippet}")
    print("\nReview each hit. Demo accounts (@demo.com) and reserved domains are "
          "allowed;\nanything else should be removed or replaced before pushing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
