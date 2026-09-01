#!/usr/bin/env python3
"""
scan_for_pii.py — scan the Drobo-Dashboard-REBORN-public working tree for personal or
identifying information before you push.

Run it any time from the repo root:

    py scripts/scan_for_pii.py

Exits 0 (clean) or 1 (hits found). It looks for the same things the
pre-commit hook blocks, across ALL text files in the tree (not just
staged ones), so you can catch problems before you even stage them.

It is deliberately GENERIC — it names no real person or username, so
this script carries no identifying data of its own. To also flag
machine-specific strings (your username, real name), add one regex per
line to scripts/pii_patterns.local (gitignored, optional).

It skips .git/, .claude/, virtualenvs, __pycache__, and binary files.
"""
import argparse
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Generic patterns — no hardcoded personal identifiers.
PATTERNS = [
    ("absolute Windows user path", re.compile(r"C:[\\/]+Users[\\/]+", re.I)),
    ("email address",             re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]

# Emails that are fine: demo accounts and reserved example domains
# (RFC 2606 reserves example.com/net/org AND the .example TLD).
EMAIL_OK = re.compile(
    r"@(demo\.com|example\.(com|org|net)|[A-Za-z0-9.-]+\.example)$", re.I)

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


# The detection scripts (and the local pattern list) legitimately contain
# the patterns below, so they must not scan themselves.
SKIP_FILES = {"scripts/scan_for_pii.py", "scripts/hooks/pre-commit",
              "scripts/pii_patterns.local"}


def scan(patterns):
    hits = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, REPO).replace("\\", "/")
            if rel in SKIP_FILES:
                continue
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, 1):
                        for label, rx in patterns:
                            m = rx.search(line)
                            if not m:
                                continue
                            if label == "email address" and EMAIL_OK.search(m.group(0)):
                                continue
                            hits.append((rel, lineno, label, m.group(0).strip()))
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
    args = ap.parse_args()
    if args.path:
        REPO = os.path.abspath(args.path)
        if not os.path.isdir(REPO):
            print(f"No such directory: {REPO}")
            return 2
    print(f"Scanning {REPO}")
    hits = scan(PATTERNS + load_local_patterns())
    if not hits:
        print("PII scan: clean — no personal or identifying data found.")
        return 0
    print(f"PII scan: {len(hits)} potential hit(s) found:\n")
    for rel, lineno, label, snippet in hits:
        print(f"  {rel}:{lineno}  [{label}]  {snippet}")
    print("\nReview each hit. Demo accounts (@demo.com) and example domains are "
          "allowed;\nanything else should be removed or replaced before pushing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
